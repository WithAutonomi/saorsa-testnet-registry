"""`deployments upgrade-service-script` (INFRA-484).

There was no way to ship a changed `prod_downloader.py` to PROD-DL-01:
`prod continuous-downloader` has only `deploy` and `destroy`, and `deploy` calls
`record_deployment_created` unconditionally, so a redeploy starts a fresh
registry row and breaks the continuous series the deployment exists to produce.
`upgrade-wasm-client` redeploys the same script but refuses
`client_kind: native`.

What is pinned here is what could go silently wrong: the unit each host group
restarts (one hard-coded name would restart a service that exists on neither of
a continuous uploader's two roles), the refusal of an unsupported kind, and that
one unreachable group does not abandon the others.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.cmd.upgrade_service_script import (
    SUPPORTED_KINDS,
    cmd_upgrade_service_script,
)
from saorsa_deploy.cmd.upgrade_wasm_client import SCRIPT_BY_KIND
from saorsa_deploy.provisioning.prod_continuous_uploader import (
    UPLOADER_SERVICE,
    VERIFIER_SERVICE,
)
from saorsa_deploy.provisioning.prod_downloader import (
    SERVICE_NAME as DOWNLOADER_SERVICE,
)

MODULE = "saorsa_deploy.cmd.upgrade_service_script"


def _args(**over):
    defaults = {"name": "PROD-DL-01", "ssh_key_path": "~/.ssh/id_rsa", "restart": True}
    defaults.update(over)
    return SimpleNamespace(**defaults)


def _repo(kind, *, client_kind="native", daily_at="00:00", name="PROD-DL-01"):
    deployment = SimpleNamespace(
        id=674,
        name=name,
        deploy_args={"kind": kind, "client_kind": client_kind, "daily_at": daily_at},
    )
    repo = MagicMock()
    repo.get_active_by_name.return_value = deployment
    repo.get_by_name.return_value = deployment
    return repo


class _RunRecorder:
    """Captures the commands each host group would run, without pyinfra."""

    def __init__(self, failing_ips=()):
        self.groups: list[tuple[list[str], list[str]]] = []
        self.failing_ips = set(failing_ips)

    def install(self, stack):
        def _inventory(spec):
            # `((hosts_data, {}))` -- the IPs are the first element of each
            # tuple. Stood in for rather than constructed, because a real
            # pyinfra Inventory needs a State attached before it will list its
            # hosts, and this test never builds one.
            hosts_data, _data = spec
            return SimpleNamespace(_ips=[ip for ip, _meta in hosts_data])

        def _state(*, inventory, config):
            return SimpleNamespace(failed_hosts=set(), _ips=list(inventory._ips))

        def _add_op(state, _op, name=None, commands=None):
            self.groups.append((list(state._ips), list(commands or [])))
            if self.failing_ips & set(state._ips):
                state.failed_hosts = {
                    SimpleNamespace(name=ip) for ip in state._ips if ip in self.failing_ips
                }

        stack.enter_context(patch(f"{MODULE}.Inventory", _inventory))
        stack.enter_context(patch(f"{MODULE}.State", _state))
        stack.enter_context(patch(f"{MODULE}.connect_all", lambda _state: None))
        stack.enter_context(patch(f"{MODULE}.disconnect_all", lambda _state: None))
        stack.enter_context(patch(f"{MODULE}.run_ops", lambda _state: None))
        stack.enter_context(patch(f"{MODULE}.add_op", _add_op))
        stack.enter_context(patch(f"{MODULE}.clear_known_hosts", lambda *_a, **_k: None))
        stack.enter_context(patch(f"{MODULE}.record_event", lambda *_a, **_k: None))


def _run(args, repo, clients, failing_ips=()):
    import contextlib

    recorder = _RunRecorder(failing_ips)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch(f"{MODULE}.get_repository", return_value=repo))
        stack.enter_context(patch(f"{MODULE}.load_client_state", return_value={"clients": clients}))
        recorder.install(stack)
        try:
            cmd_upgrade_service_script(args)
            code = 0
        except SystemExit as exc:
            code = exc.code
    return recorder, code


class TestScope:
    def test_only_the_two_continuous_kinds_are_supported(self):
        assert set(SUPPORTED_KINDS) == set(SCRIPT_BY_KIND)
        assert set(SUPPORTED_KINDS) == {
            "prod-continuous-downloader",
            "prod-continuous-uploader",
        }

    @pytest.mark.parametrize("kind", ["prod-downloaders", "prod-uploaders", "testnet", ""])
    def test_an_unsupported_kind_is_refused_by_name(self, kind):
        recorder, code = _run(_args(), _repo(kind), [{"ip": "10.0.0.1"}])
        assert code == 1
        assert recorder.groups == []

    def test_a_missing_deployment_is_refused(self):
        repo = MagicMock()
        repo.get_active_by_name.return_value = None
        repo.get_by_name.return_value = None
        recorder, code = _run(_args(), repo, [{"ip": "10.0.0.1"}])
        assert code == 1
        assert recorder.groups == []

    def test_no_registry_is_refused(self):
        with patch(f"{MODULE}.get_repository", return_value=None):
            with pytest.raises(SystemExit) as exc:
                cmd_upgrade_service_script(_args())
        assert exc.value.code == 1

    def test_no_client_ip_is_refused(self):
        recorder, code = _run(_args(), _repo("prod-continuous-downloader"), [{"role": "download"}])
        assert code == 1
        assert recorder.groups == []


class TestBothTransportsAreCovered:
    """The whole reason this is not `upgrade-wasm-client`."""

    @pytest.mark.parametrize("client_kind", ["native", "wasm"])
    def test_a_downloader_of_either_transport_is_upgraded(self, client_kind):
        recorder, code = _run(
            _args(),
            _repo("prod-continuous-downloader", client_kind=client_kind),
            [{"ip": "10.0.0.1", "role": "download"}],
        )
        assert code == 0
        assert len(recorder.groups) == 1
        ips, commands = recorder.groups[0]
        assert ips == ["10.0.0.1"]
        assert f"systemctl restart {DOWNLOADER_SERVICE}" in commands
        assert any("prod_downloader.py" in c for c in commands)


class TestUnitGrouping:
    def test_a_continuous_uploader_restarts_both_of_its_units(self):
        """One hard-coded unit name would restart a service on neither role."""
        recorder, code = _run(
            _args(name="PROD-UL-01"),
            _repo("prod-continuous-uploader", name="PROD-UL-01"),
            [
                {"ip": "10.0.0.1", "role": "upload"},
                {"ip": "10.0.0.2", "role": "verify"},
                {"ip": "10.0.0.3", "role": "verify"},
            ],
        )
        assert code == 0
        restarts = {
            ip: [c for c in commands if c.startswith("systemctl restart")]
            for ips, commands in recorder.groups
            for ip in ips
        }
        assert restarts["10.0.0.1"] == [f"systemctl restart {UPLOADER_SERVICE}"]
        assert restarts["10.0.0.2"] == [f"systemctl restart {VERIFIER_SERVICE}"]
        assert restarts["10.0.0.3"] == [f"systemctl restart {VERIFIER_SERVICE}"]

    def test_a_wasm_uploader_s_native_verifiers_are_upgraded_too(self):
        """Unlike the bundle, the script is the same file on every role."""
        recorder, code = _run(
            _args(name="PROD-WUL-01"),
            _repo("prod-continuous-uploader", client_kind="wasm", name="PROD-WUL-01"),
            [
                {"ip": "10.0.0.1", "role": "upload"},
                {"ip": "10.0.0.2", "role": "verify"},
            ],
        )
        assert code == 0
        upgraded = {ip for ips, _ in recorder.groups for ip in ips}
        assert upgraded == {"10.0.0.1", "10.0.0.2"}

    def test_a_spec_with_no_ip_is_dropped(self):
        recorder, code = _run(
            _args(),
            _repo("prod-continuous-downloader"),
            [{"ip": "10.0.0.1", "role": "download"}, {"role": "download"}],
        )
        assert code == 0
        assert [ip for ips, _ in recorder.groups for ip in ips] == ["10.0.0.1"]


class TestRestartFlag:
    def test_no_restart_writes_the_script_and_restarts_nothing(self):
        recorder, code = _run(
            _args(restart=False),
            _repo("prod-continuous-downloader"),
            [{"ip": "10.0.0.1", "role": "download"}],
        )
        assert code == 0
        _ips, commands = recorder.groups[0]
        assert any("prod_downloader.py" in c for c in commands)
        assert not any(c.startswith("systemctl restart") for c in commands)

    def test_the_timing_warning_names_the_deployment_s_own_anchor(self):
        """PROD-WDL-01 is at 02:00 and the other three at 00:00."""
        printed: list[str] = []
        with patch(f"{MODULE}.Console") as console_cls:
            console_cls.return_value = SimpleNamespace(
                print=lambda *a, **_k: printed.append(" ".join(map(str, a))),
                rule=lambda *_a, **_k: None,
            )
            _run(
                _args(name="PROD-WDL-01"),
                _repo("prod-continuous-downloader", daily_at="02:00", name="PROD-WDL-01"),
                [{"ip": "10.0.0.1", "role": "download"}],
            )
        warning = [line for line in printed if "Warning" in line]
        assert warning and "02:00 UTC" in warning[0]

    def test_no_warning_is_printed_without_a_restart(self):
        printed: list[str] = []
        with patch(f"{MODULE}.Console") as console_cls:
            console_cls.return_value = SimpleNamespace(
                print=lambda *a, **_k: printed.append(" ".join(map(str, a))),
                rule=lambda *_a, **_k: None,
            )
            _run(
                _args(restart=False),
                _repo("prod-continuous-downloader"),
                [{"ip": "10.0.0.1", "role": "download"}],
            )
        assert not [line for line in printed if "Warning" in line]


class TestOneFailureDoesNotStopTheRest:
    def test_a_failed_group_is_reported_and_the_other_still_runs(self):
        recorder, code = _run(
            _args(name="PROD-UL-01"),
            _repo("prod-continuous-uploader", name="PROD-UL-01"),
            [
                {"ip": "10.0.0.1", "role": "upload"},
                {"ip": "10.0.0.2", "role": "verify"},
            ],
            failing_ips=["10.0.0.1"],
        )
        # Both groups were attempted...
        assert {ip for ips, _ in recorder.groups for ip in ips} == {"10.0.0.1", "10.0.0.2"}
        # ...and the run still exits non-zero.
        assert code == 1
