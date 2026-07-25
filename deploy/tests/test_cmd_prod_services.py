"""Tests for `prod services ls/rm/add` (deploy/saorsa_deploy/cmd/prod_services.py)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from saorsa_db.models.prod_service_audit import ProdServiceAuditAction

from saorsa_deploy.cmd.prod_services import (
    cmd_prod_services_add,
    cmd_prod_services_disk_survey,
    cmd_prod_services_ls,
    cmd_prod_services_reset_data,
    cmd_prod_services_rm,
    cmd_prod_services_survey_report,
)
from saorsa_deploy.provisioning.node import _build_node_exec_start, _build_node_unit_file


def _entry(name, ip="10.0.0.1"):
    return {
        "name": name,
        "provider": "hetzner",
        "provider_id": f"id-{name}",
        "region": "fsn1",
        "ip": ip,
        "ssh_user": "root",
        "ssh_key": "~/.ssh/ant_deploy",
        "services": ["ant-node-1"],
    }


def _write_inventory(tmp_path, node_vms):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"schema_version": 4, "node_vms": node_vms}))
    return path


def _make_ssh_key(tmp_path):
    key = tmp_path / "id_rsa"
    key.write_text("fake key")
    return str(key)


def _args(**overrides):
    defaults = {
        "inventory_path": "",
        "name": "host-a",
        "service_name": None,
        "ssh_key_path": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _op_result(host, stdout_lines):
    meta = MagicMock()
    meta.stdout_lines = stdout_lines
    return {host: meta}


def _sibling_unit(index=1, port=10000, **overrides):
    params = {
        "bootstrap_peers": ["1.2.3.4:12000"],
        "root_dir": f"/mnt/ant-node-storage/data/ant-node-{index}",
        "log_dir": f"/mnt/ant-node-storage/log/ant-node-{index}",
        "port": port,
        "log_level": "info",
        "testnet": False,
        "upgrade_channel": None,
        "rewards_address": "0xabc",
        "evm_network": "arbitrum-one",
    }
    params.update(overrides)
    exec_start = _build_node_exec_start(**params)
    return _build_node_unit_file(
        f"ant-node-{index}",
        exec_start,
        is_nat=False,
        restart_policy="always",
        restart_sec=10,
        limit_nofile=65536,
    )


@pytest.fixture
def services_patches():
    with (
        patch("saorsa_deploy.cmd.prod_services.clear_known_hosts") as p_clear,
        patch("saorsa_deploy.cmd.prod_services.Inventory") as p_inv,
        patch("saorsa_deploy.cmd.prod_services.State") as p_state_cls,
        patch("saorsa_deploy.cmd.prod_services.connect_all") as p_connect,
        patch("saorsa_deploy.cmd.prod_services.disconnect_all") as p_disconnect,
        patch("saorsa_deploy.cmd.prod_services.add_op") as p_add_op,
        patch("saorsa_deploy.cmd.prod_services.run_ops") as p_run_ops,
        patch("saorsa_deploy.cmd.prod_services.subprocess.run") as p_subproc,
    ):
        mock_state_instance = MagicMock()
        mock_state_instance.failed_hosts = set()
        p_state_cls.return_value = mock_state_instance

        fake_host = MagicMock(name="fake-host")
        p_add_op.return_value = _op_result(fake_host, [])

        yield SimpleNamespace(
            clear=p_clear,
            inventory=p_inv,
            state_cls=p_state_cls,
            state_instance=mock_state_instance,
            connect=p_connect,
            disconnect=p_disconnect,
            add_op=p_add_op,
            run_ops=p_run_ops,
            subprocess_run=p_subproc,
            host=fake_host,
        )


class TestResolveHost:
    def test_missing_inventory_file_exits(self, services_patches, tmp_path):
        args = _args(inventory_path=str(tmp_path / "nope.json"), service_name="ant-node-1")
        with pytest.raises(SystemExit):
            cmd_prod_services_rm(args)
        services_patches.add_op.assert_not_called()

    def test_unknown_host_name_exits(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        args = _args(inventory_path=str(inv_path), name="host-b", service_name="ant-node-1")
        with pytest.raises(SystemExit):
            cmd_prod_services_rm(args)
        services_patches.add_op.assert_not_called()

    def test_missing_ssh_key_exits(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        args = _args(
            inventory_path=str(inv_path),
            service_name="ant-node-1",
            ssh_key_path=str(tmp_path / "no-such-key"),
        )
        with pytest.raises(SystemExit):
            cmd_prod_services_rm(args)
        services_patches.add_op.assert_not_called()


class TestCmdProdServicesLs:
    def test_no_services_found_prints_message_without_error(
        self, services_patches, tmp_path, capsys
    ):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        ssh_key = _make_ssh_key(tmp_path)
        services_patches.add_op.return_value = _op_result(
            services_patches.host, ["ANT_SVC:LIST:NONE"]
        )
        cmd_prod_services_ls(
            _args(inventory_path=str(inv_path), ssh_key_path=ssh_key)
        )
        assert "No ant-node-* services found" in capsys.readouterr().out

    def test_lists_discovered_services(self, services_patches, tmp_path, capsys):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        ssh_key = _make_ssh_key(tmp_path)
        services_patches.add_op.return_value = _op_result(
            services_patches.host,
            [
                "ANT_SVC:LIST:ant-node-1:active:enabled:1048576",
                "ANT_SVC:LIST:ant-node-2:inactive:disabled:0",
            ],
        )
        cmd_prod_services_ls(
            _args(inventory_path=str(inv_path), ssh_key_path=ssh_key)
        )
        out = capsys.readouterr().out
        assert "ant-node-1" in out
        assert "ant-node-2" in out
        services_patches.add_op.assert_called_once()

    def test_rejects_invalid_service_name_filter(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        with pytest.raises(SystemExit):
            cmd_prod_services_ls(
                _args(inventory_path=str(inv_path), service_name="not-valid")
            )
        services_patches.add_op.assert_not_called()


class TestCmdProdServicesRm:
    def test_invalid_service_name_exits_before_any_ssh(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        with pytest.raises(SystemExit):
            cmd_prod_services_rm(
                _args(inventory_path=str(inv_path), service_name="bogus")
            )
        services_patches.connect.assert_not_called()

    def test_service_not_on_host_exits(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        ssh_key = _make_ssh_key(tmp_path)
        services_patches.add_op.return_value = _op_result(
            services_patches.host, ["ANT_SVC:LIST:ant-node-1:active:enabled:100"]
        )
        with pytest.raises(SystemExit):
            cmd_prod_services_rm(
                _args(
                    inventory_path=str(inv_path),
                    service_name="ant-node-9",
                    ssh_key_path=ssh_key,
                )
            )
        # Only the discovery op ran — no removal was attempted.
        services_patches.add_op.assert_called_once()

    def test_removes_only_the_targeted_service(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        ssh_key = _make_ssh_key(tmp_path)
        services_patches.add_op.side_effect = [
            _op_result(
                services_patches.host,
                [
                    "ANT_SVC:LIST:ant-node-1:active:enabled:100",
                    "ANT_SVC:LIST:ant-node-2:active:enabled:200",
                ],
            ),
            _op_result(services_patches.host, ["ANT_SVC:REMOVED:ant-node-2"]),
        ]
        cmd_prod_services_rm(
            _args(
                inventory_path=str(inv_path),
                service_name="ant-node-2",
                ssh_key_path=ssh_key,
            )
        )
        remove_call = services_patches.add_op.call_args_list[1]
        cmds = " ".join(remove_call.kwargs["commands"])
        assert "ant-node-2" in cmds
        assert "ant-node-1" not in cmds

    def test_removed_service_dropped_from_inventory_file(self, services_patches, tmp_path):
        entry = _entry("host-a")
        entry["services"] = ["ant-node-1", "ant-node-2"]
        inv_path = _write_inventory(tmp_path, [entry])
        ssh_key = _make_ssh_key(tmp_path)
        services_patches.add_op.side_effect = [
            _op_result(
                services_patches.host,
                [
                    "ANT_SVC:LIST:ant-node-1:active:enabled:100",
                    "ANT_SVC:LIST:ant-node-2:active:enabled:200",
                ],
            ),
            _op_result(services_patches.host, ["ANT_SVC:REMOVED:ant-node-2"]),
        ]
        cmd_prod_services_rm(
            _args(
                inventory_path=str(inv_path),
                service_name="ant-node-2",
                ssh_key_path=ssh_key,
            )
        )
        raw = json.loads(inv_path.read_text())
        assert raw["node_vms"][0]["services"] == ["ant-node-1"]

    def test_inventory_write_failure_warns_but_does_not_raise(
        self, services_patches, tmp_path, capsys
    ):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        ssh_key = _make_ssh_key(tmp_path)
        services_patches.add_op.side_effect = [
            _op_result(
                services_patches.host, ["ANT_SVC:LIST:ant-node-1:active:enabled:100"]
            ),
            _op_result(services_patches.host, ["ANT_SVC:REMOVED:ant-node-1"]),
        ]
        with patch(
            "saorsa_deploy.cmd.prod_services.update_service_in_inventory",
            side_effect=OSError("disk full"),
        ):
            cmd_prod_services_rm(
                _args(
                    inventory_path=str(inv_path),
                    service_name="ant-node-1",
                    ssh_key_path=ssh_key,
                )
            )
        out = capsys.readouterr().out.replace("\n", " ")
        assert "could not be updated" in out


class TestCmdProdServicesAdd:
    def test_already_exists_exits(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        ssh_key = _make_ssh_key(tmp_path)
        services_patches.add_op.return_value = _op_result(
            services_patches.host, ["ANT_SVC:LIST:ant-node-4:active:enabled:100"]
        )
        with pytest.raises(SystemExit):
            cmd_prod_services_add(
                _args(
                    inventory_path=str(inv_path),
                    service_name="ant-node-4",
                    ssh_key_path=ssh_key,
                )
            )
        services_patches.subprocess_run.assert_not_called()

    def test_no_active_sibling_exits(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        ssh_key = _make_ssh_key(tmp_path)
        services_patches.add_op.return_value = _op_result(
            services_patches.host, ["ANT_SVC:LIST:ant-node-1:inactive:disabled:100"]
        )
        with pytest.raises(SystemExit):
            cmd_prod_services_add(
                _args(
                    inventory_path=str(inv_path),
                    service_name="ant-node-4",
                    ssh_key_path=ssh_key,
                )
            )
        services_patches.subprocess_run.assert_not_called()

    def test_clones_lowest_index_active_sibling_and_offsets_ports(
        self, services_patches, tmp_path
    ):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        ssh_key = _make_ssh_key(tmp_path)

        services_patches.add_op.side_effect = [
            _op_result(
                services_patches.host,
                [
                    "ANT_SVC:LIST:ant-node-3:active:enabled:300",
                    "ANT_SVC:LIST:ant-node-1:active:enabled:100",
                ],
            ),
            _op_result(services_patches.host, ["ANT_SVC:ADDED:ant-node-4"]),
        ]
        services_patches.subprocess_run.return_value = SimpleNamespace(
            returncode=0,
            stdout=_sibling_unit(index=1, port=10000),
            stderr="",
        )

        cmd_prod_services_add(
            _args(
                inventory_path=str(inv_path),
                service_name="ant-node-4",
                ssh_key_path=ssh_key,
            )
        )

        # Clones the lowest-index active sibling (ant-node-1), not ant-node-3.
        read_call = services_patches.subprocess_run.call_args
        assert "ant-node-1.service" in read_call.args[0][-1]

        write_call = services_patches.add_op.call_args_list[1]
        cmds = " ".join(write_call.kwargs["commands"])
        assert "ant-node-4" in cmds
        # index 4 - sibling index 1 = delta 3
        assert "--port 10003" in cmds
        assert "--metrics-port" not in cmds
        assert "ant-node-1" not in cmds

    def test_added_service_appears_in_inventory_file(self, services_patches, tmp_path):
        entry = _entry("host-a")
        entry["services"] = ["ant-node-1"]
        inv_path = _write_inventory(tmp_path, [entry])
        ssh_key = _make_ssh_key(tmp_path)

        services_patches.add_op.side_effect = [
            _op_result(
                services_patches.host, ["ANT_SVC:LIST:ant-node-1:active:enabled:100"]
            ),
            _op_result(services_patches.host, ["ANT_SVC:ADDED:ant-node-4"]),
        ]
        services_patches.subprocess_run.return_value = SimpleNamespace(
            returncode=0,
            stdout=_sibling_unit(index=1, port=10000),
            stderr="",
        )

        cmd_prod_services_add(
            _args(
                inventory_path=str(inv_path),
                service_name="ant-node-4",
                ssh_key_path=ssh_key,
            )
        )

        raw = json.loads(inv_path.read_text())
        assert raw["node_vms"][0]["services"] == ["ant-node-1", "ant-node-4"]

    def test_ssh_read_failure_exits(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        ssh_key = _make_ssh_key(tmp_path)
        services_patches.add_op.return_value = _op_result(
            services_patches.host, ["ANT_SVC:LIST:ant-node-1:active:enabled:100"]
        )
        services_patches.subprocess_run.return_value = SimpleNamespace(
            returncode=1, stdout="", stderr="no such file"
        )
        with pytest.raises(SystemExit):
            cmd_prod_services_add(
                _args(
                    inventory_path=str(inv_path),
                    service_name="ant-node-4",
                    ssh_key_path=ssh_key,
                )
            )


class TestResetData:
    def test_invalid_service_name_exits_before_any_ssh(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        with pytest.raises(SystemExit):
            cmd_prod_services_reset_data(
                _args(inventory_path=str(inv_path), service_name="node-1")
            )
        services_patches.add_op.assert_not_called()

    def test_service_not_on_host_exits(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        ssh_key = _make_ssh_key(tmp_path)
        services_patches.add_op.return_value = _op_result(
            services_patches.host, ["ANT_SVC:LIST:ant-node-1:active:enabled:100"]
        )
        with pytest.raises(SystemExit):
            cmd_prod_services_reset_data(
                _args(
                    inventory_path=str(inv_path),
                    service_name="ant-node-9",
                    ssh_key_path=ssh_key,
                )
            )
        # Only the discovery op ran — no reset was attempted.
        services_patches.add_op.assert_called_once()

    def test_resets_the_targeted_service_only(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        ssh_key = _make_ssh_key(tmp_path)
        services_patches.add_op.side_effect = [
            _op_result(
                services_patches.host,
                [
                    "ANT_SVC:LIST:ant-node-1:active:enabled:100",
                    "ANT_SVC:LIST:ant-node-2:active:enabled:200",
                ],
            ),
            _op_result(services_patches.host, ["ANT_SVC:RESET:ant-node-2"]),
        ]
        cmd_prod_services_reset_data(
            _args(
                inventory_path=str(inv_path),
                service_name="ant-node-2",
                ssh_key_path=ssh_key,
            )
        )
        reset_call = services_patches.add_op.call_args_list[1]
        cmds = " ".join(reset_call.kwargs["commands"])
        assert "systemctl stop ant-node-2" in cmds
        assert "systemctl start ant-node-2" in cmds
        assert "node_identity.key" in cmds
        assert "ant-node-1" not in cmds

    def test_does_not_write_inventory(self, services_patches, tmp_path):
        # reset-data leaves the service definition intact, so the inventory file
        # must be untouched.
        entry = _entry("host-a")
        entry["services"] = ["ant-node-1", "ant-node-2"]
        inv_path = _write_inventory(tmp_path, [entry])
        ssh_key = _make_ssh_key(tmp_path)
        services_patches.add_op.side_effect = [
            _op_result(
                services_patches.host,
                [
                    "ANT_SVC:LIST:ant-node-1:active:enabled:100",
                    "ANT_SVC:LIST:ant-node-2:active:enabled:200",
                ],
            ),
            _op_result(services_patches.host, ["ANT_SVC:RESET:ant-node-2"]),
        ]
        cmd_prod_services_reset_data(
            _args(
                inventory_path=str(inv_path),
                service_name="ant-node-2",
                ssh_key_path=ssh_key,
            )
        )
        raw = json.loads(inv_path.read_text())
        assert raw["node_vms"][0]["services"] == ["ant-node-1", "ant-node-2"]


class TestAuditTrail:
    """rm/add/reset-data record a best-effort audit row after the SSH change."""

    def _ls_then_op(self, services_patches, op_marker):
        services_patches.add_op.side_effect = [
            _op_result(
                services_patches.host,
                [
                    "ANT_SVC:LIST:ant-node-1:active:enabled:100",
                    "ANT_SVC:LIST:ant-node-2:active:enabled:222",
                ],
            ),
            _op_result(services_patches.host, [op_marker]),
        ]

    def test_rm_records_removed_with_size(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        ssh_key = _make_ssh_key(tmp_path)
        self._ls_then_op(services_patches, "ANT_SVC:REMOVED:ant-node-2")
        with patch("saorsa_deploy.cmd.prod_services.registry.record_service_audit") as m:
            cmd_prod_services_rm(
                _args(inventory_path=str(inv_path), service_name="ant-node-2", ssh_key_path=ssh_key)
            )
        m.assert_called_once()
        kw = m.call_args
        assert kw.args[2] == "ant-node-2"
        assert kw.args[3] == ProdServiceAuditAction.REMOVED
        assert kw.kwargs["size_bytes"] == 222  # size from the pre-rm ls

    def test_reset_records_reset_with_size(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        ssh_key = _make_ssh_key(tmp_path)
        self._ls_then_op(services_patches, "ANT_SVC:RESET:ant-node-2")
        with patch("saorsa_deploy.cmd.prod_services.registry.record_service_audit") as m:
            cmd_prod_services_reset_data(
                _args(inventory_path=str(inv_path), service_name="ant-node-2", ssh_key_path=ssh_key)
            )
        m.assert_called_once()
        assert m.call_args.args[3] == ProdServiceAuditAction.RESET
        assert m.call_args.kwargs["size_bytes"] == 222

    def test_add_records_added(self, services_patches, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        ssh_key = _make_ssh_key(tmp_path)
        services_patches.add_op.side_effect = [
            _op_result(services_patches.host, ["ANT_SVC:LIST:ant-node-1:active:enabled:100"]),
            _op_result(services_patches.host, ["ANT_SVC:ADDED:ant-node-4"]),
        ]
        services_patches.subprocess_run.return_value = SimpleNamespace(
            returncode=0, stdout=_sibling_unit(index=1, port=10000), stderr=""
        )
        with patch("saorsa_deploy.cmd.prod_services.registry.record_service_audit") as m:
            cmd_prod_services_add(
                _args(inventory_path=str(inv_path), service_name="ant-node-4", ssh_key_path=ssh_key)
            )
        m.assert_called_once()
        assert m.call_args.args[3] == ProdServiceAuditAction.ADDED
        assert m.call_args.kwargs.get("size_bytes") is None


class TestDiskSurvey:
    def _survey_args(self, inv_path, **over):
        d = {
            "inventory_path": str(inv_path),
            "provider": None,
            "limit_to": None,
            "ssh_key_path": None,
            "no_store": False,
        }
        d.update(over)
        return SimpleNamespace(**d)

    def test_aggregates_sorts_ascending_and_stores(self, tmp_path):
        inv_path = _write_inventory(
            tmp_path, [_entry("host-a", ip="10.0.0.1"), _entry("host-b", ip="10.0.0.2")]
        )

        def _svc(name, idx, active, size):
            return {
                "service_name": name, "index": idx,
                "active": active, "enabled": "enabled", "size_bytes": size,
            }

        def fake_survey(vm, ssh_key):
            if vm.name == "host-a":
                return {
                    "vm": vm,
                    "services": [
                        _svc("ant-node-1", 1, "active", 30),
                        _svc("ant-node-2", 2, "active", 10),
                    ],
                    "disk": {"total_bytes": 400, "used_bytes": 390, "free_bytes": 10},
                }
            return {
                "vm": vm,
                "services": [_svc("ant-node-1", 1, "inactive", 20)],
                "disk": {"total_bytes": 400, "used_bytes": 100, "free_bytes": 300},
            }

        with (
            patch(
                "saorsa_deploy.cmd.prod_services._survey_one_host",
                side_effect=fake_survey,
            ),
            patch(
                "saorsa_deploy.cmd.prod_services.registry.record_disk_survey",
                return_value=7,
            ) as store,
        ):
            cmd_prod_services_disk_survey(self._survey_args(inv_path))

        store.assert_called_once()
        kw = store.call_args.kwargs
        sizes = [s["size_bytes"] for s in kw["services"]]
        assert sizes == [10, 20, 30]  # ascending across hosts
        assert kw["services"][0]["active"] is True  # str -> bool
        assert set(kw["host_disk"]) == {"host-a", "host-b"}
        assert kw["host_disk"]["host-a"]["free_bytes"] == 10

    def test_no_store_skips_persistence(self, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])

        def fake_survey(vm, ssh_key):
            return {
                "vm": vm, "services": [],
                "disk": {"total_bytes": 1, "used_bytes": 1, "free_bytes": 0},
            }

        with (
            patch("saorsa_deploy.cmd.prod_services._survey_one_host", side_effect=fake_survey),
            patch("saorsa_deploy.cmd.prod_services.registry.record_disk_survey") as store,
        ):
            cmd_prod_services_disk_survey(self._survey_args(inv_path, no_store=True))
        store.assert_not_called()


class TestSurveyReport:
    def test_no_database_url_exits(self, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        with patch(
            "saorsa_deploy.cmd.prod_services.registry.get_prod_fleet_repository",
            return_value=None,
        ):
            with pytest.raises(SystemExit):
                cmd_prod_services_survey_report(
                    SimpleNamespace(inventory_path=str(inv_path))
                )

    def test_compares_two_latest_and_reads_audit(self, tmp_path):
        from datetime import datetime, timezone

        from saorsa_db.models.prod_disk_survey import ProdDiskSurvey
        from saorsa_db.models.prod_service_audit import (
            ProdServiceAudit,
            ProdServiceAuditAction,
        )

        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        t1 = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
        def _hd(free, used):
            return {
                "host-a": {
                    "provider": "hetzner", "total_bytes": 400,
                    "used_bytes": used, "free_bytes": free,
                }
            }

        prev = ProdDiskSurvey(
            inventory_name="inventory", surveyed_at=t1, host_disk=_hd(10, 390),
            host_count=1, service_count=3, total_data_bytes=90,
        )
        latest = ProdDiskSurvey(
            inventory_name="inventory", surveyed_at=t2, host_disk=_hd(40, 360),
            host_count=1, service_count=2, total_data_bytes=60,
        )
        event = ProdServiceAudit(
            inventory_name="inventory", host="host-a", service_name="ant-node-3",
            action=ProdServiceAuditAction.REMOVED, occurred_at=t2, size_bytes=30,
        )
        fake_repo = MagicMock()
        fake_repo.latest_disk_surveys.return_value = [latest, prev]
        fake_repo.list_service_audit.return_value = [event]

        with patch(
            "saorsa_deploy.cmd.prod_services.registry.get_prod_fleet_repository",
            return_value=fake_repo,
        ):
            cmd_prod_services_survey_report(SimpleNamespace(inventory_path=str(inv_path)))

        fake_repo.latest_disk_surveys.assert_called_once()
        fake_repo.list_service_audit.assert_called_once_with(
            "inventory", since=t1, until=t2
        )
