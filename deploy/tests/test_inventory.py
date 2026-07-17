"""Tests for the production inventory loader (`load_node_inventory`)."""

import json

import pytest

from saorsa_deploy.inventory import SUPPORTED_PROVIDERS, load_node_inventory


def _write_inventory(tmp_path, node_vms):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"schema_version": 3, "node_vms": node_vms}))
    return path


def _entry(name, provider, region):
    return {
        "name": name,
        "provider": provider,
        "provider_id": f"id-{name}",
        "region": region,
        "ip": "10.0.0.1",
        "ssh_user": "root",
        "ssh_key": "~/.ssh/ant_deploy",
        "services": ["ant-node-1"],
    }


def test_contabo_is_a_supported_provider():
    # Regression: `migration complete` splices provider="contabo" node entries
    # into the prod inventory; the loader must accept them (else every later
    # command that reads the inventory — e.g. the next `migration plan` — fails).
    assert "contabo" in SUPPORTED_PROVIDERS


def test_load_inventory_with_contabo_and_vultr(tmp_path):
    path = _write_inventory(
        tmp_path,
        [
            _entry("ant-prod-01-cntb-US-central-1", "contabo", "US-central"),
            _entry("ant-prod-01-vl-nrt-1", "vultr", "nrt"),
        ],
    )
    vms = load_node_inventory(path)
    assert {v.provider for v in vms} == {"contabo", "vultr"}

    # The source-provider filter still isolates the leg being migrated.
    nrt = load_node_inventory(path, provider_filter="vultr")
    assert [v.name for v in nrt] == ["ant-prod-01-vl-nrt-1"]


def test_load_inventory_rejects_unknown_provider(tmp_path):
    path = _write_inventory(tmp_path, [_entry("x", "nimbus", "eu")])
    with pytest.raises(ValueError, match="unknown provider"):
        load_node_inventory(path)
