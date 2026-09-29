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
# Largest file this downloader will pick, in bytes; 0 = no bound. Address lines
# carry the uploaded size as an optional second field (`<address> <bytes>`),
# so a client with a hard cap (the WASM tier's browser limit) can skip an
# over-cap entry before fetching anything. Lines with no size are never
# excluded: the size is unknown, not known to be too big.
: "${MAX_DOWNLOAD_BYTES:=0}"
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
# additionally export RUST_LOG. The systemd EnvironmentFile always defines
# RUST_LOG (empty when none configured), and an EMPTY RUST_LOG sets ant's
# EnvFilter to an empty filter that silences ALL output, overriding -v — so it
# MUST be unset (not merely left unexported, since systemd already exported it).
VERBOSITY_FLAG=(-v)
if [ -n "$RUST_LOG" ]; then
    export RUST_LOG
else
    unset RUST_LOG
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

eligible_addresses() {
    # stdin: address lines, `<address>`, `<address> <bytes>` or
    # `<address> <bytes> <sha256>`. stdout: `<address>` or `<address> <sha256>`
    # for each entry whose size is unknown or within MAX_DOWNLOAD_BYTES.
    #
    # The hash is carried through selection rather than looked up afterwards:
    # by the time an address has been picked the line it came from is gone, and
    # an address alone cannot say what bytes were uploaded under it. A line with
    # no third field stays eligible -- the WASM uploaders publish two-field
    # lines into this same shared pool -- and its download verifies as
    # `unavailable`, never as a mismatch.
    awk -v max="${MAX_DOWNLOAD_BYTES:-0}" '
        NF == 0 { next }
        max > 0 && NF >= 2 && ($2 + 0) > max { next }
        NF >= 3 { print $1, $3; next }
        { print $1 }'
}

verify_download() {
    # Compare a downloaded file against the SHA-256 its uploader published.
    # Echoes `<code> <actual>`: 0 = ok, 1 = mismatch, 2 = unavailable (the line
    # carried no hash, or the output is not readable). Unavailable is NOT a
    # failure -- the content may be perfectly healthy, we simply have nothing
    # to check it against -- so it is its own code rather than a mismatch.
    local expected="$1" path="$2" actual=""
    if [ -n "$expected" ] && [ -f "$path" ]; then
        actual=$(sha256sum "$path" 2>/dev/null | awk '{print $1}')
    fi
    if [ -z "$expected" ] || [ -z "$actual" ]; then
        echo "2"
    elif [ "$actual" = "$expected" ]; then
        echo "0 $actual"
    else
        echo "1 $actual"
    fi
}

pick_random_address() {
    # Prefer a locally-provisioned static address list (e.g. from
    # prod-continuous-downloader) when present and non-empty. Falls back to
    # listing S3 uploader-emitted objects when no local file is available
    # (the standard testnet flow). The S3 path uses unsigned public HTTPS so
    # it keeps working on clock-skewed VMs.
    #
    # Each address list is filtered through eligible_addresses, so an
    # over-cap entry is skipped rather than fetched and rejected. One
    # uploader's whole list may be over the cap (every entry it publishes is
    # the same size), so the S3 path tries a few keys before giving up.
    #
    # Prints the selected entry as `<address>` or `<address> <sha256>`, the
    # shape eligible_addresses emits.
    local listing key address attempt
    if [ -s "$ADDRESSES_FILE" ]; then
        eligible_addresses < "$ADDRESSES_FILE" | shuf -n 1 || true
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
    for attempt in 1 2 3 4 5; do
        key=$(echo "$listing" | shuf -n 1)
        if [ -z "$key" ]; then
            return 0
        fi
        address=$(curl -sf "${S3_HTTP_BASE}/${ADDRESSES_S3_PREFIX}/${key}" 2>/dev/null \
            | eligible_addresses | shuf -n 1 || true)
        if [ -n "$address" ]; then
            echo "$address"
            return 0
        fi
    done
}

mkdir -p "$DOWNLOAD_DIR"

if [ -s "$ADDRESSES_FILE" ]; then
    echo "Downloader starting: host=${CLIENT_HOST} region=${REGION} source=file:${ADDRESSES_FILE} ($(wc -l < "$ADDRESSES_FILE") addresses)"
else
    echo "Downloader starting: host=${CLIENT_HOST} region=${REGION} source=s3:${S3_URI_PREFIX:-<none>}"
fi

while true; do
    selection=$(pick_random_address)
    address=""
    expected_sha256=""
    if [ -n "$selection" ]; then
        read -r address expected_sha256 <<< "$selection"
    fi
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

    # Content verification runs here, while the output file is still on disk:
    # the cleanup at the foot of the loop removes it. Only a SUCCESSFUL
    # download is verified -- one that failed is an availability problem and
    # has no bytes to check.
    verify_status=""
    actual_sha256=""
    if [ "$exit_code" -eq 0 ]; then
        read -r verify_status actual_sha256 <<< "$(verify_download "$expected_sha256" "$output_path")"
        actual_sha256="${actual_sha256:-}"
        echo "[$(date -u +%H:%M:%S)] Download OK: ${address} total=${total_ms}ms download_time=${download_secs}s size=${file_bytes} verify=${verify_status}"
        if [ "$verify_status" = "1" ]; then
            echo "[$(date -u +%H:%M:%S)] CONTENT MISMATCH: ${address} expected=${expected_sha256} actual=${actual_sha256}"
        fi
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
    #
    # `status` is NOT ant's exit code. `record-results` reads it as an enum --
    # 0 success, 1 failure, 2 skipped-by-the-transport -- and DROPS anything
    # else as an unusable point, so an `ant` exit of 3 would be discarded
    # rather than recorded as the failure it is. This script has no skip path,
    # so every non-zero exit is clamped to 1 here, at the emit site.
    #
    # `verify_status` is omitted entirely on a failed download: there were no
    # bytes to check, which is not the same as having checked and found
    # nothing wrong. The two hashes ride along on a MISMATCH only -- on a match
    # they are the same value, already published, and on an unverifiable
    # download there is nothing to record.
    op_status=0
    if [ "$exit_code" -ne 0 ]; then
        op_status=1
    fi
    line="download,host=${CLIENT_HOST},region=${REGION} status=${op_status}i,total_ms=${total_ms}i,download_secs=${download_secs},file_size=${file_bytes}i,start_ns=${start_ns}i"
    if [ -n "$verify_status" ]; then
        line="${line},verify_status=${verify_status}i"
        if [ "$verify_status" = "1" ]; then
            line="${line},expected_sha256=\"${expected_sha256}\",actual_sha256=\"${actual_sha256}\""
        fi
    fi
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
