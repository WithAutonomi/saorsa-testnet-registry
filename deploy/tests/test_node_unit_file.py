from saorsa_deploy.provisioning.nat import NAT_IP_EXEC_PREFIX
from saorsa_deploy.provisioning.node import (
    _build_node_exec_start,
    _build_node_unit_file,
)


def _sample_exec_start() -> str:
    return _build_node_exec_start(
        bootstrap_peers=["1.2.3.4:10000", "5.6.7.8:10000"],
        root_dir="/mnt/ant-node-storage/data/ant-node-1",
        log_dir="/var/log/ant-node/ant-node-1",
        port=10000,
    )


class TestPlainUnitFile:
    def test_description_has_no_nat_suffix(self):
        exec_start = _sample_exec_start()
        unit = _build_node_unit_file("ant-node-1", exec_start)
        assert "Description=Ant Node (ant-node-1)\n" in unit
        assert "port-restricted NAT" not in unit

    def test_exec_start_unwrapped(self):
        exec_start = _sample_exec_start()
        unit = _build_node_unit_file("ant-node-1", exec_start)
        assert f"ExecStart={exec_start}\n" in unit
        assert "ip netns exec" not in unit

    def test_has_service_boilerplate(self):
        unit = _build_node_unit_file("ant-node-1", _sample_exec_start())
        assert "Type=simple" in unit
        assert "Restart=on-success" in unit
        assert "RestartSec=5" in unit
        assert "WantedBy=multi-user.target" in unit


class TestNatUnitFile:
    def test_description_has_nat_suffix(self):
        unit = _build_node_unit_file("ant-node-1", _sample_exec_start(), is_nat=True)
        assert "Description=Ant Node (ant-node-1) (port-restricted NAT)" in unit

    def test_exec_start_is_wrapped_with_ip_netns_exec(self):
        exec_start = _sample_exec_start()
        unit = _build_node_unit_file("ant-node-1", exec_start, is_nat=True)
        assert f"ExecStart={NAT_IP_EXEC_PREFIX}{exec_start}" in unit
        assert "/usr/sbin/ip netns exec ant_nat " in unit

    def test_keeps_same_ant_node_flags_as_plain(self):
        exec_start = _sample_exec_start()
        nat_unit = _build_node_unit_file("ant-node-1", exec_start, is_nat=True)
        plain_unit = _build_node_unit_file("ant-node-1", exec_start, is_nat=False)
        # Everything after the NAT wrapper should match the plain ExecStart.
        nat_exec = [
            line for line in nat_unit.splitlines() if line.startswith("ExecStart=")
        ][0]
        plain_exec = [
            line for line in plain_unit.splitlines() if line.startswith("ExecStart=")
        ][0]
        assert nat_exec == f"ExecStart={NAT_IP_EXEC_PREFIX}{exec_start}"
        assert plain_exec == f"ExecStart={exec_start}"
        # Same ant-node arguments after stripping the NAT wrapper.
        assert nat_exec[len(f"ExecStart={NAT_IP_EXEC_PREFIX}"):] == plain_exec[len("ExecStart="):]

    def test_other_unit_settings_match_plain(self):
        exec_start = _sample_exec_start()
        nat_unit = _build_node_unit_file("ant-node-1", exec_start, is_nat=True)
        plain_unit = _build_node_unit_file("ant-node-1", exec_start, is_nat=False)
        # User directive: only take the NAT simulation from the reference;
        # restart policy etc. stay the same across variants.
        for key in ("Type=simple", "Restart=on-success", "RestartSec=5"):
            assert key in nat_unit
            assert key in plain_unit


class TestHeaptrackExecStart:
    def test_no_heaptrack_by_default(self):
        exec_start = _sample_exec_start()
        assert not exec_start.startswith("heaptrack")
        assert "heaptrack" not in exec_start

    def test_heaptrack_wraps_ant_node(self):
        exec_start = _build_node_exec_start(
            bootstrap_peers=["1.2.3.4:10000"],
            root_dir="/mnt/ant-node-storage/data/ant-node-1",
            heaptrack_base="/mnt/ant-node-storage/heaptrack/ant-node-1",
        )
        # heaptrack must be the outermost program, with -o pointing at the base,
        # immediately followed by the ant-node binary.
        assert exec_start.startswith(
            "heaptrack -o /mnt/ant-node-storage/heaptrack/ant-node-1 "
            "/usr/local/bin/ant-node "
        )

    def test_heaptrack_keeps_all_ant_node_flags(self):
        plain = _build_node_exec_start(
            bootstrap_peers=["1.2.3.4:10000"],
            root_dir="/mnt/ant-node-storage/data/ant-node-1",
        )
        wrapped = _build_node_exec_start(
            bootstrap_peers=["1.2.3.4:10000"],
            root_dir="/mnt/ant-node-storage/data/ant-node-1",
            heaptrack_base="/mnt/ant-node-storage/heaptrack/ant-node-1",
        )
        # Stripping the heaptrack prefix yields the identical ant-node command.
        prefix = "heaptrack -o /mnt/ant-node-storage/heaptrack/ant-node-1 "
        assert wrapped.startswith(prefix)
        assert wrapped[len(prefix):] == plain

    def test_heaptrack_composes_inside_nat_prefix(self):
        # In a NAT unit the netns prefix must remain outermost so heaptrack
        # profiles the node process, not `ip netns exec`.
        exec_start = _build_node_exec_start(
            bootstrap_peers=["1.2.3.4:10000"],
            root_dir="/mnt/ant-node-storage/data/ant-node-1",
            heaptrack_base="/mnt/ant-node-storage/heaptrack/ant-node-1",
        )
        unit = _build_node_unit_file("ant-node-1", exec_start, is_nat=True)
        assert (
            f"ExecStart={NAT_IP_EXEC_PREFIX}heaptrack -o "
            "/mnt/ant-node-storage/heaptrack/ant-node-1 /usr/local/bin/ant-node "
            in unit
        )
