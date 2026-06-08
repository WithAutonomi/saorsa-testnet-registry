import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from saorsa_db.db import Base
from saorsa_db.models import DeploymentStatus
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from saorsa_deploy.cmd.slack_post import (
    build_slack_blocks,
    cmd_slack_post,
    render_payload_text,
)


@pytest.fixture(autouse=True)
def _no_real_slack(monkeypatch):
    """Never let a test post to the real Slack workspace.

    ``cmd_slack_post`` checks ``ANT_DEPLOY_SLACK_BOT_TOKEN`` before the webhook,
    so a developer running pytest with those vars exported (e.g. from .env)
    could otherwise fire a real ``chat.postMessage``. Clear all three for every
    test in this module.
    """
    for var in (
        "ANT_DEPLOY_SLACK_BOT_TOKEN",
        "ANT_DEPLOY_SLACK_CHANNEL",
        "ANT_DEPLOY_SLACK_WEBHOOK_URL",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session)
    session.close()
    engine.dispose()


def _deployment(name="DEV-01", deploy_args=None, status=DeploymentStatus.RUNNING):
    return SimpleNamespace(
        name=name,
        status=status,
        deploy_args=deploy_args or {},
        created_at=datetime(2026, 4, 29, 14, 0, tzinfo=timezone.utc),
    )


def _all_text(payload: dict) -> str:
    """Flatten every Block-Kit text fragment into one searchable string."""
    out = [payload.get("text", "")]
    for block in payload.get("blocks", []):
        if block.get("type") == "header":
            out.append(block["text"]["text"])
        elif block.get("type") == "section":
            text = block.get("text")
            if text:
                out.append(text["text"])
            for f in block.get("fields") or []:
                out.append(f["text"])
    return "\n".join(out)


def test_testnet_with_clients_renders_full_summary():
    deployment = _deployment(
        deploy_args={
            "bootstrap_count": 3,
            "purpose": "Test churn handling under load",
            "vm_count": 6,
            "nodes_per_vm": 25,
            "nat_percent": 33,
            "node_binary": {"kind": "release", "version": "0.4.1"},
            "node_specs": [
                {"provider": "digitalocean", "vm_count": 2, "region_count": 3},
            ],
        },
    )
    state = {
        "regions": [["digitalocean", "lon1"], ["digitalocean", "fra1"], ["digitalocean", "ams3"]],
        "nat_vm_ips": ["1.1.1.1", "2.2.2.2"],
    }
    client_state = {
        "clients": [
            {"role": "upload", "region": "lon1", "vm_size": "s-2vcpu-4gb", "file_size_mb": 50},
            {"role": "download", "region": "fra1", "vm_size": "s-2vcpu-4gb"},
        ]
    }

    payload = build_slack_blocks(deployment, state, client_state)

    text = _all_text(payload)
    assert "DEV-01" in text
    assert "Testnet" in text
    assert "Test churn handling under load" in text
    # 6 VMs × 25/VM = 150 nodes total
    assert "150" in text
    assert "6 VMs × 25/VM" in text
    # 2 NAT VMs × 25/VM = 50 NAT nodes
    assert "50" in text
    assert "33%" in text
    assert "digitalocean" in text
    assert "lon1, fra1, ams3" in text
    assert "Node binary:" in text
    assert "release v0.4.1" in text
    assert "upload — lon1" in text
    assert "50MB" in text
    assert "download — fra1" in text


def test_testnet_renders_ant_binary_when_recorded():
    deployment = _deployment(
        deploy_args={
            "bootstrap_count": 1,
            "vm_count": 1,
            "nodes_per_vm": 5,
            "purpose": "p",
            "node_binary": {"kind": "release", "version": "0.11.1-rc.2"},
            "ant_binary": {"kind": "release", "version": "0.5.0"},
        },
    )
    text = _all_text(build_slack_blocks(deployment, {}, None))
    assert "Node binary:" in text
    assert "release v0.11.1-rc.2" in text
    assert "Client binary:" in text
    assert "release v0.5.0" in text


def test_testnet_renders_ant_binary_unknown_when_missing():
    deployment = _deployment(
        deploy_args={
            "bootstrap_count": 1,
            "vm_count": 1,
            "nodes_per_vm": 5,
            "purpose": "p",
        },
    )
    text = _all_text(build_slack_blocks(deployment, {}, None))
    assert "Client binary:" in text


def test_testnet_clients_render_per_uploader_payment():
    deployment = _deployment(
        deploy_args={"bootstrap_count": 1, "vm_count": 1, "nodes_per_vm": 5, "purpose": "p"},
    )
    client_state = {
        "clients": [
            {
                "role": "upload", "region": "lon1", "vm_size": "s-2vcpu-4gb",
                "file_size_mb": 50, "payment_type": "merkle",
            },
            {
                "role": "upload", "region": "nyc1", "vm_size": "s-4vcpu-8gb",
                "file_size_mb": 100, "payment_type": "no-merkle",
            },
            {
                "role": "upload", "region": "fra1", "vm_size": "s-2vcpu-4gb",
                "file_size_mb": 50,
            },
            {"role": "download", "region": "ams3", "vm_size": "s-2vcpu-4gb"},
        ]
    }
    text = _all_text(build_slack_blocks(deployment, {}, client_state))
    assert "upload — lon1 (s-2vcpu-4gb, 50MB files) — payment: merkle" in text
    assert "upload — nyc1 (s-4vcpu-8gb, 100MB files) — payment: no-merkle" in text
    assert "upload — fra1 (s-2vcpu-4gb, 50MB files) — payment: default" in text
    # Downloaders have no payment field.
    assert "download — ams3" in text
    assert "download — ams3 (s-2vcpu-4gb) — payment" not in text


def test_prod_upload_client_omits_zero_file_size():
    """For prod uploads file_size_mb is 0 (real arbitrary-size files); don't render it."""
    deployment = _deployment(
        name="PROD-UP-Z",
        deploy_args={
            "kind": "prod-uploaders",
            "purpose": "p",
            "ant_binary": {"kind": "release", "version": "0.2.1"},
            "uploaders": [],
        },
    )
    client_state = {
        "clients": [
            {"role": "upload", "region": "lon1", "vm_size": "s-2vcpu-4gb", "file_size_mb": 0},
        ]
    }
    text = _all_text(build_slack_blocks(deployment, None, client_state))
    assert "0MB" not in text
    assert "upload — lon1 (s-2vcpu-4gb)" in text


def test_payload_omits_status_and_uses_deployed_label():
    deployment = _deployment(
        deploy_args={"bootstrap_count": 1, "vm_count": 1, "nodes_per_vm": 5, "purpose": "p"},
    )
    payload = build_slack_blocks(deployment, {}, None)
    text = _all_text(payload)
    assert "Deployed:" in text
    assert "Status" not in text
    assert "Created:" not in text


def test_testnet_without_purpose_renders_none_recorded():
    deployment = _deployment(
        deploy_args={"bootstrap_count": 1, "vm_count": 1, "nodes_per_vm": 5},
    )
    payload = build_slack_blocks(deployment, {}, None)
    text = _all_text(payload)
    assert "none recorded" in text


def test_testnet_without_clients_omits_clients_block():
    deployment = _deployment(
        deploy_args={"bootstrap_count": 1, "vm_count": 1, "nodes_per_vm": 5},
    )
    payload = build_slack_blocks(deployment, {}, None)
    text = _all_text(payload)
    assert "Clients" not in text


def test_testnet_missing_nodes_per_vm_renders_unknown():
    """Pre-fix legacy deployments lack nodes_per_vm and should render gracefully."""
    deployment = _deployment(deploy_args={"bootstrap_count": 1, "vm_count": 4})
    payload = build_slack_blocks(deployment, {}, None)
    text = _all_text(payload)
    assert "unknown" in text.lower()
    assert "4 VMs" in text


def test_prod_uploaders_renders_ant_binary_release_version():
    deployment = _deployment(
        name="PROD-UP-V",
        deploy_args={
            "kind": "prod-uploaders",
            "purpose": "p",
            "ant_binary": {"kind": "release", "version": "0.3.0"},
            "uploaders": [],
        },
    )
    text = _all_text(build_slack_blocks(deployment, None, None))
    assert "Client binary:" in text
    assert "release v0.3.0" in text


def test_prod_uploaders_renders_ant_binary_branch():
    deployment = _deployment(
        name="PROD-UP-B",
        deploy_args={
            "kind": "prod-uploaders",
            "purpose": "p",
            "ant_binary": {
                "kind": "branch",
                "branch": "feature-x",
                "repo_owner": "myorg",
            },
            "uploaders": [],
        },
    )
    text = _all_text(build_slack_blocks(deployment, None, None))
    assert "branch `myorg/feature-x`" in text


def test_prod_uploaders_renders_ant_binary_latest():
    deployment = _deployment(
        name="PROD-UP-L",
        deploy_args={
            "kind": "prod-uploaders",
            "purpose": "p",
            "ant_binary": {"kind": "latest"},
            "uploaders": [],
        },
    )
    text = _all_text(build_slack_blocks(deployment, None, None))
    assert "latest release" in text


def test_prod_uploaders_renders_uploader_specs():
    deployment = _deployment(
        name="PROD-UP-01",
        deploy_args={
            "kind": "prod-uploaders",
            "purpose": "Smoke-test mainnet uploads",
            "ant_binary": {"kind": "release", "version": "0.3.0"},
            "uploaders": [
                {
                    "region": "lon1",
                    "vm_size": "s-2vcpu-4gb",
                    "urls_file": "/tmp/urls.txt",
                    "file_names": ["pic1.jpg", "pic2.jpg"],
                },
                {
                    "region": "fra1",
                    "vm_size": "s-4vcpu-8gb",
                    "urls_file": "/tmp/urls2.txt",
                    "file_names": ["movie.mp4"],
                },
            ],
        },
    )
    payload = build_slack_blocks(deployment, None, None)
    text = _all_text(payload)
    assert "PROD-UP-01" in text
    assert "Prod Uploaders" in text
    assert "lon1 (s-2vcpu-4gb)" in text
    assert "pic1.jpg" in text
    assert "pic2.jpg" in text
    assert "movie.mp4" in text
    # Local URL file paths must not leak into the post.
    assert "/tmp/urls.txt" not in text
    assert "/tmp/urls2.txt" not in text
    # Testnet-only fields must not appear.
    assert "Total nodes" not in text
    assert "NAT-simulated" not in text


def test_prod_uploaders_renders_payment_default_per_uploader():
    deployment = _deployment(
        name="PROD-UP-PT-D",
        deploy_args={
            "kind": "prod-uploaders",
            "purpose": "p",
            "ant_binary": {"kind": "release", "version": "0.3.0"},
            "uploaders": [
                {"region": "lon1", "vm_size": "s-2vcpu-4gb", "file_names": []},
            ],
        },
    )
    text = _all_text(build_slack_blocks(deployment, None, None))
    assert "lon1 (s-2vcpu-4gb)" in text
    assert "payment: default" in text
    # Top-level "Payment type:" line is removed.
    assert "Payment type:" not in text


def test_prod_uploaders_renders_per_uploader_payment_modes():
    deployment = _deployment(
        name="PROD-UP-PT-MIX",
        deploy_args={
            "kind": "prod-uploaders",
            "purpose": "p",
            "ant_binary": {"kind": "release", "version": "0.3.0"},
            "uploaders": [
                {
                    "region": "lon1", "vm_size": "s-2vcpu-4gb",
                    "file_names": [], "payment_type": "merkle",
                },
                {
                    "region": "nyc1", "vm_size": "s-4vcpu-8gb",
                    "file_names": [], "payment_type": "no-merkle",
                },
                {
                    "region": "fra1", "vm_size": "s-2vcpu-4gb",
                    "file_names": [],
                },
            ],
        },
    )
    text = _all_text(build_slack_blocks(deployment, None, None))
    assert "lon1 (s-2vcpu-4gb) — payment: merkle" in text
    assert "nyc1 (s-4vcpu-8gb) — payment: no-merkle" in text
    assert "fra1 (s-2vcpu-4gb) — payment: default" in text


def test_prod_continuous_downloader_renders_spec_and_addresses():
    deployment = _deployment(
        name="PROD-CD-01",
        deploy_args={
            "kind": "prod-continuous-downloader",
            "spec": "lon1:s-2vcpu-4gb",
            "addresses_file": "/path/to/addrs.txt",
            "ant_binary": {"kind": "release", "version": "0.3.0"},
        },
    )
    payload = build_slack_blocks(deployment, None, None)
    text = _all_text(payload)
    assert "PROD-CD-01" in text
    assert "Prod Continuous Downloader" in text
    assert "lon1:s-2vcpu-4gb" in text
    assert "/path/to/addrs.txt" in text
    assert "Client binary:" in text
    assert "release v0.3.0" in text


def test_prod_continuous_downloader_renders_addresses_inline_when_persisted():
    deployment = _deployment(
        name="PROD-CD-02",
        deploy_args={
            "kind": "prod-continuous-downloader",
            "spec": "lon1",
            "addresses_file": "/path/to/addrs.txt",
            "addresses": ["abc123", "def456", "ghi789"],
            "sleep_interval_secs": 900,
            "ant_binary": {"kind": "release", "version": "0.3.0"},
        },
    )
    payload = build_slack_blocks(deployment, None, None)
    text = _all_text(payload)
    # Inline addresses, not the file path
    assert "abc123" in text
    assert "def456" in text
    assert "ghi789" in text
    assert "Addresses (3)" in text
    assert "/path/to/addrs.txt" not in text
    assert "Sleep interval" in text


def test_prod_downloaders_kind_renders_without_sleep_interval():
    deployment = _deployment(
        name="PROD-DL-OS-01",
        deploy_args={
            "kind": "prod-downloaders",
            "spec": "lon1",
            "addresses": ["addrA", "addrB"],
            "ant_binary": {"kind": "release", "version": "0.3.0"},
        },
    )
    payload = build_slack_blocks(deployment, None, None)
    text = _all_text(payload)
    assert "Prod Downloaders" in text
    assert "addrA" in text
    assert "addrB" in text
    # No sleep_interval_secs in deploy_args ⇒ no Sleep interval row
    assert "Sleep interval" not in text


def test_prod_local_uploads_chunks_long_file_lists_under_section_cap():
    """Slack rejects sections longer than 3000 chars with `invalid_blocks`.

    A real run with ~200 files easily exceeds that, so the file list must
    be split across multiple section blocks. Verify (a) every section
    stays under the cap, (b) every file still appears somewhere, and
    (c) the chunked headers are paginated.
    """
    files = [
        {"name": f"clip{i:03d}.avi", "size_bytes": 10_000_000 + i}
        for i in range(184)
    ]
    deployment = _deployment(
        name="PROD-LOCAL-UL-99",
        deploy_args={
            "kind": "prod-local-uploads",
            "input_path": "/tmp/something",
            "input_mode": "local-dir",
            "file_count": len(files),
            "files": files,
            "ant_binary": {"kind": "release", "version": "0.2.2"},
        },
    )

    payload = build_slack_blocks(deployment, None, None)

    file_blocks = [
        b for b in payload["blocks"]
        if b.get("type") == "section"
        and b.get("text")
        and "*Files" in b["text"]["text"]
    ]
    assert len(file_blocks) >= 2, (
        "Expected the long file list to be split into multiple section blocks"
    )
    for block in file_blocks:
        assert len(block["text"]["text"]) < 3000, (
            "Section text must stay under Slack's 3000-char cap"
        )

    rendered = "\n".join(b["text"]["text"] for b in file_blocks)
    for f in files:
        assert f["name"] in rendered, f"missing file {f['name']!r}"

    assert f"(1/{len(file_blocks)})" in file_blocks[0]["text"]["text"]
    assert (
        f"({len(file_blocks)}/{len(file_blocks)})"
        in file_blocks[-1]["text"]["text"]
    )


def test_prod_local_uploads_short_file_list_uses_single_unpaginated_section():
    """When everything fits in one block we don't want a (1/1) suffix."""
    files = [
        {"name": f"f{i}.bin", "size_bytes": 1_000_000} for i in range(3)
    ]
    deployment = _deployment(
        name="PROD-LOCAL-UL-100",
        deploy_args={
            "kind": "prod-local-uploads",
            "input_path": "/tmp/something",
            "input_mode": "local-dir",
            "file_count": len(files),
            "files": files,
            "ant_binary": {"kind": "release", "version": "0.2.2"},
        },
    )
    payload = build_slack_blocks(deployment, None, None)
    file_blocks = [
        b for b in payload["blocks"]
        if b.get("type") == "section"
        and b.get("text")
        and "*Files" in b["text"]["text"]
    ]
    assert len(file_blocks) == 1
    assert "(1/" not in file_blocks[0]["text"]["text"]


def test_dry_run_prints_payload_and_does_not_post(repo, capsys):
    repo.create(
        name="DEV-DRY",
        deploy_args={"bootstrap_count": 1, "vm_count": 1, "nodes_per_vm": 5},
    )
    args = SimpleNamespace(name="DEV-DRY", dry_run=True)

    with patch("saorsa_deploy.cmd.slack_post.get_repository", return_value=repo):
        with patch("saorsa_deploy.cmd.slack_post.requests.post") as mock_post:
            with patch(
                "saorsa_deploy.cmd.slack_post.load_deployment_state",
                side_effect=RuntimeError,
            ):
                with patch(
                    "saorsa_deploy.cmd.slack_post.load_client_state",
                    return_value={},
                ):
                    cmd_slack_post(args)

    mock_post.assert_not_called()
    out = capsys.readouterr().out
    # Dry-run prints a human-readable preview, not JSON. Just assert the
    # deployment name appears in the rendered text.
    assert "DEV-DRY" in out
    assert "{" not in out


def test_missing_webhook_url_errors_when_not_dry_run(repo, monkeypatch):
    repo.create(
        name="DEV-NOWH",
        deploy_args={"bootstrap_count": 1, "vm_count": 1, "nodes_per_vm": 5},
    )
    monkeypatch.delenv("ANT_DEPLOY_SLACK_WEBHOOK_URL", raising=False)
    args = SimpleNamespace(name="DEV-NOWH", dry_run=False)

    with patch("saorsa_deploy.cmd.slack_post.get_repository", return_value=repo):
        with patch(
            "saorsa_deploy.cmd.slack_post.load_deployment_state",
            side_effect=RuntimeError,
        ):
            with patch(
                "saorsa_deploy.cmd.slack_post.load_client_state",
                return_value={},
            ):
                with pytest.raises(SystemExit) as exc:
                    cmd_slack_post(args)

    assert exc.value.code == 1


def test_post_includes_recorded_purpose_from_deploy_args(repo, monkeypatch):
    repo.create(
        name="DEV-WITH-PURPOSE",
        deploy_args={
            "bootstrap_count": 1,
            "vm_count": 1,
            "nodes_per_vm": 5,
            "purpose": "Recorded at deploy time",
        },
    )
    monkeypatch.setenv("ANT_DEPLOY_SLACK_WEBHOOK_URL", "https://hooks.slack.example/T/B/X")
    args = SimpleNamespace(name="DEV-WITH-PURPOSE", dry_run=False)

    response = MagicMock(ok=True, status_code=200)
    with patch("saorsa_deploy.cmd.slack_post.get_repository", return_value=repo):
        with patch(
            "saorsa_deploy.cmd.slack_post.load_deployment_state",
            side_effect=RuntimeError,
        ):
            with patch(
                "saorsa_deploy.cmd.slack_post.load_client_state",
                return_value={},
            ):
                with patch(
                    "saorsa_deploy.cmd.slack_post.requests.post",
                    return_value=response,
                ) as mock_post:
                    cmd_slack_post(args)

    mock_post.assert_called_once()
    posted_payload = mock_post.call_args.kwargs["json"]
    assert "Recorded at deploy time" in json.dumps(posted_payload)

def test_render_payload_text_includes_header_and_section_text():
    payload = {
        "text": "fallback",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "DEV-01 — Testnet"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Deployed:* 2026-04-30 18:24 UTC",
                },
            },
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": "*Region:*\nlon1"},
                {"type": "mrkdwn", "text": "*VMs:*\n3"},
            ]},
        ],
    }
    text = render_payload_text(payload)

    assert "=== DEV-01 — Testnet ===" in text
    assert "*Deployed:* 2026-04-30 18:24 UTC" in text
    assert "*Region:*\nlon1" in text
    assert "*VMs:*\n3" in text
    # The wire-format JSON keys must not leak into the preview.
    assert "mrkdwn" not in text
    assert "plain_text" not in text


def test_render_payload_text_handles_empty_blocks():
    assert render_payload_text({"text": "x", "blocks": []}) == "\n"


def _testnet_deployment():
    return _deployment(
        deploy_args={
            "bootstrap_count": 1,
            "vm_count": 1,
            "nodes_per_vm": 5,
            "purpose": "p",
            "node_binary": {"kind": "release", "version": "0.1.0"},
        },
    )


def test_monitoring_block_included_when_state_present():
    monitoring_state = {
        "influxdb_url": "http://1.2.3.4:8086",
        "influxdb_token": "tok-abc",
        "influxdb_org": "org-x",
        "influxdb_bucket": "bucket-y",
        "grafana_url": "http://1.2.3.4:3000",
        "grafana_password": "pw-z",
    }
    payload = build_slack_blocks(
        _testnet_deployment(), {}, None, monitoring_state=monitoring_state
    )
    text = _all_text(payload)
    assert "Monitoring (InfluxDB + Grafana)" in text
    assert "http://1.2.3.4:8086" in text
    assert "http://1.2.3.4:3000" in text
    assert "admin / pw-z" in text
    assert "org-x" in text
    assert "bucket-y" in text
    assert "tok-abc" in text


def test_monitoring_block_skipped_when_url_missing():
    payload = build_slack_blocks(
        _testnet_deployment(),
        {},
        None,
        monitoring_state={"influxdb_url": "", "influxdb_token": "t"},
    )
    assert "Monitoring (InfluxDB" not in _all_text(payload)


def test_elastic_block_included_when_state_present():
    elastic_state = {
        "elasticsearch_url": "https://es.example:9200",
        "elasticsearch_username": "ant-log-writer",
        "elasticsearch_password": "es-pass",
        "elasticsearch_api_key": "api-key-123",
        "elasticsearch_index_pattern": "telegraf-ant-nodes-dev-01-%Y.%m.%d",
    }
    payload = build_slack_blocks(
        _testnet_deployment(), {}, None, elastic_state=elastic_state
    )
    text = _all_text(payload)
    assert "Elasticsearch" in text
    assert "https://es.example:9200" in text
    assert "ant-log-writer" in text
    assert "es-pass" in text
    assert "api-key-123" in text
    assert "telegraf-ant-nodes-dev-01" in text


def test_elastic_block_skipped_when_url_missing():
    payload = build_slack_blocks(
        _testnet_deployment(),
        {},
        None,
        elastic_state={"elasticsearch_url": "", "elasticsearch_username": "u"},
    )
    assert "Elasticsearch" not in _all_text(payload)


def test_both_monitoring_and_elastic_blocks_present():
    monitoring_state = {
        "influxdb_url": "http://mon.example:8086",
        "influxdb_token": "t",
        "influxdb_org": "o",
        "influxdb_bucket": "b",
        "grafana_url": "http://mon.example:3000",
        "grafana_password": "gp",
    }
    elastic_state = {
        "elasticsearch_url": "https://es.example:9200",
        "elasticsearch_username": "u",
        "elasticsearch_password": "p",
        "elasticsearch_api_key": "k",
        "elasticsearch_index_pattern": "ix",
    }
    payload = build_slack_blocks(
        _testnet_deployment(),
        {},
        None,
        monitoring_state=monitoring_state,
        elastic_state=elastic_state,
    )
    text = _all_text(payload)
    assert "Monitoring (InfluxDB + Grafana)" in text
    assert "Elasticsearch" in text
    # Monitoring section should appear before Elasticsearch.
    assert text.index("Monitoring (InfluxDB") < text.index("Elasticsearch")


def test_neither_monitoring_nor_elastic_blocks_when_states_none():
    payload = build_slack_blocks(_testnet_deployment(), {}, None)
    text = _all_text(payload)
    assert "Monitoring (InfluxDB" not in text
    assert "Elasticsearch" not in text


def test_cmd_slack_post_loads_monitoring_and_elastic_state(repo):
    repo.create(
        name="DEV-ML",
        deploy_args={
            "bootstrap_count": 1,
            "vm_count": 1,
            "nodes_per_vm": 5,
            "purpose": "p",
            "node_binary": {"kind": "release", "version": "0.1.0"},
        },
    )

    monitoring_state = {
        "influxdb_url": "http://mon:8086",
        "influxdb_token": "t",
        "influxdb_org": "o",
        "influxdb_bucket": "b",
        "grafana_url": "http://mon:3000",
        "grafana_password": "gp",
    }
    elastic_state = {
        "elasticsearch_url": "https://es:9200",
        "elasticsearch_username": "u",
        "elasticsearch_password": "p",
        "elasticsearch_api_key": "k",
        "elasticsearch_index_pattern": "ix",
    }

    captured = {}

    def fake_post(url, json, timeout):  # noqa: A002 - mirroring requests signature
        captured["payload"] = json
        return SimpleNamespace(ok=True, status_code=200, text="ok")

    args = SimpleNamespace(name="DEV-ML", dry_run=False)

    with (
        patch("saorsa_deploy.cmd.slack_post.get_repository", return_value=repo),
        patch("saorsa_deploy.cmd.slack_post._try_load_state", return_value={}),
        patch("saorsa_deploy.cmd.slack_post._try_load_client_state", return_value=None),
        patch(
            "saorsa_deploy.cmd.slack_post._try_load_monitoring_state",
            return_value=monitoring_state,
        ),
        patch(
            "saorsa_deploy.cmd.slack_post._try_load_elastic_state",
            return_value=elastic_state,
        ),
        patch.dict("os.environ", {"ANT_DEPLOY_SLACK_WEBHOOK_URL": "http://hook"}),
        patch("saorsa_deploy.cmd.slack_post.requests.post", side_effect=fake_post),
    ):
        cmd_slack_post(args)

    rendered = render_payload_text(captured["payload"])
    assert "Monitoring (InfluxDB + Grafana)" in rendered
    assert "Elasticsearch" in rendered
    assert "http://mon:8086" in rendered
    assert "https://es:9200" in rendered



# --------------------------------------------------------------------------- #
# clock skew rendering
# --------------------------------------------------------------------------- #
def test_testnet_renders_storage_full_nodes():
    deployment = _deployment(
        deploy_args={
            "bootstrap_count": 3,
            "vm_count": 6,
            "nodes_per_vm": 25,
            "full_percent": 33,
            "node_binary": {"kind": "release", "version": "0.4.1"},
        },
    )
    state = {"full_vm_ips": ["1.1.1.1", "2.2.2.2"]}
    text = _all_text(build_slack_blocks(deployment, state, None))
    assert "Storage-full nodes" in text
    # 2 full VMs × 25/VM = 50 full nodes, at 33%.
    assert "50 (2 VMs (33%))" in text


def test_testnet_storage_full_zero_when_unset():
    deployment = _deployment(
        deploy_args={
            "bootstrap_count": 3,
            "vm_count": 6,
            "nodes_per_vm": 25,
            "node_binary": {"kind": "release", "version": "0.4.1"},
        },
    )
    text = _all_text(build_slack_blocks(deployment, {}, None))
    assert "Storage-full nodes" in text


def test_testnet_clients_render_clock_skew():
    deployment = _deployment(
        deploy_args={
            "bootstrap_count": 3,
            "vm_count": 6,
            "nodes_per_vm": 25,
            "clock_skew_count": 1,
            "node_binary": {"kind": "release", "version": "0.4.1"},
        },
    )
    state = {"clock_skew_vm_ips": ["1.1.1.1"]}
    client_state = {
        "clients": [
            {
                "role": "upload",
                "region": "lon1",
                "vm_size": "s-2vcpu-4gb",
                "file_size_mb": 50,
                "clock_skew": True,
            },
            {
                "role": "download",
                "region": "fra1",
                "vm_size": "s-2vcpu-4gb",
                "clock_skew": True,
            },
            {
                "role": "download",
                "region": "ams3",
                "vm_size": "s-2vcpu-4gb",
            },
        ]
    }
    text = _all_text(build_slack_blocks(deployment, state, client_state))
    # Summary field counts both tiers: 1 node + 2 client VMs = 3.
    assert "Clock-skewed VMs" in text
    assert "3 (1 node, 2 client)" in text
    # Per-client clock-skew markers — uploader and downloader that opted in.
    assert "upload — lon1" in text
    assert "clock-skew" in text
    # The non-skewed downloader line must not carry the marker.
    ams_line = next(line for line in text.splitlines() if "ams3" in line)
    assert "clock-skew" not in ams_line


def test_testnet_client_only_clock_skew_counted_in_summary():
    # Regression: clock_skew set on a client (not on nodes) must still show in
    # the "Clock-skewed VMs" summary, not report 0.
    deployment = _deployment(
        deploy_args={"bootstrap_count": 3, "vm_count": 6, "nodes_per_vm": 25},
    )
    state = {}  # no node-tier skew selected
    client_state = {
        "clients": [
            {"role": "upload", "region": "lon1", "vm_size": "s-2vcpu-4gb", "file_size_mb": 50},
            {"role": "download", "region": "fra1", "vm_size": "s-2vcpu-4gb", "clock_skew": True},
        ]
    }
    text = _all_text(build_slack_blocks(deployment, state, client_state))
    assert "Clock-skewed VMs" in text
    assert "1 (1 client)" in text


def test_testnet_clients_omit_clock_skew_when_unset():
    deployment = _deployment(
        deploy_args={"bootstrap_count": 3, "vm_count": 6, "nodes_per_vm": 25},
    )
    state = {}
    client_state = {
        "clients": [
            {"role": "upload", "region": "lon1", "vm_size": "s-2vcpu-4gb", "file_size_mb": 50},
            {"role": "download", "region": "fra1", "vm_size": "s-2vcpu-4gb"},
        ]
    }
    text = _all_text(build_slack_blocks(deployment, state, client_state))
    assert "clock-skew" not in text
    # Field is still present but reports zero.
    assert "Clock-skewed VMs" in text


def test_prod_uploaders_render_clock_skew():
    deployment = _deployment(
        name="PROD-UP-SKEW",
        deploy_args={
            "kind": "prod-uploaders",
            "purpose": "p",
            "ant_binary": {"kind": "release", "version": "0.3.0"},
            "uploaders": [
                {
                    "region": "lon1",
                    "vm_size": "s-2vcpu-4gb",
                    "file_names": [],
                    "clock_skew": True,
                },
                {
                    "region": "fra1",
                    "vm_size": "s-2vcpu-4gb",
                    "file_names": [],
                },
            ],
        },
    )
    text = _all_text(build_slack_blocks(deployment, None, None))
    assert "lon1 (s-2vcpu-4gb)" in text
    assert "clock-skew" in text
    # The non-skewed fra1 line must not carry the marker.
    fra_line = next(line for line in text.splitlines() if "fra1 (s-2vcpu-4gb)" in line)
    assert "clock-skew" not in fra_line


def test_prod_downloader_renders_clock_skew_field():
    deployment = _deployment(
        name="PROD-CD-SKEW",
        deploy_args={
            "kind": "prod-continuous-downloader",
            "spec": "lon1:s-2vcpu-4gb",
            "clock_skew": True,
            "addresses_file": "/path/to/addrs.txt",
            "ant_binary": {"kind": "release", "version": "0.3.0"},
        },
    )
    text = _all_text(build_slack_blocks(deployment, None, None))
    assert "Clock skew" in text


def test_prod_downloader_omits_clock_skew_field_when_unset():
    deployment = _deployment(
        name="PROD-CD-NOSKEW",
        deploy_args={
            "kind": "prod-continuous-downloader",
            "spec": "lon1:s-2vcpu-4gb",
            "clock_skew": False,
            "addresses_file": "/path/to/addrs.txt",
            "ant_binary": {"kind": "release", "version": "0.3.0"},
        },
    )
    text = _all_text(build_slack_blocks(deployment, None, None))
    assert "Clock skew" not in text


# --------------------------------------------------------------------------- #
# bootstrap_cache rendering
# --------------------------------------------------------------------------- #
def test_testnet_clients_render_bootstrap_cache_markers():
    deployment = _deployment(
        deploy_args={"bootstrap_count": 3, "vm_count": 6, "nodes_per_vm": 25},
    )
    client_state = {
        "clients": [
            {
                "role": "upload",
                "region": "lon1",
                "vm_size": "s-2vcpu-4gb",
                "file_size_mb": 50,
                "bootstrap_cache": True,
            },
            {
                "role": "download",
                "region": "fra1",
                "vm_size": "s-2vcpu-4gb",
                "bootstrap_cache": False,
            },
            {
                # predates the option — no marker at all
                "role": "download",
                "region": "ams3",
                "vm_size": "s-2vcpu-4gb",
            },
        ]
    }
    text = _all_text(build_slack_blocks(deployment, {}, client_state))
    lon_line = next(line for line in text.splitlines() if "lon1" in line)
    fra_line = next(line for line in text.splitlines() if "fra1" in line)
    ams_line = next(line for line in text.splitlines() if "ams3" in line)
    assert "bootstrap-cache: on" in lon_line
    assert "bootstrap-cache: off" in fra_line
    assert "bootstrap-cache" not in ams_line


def test_prod_uploaders_render_bootstrap_cache_per_uploader():
    deployment = _deployment(
        name="PROD-UL-01",
        deploy_args={
            "kind": "prod-uploaders",
            "ant_binary": {"kind": "release", "version": "0.2.4"},
            "uploaders": [
                {"region": "lon1", "vm_size": "s-2vcpu-4gb", "bootstrap_cache": True},
                {"region": "nyc1", "vm_size": "s-2vcpu-4gb", "bootstrap_cache": False},
            ],
        },
    )
    text = _all_text(build_slack_blocks(deployment, {}, None))
    lon_line = next(line for line in text.splitlines() if "lon1" in line)
    nyc_line = next(line for line in text.splitlines() if "nyc1" in line)
    assert "bootstrap-cache: on" in lon_line
    assert "bootstrap-cache: off" in nyc_line


def test_prod_downloader_renders_bootstrap_cache_field():
    deployment = _deployment(
        name="PROD-DL-01",
        deploy_args={
            "kind": "prod-downloaders",
            "spec": "lon1",
            "bootstrap_cache": False,
            "ant_binary": {"kind": "release", "version": "0.2.4"},
        },
    )
    text = _all_text(build_slack_blocks(deployment, {}, None))
    assert "Bootstrap cache" in text
    assert "off" in text


def test_prod_local_kinds_render_bootstrap_cache():
    deployment = _deployment(
        name="LOCAL-UL-01",
        deploy_args={
            "kind": "prod-local-uploads",
            "input_mode": "urls",
            "input_path": "/tmp/l.txt",
            "file_count": 2,
            "bootstrap_cache": True,
            "ant_binary": {"kind": "release", "version": "0.2.4"},
        },
    )
    text = _all_text(build_slack_blocks(deployment, {}, None))
    assert "*Bootstrap cache:* on" in text

    deployment = _deployment(
        name="LOCAL-DL-01",
        deploy_args={
            "kind": "prod-local-downloads",
            "addresses_path": "/tmp/a.txt",
            "address_count": 3,
            "bootstrap_cache": False,
            "ant_binary": {"kind": "release", "version": "0.2.4"},
        },
    )
    text = _all_text(build_slack_blocks(deployment, {}, None))
    assert "*Bootstrap cache:* off" in text


def test_prod_canary_renders_bootstrap_cache():
    deployment = _deployment(
        name="PROD-CAN-01",
        deploy_args={"kind": "prod-canary", "bootstrap_cache": False},
    )
    text = _all_text(build_slack_blocks(deployment, {}, None))
    assert "*Bootstrap cache:* off" in text


def test_bootstrap_cache_omitted_for_old_records():
    deployment = _deployment(
        name="PROD-DL-OLD",
        deploy_args={
            "kind": "prod-downloaders",
            "spec": "lon1",
            "ant_binary": {"kind": "release", "version": "0.2.4"},
        },
    )
    text = _all_text(build_slack_blocks(deployment, {}, None))
    assert "Bootstrap cache" not in text
