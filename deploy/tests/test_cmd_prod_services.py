"""Tests for `prod services ls/rm/add` (deploy/saorsa_deploy/cmd/prod_services.py)."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from saorsa_db.models.prod_service_audit import ProdServiceAuditAction

from saorsa_deploy.cmd.prod_services import (
    SURVEY_RETRY_TIMEOUT_SECS,
    _format_bytes,
    _signed_bytes,
    cmd_prod_services_add,
    cmd_prod_services_disk_survey,
    cmd_prod_services_ls,
    cmd_prod_services_reset_data,
    cmd_prod_services_rm,
    cmd_prod_services_survey_report,
)
from saorsa_deploy.disk_survey import SurveyFailureKind
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
            "survey_retries": 1,
            "retry_backoff_secs": 0,
            "max_parallel": 16,
        }
        d.update(over)
        return SimpleNamespace(**d)


class TestDiskSurveyRetry:
    """V2-1147: a failed probe must be retried, and a host that stays failed
    must be recorded rather than silently dropped from the fleet total."""

    def _args(self, inv_path, **over):
        d = {
            "inventory_path": str(inv_path),
            "provider": None,
            "limit_to": None,
            "ssh_key_path": None,
            "no_store": False,
            "survey_retries": 1,
            "retry_backoff_secs": 0,
            "max_parallel": 16,
        }
        d.update(over)
        return SimpleNamespace(**d)

    @staticmethod
    def _ok(vm, size=100):
        return {
            "vm": vm,
            "services": [
                {
                    "service_name": "ant-node-1", "index": 1,
                    "active": "active", "enabled": "enabled", "size_bytes": size,
                }
            ],
            "disk": {"total_bytes": 400, "used_bytes": 300, "free_bytes": 100},
        }

    @staticmethod
    def _timeout(vm):
        return {"vm": vm, "error": "ssh timed out", "failure_kind": SurveyFailureKind.TIMEOUT}

    def test_host_recovered_on_retry_enters_the_totals(self, tmp_path):
        inv_path = _write_inventory(
            tmp_path, [_entry("host-a", ip="10.0.0.1"), _entry("host-b", ip="10.0.0.2")]
        )
        calls = []

        def fake_survey(vm, ssh_key, timeout=120):
            calls.append((vm.name, timeout))
            # host-b times out on the parallel pass, succeeds on the retry.
            if vm.name == "host-b" and len([c for c in calls if c[0] == "host-b"]) == 1:
                return self._timeout(vm)
            return self._ok(vm, size=50 if vm.name == "host-b" else 100)

        with (
            patch(
                "saorsa_deploy.cmd.prod_services._survey_one_host",
                side_effect=fake_survey,
            ),
            patch(
                "saorsa_deploy.cmd.prod_services.registry.record_disk_survey",
                return_value=1,
            ) as store,
            patch(
                "saorsa_deploy.cmd.prod_services.registry.get_prod_fleet_repository",
                return_value=None,
            ),
        ):
            cmd_prod_services_disk_survey(self._args(inv_path))

        kw = store.call_args.kwargs
        # The recovered host is in the totals, not the failure set.
        assert set(kw["host_disk"]) == {"host-a", "host-b"}
        assert kw["failed_hosts"] == {}
        assert kw["attempted_host_count"] == 2
        # The retry used the longer deadline, not the first-pass one.
        retry_timeouts = [t for name, t in calls if name == "host-b"][1:]
        assert retry_timeouts == [SURVEY_RETRY_TIMEOUT_SECS]

    def test_host_that_stays_failed_is_recorded_with_its_reason(self, tmp_path):
        inv_path = _write_inventory(
            tmp_path, [_entry("host-a", ip="10.0.0.1"), _entry("host-b", ip="10.0.0.2")]
        )

        def fake_survey(vm, ssh_key, timeout=120):
            if vm.name == "host-b":
                return self._timeout(vm)
            return self._ok(vm)

        with (
            patch(
                "saorsa_deploy.cmd.prod_services._survey_one_host",
                side_effect=fake_survey,
            ),
            patch(
                "saorsa_deploy.cmd.prod_services.registry.record_disk_survey",
                return_value=1,
            ) as store,
            patch(
                "saorsa_deploy.cmd.prod_services.registry.get_prod_fleet_repository",
                return_value=None,
            ),
        ):
            cmd_prod_services_disk_survey(self._args(inv_path))

        kw = store.call_args.kwargs
        assert set(kw["host_disk"]) == {"host-a"}
        assert kw["failed_hosts"] == {
            "host-b": {"kind": "timeout", "message": "ssh timed out", "attempts": 2}
        }
        # attempted stays 2 even though only 1 host was reached — that is the
        # denominator that makes the under-count visible.
        assert kw["attempted_host_count"] == 2

    def test_auth_failure_is_not_retried(self, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        calls = []

        def fake_survey(vm, ssh_key, timeout=120):
            calls.append(vm.name)
            return {
                "vm": vm,
                "error": "Permission denied (publickey).",
                "failure_kind": SurveyFailureKind.AUTH,
            }

        with (
            patch(
                "saorsa_deploy.cmd.prod_services._survey_one_host",
                side_effect=fake_survey,
            ),
            patch(
                "saorsa_deploy.cmd.prod_services.registry.record_disk_survey",
                return_value=1,
            ) as store,
            patch(
                "saorsa_deploy.cmd.prod_services.registry.get_prod_fleet_repository",
                return_value=None,
            ),
        ):
            cmd_prod_services_disk_survey(self._args(inv_path))

        assert calls == ["host-a"]  # no retry
        assert store.call_args.kwargs["failed_hosts"]["host-a"]["attempts"] == 1

    def test_retries_zero_disables_the_retry_pass(self, tmp_path):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        calls = []

        def fake_survey(vm, ssh_key, timeout=120):
            calls.append(vm.name)
            return self._timeout(vm)

        with (
            patch(
                "saorsa_deploy.cmd.prod_services._survey_one_host",
                side_effect=fake_survey,
            ),
            patch(
                "saorsa_deploy.cmd.prod_services.registry.record_disk_survey",
                return_value=1,
            ),
            patch(
                "saorsa_deploy.cmd.prod_services.registry.get_prod_fleet_repository",
                return_value=None,
            ),
        ):
            cmd_prod_services_disk_survey(self._args(inv_path, survey_retries=0))

        assert calls == ["host-a"]

    def test_dropped_host_is_called_out_against_the_previous_survey(
        self, tmp_path, capsys
    ):
        """The V2-1147 signature: the raw total falls because a host dropped
        out, while the hosts measured by both runs actually grew."""
        inv_path = _write_inventory(
            tmp_path, [_entry("host-a", ip="10.0.0.1"), _entry("host-b", ip="10.0.0.2")]
        )

        def fake_survey(vm, ssh_key, timeout=120):
            if vm.name == "host-b":
                return self._timeout(vm)
            return self._ok(vm, size=110)  # host-a grew from 100

        fake_repo = MagicMock()
        fake_repo.latest_disk_surveys.return_value = [
            SimpleNamespace(id=39, surveyed_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
        ]
        fake_repo.disk_survey_host_data_bytes.return_value = {
            "host-a": 100, "host-b": 900,
        }

        with (
            patch(
                "saorsa_deploy.cmd.prod_services._survey_one_host",
                side_effect=fake_survey,
            ),
            patch(
                "saorsa_deploy.cmd.prod_services.registry.record_disk_survey",
                return_value=1,
            ),
            patch(
                "saorsa_deploy.cmd.prod_services.registry.get_prod_fleet_repository",
                return_value=fake_repo,
            ),
        ):
            cmd_prod_services_disk_survey(self._args(inv_path))

        out = capsys.readouterr().out
        assert "Coverage dropped" in out
        assert "host-b" in out
        assert "Like-for-like" in out
        assert "misleading" in out.lower()

    def test_no_previous_survey_prints_no_comparison(self, tmp_path, capsys):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        fake_repo = MagicMock()
        fake_repo.latest_disk_surveys.return_value = []

        with (
            patch(
                "saorsa_deploy.cmd.prod_services._survey_one_host",
                side_effect=lambda vm, k, timeout=120: self._ok(vm),
            ),
            patch(
                "saorsa_deploy.cmd.prod_services.registry.record_disk_survey",
                return_value=1,
            ),
            patch(
                "saorsa_deploy.cmd.prod_services.registry.get_prod_fleet_repository",
                return_value=fake_repo,
            ),
        ):
            cmd_prod_services_disk_survey(self._args(inv_path))

        out = capsys.readouterr().out
        assert "Like-for-like" not in out
        assert "of 1 attempted" in out


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


def _dewrap(text: str) -> str:
    """Collapse Rich's 80-column hard wrapping so assertions can match phrases
    rather than whatever fragment happened to land on one line."""
    return " ".join(text.split())


class TestSurveyReportLikeForLike:
    """V2-1147: the report's node-data delta must be computed over the hosts
    both surveys reached, not over each run's whole total."""

    def _surveys(self, *, prev_failed=None, latest_failed=None):
        from saorsa_db.models.prod_disk_survey import ProdDiskSurvey

        t1 = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
        t2 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        hd = {"host-a": {"provider": "contabo", "total_bytes": 400,
                         "used_bytes": 300, "free_bytes": 100}}
        prev = ProdDiskSurvey(
            inventory_name="inventory", surveyed_at=t1, host_disk=hd,
            host_count=2, service_count=2, total_data_bytes=1000,
            failed_hosts=prev_failed, attempted_host_count=2,
        )
        latest = ProdDiskSurvey(
            inventory_name="inventory", surveyed_at=t2, host_disk=hd,
            host_count=1, service_count=1, total_data_bytes=110,
            failed_hosts=latest_failed, attempted_host_count=2,
        )
        prev.id, latest.id = 38, 39
        return prev, latest

    def _run(self, tmp_path, repo):
        inv_path = _write_inventory(tmp_path, [_entry("host-a")])
        with patch(
            "saorsa_deploy.cmd.prod_services.registry.get_prod_fleet_repository",
            return_value=repo,
        ):
            cmd_prod_services_survey_report(SimpleNamespace(inventory_path=str(inv_path)))

    def test_delta_is_computed_over_the_common_hosts(self, tmp_path, capsys):
        prev, latest = self._surveys(latest_failed={
            "host-b": {"kind": "timeout", "message": "ssh timed out", "attempts": 2}
        })
        repo = MagicMock()
        repo.latest_disk_surveys.return_value = [latest, prev]
        # host-b dropped out; host-a actually grew 100 -> 110.
        repo.disk_survey_host_data_bytes.side_effect = lambda sid: (
            {"host-a": 100, "host-b": 900} if sid == 38 else {"host-a": 110}
        )
        repo.list_service_audit.return_value = []

        self._run(tmp_path, repo)
        out = _dewrap(capsys.readouterr().out)

        assert "Coverage dropped" in out
        assert "Like-for-like" in out
        # The raw total fell by 890 B; like-for-like it grew by 110 B.
        assert "DIFFERING host sets" in out
        assert "misleading" in out.lower()

    def test_identical_coverage_prints_one_total_line(self, tmp_path, capsys):
        prev, latest = self._surveys(prev_failed={}, latest_failed={})
        repo = MagicMock()
        repo.latest_disk_surveys.return_value = [latest, prev]
        repo.disk_survey_host_data_bytes.side_effect = lambda sid: (
            {"host-a": 100} if sid == 38 else {"host-a": 110}
        )
        repo.list_service_audit.return_value = []

        self._run(tmp_path, repo)
        out = _dewrap(capsys.readouterr().out)

        assert "Total node data" in out
        assert "Like-for-like" not in out
        assert "Coverage dropped" not in out

    def test_pre_migration_survey_reads_as_unknown_not_as_no_failures(
        self, tmp_path, capsys
    ):
        # failed_hosts is NULL on every survey recorded before V2-1147. That
        # must never render as "all hosts reached".
        prev, latest = self._surveys(prev_failed=None, latest_failed=None)
        repo = MagicMock()
        repo.latest_disk_surveys.return_value = [latest, prev]
        repo.disk_survey_host_data_bytes.side_effect = lambda sid: (
            {"host-a": 100} if sid == 38 else {"host-a": 110}
        )
        repo.list_service_audit.return_value = []

        self._run(tmp_path, repo)
        out = _dewrap(capsys.readouterr().out)

        assert "recorded before failed-host tracking" in out
        assert "whether any host was missed is unknown" in out
        # It must not make the definite claim that hosts WERE missed, nor the
        # opposite claim that none were.
        assert "totals are short" not in out

    def test_stored_failures_are_rendered_with_their_reason(self, tmp_path, capsys):
        prev, latest = self._surveys(
            prev_failed={},
            latest_failed={
                "cntb-US-central-1": {
                    "kind": "timeout", "message": "ssh timed out", "attempts": 2,
                }
            },
        )
        repo = MagicMock()
        repo.latest_disk_surveys.return_value = [latest, prev]
        repo.disk_survey_host_data_bytes.side_effect = lambda sid: {"host-a": 100}
        repo.list_service_audit.return_value = []

        self._run(tmp_path, repo)
        out = _dewrap(capsys.readouterr().out)

        assert "timeout" in out
        assert "totals are short" in out

    def test_missing_per_host_rows_falls_back_to_stored_totals(self, tmp_path, capsys):
        prev, latest = self._surveys(prev_failed={}, latest_failed={})
        repo = MagicMock()
        repo.latest_disk_surveys.return_value = [latest, prev]
        repo.disk_survey_host_data_bytes.return_value = {}
        repo.list_service_audit.return_value = []

        self._run(tmp_path, repo)
        out = _dewrap(capsys.readouterr().out)

        assert "per-host detail unavailable" in out


class TestByteFormatting:
    def test_negative_values_are_scaled_not_printed_raw(self):
        # A shrinking figure used to fall straight through to the bytes branch,
        # so a free-disk delta rendered as "-55336960 B".
        assert _format_bytes(-55336960) == "-52.8 MiB"

    def test_signed_bytes_always_carries_a_sign(self):
        assert _signed_bytes(0) == "+0 B"
        assert _signed_bytes(1024) == "+1.0 KiB"
        assert _signed_bytes(-1024) == "-1.0 KiB"

    def test_positive_formatting_is_unchanged(self):
        assert _format_bytes(0) == "0 B"
        assert _format_bytes(1023) == "1023 B"
        assert _format_bytes(1024) == "1.0 KiB"
