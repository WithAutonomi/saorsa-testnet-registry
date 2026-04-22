"""Elasticsearch 8.x provisioning for log forwarding VMs."""

import secrets
import subprocess

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.provisioning.apt import (
    DISABLE_UNATTENDED_UPGRADES_OP_NAME,
    build_disable_unattended_upgrades_command,
)
from saorsa_deploy.provisioning.volume import VOLUME_DEVICE_GLOB

ELASTIC_MOUNT_POINT = "/mnt/elastic-storage"
ELASTIC_DATA_DIR = f"{ELASTIC_MOUNT_POINT}/elasticsearch"
ELASTIC_LOG_DIR = f"{ELASTIC_MOUNT_POINT}/elasticsearch/logs"

ES_VERSION = "8.17.4"
ES_DEB_URL = (
    f"https://artifacts.elastic.co/downloads/elasticsearch/"
    f"elasticsearch-{ES_VERSION}-amd64.deb"
)

INDEX_PATTERN = "telegraf-ant-nodes-*"
ES_USERNAME = "ant-log-writer"
ILM_POLICY_NAME = "ant-logs-policy"
DEFAULT_RETENTION_DAYS = 7

#: Default shards per daily index. With a single-node ES cluster, each
#: shard pins to exactly one indexing thread — so N shards means N CPUs
#: can flush concurrently. Matches the default ``s-4vcpu-8gb`` droplet;
#: bump to 8 when running on ``s-8vcpu-16gb`` or larger. Setting > CPU
#: count gives diminishing returns and raises per-shard overhead.
DEFAULT_INDEX_SHARDS = 4


class ElasticProvisioner:
    """Provisions Elasticsearch 8.x on a dedicated VM."""

    def __init__(
        self,
        host_ip: str,
        ssh_key_path: str,
        testnet_name: str,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        shards: int = DEFAULT_INDEX_SHARDS,
        console: Console | None = None,
    ):
        if retention_days < 1:
            raise ValueError(
                f"retention_days must be >= 1 (got {retention_days})"
            )
        if shards < 1:
            raise ValueError(f"shards must be >= 1 (got {shards})")
        self.host_ip = host_ip
        self.ssh_key_path = ssh_key_path
        self.testnet_name = testnet_name
        self.retention_days = retention_days
        self.shards = shards
        self.console = console or Console()
        self._es_password = secrets.token_urlsafe(24)

    def execute(self) -> dict:
        """Install and configure Elasticsearch.

        Returns a dict with connection details:
            elasticsearch_url, elasticsearch_username, elasticsearch_password,
            elasticsearch_api_key, elasticsearch_index_pattern
        """
        hosts_data = [
            (self.host_ip, {"ssh_user": "root", "ssh_key": self.ssh_key_path})
        ]
        inventory = Inventory((hosts_data, {}))
        state = State(inventory=inventory, config=Config())

        self.console.print(f"Connecting to {self.host_ip}...")
        connect_all(state)

        try:
            add_op(
                state,
                server.shell,
                name="Wait for cloud-init to finish",
                commands=["cloud-init status --wait || true"],
            )

            add_op(
                state,
                server.shell,
                name=DISABLE_UNATTENDED_UPGRADES_OP_NAME,
                commands=[build_disable_unattended_upgrades_command()],
            )

            self._add_mount_volume_op(state)
            self._add_install_elasticsearch_ops(state)
            self._add_configure_elasticsearch_ops(state)
            self._add_start_elasticsearch_ops(state)
            self._add_setup_security_ops(state)

            self.console.print("Running Elasticsearch provisioning operations...")
            run_ops(state)
        finally:
            disconnect_all(state)

        failed = state.failed_hosts
        if failed:
            for host in failed:
                self.console.print(f"  [red]Failed: {host.name}[/red]")
            raise RuntimeError("Elasticsearch provisioning failed")

        connection_details = self._retrieve_connection_details()
        return connection_details

    def _add_mount_volume_op(self, state: State):
        mount_cmd = (
            f"if mountpoint -q {ELASTIC_MOUNT_POINT}; then "
            f"echo 'ELASTIC_VOLUME:ALREADY_MOUNTED'; "
            f"else "
            f"DEVICE=$(ls {VOLUME_DEVICE_GLOB} 2>/dev/null | head -n1); "
            f'if [ -z "$DEVICE" ]; then '
            f"echo 'ELASTIC_VOLUME:NO_DEVICE'; exit 1; "
            f"fi; "
            f"mkdir -p {ELASTIC_MOUNT_POINT} && "
            f"mount -o defaults,nofail,discard $DEVICE {ELASTIC_MOUNT_POINT} && "
            f"grep -q '{ELASTIC_MOUNT_POINT}' /etc/fstab || "
            f'echo "$DEVICE {ELASTIC_MOUNT_POINT} ext4 defaults,nofail,discard 0 2"'
            f" >> /etc/fstab && "
            f"echo 'ELASTIC_VOLUME:MOUNTED'; "
            f"fi"
        )
        add_op(
            state,
            server.shell,
            name="Mount Elasticsearch storage volume",
            commands=[mount_cmd],
        )

    def _add_install_elasticsearch_ops(self, state: State):
        add_op(
            state,
            server.shell,
            name="Install Elasticsearch",
            commands=[
                "dpkg -s elasticsearch >/dev/null 2>&1 && echo 'ES:SKIP' || "
                f"(wget -q {ES_DEB_URL} -O /tmp/elasticsearch.deb && "
                "dpkg -i /tmp/elasticsearch.deb && "
                "rm -f /tmp/elasticsearch.deb && "
                "echo 'ES:INSTALLED')",
            ],
        )

    def _add_configure_elasticsearch_ops(self, state: State):
        es_yml = self._build_elasticsearch_yml()

        add_op(
            state,
            server.shell,
            name="Configure Elasticsearch data and log directories",
            commands=[
                f"mkdir -p {ELASTIC_DATA_DIR}",
                f"mkdir -p {ELASTIC_LOG_DIR}",
                f"chown -R elasticsearch:elasticsearch {ELASTIC_MOUNT_POINT}",
            ],
        )

        add_op(
            state,
            server.shell,
            name="Write Elasticsearch configuration",
            commands=[
                f"cat > /etc/elasticsearch/elasticsearch.yml << 'ES_YML_EOF'\n"
                f"{es_yml}ES_YML_EOF",
            ],
        )

        add_op(
            state,
            server.shell,
            name="Write JVM options override (dynamic heap sizing)",
            commands=[self._build_heap_options_command()],
        )

    def _add_start_elasticsearch_ops(self, state: State):
        add_op(
            state,
            server.shell,
            name="Start Elasticsearch",
            commands=[
                "systemctl daemon-reload",
                "systemctl enable elasticsearch",
                "systemctl start elasticsearch",
                # Wait for ES to become ready (use root endpoint which
                # allows anonymous access via the monitoring role)
                "for i in $(seq 1 60); do "
                "curl -sf http://localhost:9200 >/dev/null 2>&1 && break; "
                "sleep 2; done",
            ],
        )

    def _add_setup_security_ops(self, state: State):
        setup_script = self._build_setup_script()
        add_op(
            state,
            server.shell,
            name="Write Elasticsearch setup script",
            commands=[
                f"cat > /tmp/es_setup.py << 'SETUP_EOF'\n{setup_script}SETUP_EOF",
            ],
        )

        add_op(
            state,
            server.shell,
            name="Run Elasticsearch setup (user, index template, API key)",
            commands=[
                "python3 /tmp/es_setup.py && rm -f /tmp/es_setup.py",
            ],
        )

    def _build_elasticsearch_yml(self) -> str:
        return (
            f"cluster.name: ant-logs-{self.testnet_name.lower()}\n"
            f"node.name: ant-elastic-1\n"
            f"path.data: {ELASTIC_DATA_DIR}\n"
            f"path.logs: {ELASTIC_LOG_DIR}\n"
            f"network.host: 0.0.0.0\n"
            f"discovery.type: single-node\n"
            f"\n"
            f"# Disable TLS for single-node ephemeral deployment\n"
            f"xpack.security.enabled: true\n"
            f"xpack.security.http.ssl.enabled: false\n"
            f"xpack.security.transport.ssl.enabled: false\n"
            f"\n"
            f"# Allow anonymous access with monitor role so Telegraf can\n"
            f"# detect the ES version via the unauthenticated root endpoint.\n"
            f"xpack.security.authc:\n"
            f"  anonymous:\n"
            f"    roles: remote_monitoring_collector\n"
            f"    authz_exception: false\n"
        )

    def _build_heap_options_command(self) -> str:
        # Heap = min(50% of RAM, 31g) with a 1g floor. 31g cap preserves the
        # JVM compressed-oops threshold.
        return (
            "mem_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo) && "
            "heap_mb=$((mem_kb / 1024 / 2)) && "
            "if [ \"$heap_mb\" -gt 31744 ]; then heap_mb=31744; fi && "
            "if [ \"$heap_mb\" -lt 1024 ]; then heap_mb=1024; fi && "
            "printf '# Auto-sized heap: 50%% of RAM, capped at 31g\\n"
            "-Xms%sm\\n-Xmx%sm\\n' \"$heap_mb\" \"$heap_mb\" "
            "> /etc/elasticsearch/jvm.options.d/heap.options"
        )

    def _build_setup_script(self) -> str:
        return (
            "import json\n"
            "import sys\n"
            "import urllib.request\n"
            "import urllib.error\n"
            "\n"
            "ES_URL = 'http://localhost:9200'\n"
            f"USERNAME = '{ES_USERNAME}'\n"
            f"PASSWORD = '{self._es_password}'\n"
            f"INDEX_PATTERN = '{INDEX_PATTERN}'\n"
            f"ILM_POLICY_NAME = '{ILM_POLICY_NAME}'\n"
            f"RETENTION_DAYS = {self.retention_days}\n"
            f"NUMBER_OF_SHARDS = {self.shards}\n"
            "\n"
            "\n"
            "def es_request(method, path, data=None):\n"
            "    url = f'{ES_URL}{path}'\n"
            "    body = json.dumps(data).encode() if data else None\n"
            "    req = urllib.request.Request(\n"
            "        url, data=body, method=method,\n"
            "        headers={'Content-Type': 'application/json'},\n"
            "    )\n"
            "    try:\n"
            "        with urllib.request.urlopen(req) as resp:\n"
            "            return json.loads(resp.read())\n"
            "    except urllib.error.HTTPError as e:\n"
            "        body = e.read().decode()\n"
            "        print(f'ES API error {e.code}: {body}', file=sys.stderr)\n"
            "        raise\n"
            "\n"
            "\n"
            "# 1. Set the built-in elastic superuser password\n"
            "print('Setting elastic superuser password...')\n"
            "try:\n"
            "    es_request('POST', '/_security/user/elastic/_password', {\n"
            "        'password': PASSWORD,\n"
            "    })\n"
            "except urllib.error.HTTPError:\n"
            "    # First-time setup: ES 8.x auto-generates a random password.\n"
            "    # The enrollment token approach won't work for scripted setup,\n"
            "    # so we use the keystore-based reset instead.\n"
            "    import subprocess\n"
            "    result = subprocess.run(\n"
            "        ['/usr/share/elasticsearch/bin/elasticsearch-reset-password',\n"
            "         '-u', 'elastic', '-b', '-i'],\n"
            "        input=f'{PASSWORD}\\n{PASSWORD}\\n',\n"
            "        capture_output=True, text=True,\n"
            "    )\n"
            "    if result.returncode != 0:\n"
            "        print(f'Password reset failed: {result.stderr}', file=sys.stderr)\n"
            "        sys.exit(1)\n"
            "    print('Password set via elasticsearch-reset-password.')\n"
            "\n"
            "\n"
            "# From here on, use basic auth\n"
            "import base64\n"
            "auth_header = 'Basic ' + base64.b64encode(\n"
            "    f'elastic:{PASSWORD}'.encode()).decode()\n"
            "\n"
            "\n"
            "def es_auth_request(method, path, data=None):\n"
            "    url = f'{ES_URL}{path}'\n"
            "    body = json.dumps(data).encode() if data else None\n"
            "    req = urllib.request.Request(\n"
            "        url, data=body, method=method,\n"
            "        headers={\n"
            "            'Content-Type': 'application/json',\n"
            "            'Authorization': auth_header,\n"
            "        },\n"
            "    )\n"
            "    try:\n"
            "        with urllib.request.urlopen(req) as resp:\n"
            "            return json.loads(resp.read())\n"
            "    except urllib.error.HTTPError as e:\n"
            "        body = e.read().decode()\n"
            "        print(f'ES API error {e.code}: {body}', file=sys.stderr)\n"
            "        raise\n"
            "\n"
            "\n"
            "# 2a. Create ILM policy for log retention\n"
            "print(f'Creating ILM policy {ILM_POLICY_NAME} ('\n"
            "      f'retention={RETENTION_DAYS}d)...')\n"
            "es_auth_request('PUT', f'/_ilm/policy/{ILM_POLICY_NAME}', {\n"
            "    'policy': {\n"
            "        'phases': {\n"
            "            'hot': {\n"
            "                'min_age': '0ms',\n"
            "                'actions': {\n"
            "                    'set_priority': {'priority': 100},\n"
            "                },\n"
            "            },\n"
            "            'delete': {\n"
            "                'min_age': f'{RETENTION_DAYS}d',\n"
            "                'actions': {'delete': {}},\n"
            "            },\n"
            "        }\n"
            "    }\n"
            "})\n"
            "print('ILM policy created.')\n"
            "\n"
            "\n"
            "# 2b. Create index template for ant node logs (attaches ILM policy)\n"
            "print('Creating index template...')\n"
            "es_auth_request('PUT', '/_index_template/ant-node-logs', {\n"
            "    'index_patterns': [INDEX_PATTERN],\n"
            "    'template': {\n"
            "        'settings': {\n"
            "            'number_of_shards': NUMBER_OF_SHARDS,\n"
            "            'number_of_replicas': 0,\n"
            "            'index.lifecycle.name': ILM_POLICY_NAME,\n"
            "        },\n"
            "        'mappings': {\n"
            "            'properties': {\n"
            "                'timestamp': {'type': 'date'},\n"
            "                'message': {'type': 'text'},\n"
            "                'level': {'type': 'keyword'},\n"
            "                'target': {'type': 'keyword'},\n"
            "                'peer_id': {'type': 'keyword'},\n"
            "                'version': {'type': 'keyword'},\n"
            "                'commit': {'type': 'keyword'},\n"
            "                'host': {'type': 'keyword'},\n"
            "                'service_number': {'type': 'keyword'},\n"
            "                'testnet_name': {'type': 'keyword'},\n"
            "                'host_role': {'type': 'keyword'},\n"
            "            }\n"
            "        }\n"
            "    }\n"
            "})\n"
            "print('Index template created.')\n"
            "\n"
            "\n"
            "# 3. Create a role with read/write access to the log index\n"
            "print('Creating log writer role...')\n"
            "es_auth_request('PUT', '/_security/role/ant_log_writer', {\n"
            "    'indices': [{\n"
            "        'names': [INDEX_PATTERN],\n"
            "        'privileges': ['create_index', 'write', 'read', 'view_index_metadata',\n"
            "                       'monitor', 'manage'],\n"
            "    }]\n"
            "})\n"
            "print('Role created.')\n"
            "\n"
            "\n"
            f"# 4. Create user '{ES_USERNAME}' with the role\n"
            "print(f'Creating user {USERNAME}...')\n"
            "es_auth_request('PUT', f'/_security/user/{USERNAME}', {\n"
            "    'password': PASSWORD,\n"
            "    'roles': ['ant_log_writer'],\n"
            "    'full_name': 'Ant Node Log Writer',\n"
            "})\n"
            "print(f'User {USERNAME} created.')\n"
            "\n"
            "\n"
            "# 5. Create an API key for read-only querying\n"
            "print('Creating read-only API key...')\n"
            "api_key_resp = es_auth_request('POST', '/_security/api_key', {\n"
            "    'name': f'ant-log-reader-{USERNAME}',\n"
            "    'role_descriptors': {\n"
            "        'ant_log_reader': {\n"
            "            'indices': [{\n"
            "                'names': [INDEX_PATTERN],\n"
            "                'privileges': ['read', 'view_index_metadata'],\n"
            "            }]\n"
            "        }\n"
            "    }\n"
            "})\n"
            "api_key_encoded = api_key_resp['encoded']\n"
            "print(f'API key created.')\n"
            "\n"
            "\n"
            "# 6. Write credentials to files for retrieval\n"
            "import pathlib\n"
            "creds_dir = pathlib.Path('/etc/elasticsearch/ant-credentials')\n"
            "creds_dir.mkdir(exist_ok=True)\n"
            "(creds_dir / 'username').write_text(USERNAME)\n"
            "(creds_dir / 'password').write_text(PASSWORD)\n"
            "(creds_dir / 'api-key').write_text(api_key_encoded)\n"
            "print('Credentials written to /etc/elasticsearch/ant-credentials/')\n"
            "print('SETUP_COMPLETE')\n"
        )

    def _retrieve_connection_details(self) -> dict:
        """SSH back in to read the generated credentials."""
        def _ssh_read(path: str) -> str:
            result = subprocess.run(
                [
                    "ssh",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    "-i", self.ssh_key_path,
                    f"root@{self.host_ip}",
                    f"cat {path}",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to read {path}: {result.stderr}"
                )
            return result.stdout.strip()

        username = _ssh_read("/etc/elasticsearch/ant-credentials/username")
        password = _ssh_read("/etc/elasticsearch/ant-credentials/password")
        api_key = _ssh_read("/etc/elasticsearch/ant-credentials/api-key")

        testnet_lower = self.testnet_name.lower()
        return {
            "elasticsearch_url": f"http://{self.host_ip}:9200",
            "elasticsearch_username": username,
            "elasticsearch_password": password,
            "elasticsearch_api_key": api_key,
            "elasticsearch_index_pattern": (
                f"telegraf-ant-nodes-{testnet_lower}-%Y.%m.%d"
            ),
        }
