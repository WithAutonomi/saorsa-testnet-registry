#!/usr/bin/env bash
set -euo pipefail

# --- Configuration (set via environment) ---
: "${BOOTSTRAP_CACHE:=true}"
if [ "$BOOTSTRAP_CACHE" != "true" ]; then
    : "${BOOTSTRAP_ARGS:?BOOTSTRAP_ARGS must be set when BOOTSTRAP_CACHE=false}"
fi
: "${BOOTSTRAP_ARGS:=}"
: "${REGION:?REGION must be set}"
: "${ADDRESSES_FILE:=/var/lib/ant-client/addresses.txt}"
: "${ADDRESSES_S3_BUCKET:=}"
: "${ADDRESSES_S3_PREFIX:=}"
: "${INFLUX_URL:=}"
: "${INFLUX_TOKEN:=}"
: "${INFLUX_ORG:=ant}"
: "${INFLUX_BUCKET:=testnet-metrics}"
: "${SLEEP_SECS:=10}"
: "${DOWNLOAD_DIR:=/var/lib/ant-client/downloads}"
: "${ANT:=/root/.local/bin/ant}"
: "${RUN_ONCE:=false}"
: "${HEAPTRACK:=0}"
: "${HEAPTRACK_DIR:=/var/log/ant-heaptrack}"
: "${FAILURE_LOG_DIR:=/var/log/ant-downloader}"
: "${FORWARD_LOGS:=false}"
: "${RUST_LOG:=}"
: "${FORWARD_LOG_DIR:=/var/log/ant-client-forward}"
# Coarse disk guard for the append-only forward logs (truncate beyond this).
: "${FORWARD_LOG_MAX_BYTES:=1073741824}"

if [ "$HEAPTRACK" = "1" ]; then
    mkdir -p "$HEAPTRACK_DIR"
    echo "Heaptrack enabled: profiles will be written under ${HEAPTRACK_DIR}"
fi

# Client ant emits NO logs unless -v is passed (it gates the tracing subscriber
# behind -v — "privacy by design"). RUST_LOG, when set, is read by that
# subscriber's EnvFilter and overrides the -v level. So ALWAYS pass -v, and
# additionally export RUST_LOG.
VERBOSITY_FLAG=(-v)
if [ -n "$RUST_LOG" ]; then
    export RUST_LOG
fi

if [ "$FORWARD_LOGS" = "true" ]; then
    mkdir -p "$FORWARD_LOG_DIR"
fi

# Append a captured per-op log to its persistent, telegraf-tailed aggregate.
# Runs for every op (success and failure) before the per-op files are moved to
# FAILURE_LOG_DIR or removed.
append_forward_log() {
    [ "$FORWARD_LOGS" = "true" ] || return 0
    local src="$1" dst="${FORWARD_LOG_DIR}/$2"
    cat "$src" >> "$dst" 2>/dev/null || true
    if [ "$(stat -c%s "$dst" 2>/dev/null || echo 0)" -gt "$FORWARD_LOG_MAX_BYTES" ]; then
        : > "$dst"
    fi
}

# With the bootstrap cache enabled (default), ant is invoked WITHOUT
# --bootstrap: it reads ~/.config/ant/bootstrap_peers.toml (written by the
# provisioner) and maintains its peer cache. BOOTSTRAP_CACHE=false restores
# the legacy explicit --bootstrap invocation, which disables the cache.
if [ "$BOOTSTRAP_CACHE" = "true" ]; then
    BOOTSTRAP_ARGS=""
fi

dl_seq=0

CLIENT_HOST=$(hostname)
if [ -n "$ADDRESSES_S3_BUCKET" ] && [ -n "$ADDRESSES_S3_PREFIX" ]; then
    S3_URI_PREFIX="s3://${ADDRESSES_S3_BUCKET}/${ADDRESSES_S3_PREFIX}/"
    # The addresses bucket is public-read, so list + fetch go over plain
    # unsigned HTTPS. SigV4-signed `aws s3` calls are rejected from
    # clock-skewed VMs (~15min tolerance), which silently starved skewed
    # downloaders of addresses.
    S3_HTTP_BASE="https://${ADDRESSES_S3_BUCKET}.s3.${AWS_DEFAULT_REGION:-eu-west-2}.amazonaws.com"
else
    S3_URI_PREFIX=""
    S3_HTTP_BASE=""
fi

submit_metric() {
    if [ -z "$INFLUX_URL" ] || [ -z "$INFLUX_TOKEN" ]; then
        return
    fi
    local line_protocol="$1"
    curl -s -XPOST "${INFLUX_URL}/api/v2/write?org=${INFLUX_ORG}&bucket=${INFLUX_BUCKET}&precision=ns" \
        -H "Authorization: Token ${INFLUX_TOKEN}" \
        -H "Content-Type: text/plain" \
        --data-binary "$line_protocol" || echo "WARN: failed to submit metric to InfluxDB"
}

preserve_failure_logs() {
    # Keep the full ant stdout/stderr for a FAILED download under
    # FAILURE_LOG_DIR, mirroring the prod downloader's
    # {idx:04d}-{addr16}.{stdout,stderr} per-op layout. Successes keep
    # nothing — a testnet run produces thousands of downloads and only the
    # failures need diagnosing. Prints the kept file stem.
    local seq="$1" address="$2" stdout_file="$3" stderr_file="$4"
    local safe stem
    safe=$(printf '%s' "${address:0:16}" | tr -c 'A-Za-z0-9._-' '_')
    safe="${safe:-unnamed}"
    stem=$(printf '%04d-%s' "$seq" "$safe")
    mkdir -p "$FAILURE_LOG_DIR"
    mv -f "$stdout_file" "${FAILURE_LOG_DIR}/${stem}.stdout"
    mv -f "$stderr_file" "${FAILURE_LOG_DIR}/${stem}.stderr"
    echo "${FAILURE_LOG_DIR}/${stem}"
}

pick_random_address() {
    # Prefer a locally-provisioned static address list (e.g. from
    # prod-continuous-downloader) when present and non-empty. Falls back to
    # listing S3 uploader-emitted objects when no local file is available
    # (the standard testnet flow). The S3 path uses unsigned public HTTPS so
    # it keeps working on clock-skewed VMs.
    local listing key address
    if [ -s "$ADDRESSES_FILE" ]; then
        shuf -n 1 "$ADDRESSES_FILE" || true
        return 0
    fi
    if [ -z "$S3_HTTP_BASE" ]; then
        return 0
    fi
    listing=$(curl -sf "${S3_HTTP_BASE}/?list-type=2&prefix=${ADDRESSES_S3_PREFIX}/" 2>/dev/null \
        | grep -o '<Key>[^<]*</Key>' \
        | sed -e 's|<Key>||' -e 's|</Key>||' -e "s|^${ADDRESSES_S3_PREFIX}/||" \
        | grep '^upload-' || true)
    if [ -z "$listing" ]; then
        return 0
    fi
    key=$(echo "$listing" | shuf -n 1)
    if [ -z "$key" ]; then
        return 0
    fi
    address=$(curl -sf "${S3_HTTP_BASE}/${ADDRESSES_S3_PREFIX}/${key}" 2>/dev/null | shuf -n 1 || true)
    echo "$address"
}

mkdir -p "$DOWNLOAD_DIR"

if [ -s "$ADDRESSES_FILE" ]; then
    echo "Downloader starting: host=${CLIENT_HOST} region=${REGION} source=file:${ADDRESSES_FILE} ($(wc -l < "$ADDRESSES_FILE") addresses)"
else
    echo "Downloader starting: host=${CLIENT_HOST} region=${REGION} source=s3:${S3_URI_PREFIX:-<none>}"
fi

while true; do
    address=$(pick_random_address)
    if [ -z "$address" ]; then
        if [ -s "$ADDRESSES_FILE" ]; then
            source_desc="file:${ADDRESSES_FILE}"
        else
            source_desc="s3:${S3_URI_PREFIX:-<none>}"
        fi
        echo "[$(date -u +%H:%M:%S)] No addresses available in ${source_desc}, waiting..."
        if [ "$RUN_ONCE" = "true" ]; then
            echo "RUN_ONCE=true but no addresses available, exiting."
            exit 1
        fi
        sleep "$SLEEP_SECS"
        continue
    fi

    output_path="${DOWNLOAD_DIR}/${address}"
    stdout_file="/tmp/ant-download-${address:0:16}.stdout"
    stderr_file="/tmp/ant-download-${address:0:16}.stderr"

    echo "[$(date -u +%H:%M:%S)] Downloading: ${address}"

    # Run ant download and capture wall-clock time + exit code + output
    start_ns=$(date +%s%N)

    dl_seq=$((dl_seq + 1))
    HEAPTRACK_PREFIX=()
    if [ "$HEAPTRACK" = "1" ]; then
        HEAPTRACK_PREFIX=(heaptrack -o "${HEAPTRACK_DIR}/download-${dl_seq}")
    fi

    # shellcheck disable=SC2086
    set +e
    "${HEAPTRACK_PREFIX[@]}" "$ANT" "${VERBOSITY_FLAG[@]}" $BOOTSTRAP_ARGS --evm-network arbitrum-sepolia file download "$address" \
        -o "$output_path" > "$stdout_file" 2> "$stderr_file"
    exit_code=$?
    set -e

    end_ns=$(date +%s%N)
    total_ms=$(( (end_ns - start_ns) / 1000000 ))

    # Parse the Time: field (download-only time, excludes peer discovery)
    download_secs=$(grep -oP 'Time:\s+\K[0-9.]+' "$stdout_file" 2>/dev/null | head -1 || true)
    download_secs="${download_secs:-0}"

    # Get downloaded file size
    file_bytes=0
    if [ -f "$output_path" ]; then
        file_bytes=$(stat -c %s "$output_path" 2>/dev/null || echo "0")
    fi

    # Forward this op's stdout+stderr to the persistent aggregate logs (when
    # enabled) BEFORE preserve_failure_logs moves them or the cleanup removes
    # them. Runs for every op, success or failure.
    append_forward_log "$stdout_file" "downloader.stdout.log"
    append_forward_log "$stderr_file" "downloader.stderr.log"

    if [ "$exit_code" -eq 0 ]; then
        echo "[$(date -u +%H:%M:%S)] Download OK: ${address} total=${total_ms}ms download_time=${download_secs}s size=${file_bytes}"
    else
        kept_stem=$(preserve_failure_logs "$dl_seq" "$address" "$stdout_file" "$stderr_file")
        echo "[$(date -u +%H:%M:%S)] Download FAILED: ${address} exit_code=${exit_code} total=${total_ms}ms logs=${kept_stem}.{stdout,stderr}"
    fi

    # Submit to InfluxDB.
    # No explicit line-protocol timestamp: InfluxDB stamps each point with its
    # own (correct) server-receive time. This is essential for clock-skewed
    # clients — their wall clock can be hours off, and an explicit client
    # timestamp would land the point in the future, hiding it behind the
    # implicit `stop: now()` of every read query. The client's perceived
    # download-start time is still preserved in the `start_ns` field.
    line="download,host=${CLIENT_HOST},region=${REGION} status=${exit_code}i,total_ms=${total_ms}i,download_secs=${download_secs},file_size=${file_bytes}i,start_ns=${start_ns}i"
    submit_metric "$line"
    if [ -n "$INFLUX_URL" ]; then
        echo "[$(date -u +%H:%M:%S)] Metric submitted to InfluxDB"
    fi

    # Clean up downloaded file. On failure the tmp output files were already
    # moved into FAILURE_LOG_DIR, so the rm is a no-op for them.
    rm -f "$stdout_file" "$stderr_file" "$output_path"

    if [ "$RUN_ONCE" = "true" ]; then
        echo "RUN_ONCE=true, exiting after first download."
        exit "$exit_code"
    fi

    sleep "$SLEEP_SECS"
done
