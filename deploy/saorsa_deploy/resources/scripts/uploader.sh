#!/usr/bin/env bash
set -euo pipefail

# --- Configuration (set via environment) ---
: "${BOOTSTRAP_ARGS:?BOOTSTRAP_ARGS must be set}"
: "${FILE_SIZE_KB:?FILE_SIZE_KB must be set (e.g. 51200 for 50MB)}"
: "${SECRET_KEY:?SECRET_KEY must be set}"
: "${ADDRESSES_S3_BUCKET:?ADDRESSES_S3_BUCKET must be set}"
: "${ADDRESSES_S3_PREFIX:?ADDRESSES_S3_PREFIX must be set}"
: "${REGION:?REGION must be set}"
: "${CLIENT_INDEX:?CLIENT_INDEX must be set}"
: "${INFLUX_URL:=}"
: "${INFLUX_TOKEN:=}"
: "${INFLUX_ORG:=ant}"
: "${INFLUX_BUCKET:=testnet-metrics}"
: "${SLEEP_SECS:=1}"
: "${ADDRESSES_FILE:=/var/lib/ant-client/addresses.txt}"
: "${UPLOAD_DIR:=/var/lib/ant-client/uploads}"
: "${ANT:=/root/.local/bin/ant}"
: "${RUN_ONCE:=false}"
: "${ADDRESSES_TAIL_SIZE:=10000}"
: "${S3_FLUSH_INTERVAL_SECS:=30}"
: "${S3_FLUSH_EVERY_N:=50}"
: "${PAYMENT_TYPE:=}"
: "${HEAPTRACK:=0}"
: "${HEAPTRACK_DIR:=/var/log/ant-heaptrack}"

# Translate PAYMENT_TYPE into the matching ant flag (or nothing).
PAYMENT_FLAG=()
case "$PAYMENT_TYPE" in
    merkle)    PAYMENT_FLAG=(--merkle) ;;
    no-merkle) PAYMENT_FLAG=(--no-merkle) ;;
esac

CLIENT_HOST=$(hostname)
S3_KEY="${ADDRESSES_S3_PREFIX}/upload-${REGION}-${CLIENT_INDEX}.txt"
S3_URI="s3://${ADDRESSES_S3_BUCKET}/${S3_KEY}"

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

flush_to_s3() {
    # Rotate local buffer to last ADDRESSES_TAIL_SIZE lines, then upload.
    # If upload fails we keep the buffer and try again next flush window.
    if [ ! -s "$ADDRESSES_FILE" ]; then
        return 0
    fi
    local tmp="${ADDRESSES_FILE}.tail"
    tail -n "$ADDRESSES_TAIL_SIZE" "$ADDRESSES_FILE" > "$tmp"
    mv "$tmp" "$ADDRESSES_FILE"
    if aws s3 cp --quiet "$ADDRESSES_FILE" "$S3_URI"; then
        echo "[$(date -u +%H:%M:%S)] Flushed $(wc -l < "$ADDRESSES_FILE") addresses to ${S3_URI}"
        return 0
    else
        echo "[$(date -u +%H:%M:%S)] WARN: S3 flush failed (${S3_URI})"
        return 1
    fi
}

mkdir -p "$UPLOAD_DIR"
touch "$ADDRESSES_FILE"

if [ "$HEAPTRACK" = "1" ]; then
    mkdir -p "$HEAPTRACK_DIR"
    echo "Heaptrack enabled: profiles will be written under ${HEAPTRACK_DIR}"
fi

echo "Uploader starting: host=${CLIENT_HOST} region=${REGION} index=${CLIENT_INDEX} file_size=${FILE_SIZE_KB}KB s3=${S3_URI}"

addresses_since_flush=0
last_flush_ts=$(date +%s)
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

    HEAPTRACK_PREFIX=()
    if [ "$HEAPTRACK" = "1" ]; then
        HEAPTRACK_PREFIX=(heaptrack -o "${HEAPTRACK_DIR}/upload-${seq_num}")
    fi

    # shellcheck disable=SC2086
    set +e
    "${HEAPTRACK_PREFIX[@]}" "$ANT" -v $BOOTSTRAP_ARGS --evm-network arbitrum-sepolia file upload --public "${PAYMENT_FLAG[@]}" "$file_path" \
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
        addresses_since_flush=$((addresses_since_flush + 1))
        echo "[$(date -u +%H:%M:%S)] Upload #${seq_num} OK: address=${address} total=${total_ms}ms upload_time=${upload_secs}s"
    else
        echo "[$(date -u +%H:%M:%S)] Upload #${seq_num} FAILED: exit_code=${exit_code} total=${total_ms}ms"
    fi

    # Flush to S3 if enough addresses accrued or enough time elapsed
    now_ts=$(date +%s)
    elapsed=$((now_ts - last_flush_ts))
    if [ "$addresses_since_flush" -ge "$S3_FLUSH_EVERY_N" ] || \
       { [ "$addresses_since_flush" -gt 0 ] && [ "$elapsed" -ge "$S3_FLUSH_INTERVAL_SECS" ]; }; then
        if flush_to_s3; then
            addresses_since_flush=0
            last_flush_ts=$now_ts
        fi
    fi

    # Submit to InfluxDB
    timestamp_ns="$end_ns"
    line="upload,host=${CLIENT_HOST},region=${REGION} status=${exit_code}i,total_ms=${total_ms}i,upload_secs=${upload_secs},file_size=${file_bytes}i,start_ns=${start_ns}i ${timestamp_ns}"
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
        echo "RUN_ONCE=true, flushing and exiting after first upload."
        flush_to_s3 || true
        exit "$exit_code"
    fi

    sleep "$SLEEP_SECS"
done
