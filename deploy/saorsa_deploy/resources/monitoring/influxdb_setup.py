"""InfluxDB initial setup via HTTP API.

Deployed to the monitoring VM and executed once to create the org, bucket,
admin token, and a read/write Telegraf token.

Placeholder values (replaced at deploy time):
  {{INFLUXDB_ADMIN_USER}}  - admin username
  {{INFLUXDB_ADMIN_PASSWORD}} - generated admin password
  {{INFLUXDB_ORG}}          - organisation name
  {{INFLUXDB_BUCKET}}       - bucket name
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


# Step 1: Initial setup
setup = api("POST", "/api/v2/setup", {
    "username": "{{INFLUXDB_ADMIN_USER}}",
    "password": "{{INFLUXDB_ADMIN_PASSWORD}}",
    "org": "{{INFLUXDB_ORG}}",
    "bucket": "{{INFLUXDB_BUCKET}}",
    "retentionPeriodSeconds": 0,
})
admin_token = setup["auth"]["token"]
org_id = setup["org"]["id"]
bucket_id = setup["bucket"]["id"]

# Step 2: Create a read/write token for Telegraf
auth = api("POST", "/api/v2/authorizations", {
    "orgID": org_id,
    "description": "Telegraf write token",
    "permissions": [
        {"action": "read", "resource": {"type": "buckets", "id": bucket_id}},
        {"action": "write", "resource": {"type": "buckets", "id": bucket_id}},
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
