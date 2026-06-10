"""Tests for the client/node log-forwarding + rust_log feature.

Covers the new ``forward_logs`` / ``rust_log`` options end to end at the unit
level: spec-token parsing, manifest validation, orchestrator emission, the node
systemd RUST_LOG env line, the client telegraf forwarder config, the ES client
index template, and the slack-post markers.
"""

import pytest

from saorsa_deploy.cmd.infra_clients import (
    parse_downloader_spec,
    parse_uploader_spec,
)
from saorsa_deploy.cmd.prod_uploaders import parse_prod_uploader_spec
from saorsa_deploy.config.models import (
    Binary,
    Clients,
    Downloader,
    Elastic,
    Manifest,
    Nodes,
    ProdCanarySpec,
    ProdProbe,
    Uploader,
)

# Aliased so pytest doesn't try to collect these `Test*`-named model classes.
from saorsa_deploy.config.models import TestnetBinaries as _TestnetBinaries
from saorsa_deploy.config.models import TestnetSpec as _TestnetSpec

RUST_LOG_WITH_COLONS = "ant_cli=info,saorsa_core::dht_network_manager=debug"


# --------------------------------------------------------------------------- #
# testnet client spec-token parsing (rust_log as trailing remainder)
# --------------------------------------------------------------------------- #
class TestClientSpecParsing:
    def test_uploader_forward_logs_and_rust_log(self):
        spec = parse_uploader_spec(
            f"vultr:syd:1000:vc2-2c-4gb:payment=merkle:bootstrap_cache=true"
            f":forward_logs=true:rust_log={RUST_LOG_WITH_COLONS}"
        )
        assert spec["forward_logs"] is True
        assert spec["rust_log"] == RUST_LOG_WITH_COLONS
        # the remainder split must not corrupt the earlier tokens
        assert spec["payment_type"] == "merkle"
        assert spec["bootstrap_cache"] is True
        assert spec["vm_size"] == "vc2-2c-4gb"

    def test_downloader_forward_logs_and_rust_log(self):
        spec = parse_downloader_spec(
            f"digital_ocean:tor1:forward_logs=false:rust_log={RUST_LOG_WITH_COLONS}"
        )
        assert spec["forward_logs"] is False
        assert spec["rust_log"] == RUST_LOG_WITH_COLONS

    def test_defaults_when_absent(self):
        up = parse_uploader_spec("vultr:syd:1000")
        assert up["forward_logs"] is None
        assert up["rust_log"] is None
        dn = parse_downloader_spec("digital_ocean:tor1")
        assert dn["forward_logs"] is None
        assert dn["rust_log"] is None

    def test_empty_rust_log_rejected(self):
        with pytest.raises(ValueError, match="rust_log cannot be empty"):
            parse_uploader_spec("vultr:syd:1000:rust_log=")

    def test_forward_logs_invalid_value_rejected(self):
        with pytest.raises(ValueError, match="forward_logs"):
            parse_downloader_spec("digital_ocean:tor1:forward_logs=maybe")


class TestProdUploaderSpecParsing:
    def test_forward_logs_and_rust_log(self, tmp_path):
        urls = tmp_path / "u.txt"
        urls.write_text("https://example.com/a.bin\n")
        spec = parse_prod_uploader_spec(
            f"lon1:s-2vcpu-4gb:urls={urls}:forward_logs=true"
            f":rust_log={RUST_LOG_WITH_COLONS}"
        )
        assert spec["forward_logs"] is True
        assert spec["rust_log"] == RUST_LOG_WITH_COLONS


# --------------------------------------------------------------------------- #
# manifest validation
# --------------------------------------------------------------------------- #
def _testnet(elastic_enabled: bool, forward_logs: bool):
    return _TestnetSpec(
        binaries=_TestnetBinaries(
            ant_node=Binary(version="0.1"), ant=Binary(version="0.1")
        ),
        nodes=Nodes(per_vm=1, providers={"digital_ocean": {"vm_count": 1}}),
        clients=Clients(
            uploaders=[
                Uploader(
                    provider="vultr",
                    region="syd",
                    file_size_mb=1,
                    forward_logs=forward_logs,
                )
            ],
            downloaders=[Downloader(provider="digital_ocean", region="tor1")],
        ),
        elastic=Elastic(enabled=elastic_enabled),
    )


class TestForwardLogsValidation:
    def test_forward_logs_without_elastic_rejected(self):
        with pytest.raises(ValueError, match="forward_logs requires elastic"):
            _testnet(elastic_enabled=False, forward_logs=True)

    def test_forward_logs_with_elastic_ok(self):
        spec = _testnet(elastic_enabled=True, forward_logs=True)
        assert spec.elastic.enabled is True

    def test_no_forward_logs_no_elastic_ok(self):
        spec = _testnet(elastic_enabled=False, forward_logs=False)
        assert spec.elastic.enabled is False


# --------------------------------------------------------------------------- #
# orchestrator emission
# --------------------------------------------------------------------------- #
class TestOrchestratorEmission:
    def test_testnet_emits_client_and_node_options(self):
        from saorsa_deploy.orchestrator.testnet import build_testnet_deploy_steps

        nodes = Nodes(
            per_vm=15,
            log_level="debug",
            providers={"digital_ocean": {"vm_count": 1}},
        )
        u = Uploader(
            provider="vultr",
            region="syd",
            file_size_mb=1000,
            forward_logs=True,
            rust_log=RUST_LOG_WITH_COLONS,
        )
        d = Downloader(provider="digital_ocean", region="tor1", forward_logs=True)
        m = Manifest(
            name="DEV-01",
            kind="testnet",
            purpose="t",
            testnet=_TestnetSpec(
                binaries=_TestnetBinaries(
                    ant_node=Binary(version="0.1"), ant=Binary(version="0.1")
                ),
                nodes=nodes,
                clients=Clients(uploaders=[u], downloaders=[d]),
                elastic=Elastic(enabled=True),
            ),
        )
        argvs = [" ".join(s.argv) for s in build_testnet_deploy_steps(m)]
        clients_infra = next(a for a in argvs if "clients infra" in a)
        assert f"rust_log={RUST_LOG_WITH_COLONS}" in clients_infra
        assert "forward_logs=true" in clients_infra
        # node log_level -> --log-level on genesis + nodes provision (NOT rust-log)
        node_steps = [
            a for a in argvs if "nodes provision" in a or "provision-genesis" in a
        ]
        assert node_steps
        for step in node_steps:
            assert "--log-level debug" in step
            assert "--rust-log" not in step

    def test_prod_canary_emits_flags(self):
        from saorsa_deploy.orchestrator.prod import (
            build_deploy_steps,
            build_destroy_steps,
        )

        m = Manifest(
            name="CAN-01",
            kind="prod-canary",
            purpose="t",
            prod_canary=ProdCanarySpec(
                binary=Binary(version="0.1"),
                upload_spec="lon1",
                probes=[ProdProbe(region="lon1", count=2)],
                urls_file="/tmp/u.txt",
                forward_logs=True,
                rust_log=RUST_LOG_WITH_COLONS,
                keep_elastic=True,
            ),
        )
        deploy = " ".join(build_deploy_steps(m)[-1].argv)
        assert "--forward-logs" in deploy
        assert f"--rust-log {RUST_LOG_WITH_COLONS}" in deploy
        destroy = " ".join(build_destroy_steps(m)[0].argv)
        assert "--keep-elastic" in destroy


# --------------------------------------------------------------------------- #
# node systemd units must NOT set RUST_LOG (ant-node binds --log-level to it and
# rejects a multi-target value); node level rides ant-node --log-level instead.
# --------------------------------------------------------------------------- #
class TestNodeUnitsNoRustLog:
    def test_node_genesis_bootstrap_units_have_no_environment(self):
        from saorsa_deploy.provisioning.bootstrap import (
            _build_unit_file as boot_unit,
        )
        from saorsa_deploy.provisioning.genesis import (
            _build_unit_file as gen_unit,
        )
        from saorsa_deploy.provisioning.node import _build_node_unit_file

        for unit in (
            _build_node_unit_file("ant-node-1", "EXE"),
            gen_unit("EXE"),
            boot_unit("ant-bootstrap-2", "EXE"),
        ):
            assert "Environment=" not in unit
            assert "RUST_LOG" not in unit

    def test_node_exec_start_carries_log_level(self):
        # The single node log level is a --log-level flag on the ant-node
        # command line (built by _build_node_exec_start), not a RUST_LOG env.
        from saorsa_deploy.provisioning.node import _build_node_exec_start

        exec_start = _build_node_exec_start(
            bootstrap_peers=["1.2.3.4:10000"], root_dir="/d", log_level="debug"
        )
        assert "--enable-logging" in exec_start
        assert "--log-level debug" in exec_start


# --------------------------------------------------------------------------- #
# client telegraf forwarder + ES client template
# --------------------------------------------------------------------------- #
class TestClientForwarder:
    def test_config_renders_client_index_and_globs(self):
        from saorsa_deploy.provisioning.telegraf_client import (
            ClientTelegrafProvisioner,
        )

        p = ClientTelegrafProvisioner(
            host_ips=["1.2.3.4"],
            ssh_key_path="/x",
            testnet_name="DEV-01",
            elasticsearch_url="http://es:9200",
            elasticsearch_username="u",
            elasticsearch_password="pw",
            role="uploader",
            tail_globs=["/var/log/ant-client-forward/uploader.stdout.log"],
        )
        cfg = p._build_telegraf_config()
        for placeholder in (
            "__FILES__",
            "__FROM_BEGINNING__",
            "__METRIC_BUFFER_LIMIT__",
            "__MAX_UNDELIVERED_LINES__",
        ):
            assert placeholder not in cfg
        assert "telegraf-ant-clients-${ANT_TESTNET_NAME_LOWER}" in cfg
        assert "/var/log/ant-client-forward/uploader.stdout.log" in cfg
        # raw line capture + stream derivation
        assert 'data_format = "value"' in cfg
        assert 'dest = "message"' in cfg

    def test_empty_globs_rejected(self):
        from saorsa_deploy.provisioning.telegraf_client import (
            ClientTelegrafProvisioner,
        )

        with pytest.raises(ValueError, match="tail_globs"):
            ClientTelegrafProvisioner(
                host_ips=["1.2.3.4"],
                ssh_key_path="/x",
                testnet_name="DEV-01",
                elasticsearch_url="http://es:9200",
                elasticsearch_username="u",
                elasticsearch_password="pw",
                role="uploader",
                tail_globs=[],
            )


class TestElasticClientTemplate:
    def test_setup_script_has_client_template(self):
        from saorsa_deploy.provisioning.elastic import ElasticProvisioner

        p = ElasticProvisioner(
            host_ip="1.2.3.4", ssh_key_path="/x", testnet_name="DEV-01"
        )
        script = p._build_setup_script()
        compile(script, "es_setup.py", "exec")  # valid python
        assert "ant-client-logs" in script
        assert "telegraf-ant-clients-*" in script


# --------------------------------------------------------------------------- #
# slack-post markers
# --------------------------------------------------------------------------- #
class TestSlackMarkers:
    def test_forward_logs_and_rust_log_markers(self):
        from saorsa_deploy.cmd.slack_post import (
            _forward_logs_marker,
            _rust_log_marker,
        )

        assert "forward-logs" in _forward_logs_marker(True)
        assert _forward_logs_marker(False) == ""
        assert _forward_logs_marker(None) == ""
        assert "RUST_LOG=" in _rust_log_marker("debug")
        assert _rust_log_marker(None) == ""

    def test_clients_block_renders_markers(self):
        from saorsa_deploy.cmd.slack_post import _clients_block

        block = _clients_block(
            [
                {
                    "role": "upload",
                    "region": "syd",
                    "vm_size": "vc2-2c-4gb",
                    "forward_logs": True,
                    "rust_log": "debug",
                }
            ]
        )
        text = block["text"]["text"]
        assert "forward-logs" in text
        assert "RUST_LOG=`debug`" in text

    def test_testnet_block_renders_node_log_level(self):
        from saorsa_deploy.cmd.slack_post import _testnet_blocks

        blocks = _testnet_blocks({"node_log_level": "debug"}, {}, None)
        joined = "\n".join(
            b.get("text", {}).get("text", "") for b in blocks if "text" in b
        )
        assert "Node log level" in joined
        assert "debug" in joined
