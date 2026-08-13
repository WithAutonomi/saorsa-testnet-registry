#!/bin/sh
# Collect per-process memory composition and POST it to InfluxDB.
#
# A script rather than an inline systemd ExecStart pipeline. The inline version
# silently delivered nothing: systemd applies its own quote and variable
# expansion to ExecStart before `sh -c` ever sees the string, and a trailing
# `|| true` then swallowed whatever went wrong, so the unit reported success on
# every run while writing no data at all. Exactly the failure mode that already
# cost this ticket one canary cycle, so this script fails loudly instead.
#
# The collector emits Influx line protocol on stdout; curl posts it. Keeping the
# HTTP out of the collector keeps it a pure, unit-testable formatter, and means
# "could not read /proc" and "could not reach InfluxDB" stay distinguishable.
#
# INFLUXDB_URL / _ORG / _BUCKET / _TOKEN come from the unit's EnvironmentFile
# (/etc/default/telegraf-ant-metrics) — the same endpoint and token the metrics
# forwarder already uses, rather than a second credential to manage.
set -eu

: "${INFLUXDB_URL:?INFLUXDB_URL not set}"
: "${INFLUXDB_ORG:?INFLUXDB_ORG not set}"
: "${INFLUXDB_BUCKET:?INFLUXDB_BUCKET not set}"
: "${INFLUXDB_TOKEN:?INFLUXDB_TOKEN not set}"

COLLECTOR="${COLLECTOR:-/usr/local/bin/ant-node-memory-collector}"
DEADLINE="${DEADLINE_SECS:-30}"
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

"$COLLECTOR" --source both --deadline-secs "$DEADLINE" > "$OUT"

# No output is legitimate — a host running no node services — and must not be
# reported as a failure. The collector itself exits non-zero when services exist
# but none could be read, and `set -e` above lets that propagate.
if [ ! -s "$OUT" ]; then
    exit 0
fi

# --fail-with-body so an InfluxDB rejection (bad field type, bad token) becomes
# a non-zero exit with the reason in the journal, rather than a silent no-op.
exec curl --silent --show-error --fail-with-body --max-time 30 \
    -X POST \
    "${INFLUXDB_URL%/}/api/v2/write?org=${INFLUXDB_ORG}&bucket=${INFLUXDB_BUCKET}&precision=ns" \
    -H "Authorization: Token ${INFLUXDB_TOKEN}" \
    --data-binary "@${OUT}"
