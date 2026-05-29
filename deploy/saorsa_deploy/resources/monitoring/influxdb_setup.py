"""InfluxDB initial setup via HTTP API.

Deployed to the monitoring VM and executed once to create the org, bucket,
admin token, and a read/write Telegraf token.

Placeholder values (replaced at deploy time):
  {{INFLUXDB_ADMIN_USER}}  - admin username
  {{INFLUXDB_ADMIN_PASSWORD}} - generated admin password
  {{INFLUXDB_ORG}}          - organisation name
  {{INFLUXDB_BUCKET}}       - node-tier bucket name
  {{INFLUXDB_CLIENT_BUCKET}} - client-tier bucket name
"""

import json
import os
import sys
import urllib.error
import urllib.request

TOKEN_FILE = "/etc/influxdb2/telegraf-token"
ADMIN_TOKEN_FILE = "/etc/influxdb2/admin-token"

if os.path.exists(TOKEN_FILE) and os.path.getsize(TOKEN_FILE) > 1:
    print("INFLUXDB_SETUP:SKIP")
    sys.exit(0)


def api(method, path, data=None, token=None):
    url = "http://localhost:8086" + path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Token {token}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"API error {e.code} on {path}: {body}", file=sys.stderr)
        sys.exit(1)


# Step 1: Initial setup (creates the org + the node-tier bucket)
setup = api("POST", "/api/v2/setup", {
    "username": "{{INFLUXDB_ADMIN_USER}}",
    "password": "{{INFLUXDB_ADMIN_PASSWORD}}",
    "org": "{{INFLUXDB_ORG}}",
    "bucket": "{{INFLUXDB_BUCKET}}",
    "retentionPeriodSeconds": 0,
})
admin_token = setup["auth"]["token"]
org_id = setup["org"]["id"]

# Step 2: Create the client-tier bucket. Client host metrics land here so the
# Client Host Statistics dashboard is physically isolated from node data.
api("POST", "/api/v2/buckets", {
    "orgID": org_id,
    "name": "{{INFLUXDB_CLIENT_BUCKET}}",
    "retentionRules": [],
}, token=admin_token)

# Step 3: Create a read/write token for Telegraf. Scoped at the org level so it
# covers both buckets (and any added later) — InfluxDB v2 tokens are immutable,
# so a per-bucket token would have to be reminted whenever a bucket is added.
auth = api("POST", "/api/v2/authorizations", {
    "orgID": org_id,
    "description": "Telegraf write token",
    "permissions": [
        {"action": "read", "resource": {"type": "buckets", "orgID": org_id}},
        {"action": "write", "resource": {"type": "buckets", "orgID": org_id}},
    ],
}, token=admin_token)
telegraf_token = auth["token"]

# Step 3: Save tokens
os.makedirs("/etc/influxdb2", exist_ok=True)
with open(ADMIN_TOKEN_FILE, "w") as f:
    f.write(admin_token)
os.chmod(ADMIN_TOKEN_FILE, 0o600)
with open(TOKEN_FILE, "w") as f:
    f.write(telegraf_token)
os.chmod(TOKEN_FILE, 0o600)

print("INFLUXDB_SETUP:DONE")
