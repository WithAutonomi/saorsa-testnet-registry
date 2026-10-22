"""`deployments collect-client-logs`, which replaced the skill's SSH block.

The regression that matters most is the unit: `prod-uploaders` — the kind
STG-UL-01 and STG-WUL-01 use, i.e. the main thing this capture exists for — was
handed `ant-prod-downloader` by the only host/unit resolver the tool had, which
would have archived a directory that does not exist on those hosts and reported
a complete capture. So every kind's units are asserted here, and the whole
subprocess layer is patched: no SSH, no S3, no Postgres.
"""

import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.cmd.collect_client_logs import (
    ARCHIVE_NAME,
    MANIFEST_FILE_NAME,
    PAYMENT_MARKERS_FILE_NAME,
    SERVICE_FILE_NAME,
    SUMMARY_FILE_NAME,
    cmd_collect_client_logs,
    resolve_output_dir,
    verify_archive,
)
from saorsa_deploy.prod_client_units import (
    PROD_DOWNLOADER_LOG_DIR,
    PROD_DOWNLOADER_SERVICE,
    PROD_UPLOADER_LOG_DIR,
    PROD_UPLOADER_SERVICE,
    UnknownProdClientKind,
    group_clients_by_unit,
    units_for_kind,
)
from saorsa_deploy.provisioning.prod_continuous_uploader import (
    UPLOADER_SERVICE,
    VERIFIER_SERVICE,
)


def _args(**overrides):
    defaults = {
        "name": "STG-WUL-01",
        "output": None,
        "ssh_key_path": "~/.ssh/ant_deploy",
        "kind": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _tar_gz(path: Path, entries: int = 1) -> Path:
    """A real gzip tar, so the verification under test is the real one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for i in range(entries):
            member = path.parent / f"op-{i}.stderr"
            member.write_text("quote accepted\n")
            archive.add(member, arcname=f"ant-prod-uploader/op-{i}.stderr")
    return path


class TestUnitResolution:
    """The kind -> unit mapping, which is what a wrong capture comes from."""

    def test_prod_uploaders_is_never_given_the_downloader_unit(self):
        groups = group_clients_by_unit(
            "prod-uploaders",
            [
                {"ip": "10.0.0.1", "role": "upload"},
                {"ip": "10.0.0.2", "role": "upload"},
            ],
        )

        assert [(u.service, u.log_dir, ips) for u, ips in groups] == [
            (PROD_UPLOADER_SERVICE, PROD_UPLOADER_LOG_DIR, ["10.0.0.1", "10.0.0.2"])
        ]
        assert PROD_UPLOADER_SERVICE != PROD_DOWNLOADER_SERVICE

    @pytest.mark.parametrize("kind", ["prod-downloaders", "prod-continuous-downloader"])
    def test_download_kinds_resolve_to_the_downloader_unit(self, kind):
        groups = group_clients_by_unit(kind, [{"ip": "10.0.0.1", "role": "download"}])

        assert [(u.service, u.log_dir, ips) for u, ips in groups] == [
            (PROD_DOWNLOADER_SERVICE, PROD_DOWNLOADER_LOG_DIR, ["10.0.0.1"])
        ]

    def test_continuous_uploader_splits_uploader_from_verifiers(self):
        groups = group_clients_by_unit(
            "prod-continuous-uploader",
            [
                {"ip": "10.0.0.1", "role": "upload"},
                {"ip": "10.0.0.2", "role": "download"},
                {"ip": "10.0.0.3", "role": "download"},
            ],
        )

        assert [(u.service, ips) for u, ips in groups] == [
            (UPLOADER_SERVICE, ["10.0.0.1"]),
            (VERIFIER_SERVICE, ["10.0.0.2", "10.0.0.3"]),
        ]

    def test_a_wasm_continuous_uploader_keeps_its_uploader_vm(self):
        """Unlike `upgrade-ant-binary`, which skips it: there is no `ant` to
        upgrade there, but its logs are the browser path's own evidence."""
        groups = group_clients_by_unit(
            "prod-continuous-uploader",
            [
                {"ip": "10.0.0.1", "role": "upload", "client_kind": "wasm"},
                {"ip": "10.0.0.2", "role": "download"},
            ],
        )

        assert [u.service for u, _ in groups] == [UPLOADER_SERVICE, VERIFIER_SERVICE]

    def test_wasm_is_not_a_special_case(self):
        native = group_clients_by_unit("prod-uploaders", [{"ip": "10.0.0.1", "role": "upload"}])
        wasm = group_clients_by_unit(
            "prod-uploaders",
            [{"ip": "10.0.0.1", "role": "wasm-upload", "client_kind": "wasm"}],
        )

        assert [(u.service, u.log_dir) for u, _ in native] == [
            (u.service, u.log_dir) for u, _ in wasm
        ]

    def test_only_the_upload_units_carry_payment_markers(self):
        assert [u.is_upload for u in units_for_kind("prod-uploaders")] == [True]
        assert [u.is_upload for u in units_for_kind("prod-downloaders")] == [False]
        assert [u.is_upload for u in units_for_kind("prod-continuous-uploader")] == [True, False]

    def test_hosts_without_an_ip_are_dropped(self):
        groups = group_clients_by_unit(
            "prod-uploaders", [{"role": "upload"}, {"ip": "10.0.0.2", "role": "upload"}]
        )

        assert [ips for _, ips in groups] == [["10.0.0.2"]]

    def test_an_unknown_kind_raises(self):
        with pytest.raises(UnknownProdClientKind):
            units_for_kind("prod-canary")


class TestResolveOutputDir:
    def test_defaults_under_the_state_dir_with_the_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAORSA_DEPLOY_STATE_DIR", str(tmp_path))

        assert resolve_output_dir("STG-WUL-01", 697, None) == (
            tmp_path / "client-test-forensics/STG-WUL-01-697"
        )

    def test_falls_back_to_the_name_alone_without_an_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAORSA_DEPLOY_STATE_DIR", str(tmp_path))

        assert resolve_output_dir("STG-WUL-01", None, None) == (
            tmp_path / "client-test-forensics/STG-WUL-01"
        )

    def test_output_overrides(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAORSA_DEPLOY_STATE_DIR", str(tmp_path))

        assert resolve_output_dir("STG-WUL-01", 697, str(tmp_path / "elsewhere")) == (
            tmp_path / "elsewhere"
        )

    def test_nothing_resolves_relative_to_the_cwd(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAORSA_DEPLOY_STATE_DIR", str(tmp_path))

        assert resolve_output_dir("STG-WUL-01", 697, None).is_absolute()


class TestVerifyArchive:
    def test_a_valid_archive_with_entries_verifies(self, tmp_path):
        ok, reason, entries = verify_archive(_tar_gz(tmp_path / ARCHIVE_NAME, entries=3))

        assert (ok, reason) == (True, None)
        assert entries == 3

    def test_a_missing_archive_fails(self, tmp_path):
        ok, reason, entries = verify_archive(tmp_path / ARCHIVE_NAME)

        assert ok is False
        assert "was not written" in reason
        assert entries is None

    def test_a_zero_byte_archive_fails(self, tmp_path):
        path = tmp_path / ARCHIVE_NAME
        path.write_bytes(b"")

        ok, reason, _ = verify_archive(path)

        assert ok is False
        assert "zero bytes" in reason

    def test_an_empty_but_valid_archive_fails_with_its_own_reason(self, tmp_path):
        path = tmp_path / ARCHIVE_NAME
        with tarfile.open(path, "w:gz"):
            pass

        ok, reason, entries = verify_archive(path)

        assert ok is False
        assert "lists no entries" in reason
        assert entries == 0

    def test_a_truncated_archive_fails_with_its_own_reason(self, tmp_path):
        path = _tar_gz(tmp_path / ARCHIVE_NAME, entries=3)
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])

        ok, reason, _ = verify_archive(path)

        assert ok is False
        assert "not a readable gzip tar" in reason

    def test_the_three_failures_have_distinct_reasons(self, tmp_path):
        missing = tmp_path / "missing" / ARCHIVE_NAME
        zero = tmp_path / "zero" / ARCHIVE_NAME
        zero.parent.mkdir()
        zero.write_bytes(b"")
        corrupt = _tar_gz(tmp_path / "corrupt" / ARCHIVE_NAME)
        corrupt.write_bytes(corrupt.read_bytes()[:40])

        reasons = {verify_archive(p)[1] for p in (missing, zero, corrupt)}

        assert len(reasons) == 3


@pytest.fixture
def collect_env(tmp_path, monkeypatch):
    """Patch the registry, S3 client state and the subprocess layer.

    `_fake_run` writes a real archive for the hosts in `reachable` and mimics
    ssh(1)'s own exit 255 for the rest, so the command's verification runs
    against bytes rather than a mock's say-so.
    """
    monkeypatch.setenv("SAORSA_DEPLOY_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    env = SimpleNamespace(
        kind="prod-uploaders",
        deployment_id=697,
        client_kind="wasm",
        clients=[{"ip": "10.0.0.1", "role": "upload"}],
        reachable={"10.0.0.1"},
        archive_entries=2,
        commands=[],
        tmp_path=tmp_path,
    )

    def _fake_run(argv, **kwargs):
        env.commands.append(argv)
        ip = next(a.split("@", 1)[1] for a in argv if a.startswith("root@"))
        command = argv[-1]
        if ip not in env.reachable:
            return SimpleNamespace(
                returncode=255, stdout="", stderr="ssh: connect to host port 22: No route to host"
            )
        if command.startswith("tar -cz"):
            source = _tar_gz(tmp_path / f"src-{ip}" / ARCHIVE_NAME, entries=env.archive_entries)
            kwargs["stdout"].write(source.read_bytes())
            return SimpleNamespace(returncode=0, stdout=None, stderr="")
        return SimpleNamespace(returncode=0, stdout=f"$ {command}\nok\n", stderr="")

    repo = MagicMock()
    repo.get_active_by_name.return_value = SimpleNamespace(
        id=env.deployment_id,
        deploy_args={"kind": env.kind, "client_kind": env.client_kind},
    )

    with (
        patch("saorsa_deploy.cmd.collect_client_logs.get_repository", return_value=repo),
        patch(
            "saorsa_deploy.cmd.collect_client_logs.load_client_state",
            side_effect=lambda name: {"name": name, "clients": env.clients},
        ),
        patch("saorsa_deploy.cmd.collect_client_logs.subprocess.run", side_effect=_fake_run),
    ):
        env.repo = repo
        yield env


def _out_dir(env) -> Path:
    return env.tmp_path / "state/client-test-forensics/STG-WUL-01-697"


def _summary(env) -> dict:
    return json.loads((_out_dir(env) / SUMMARY_FILE_NAME).read_text())


class TestCollectClientLogs:
    def test_a_verified_capture_exits_zero_and_writes_every_file(self, collect_env):
        cmd_collect_client_logs(_args())

        host_dir = _out_dir(collect_env) / f"{PROD_UPLOADER_SERVICE}-10.0.0.1"
        for name in (ARCHIVE_NAME, SERVICE_FILE_NAME, MANIFEST_FILE_NAME):
            assert (host_dir / name).is_file(), name
        summary = _summary(collect_env)
        assert summary["ok"] is True
        assert summary["deployment_id"] == 697
        assert summary["kind"] == "prod-uploaders"
        assert summary["client_kind"] == "wasm"
        assert summary["hosts"][0]["ok"] is True
        assert summary["hosts"][0]["archive_entries"] == 2
        assert summary["hosts"][0]["files"][ARCHIVE_NAME] > 0

    def test_payment_markers_are_collected_on_an_upload_unit(self, collect_env):
        cmd_collect_client_logs(_args())

        host_dir = _out_dir(collect_env) / f"{PROD_UPLOADER_SERVICE}-10.0.0.1"
        markers = host_dir / PAYMENT_MARKERS_FILE_NAME
        assert markers.is_file()
        assert ".paid" in markers.read_text()

    def test_payment_markers_are_not_collected_on_a_download_unit(self, collect_env):
        collect_env.kind = "prod-downloaders"
        collect_env.repo.get_active_by_name.return_value = SimpleNamespace(
            id=697, deploy_args={"kind": "prod-downloaders"}
        )
        collect_env.clients = [{"ip": "10.0.0.1", "role": "download"}]

        cmd_collect_client_logs(_args())

        host_dir = _out_dir(collect_env) / f"{PROD_DOWNLOADER_SERVICE}-10.0.0.1"
        assert (host_dir / ARCHIVE_NAME).is_file()
        assert not (host_dir / PAYMENT_MARKERS_FILE_NAME).exists()
        assert ".paid" not in " ".join(argv[-1] for argv in collect_env.commands)

    def test_the_log_directory_archived_is_the_units_own(self, collect_env):
        cmd_collect_client_logs(_args())

        tar_commands = [a[-1] for a in collect_env.commands if a[-1].startswith("tar -cz")]
        assert tar_commands == [f"tar -cz -C /var/log {Path(PROD_UPLOADER_LOG_DIR).name}"]

    def test_every_host_of_a_multi_vm_deployment_is_collected(self, collect_env):
        collect_env.clients = [
            {"ip": "10.0.0.1", "role": "upload"},
            {"ip": "10.0.0.2", "role": "upload"},
        ]
        collect_env.reachable = {"10.0.0.1", "10.0.0.2"}

        cmd_collect_client_logs(_args())

        assert [h["ip"] for h in _summary(collect_env)["hosts"]] == ["10.0.0.1", "10.0.0.2"]

    def test_one_unreachable_host_does_not_abandon_the_others(self, collect_env):
        collect_env.clients = [
            {"ip": "10.0.0.1", "role": "upload"},
            {"ip": "10.0.0.2", "role": "upload"},
        ]
        collect_env.reachable = {"10.0.0.2"}

        with pytest.raises(SystemExit) as excinfo:
            cmd_collect_client_logs(_args())

        assert excinfo.value.code != 0
        hosts = {h["ip"]: h for h in _summary(collect_env)["hosts"]}
        assert hosts["10.0.0.2"]["ok"] is True
        assert hosts["10.0.0.1"]["ok"] is False
        assert "unreachable" in hosts["10.0.0.1"]["reason"]
        good = _out_dir(collect_env) / f"{PROD_UPLOADER_SERVICE}-10.0.0.2" / ARCHIVE_NAME
        assert good.stat().st_size > 0

    def test_every_host_unreachable_exits_non_zero(self, collect_env):
        collect_env.reachable = set()

        with pytest.raises(SystemExit) as excinfo:
            cmd_collect_client_logs(_args())

        assert excinfo.value.code != 0
        summary = _summary(collect_env)
        assert summary["ok"] is False
        assert all(h["ok"] is False for h in summary["hosts"])

    def test_an_empty_archive_fails_the_host(self, collect_env):
        collect_env.archive_entries = 0

        with pytest.raises(SystemExit) as excinfo:
            cmd_collect_client_logs(_args())

        assert excinfo.value.code != 0
        assert "lists no entries" in _summary(collect_env)["hosts"][0]["reason"]

    def test_summary_is_valid_json_in_both_outcomes(self, collect_env):
        cmd_collect_client_logs(_args())
        assert _summary(collect_env)["ok"] is True

        collect_env.reachable = set()
        with pytest.raises(SystemExit):
            cmd_collect_client_logs(_args())
        assert _summary(collect_env)["ok"] is False

    def test_re_running_overwrites_in_place(self, collect_env):
        cmd_collect_client_logs(_args())
        host_dir = _out_dir(collect_env) / f"{PROD_UPLOADER_SERVICE}-10.0.0.1"
        first = (host_dir / ARCHIVE_NAME).stat().st_size

        collect_env.archive_entries = 5
        cmd_collect_client_logs(_args())

        assert (host_dir / ARCHIVE_NAME).stat().st_size != first
        assert _summary(collect_env)["hosts"][0]["archive_entries"] == 5

    def test_output_overrides_the_destination(self, collect_env):
        elsewhere = collect_env.tmp_path / "elsewhere"

        cmd_collect_client_logs(_args(output=str(elsewhere)))

        assert (elsewhere / SUMMARY_FILE_NAME).is_file()
        assert not _out_dir(collect_env).exists()

    @pytest.mark.parametrize("kind", ["prod-local-uploads", "prod-local-downloads"])
    def test_the_local_kinds_are_refused_without_touching_ssh(self, collect_env, kind):
        collect_env.repo.get_active_by_name.return_value = SimpleNamespace(
            id=697, deploy_args={"kind": kind}
        )

        with pytest.raises(SystemExit) as excinfo:
            cmd_collect_client_logs(_args())

        assert excinfo.value.code != 0
        assert collect_env.commands == []

    def test_an_unknown_kind_is_a_hard_error(self, collect_env):
        collect_env.repo.get_active_by_name.return_value = SimpleNamespace(
            id=697, deploy_args={"kind": "prod-canary"}
        )

        with pytest.raises(SystemExit) as excinfo:
            cmd_collect_client_logs(_args())

        assert excinfo.value.code != 0
        assert collect_env.commands == []

    def test_an_unresolvable_kind_is_refused_rather_than_guessed(self, collect_env):
        """No registry row, so no kind: `upgrade-ant-binary` falls through to
        the downloader unit here, which would archive a directory that does not
        exist on an uploader VM."""
        collect_env.repo.get_active_by_name.return_value = None
        collect_env.repo.get_by_name.return_value = None

        with pytest.raises(SystemExit) as excinfo:
            cmd_collect_client_logs(_args())

        assert excinfo.value.code != 0
        assert collect_env.commands == []

    def test_kind_flag_carries_a_registry_less_run(self, collect_env):
        collect_env.repo.get_active_by_name.return_value = None
        collect_env.repo.get_by_name.return_value = None

        cmd_collect_client_logs(_args(kind="prod-uploaders"))

        out_dir = collect_env.tmp_path / "state/client-test-forensics/STG-WUL-01"
        summary = json.loads((out_dir / SUMMARY_FILE_NAME).read_text())
        assert summary["deployment_id"] is None
        assert summary["ok"] is True

    def test_the_transport_comes_from_client_state_without_a_registry(self, collect_env):
        collect_env.repo.get_active_by_name.return_value = None
        collect_env.repo.get_by_name.return_value = None
        collect_env.clients = [{"ip": "10.0.0.1", "role": "upload", "client_kind": "wasm"}]

        cmd_collect_client_logs(_args(kind="prod-uploaders"))

        out_dir = collect_env.tmp_path / "state/client-test-forensics/STG-WUL-01"
        assert json.loads((out_dir / SUMMARY_FILE_NAME).read_text())["client_kind"] == "wasm"

    def test_no_client_ips_is_an_error(self, collect_env):
        collect_env.clients = [{"role": "upload"}]

        with pytest.raises(SystemExit) as excinfo:
            cmd_collect_client_logs(_args())

        assert excinfo.value.code != 0
        assert collect_env.commands == []

    def test_nothing_is_written_into_the_cwd(self, collect_env):
        cmd_collect_client_logs(_args())

        assert list(Path.cwd().iterdir()) == []

    def test_the_ssh_key_is_passed_to_every_invocation(self, collect_env):
        cmd_collect_client_logs(_args(ssh_key_path="~/.ssh/ant_deploy"))

        assert collect_env.commands
        for argv in collect_env.commands:
            assert argv[0] == "ssh"
            assert argv[1] == "-i"
            assert argv[2] == str(Path("~/.ssh/ant_deploy").expanduser())
