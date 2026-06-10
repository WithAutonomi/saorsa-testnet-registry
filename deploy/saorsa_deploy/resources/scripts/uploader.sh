#!/usr/bin/env bash
set -euo pipefail

# --- Configuration (set via environment) ---
: "${BOOTSTRAP_CACHE:=true}"
if [ "$BOOTSTRAP_CACHE" != "true" ]; then
    : "${BOOTSTRAP_ARGS:?BOOTSTRAP_ARGS must be set when BOOTSTRAP_CACHE=false}"
fi
: "${BOOTSTRAP_ARGS:=}"
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
: "${UPLOAD_PRESIGNED_URL:=}"
: "${FORWARD_LOGS:=false}"
: "${RUST_LOG:=}"
: "${FORWARD_LOG_DIR:=/var/log/ant-client-forward}"
# Coarse disk guard for the append-only forward logs (truncate beyond this).
: "${FORWARD_LOG_MAX_BYTES:=1073741824}"

# Translate PAYMENT_TYPE into the matching ant flag (or nothing).
PAYMENT_FLAG=()
case "$PAYMENT_TYPE" in
    merkle)    PAYMENT_FLAG=(--merkle) ;;
    no-merkle) PAYMENT_FLAG=(--no-merkle) ;;
esac

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
# Per-op /tmp files are deleted on success, so without this the forwarder would
# race the cleanup; appending to a stable file avoids that entirely.
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
    #
    # The upload goes through a presigned PUT URL when one was provisioned:
    # the signature is the controller's, so the flush keeps working on
    # clock-skewed VMs whose own SigV4 requests are rejected (~15min
    # tolerance). Falls back to the signed aws CLI path when no URL is set.
    if [ ! -s "$ADDRESSES_FILE" ]; then
        return 0
    fi
    local tmp="${ADDRESSES_FILE}.tail"
    tail -n "$ADDRESSES_TAIL_SIZE" "$ADDRESSES_FILE" > "$tmp"
    mv "$tmp" "$ADDRESSES_FILE"
    if [ -n "$UPLOAD_PRESIGNED_URL" ]; then
        # Insist on a literal 200: curl -f only fails on >=400, so a 307
        # redirect (e.g. presigned against the wrong endpoint) would
        # otherwise pass while storing nothing.
        local put_status
        put_status=$(curl -s -o /dev/null -w "%{http_code}" \
            -X PUT -T "$ADDRESSES_FILE" "$UPLOAD_PRESIGNED_URL" || echo 000)
        if [ "$put_status" = "200" ]; then
            echo "[$(date -u +%H:%M:%S)] Flushed $(wc -l < "$ADDRESSES_FILE") addresses to ${S3_URI} (presigned)"
            return 0
        else
            echo "[$(date -u +%H:%M:%S)] WARN: S3 flush failed (presigned PUT http=${put_status}, ${S3_URI})"
            return 1
        fi
    fi
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

    # Run ant and capture wall-clock time + exit code + output.
    # `--json` prints a single JSON object on stdout (success path) or no
    # output on early failure; `-v` trace goes to stderr separately so it
    # never interleaves into the JSON stream. Both are preserved on disk
    # so a failed run remains diagnosable.
    output_file="/tmp/ant-upload-${seq_num}.json"
    stderr_file="/tmp/ant-upload-${seq_num}.err"
    start_ns=$(date +%s%N)

    HEAPTRACK_PREFIX=()
    if [ "$HEAPTRACK" = "1" ]; then
        HEAPTRACK_PREFIX=(heaptrack -o "${HEAPTRACK_DIR}/upload-${seq_num}")
    fi

    # shellcheck disable=SC2086
    set +e
    "${HEAPTRACK_PREFIX[@]}" "$ANT" "${VERBOSITY_FLAG[@]}" $BOOTSTRAP_ARGS --evm-network arbitrum-sepolia --json \
        file upload --public "${PAYMENT_FLAG[@]}" "$file_path" \
        > "$output_file" 2> "$stderr_file"
    exit_code=$?
    set -e

    end_ns=$(date +%s%N)
    total_ms=$(( (end_ns - start_ns) / 1000000 ))

    # Parse the JSON record. Older `ant` binaries that don't carry the
    # chunk_attempts_total / retries_histogram / store_durations_ms fields
    # (added in WithAutonomi/ant-client PR #87) fall back to zeros so the
    # script keeps working unchanged.
    parsed_fields=$(python3 - "$output_file" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path) as fh:
        text = fh.read().strip()
    data = json.loads(text.splitlines()[-1]) if text else {}
except Exception:
    data = {}

def pct(xs, p):
    if not xs:
        return 0
    xs = sorted(int(v) for v in xs)
    return xs[min(len(xs) - 1, max(0, int(p * len(xs))))]

hist = list(data.get("retries_histogram") or [0, 0, 0, 0])
hist = (hist + [0, 0, 0, 0])[:4]
durs = [int(v) for v in (data.get("store_durations_ms") or []) if isinstance(v, (int, float))]

print(
    data.get("elapsed_secs", 0),
    data.get("address") or "-",
    int(data.get("chunks_stored", 0) or 0),
    int(data.get("chunks_failed", 0) or 0),
    int(data.get("total_chunks", 0) or 0),
    int(data.get("chunk_attempts_total", 0) or 0),
    hist[0], hist[1], hist[2], hist[3],
    pct(durs, 0.50), pct(durs, 0.95),
    max(durs) if durs else 0,
)
PY
)
    read -r upload_secs address chunks_stored chunks_failed total_chunks \
        chunk_attempts_total retries_round_0 retries_round_1 retries_round_2 retries_round_3 \
        store_duration_p50_ms store_duration_p95_ms store_duration_max_ms \
        <<< "$parsed_fields"
    upload_secs="${upload_secs:-0}"
    address="${address:-}"
    [ "$address" = "-" ] && address=""
    chunks_stored="${chunks_stored:-0}"
    chunks_failed="${chunks_failed:-0}"
    total_chunks="${total_chunks:-0}"
    chunk_attempts_total="${chunk_attempts_total:-0}"
    retries_round_0="${retries_round_0:-0}"
    retries_round_1="${retries_round_1:-0}"
    retries_round_2="${retries_round_2:-0}"
    retries_round_3="${retries_round_3:-0}"
    store_duration_p50_ms="${store_duration_p50_ms:-0}"
    store_duration_p95_ms="${store_duration_p95_ms:-0}"
    store_duration_max_ms="${store_duration_max_ms:-0}"

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

    # Submit to InfluxDB.
    # chunk_attempts_total / retries_round_N / store_duration_*_ms come from
    # the ant --json output and will be 0 against binaries that predate
    # WithAutonomi/ant-client PR #87 (the chunk-retry instrumentation).
    #
    # No explicit line-protocol timestamp: InfluxDB stamps each point with its
    # own (correct) server-receive time. This is essential for clock-skewed
    # clients — their wall clock can be hours off, and an explicit client
    # timestamp would land the point in the future, hiding it behind the
    # implicit `stop: now()` of every read query. The client's perceived
    # upload-start time is still preserved in the `start_ns` field.
    line="upload,host=${CLIENT_HOST},region=${REGION} status=${exit_code}i,total_ms=${total_ms}i,upload_secs=${upload_secs},file_size=${file_bytes}i,start_ns=${start_ns}i,chunks_stored=${chunks_stored}i,chunks_failed=${chunks_failed}i,total_chunks=${total_chunks}i,chunk_attempts_total=${chunk_attempts_total}i,retries_round_0=${retries_round_0}i,retries_round_1=${retries_round_1}i,retries_round_2=${retries_round_2}i,retries_round_3=${retries_round_3}i,store_duration_p50_ms=${store_duration_p50_ms}i,store_duration_p95_ms=${store_duration_p95_ms}i,store_duration_max_ms=${store_duration_max_ms}i"
    submit_metric "$line"
    if [ -n "$INFLUX_URL" ]; then
        echo "[$(date -u +%H:%M:%S)] Metric submitted to InfluxDB"
    fi

    # Forward this op's stdout+stderr to the persistent aggregate logs (when
    # enabled) BEFORE the success-path cleanup deletes the per-op files.
    append_forward_log "$output_file" "uploader.stdout.log"
    append_forward_log "$stderr_file" "uploader.stderr.log"

    # Clean up (preserve output on failure for diagnosis)
    if [ "$exit_code" -ne 0 ]; then
        echo "[$(date -u +%H:%M:%S)] Output preserved at $output_file (stderr: $stderr_file) for diagnosis"
    else
        rm -f "$output_file" "$stderr_file"
    fi
    rm -f "$file_path"

    if [ "$RUN_ONCE" = "true" ]; then
        echo "RUN_ONCE=true, flushing and exiting after first upload."
        flush_to_s3 || true
        exit "$exit_code"
    fi

    sleep "$SLEEP_SECS"
done
