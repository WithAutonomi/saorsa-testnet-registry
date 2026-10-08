"""Every provisioned node-tier and client host has ``ufw`` disabled.

Vultr's Ubuntu 24.04 image boots with ``ufw`` active, default-deny incoming and
only ``22/tcp`` allowed, so nothing can dial a node's OS-assigned high UDP port.
Run DEV-03 (V2-1410, registry id 632) made 6,821 direct QUIC attempts to the
seven Vultr node VMs and connected none of them, while the DigitalOcean VMs on
the same fleet accepted tens of thousands.

The NAT path has carried the same ``ufw disable`` since June, so these tests pin
two things: every node-tier and client plan now runs it, and the NAT setup
script's own copy is still the identical string (they share one definition, so a
drift would have to be deliberate).
"""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.provisioning.bootstrap import (
    AntBootstrapNodeProvisioner,
    AntBootstrapNodesProvisioner,
)
from saorsa_deploy.provisioning.firewall import (
    DISABLE_FIREWALL_OP_NAME,
    build_disable_firewall_command,
)
from saorsa_deploy.provisioning.nat import build_nat_setup_command
from saorsa_deploy.provisioning.node import AntNodeProvisioner

NAT_OP_NAME = "Configure port-restricted NAT (netns + iptables)"


@pytest.fixture(autouse=True)
def _mock_operator_authorized_keys_add_op():
    """Neutralize the operator-SSH-keys op's internal ``add_op``.

    ``execute()`` calls ``add_operator_authorized_keys_op``, which invokes the
    real pyinfra ``add_op`` from ``provisioning.authorized_keys`` — a module
    these tests don't patch, which makes pyinfra raise "Operation called within
    another operation" against the mocked State.
    """
    with patch("saorsa_deploy.provisioning.authorized_keys.add_op"):
        yield


def _fake_host(ip):
    return SimpleNamespace(name=ip)


def _op_names(mock_add_op):
    """Op ``name`` for every add_op call, in the order they were added."""
    return [call.kwargs.get("name") for call in mock_add_op.call_args_list]


def _run(module, provisioner, active_ips, extra_patches=()):
    """Run ``execute()`` with ``module``'s pyinfra surface mocked."""
    base = f"saorsa_deploy.provisioning.{module}"
    with ExitStack() as stack:
        for target in (
            "Inventory",
            "connect_all",
            "run_ops",
            "disconnect_all",
            *extra_patches,
        ):
            stack.enter_context(patch(f"{base}.{target}"))
        mock_state_cls = stack.enter_context(patch(f"{base}.State"))
        mock_add_op = stack.enter_context(patch(f"{base}.add_op"))
        # A plain MagicMock is not a RichLiveProgressHandler, so the Live
        # start/stop branches are skipped.
        stack.enter_context(patch(f"{base}.create_progress_handler", return_value=MagicMock()))
        state = mock_state_cls.return_value
        state.inventory.get_active_hosts.return_value = [_fake_host(ip) for ip in active_ips]
        state.failed_hosts = set()
        provisioner.execute()
        return mock_add_op


def _node_plan(**overrides):
    kwargs = dict(
        host_ips=["10.0.0.1"],
        bootstrap_peers=["10.0.0.100:10000"],
        node_count=2,
        console=MagicMock(),
    )
    kwargs.update(overrides)
    return _run(
        "node",
        AntNodeProvisioner(**kwargs),
        kwargs["host_ips"],
        extra_patches=("get_release_url",),
    )


def _genesis_plan():
    prov = AntBootstrapNodeProvisioner(
        ip="10.0.0.1",
        bootstrap_index=1,
        console=MagicMock(),
    )
    return _run("bootstrap", prov, ["10.0.0.1"], extra_patches=("get_release_url",))


def _bootstraps_plan():
    prov = AntBootstrapNodesProvisioner(
        nodes=[
            {"ip": "10.0.0.2", "bootstrap_index": 2, "region": "lon1"},
            {"ip": "10.0.0.3", "bootstrap_index": 3, "region": "ams3"},
        ],
        bootstrap_peers=["10.0.0.1:10000"],
        testnet_name="DEV-01",
        console=MagicMock(),
    )
    return _run(
        "bootstrap",
        prov,
        ["10.0.0.2", "10.0.0.3"],
        extra_patches=("get_release_url",),
    )


class TestEveryPlanDisablesTheFirewall:
    def test_plain_node_host(self):
        names = _op_names(_node_plan())
        assert names.count(DISABLE_FIREWALL_OP_NAME) == 1

    def test_nat_node_host(self):
        # The NAT host's own `ufw disable` lives inside the NAT script, which
        # short-circuits on a re-run — the standalone op is what guarantees the
        # firewall is down on every pass.
        names = _op_names(_node_plan(nat_ips={"10.0.0.1"}))
        assert names.count(DISABLE_FIREWALL_OP_NAME) == 1

    def test_genesis_host(self):
        names = _op_names(_genesis_plan())
        assert names.count(DISABLE_FIREWALL_OP_NAME) == 1

    def test_bootstrap_hosts(self):
        names = _op_names(_bootstraps_plan())
        assert names.count(DISABLE_FIREWALL_OP_NAME) == 1


class TestTheCommandIsTheNatOne:
    def test_nat_setup_still_carries_the_identical_line(self):
        # One definition, two call sites: the NAT script interpolates the same
        # builder, so this asserts they cannot drift.
        assert build_disable_firewall_command() in build_nat_setup_command()

    def test_the_line_is_unchanged_from_its_reference(self):
        # Byte-for-byte what provisioning/nat.py has run since June 2026.
        assert build_disable_firewall_command() == (
            "command -v ufw >/dev/null 2>&1 && ufw disable >/dev/null 2>&1 || true"
        )


class TestOrdering:
    def test_before_the_unit_file_op_on_a_plain_node(self):
        names = _op_names(_node_plan())
        assert names.index(DISABLE_FIREWALL_OP_NAME) < names.index("Write systemd unit files")

    def test_before_the_nat_setup_and_unit_file_ops_on_a_nat_node(self):
        names = _op_names(_node_plan(nat_ips={"10.0.0.1"}))
        firewall = names.index(DISABLE_FIREWALL_OP_NAME)
        assert firewall < names.index(NAT_OP_NAME)
        assert firewall < names.index("Write systemd unit files (NAT variant)")

    def test_before_the_unit_file_op_on_genesis(self):
        names = _op_names(_genesis_plan())
        assert names.index(DISABLE_FIREWALL_OP_NAME) < names.index("Write systemd unit file")

    def test_before_the_per_host_config_op_on_bootstraps(self):
        names = _op_names(_bootstraps_plan())
        assert names.index(DISABLE_FIREWALL_OP_NAME) < names.index("Configure ant-bootstrap-2")
