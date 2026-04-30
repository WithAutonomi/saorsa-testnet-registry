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

from saorsa_deploy.cmd.slack_post import build_slack_blocks, cmd_slack_post


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
    assert "Ant binary:" in text
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


def test_prod_uploaders_renders_payment_type_default_when_unset():
    deployment = _deployment(
        name="PROD-UP-PT-D",
        deploy_args={
            "kind": "prod-uploaders",
            "purpose": "p",
            "ant_binary": {"kind": "release", "version": "0.3.0"},
            "uploaders": [],
        },
    )
    text = _all_text(build_slack_blocks(deployment, None, None))
    assert "Payment type:" in text
    assert "default" in text


@pytest.mark.parametrize("payment_type", ["merkle", "no-merkle"])
def test_prod_uploaders_renders_payment_type_when_set(payment_type):
    deployment = _deployment(
        name="PROD-UP-PT",
        deploy_args={
            "kind": "prod-uploaders",
            "purpose": "p",
            "ant_binary": {"kind": "release", "version": "0.3.0"},
            "uploaders": [],
            "payment_type": payment_type,
        },
    )
    text = _all_text(build_slack_blocks(deployment, None, None))
    assert f"Payment type:* {payment_type}" in text or f"Payment type: {payment_type}" in text


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
    assert "Ant binary:" in text
    assert "release v0.3.0" in text


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
    payload = json.loads(out)
    assert payload["text"].startswith("Deployment DEV-DRY")


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
