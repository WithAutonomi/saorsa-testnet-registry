#!/usr/bin/env bash
set -euo pipefail

# --- Configuration (set via environment) ---
: "${BOOTSTRAP_ARGS:?BOOTSTRAP_ARGS must be set}"
: "${FILE_SIZE_KB:?FILE_SIZE_KB must be set (e.g. 51200 for 50MB)}"
: "${SECRET_KEY:?SECRET_KEY must be set}"
: "${INFLUX_URL:=}"
: "${INFLUX_TOKEN:=}"
: "${INFLUX_ORG:=ant}"
: "${INFLUX_BUCKET:=testnet-metrics}"
: "${REGION:=$(hostname | sed 's/.*-do-\([a-z0-9]*\)-.*/\1/')}"
: "${SLEEP_SECS:=1}"
: "${ADDRESSES_FILE:=/var/lib/ant-client/addresses.txt}"
: "${UPLOAD_DIR:=/var/lib/ant-client/uploads}"
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

mkdir -p "$UPLOAD_DIR"
touch "$ADDRESSES_FILE"

echo "Uploader starting: host=${CLIENT_HOST} region=${REGION} file_size=${FILE_SIZE_KB}KB"

seq_num=0
while true; do
    seq_num=$((seq_num + 1))
    file_path="${UPLOAD_DIR}/testfile-${seq_num}.bin"

    # Generate random file
    dd if=/dev/urandom of="$file_path" bs=1024 count="$FILE_SIZE_KB" 2>/dev/null
    file_bytes=$((FILE_SIZE_KB * 1024))

    echo "[$(date -u +%H:%M:%S)] Upload #${seq_num}: generating ${FILE_SIZE_KB}KB file, starting ant..."

    # Run ant and capture wall-clock time + exit code + output
    output_file="/tmp/ant-upload-${seq_num}.txt"
    start_ns=$(date +%s%N)

    # shellcheck disable=SC2086
    set +e
    "$ANT" -v $BOOTSTRAP_ARGS --evm-network arbitrum-sepolia --store-concurrency 16 file upload --public "$file_path" \
        2>&1 | tee "$output_file"
    exit_code=${PIPESTATUS[0]}
    set -e

    end_ns=$(date +%s%N)
    total_ms=$(( (end_ns - start_ns) / 1000000 ))

    # Parse the Time: field (upload-only time, excludes peer discovery)
    upload_secs=$(grep -oP 'Time:\s+\K[0-9.]+' "$output_file" 2>/dev/null | head -1 || true)
    upload_secs="${upload_secs:-0}"

    # Parse address if upload succeeded
    address=$(grep -oP 'Address:\s+\K\S+' "$output_file" 2>/dev/null | head -1 || true)
    address="${address:-}"

    if [ "$exit_code" -eq 0 ] && [ -n "$address" ]; then
        echo "$address" >> "$ADDRESSES_FILE"
        echo "[$(date -u +%H:%M:%S)] Upload #${seq_num} OK: address=${address} total=${total_ms}ms upload_time=${upload_secs}s"
    else
        echo "[$(date -u +%H:%M:%S)] Upload #${seq_num} FAILED: exit_code=${exit_code} total=${total_ms}ms"
    fi

    # Submit to InfluxDB
    timestamp_ns="$end_ns"
    line="upload,host=${CLIENT_HOST},region=${REGION} status=${exit_code}i,total_ms=${total_ms}i,upload_secs=${upload_secs},file_size=${file_bytes}i ${timestamp_ns}"
    submit_metric "$line"
    if [ -n "$INFLUX_URL" ]; then
        echo "[$(date -u +%H:%M:%S)] Metric submitted to InfluxDB"
    fi

    # Clean up (preserve output on failure for diagnosis)
    if [ "$exit_code" -ne 0 ]; then
        echo "[$(date -u +%H:%M:%S)] Output preserved at $output_file for diagnosis"
    else
        rm -f "$output_file"
    fi
    rm -f "$file_path"

    if [ "$RUN_ONCE" = "true" ]; then
        echo "RUN_ONCE=true, exiting after first upload."
        exit "$exit_code"
    fi

    sleep "$SLEEP_SECS"
done
