#!/usr/bin/env bash
set -euo pipefail

# --- Configuration (set via environment) ---
: "${BOOTSTRAP_ARGS:?BOOTSTRAP_ARGS must be set}"
: "${INFLUX_URL:=}"
: "${INFLUX_TOKEN:=}"
: "${INFLUX_ORG:=ant}"
: "${INFLUX_BUCKET:=testnet-metrics}"
: "${REGION:=$(hostname | sed 's/.*-do-\([a-z0-9]*\)-.*/\1/')}"
: "${SLEEP_SECS:=10}"
: "${ADDRESSES_FILE:=/var/lib/ant-client/addresses.txt}"
: "${DOWNLOAD_DIR:=/var/lib/ant-client/downloads}"
: "${ANT:=/root/.local/bin/ant}"
: "${RUN_ONCE:=false}"

CLIENT_HOST=$(hostname)

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

mkdir -p "$DOWNLOAD_DIR"

echo "Downloader starting: host=${CLIENT_HOST} region=${REGION}"

while true; do
    # Wait for addresses to be available
    if [ ! -s "$ADDRESSES_FILE" ]; then
        echo "[$(date -u +%H:%M:%S)] No addresses yet, waiting..."
        sleep "$SLEEP_SECS"
        if [ "$RUN_ONCE" = "true" ]; then
            echo "RUN_ONCE=true but no addresses available, exiting."
            exit 1
        fi
        continue
    fi

    address=$(shuf -n 1 "$ADDRESSES_FILE")
    if [ -z "$address" ]; then
        sleep "$SLEEP_SECS"
        continue
    fi

    output_path="${DOWNLOAD_DIR}/${address}"
    output_file="/tmp/ant-download-${address:0:16}.txt"

    echo "[$(date -u +%H:%M:%S)] Downloading: ${address}"

    # Run ant download and capture wall-clock time + exit code + output
    start_ns=$(date +%s%N)

    # shellcheck disable=SC2086
    set +e
    "$ANT" -v $BOOTSTRAP_ARGS --evm-network arbitrum-sepolia file download "$address" \
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

    # Submit to InfluxDB
    timestamp_ns="$end_ns"
    line="download,host=${CLIENT_HOST},region=${REGION} status=${exit_code}i,total_ms=${total_ms}i,download_secs=${download_secs},file_size=${file_bytes}i,start_ns=${start_ns}i ${timestamp_ns}"
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
