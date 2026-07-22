"""Tests for the production inventory loader (`load_node_inventory`)."""

import json

import pytest

from saorsa_deploy.inventory import (
    SUPPORTED_PROVIDERS,
    SUPPORTED_SCHEMA_VERSIONS,
    load_node_inventory,
    update_service_in_inventory,
)


def _write_inventory(tmp_path, node_vms, schema_version=3):
    path = tmp_path / "inventory.json"
    path.write_text(
        json.dumps({"schema_version": schema_version, "node_vms": node_vms})
    )
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


@pytest.mark.parametrize("version", sorted(SUPPORTED_SCHEMA_VERSIONS))
def test_load_inventory_accepts_supported_schema_versions(tmp_path, version):
    # generate-inventory currently emits v4; earlier files are v3. The loader
    # must accept both — rejecting v4 silently broke every inventory-driven
    # command against freshly-generated inventories.
    path = _write_inventory(
        tmp_path, [_entry("ant-prod-01-vl-nrt-1", "vultr", "nrt")], schema_version=version
    )
    vms = load_node_inventory(path)
    assert [v.name for v in vms] == ["ant-prod-01-vl-nrt-1"]


def test_load_inventory_rejects_unsupported_schema_version(tmp_path):
    path = _write_inventory(
        tmp_path, [_entry("x", "vultr", "nrt")], schema_version=99
    )
    with pytest.raises(ValueError, match="Unsupported inventory schema_version"):
        load_node_inventory(path)


class TestUpdateServiceInInventory:
    def test_remove_drops_from_both_nodes_and_services(self, tmp_path):
        entry = _entry("host-a", "hetzner", "fsn1")
        entry["services"] = ["ant-node-1", "ant-node-2", "ant-node-10"]
        entry["nodes"] = [
            {"service_name": "ant-node-1", "nat_simulated": False, "storage_full": False},
            {"service_name": "ant-node-2", "nat_simulated": False, "storage_full": True},
            {"service_name": "ant-node-10", "nat_simulated": False, "storage_full": False},
        ]
        path = _write_inventory(tmp_path, [entry])

        update_service_in_inventory(path, "host-a", "ant-node-2", added=False)

        raw = json.loads(path.read_text())
        updated = raw["node_vms"][0]
        assert updated["services"] == ["ant-node-1", "ant-node-10"]
        assert [n["service_name"] for n in updated["nodes"]] == [
            "ant-node-1", "ant-node-10",
        ]

    def test_remove_only_touches_the_matching_host(self, tmp_path):
        other = _entry("host-b", "hetzner", "fsn1")
        target = _entry("host-a", "hetzner", "fsn1")
        target["services"] = ["ant-node-1", "ant-node-2"]
        path = _write_inventory(tmp_path, [other, target])

        update_service_in_inventory(path, "host-a", "ant-node-2", added=False)

        raw = json.loads(path.read_text())
        by_name = {e["name"]: e for e in raw["node_vms"]}
        assert by_name["host-a"]["services"] == ["ant-node-1"]
        assert by_name["host-b"]["services"] == ["ant-node-1"]  # untouched

    def test_remove_is_idempotent_when_already_absent(self, tmp_path):
        entry = _entry("host-a", "hetzner", "fsn1")
        entry["services"] = ["ant-node-1"]
        path = _write_inventory(tmp_path, [entry])

        update_service_in_inventory(path, "host-a", "ant-node-9", added=False)

        raw = json.loads(path.read_text())
        assert raw["node_vms"][0]["services"] == ["ant-node-1"]

    def test_add_appends_to_services_and_nodes_matching_sibling_shape(self, tmp_path):
        entry = _entry("host-a", "hetzner", "fsn1")
        entry["services"] = ["ant-node-1"]
        entry["nodes"] = [
            {"service_name": "ant-node-1", "nat_simulated": False, "storage_full": False}
        ]
        path = _write_inventory(tmp_path, [entry])

        update_service_in_inventory(path, "host-a", "ant-node-4", added=True)

        raw = json.loads(path.read_text())
        updated = raw["node_vms"][0]
        assert updated["services"] == ["ant-node-1", "ant-node-4"]
        new_node = next(n for n in updated["nodes"] if n["service_name"] == "ant-node-4")
        assert new_node == {
            "service_name": "ant-node-4", "nat_simulated": False, "storage_full": False,
        }

    def test_add_with_no_existing_nodes_array_uses_minimal_shape(self, tmp_path):
        # v3-style entries have only `services`, no `nodes` — migration complete
        # produces this shape, so `add` must tolerate it rather than assuming v4.
        entry = _entry("host-a", "hetzner", "fsn1")
        entry["services"] = ["ant-node-1"]
        path = _write_inventory(tmp_path, [entry])

        update_service_in_inventory(path, "host-a", "ant-node-4", added=True)

        raw = json.loads(path.read_text())
        updated = raw["node_vms"][0]
        assert updated["services"] == ["ant-node-1", "ant-node-4"]
        assert updated["nodes"] == [{"service_name": "ant-node-4"}]

    def test_add_is_idempotent_when_already_present(self, tmp_path):
        entry = _entry("host-a", "hetzner", "fsn1")
        entry["services"] = ["ant-node-1", "ant-node-4"]
        path = _write_inventory(tmp_path, [entry])

        update_service_in_inventory(path, "host-a", "ant-node-4", added=True)

        raw = json.loads(path.read_text())
        assert raw["node_vms"][0]["services"] == ["ant-node-1", "ant-node-4"]

    def test_unknown_host_raises(self, tmp_path):
        path = _write_inventory(tmp_path, [_entry("host-a", "hetzner", "fsn1")])
        with pytest.raises(ValueError, match="No host named"):
            update_service_in_inventory(path, "host-z", "ant-node-1", added=False)

    def test_write_is_atomic_no_leftover_tmp_file(self, tmp_path):
        entry = _entry("host-a", "hetzner", "fsn1")
        entry["services"] = ["ant-node-1", "ant-node-2"]
        path = _write_inventory(tmp_path, [entry])

        update_service_in_inventory(path, "host-a", "ant-node-2", added=False)

        assert not path.with_suffix(path.suffix + ".tmp").exists()
        assert path.exists()

    def test_other_top_level_keys_pass_through_untouched(self, tmp_path):
        path = tmp_path / "inventory.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 4,
                    "bootstrap_vms": [{"name": "bootstrap-1"}],
                    "node_vms": [_entry("host-a", "hetzner", "fsn1")],
                    "firewalls": {"contabo": "managed by terraform"},
                }
            )
        )

        update_service_in_inventory(path, "host-a", "ant-node-1", added=False)

        raw = json.loads(path.read_text())
        assert raw["bootstrap_vms"] == [{"name": "bootstrap-1"}]
        assert raw["firewalls"] == {"contabo": "managed by terraform"}
