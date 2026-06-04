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

if [ "$HEAPTRACK" = "1" ]; then
    mkdir -p "$HEAPTRACK_DIR"
    echo "Heaptrack enabled: profiles will be written under ${HEAPTRACK_DIR}"
fi

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
else
    S3_URI_PREFIX=""
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

pick_random_address() {
    # Prefer a locally-provisioned static address list (e.g. from
    # prod-continuous-downloader) when present and non-empty. Falls back to
    # listing S3 uploader-emitted objects when no local file is available
    # (the standard testnet flow).
    local listing key s3_uri address
    if [ -s "$ADDRESSES_FILE" ]; then
        shuf -n 1 "$ADDRESSES_FILE" || true
        return 0
    fi
    if [ -z "$S3_URI_PREFIX" ]; then
        return 0
    fi
    listing=$(aws s3 ls "$S3_URI_PREFIX" 2>/dev/null | awk '{print $NF}' | grep '^upload-' || true)
    if [ -z "$listing" ]; then
        return 0
    fi
    key=$(echo "$listing" | shuf -n 1)
    if [ -z "$key" ]; then
        return 0
    fi
    s3_uri="${S3_URI_PREFIX}${key}"
    address=$(aws s3 cp --quiet "$s3_uri" - 2>/dev/null | shuf -n 1 || true)
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
    output_file="/tmp/ant-download-${address:0:16}.txt"

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
    "${HEAPTRACK_PREFIX[@]}" "$ANT" -v $BOOTSTRAP_ARGS --evm-network arbitrum-sepolia file download "$address" \
        -o "$output_path" > "$output_file" 2>&1
    exit_code=$?
    set -e

    end_ns=$(date +%s%N)
    total_ms=$(( (end_ns - start_ns) / 1000000 ))

    # Parse the Time: field (download-only time, excludes peer discovery)
    download_secs=$(grep -oP 'Time:\s+\K[0-9.]+' "$output_file" 2>/dev/null | head -1 || true)
    download_secs="${download_secs:-0}"

    # Get downloaded file size
    file_bytes=0
    if [ -f "$output_path" ]; then
        file_bytes=$(stat -c %s "$output_path" 2>/dev/null || echo "0")
    fi

    if [ "$exit_code" -eq 0 ]; then
        echo "[$(date -u +%H:%M:%S)] Download OK: ${address} total=${total_ms}ms download_time=${download_secs}s size=${file_bytes}"
    else
        echo "[$(date -u +%H:%M:%S)] Download FAILED: ${address} exit_code=${exit_code} total=${total_ms}ms"
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

    # Clean up downloaded file
    rm -f "$output_file" "$output_path"

    if [ "$RUN_ONCE" = "true" ]; then
        echo "RUN_ONCE=true, exiting after first download."
        exit "$exit_code"
    fi

    sleep "$SLEEP_SECS"
done
