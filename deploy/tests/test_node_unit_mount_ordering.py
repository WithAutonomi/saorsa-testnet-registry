"""Every node-tier unit must be ordered against the data volume.

ant-node runs as root, so losing the boot race against its own mount does not
fail the service — it silently writes chunk data to the OS disk and serves
records the volume does not hold. That is why this is asserted for all three
node-tier unit builders rather than left to the one that happened to be fixed.
"""

from saorsa_deploy.provisioning.bootstrap import _build_unit_file as bootstrap_unit
from saorsa_deploy.provisioning.genesis import _build_unit_file as genesis_unit
from saorsa_deploy.provisioning.node import _build_node_unit_file
from saorsa_deploy.provisioning.volume import MOUNT_POINT

EXEC_START = "/usr/local/bin/ant-node --root-dir /mnt/ant-node-storage/data/x"


def _all_node_tier_units() -> dict[str, str]:
    return {
        "node": _build_node_unit_file("ant-node-1", EXEC_START),
        "genesis": genesis_unit(EXEC_START),
        "bootstrap": bootstrap_unit("ant-bootstrap-1", EXEC_START),
    }


class TestMountOrdering:
    def test_every_node_tier_unit_requires_the_data_mount(self):
        for label, unit in _all_node_tier_units().items():
            assert f"RequiresMountsFor={MOUNT_POINT}" in unit, label

    def test_directive_is_in_the_unit_section(self):
        # RequiresMountsFor is only meaningful under [Unit]; systemd ignores it
        # in [Service] without erroring, so a misplaced line would silently
        # restore the original bug.
        for label, unit in _all_node_tier_units().items():
            unit_section = unit.split("[Service]")[0]
            assert "RequiresMountsFor=" in unit_section, label

    def test_emitted_unconditionally_for_root_disk_hosts_too(self):
        # On contabo / ovh_3az the path is a plain dir on /, where systemd
        # resolves the requirement to the always-active -.mount. Keeping it
        # unconditional avoids a per-host unit variant; this pins that choice.
        nat = _build_node_unit_file("ant-node-1", EXEC_START, is_nat=True)
        prod = _build_node_unit_file(
            "ant-node-1",
            EXEC_START,
            restart_policy="always",
            restart_sec=10,
            limit_nofile=65536,
        )
        for unit in (nat, prod):
            assert f"RequiresMountsFor={MOUNT_POINT}" in unit

    def test_does_not_disturb_existing_network_ordering(self):
        for label, unit in _all_node_tier_units().items():
            assert "After=network-online.target" in unit, label
            assert "Wants=network-online.target" in unit, label
