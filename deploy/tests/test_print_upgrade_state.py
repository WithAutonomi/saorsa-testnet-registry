"""`deployments print-upgrade-state` returns the arming fields and no credential.

The command exists so the release train's monitor-upgrade arming can be a CLI
read rather than a Python import, because an import does not go through the
CLI's env-file load and so reads a *local* Postgres while reporting plausible
state. These tests pin the two things a caller then depends on: the shape of
the JSON, and that nothing secret can appear in it.

The credential test is the load-bearing one, and it is a test of the mechanism
rather than of today's data: client state is a free-form dict (an uploader's
entry carries `wallet_secret_key` wherever `update_client_wallet` has run), and
the whole point of a new command rather than `deployments print` — which emits
the deployment's InfluxDB token — is that this output is pasted into a Linear
issue. So the fixtures carry secrets the real prod kinds do not, which is
exactly what an allowlist has to survive.
"""

import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd import print_upgrade_state as pus
from saorsa_deploy.cmd.print_upgrade_state import cmd_print_upgrade_state

WALLET_SECRET = "0xd3adb33fWALLETSECRETd0n0tpr1nt"
INFLUX_TOKEN = "influx-token-d0n0tpr1nt"

_CONTINUOUS_UPLOADER_ARGS = {
    "kind": "prod-continuous-uploader",
    "client_kind": "wasm",
    "ant_binary": {"kind": "release", "version": "0.3.12"},
    "wasm_build": {
        "commit": "abc123def456",
        "ref": "ant-cli-v0.3.12-rc.1",
        "repo_owner": "WithAutonomi",
        "features": "browser-wasm",
    },
    # Not written by any kind today, and that is the point: a secret that
    # appears in `deploy_args` tomorrow must not reach the output, because the
    # object is never passed through whole.
    "influx_token": INFLUX_TOKEN,
}

_NATIVE_DOWNLOADER_ARGS = {
    "kind": "prod-continuous-downloader",
    "ant_binary": {"kind": "release", "version": "0.3.11"},
}


class _FakeRepo:
    """Only the three reads the command makes."""

    def __init__(self, active=None, by_name=None, by_id=None):
        self._active = active
        self._by_name = by_name
        self._by_id = by_id or {}
        self.active_calls = []
        self.by_name_calls = []
        self.get_calls = []

    def get_active_by_name(self, name):
        self.active_calls.append(name)
        return self._active

    def get_by_name(self, name):
        self.by_name_calls.append(name)
        return self._by_name

    def get(self, deployment_id):
        self.get_calls.append(deployment_id)
        return self._by_id.get(deployment_id)


def _event(event_type, day, details=None, event_id=1):
    return SimpleNamespace(
        id=event_id,
        event_type=event_type,
        created_at=datetime(2026, 10, day, 12, 0, tzinfo=timezone.utc),
        details=details,
    )


def _deployment(name, deployment_id, deploy_args, events=None, status="running"):
    return SimpleNamespace(
        id=deployment_id,
        name=name,
        status=status,
        deploy_args=dict(deploy_args),
        saorsa_deploy_version="1.11.0",
        events=events or [],
    )


def _args(**overrides):
    base = dict(name=None, deployment_id=None, json_out=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def _run(repo, args, clients, capsys, client_state_error=None):
    """Run the command and return `(stdout, parsed JSON or None)`."""
    if client_state_error is None:
        state = {"clients": clients}
        loader = patch.object(pus, "load_client_state", return_value=state)
    else:
        loader = patch.object(
            pus, "load_client_state", side_effect=RuntimeError(client_state_error)
        )
    with patch.object(pus, "get_repository", return_value=repo), loader:
        cmd_print_upgrade_state(args)
    out = capsys.readouterr().out
    parsed = json.loads(out) if args.json_out in ("-", "/dev/stdout") else None
    return out, parsed


# ---------------------------------------------------------------------------
# A native multi-unit deployment
# ---------------------------------------------------------------------------


def test_native_multi_unit_deployment_names_a_unit_per_role(capsys):
    """PROD-UL-01's shape: an uploader VM plus verifiers on a different unit.

    The unit comes from `prod_client_units`, so the two roles land on the two
    services that module maps them to — which is what `upgrade-ant-binary` and
    `collect-client-logs` act on.
    """
    deployment = _deployment(
        "PROD-UL-01",
        672,
        {**_CONTINUOUS_UPLOADER_ARGS, "client_kind": "native", "wasm_build": None},
    )
    clients = [
        {"ip": "1.1.1.1", "role": "upload", "region": "lon1", "client_index": 1},
        {"ip": "2.2.2.2", "role": "download", "region": "gra9", "client_index": 1},
        {"ip": "3.3.3.3", "role": "download", "region": "syd1", "client_index": 2},
    ]

    _, report = _run(
        _FakeRepo(active=deployment), _args(name="PROD-UL-01", json_out="-"), clients, capsys
    )

    assert report["id"] == 672
    assert report["status"] == "running"
    assert report["kind"] == "prod-continuous-uploader"
    assert report["client_kind"] == "native"
    assert report["ant_binary"] == {"kind": "release", "version": "0.3.12"}
    assert report["wasm_build"] is None
    assert [c["unit"] for c in report["clients"]] == [
        "ant-prod-continuous-uploader",
        "ant-prod-upload-verifier",
        "ant-prod-upload-verifier",
    ]
    assert [c["is_upload"] for c in report["clients"]] == [True, False, False]
    assert [c["ip"] for c in report["clients"]] == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]
    # region + client_index are what tell two VMs of one role apart, since no
    # VM name is returned.
    assert [c["region"] for c in report["clients"]] == ["lon1", "gra9", "syd1"]
    assert [c["client_index"] for c in report["clients"]] == [1, 1, 2]


def test_a_spec_with_no_ip_is_reported_rather_than_dropped(capsys):
    """A VM that was specified and never created is the arming's problem."""
    deployment = _deployment("PROD-UL-01", 672, _CONTINUOUS_UPLOADER_ARGS)
    clients = [
        {"ip": "1.1.1.1", "role": "upload", "region": "lon1", "client_index": 1},
        {"ip": None, "role": "download", "region": "syd1", "client_index": 1},
    ]

    _, report = _run(
        _FakeRepo(active=deployment), _args(name="PROD-UL-01", json_out="-"), clients, capsys
    )

    assert len(report["clients"]) == 2
    assert report["clients"][1]["ip"] is None
    assert report["clients"][1]["unit"] == "ant-prod-upload-verifier"


# ---------------------------------------------------------------------------
# A WASM deployment
# ---------------------------------------------------------------------------


def test_wasm_deployment_reports_the_whole_bundle_descriptor(capsys):
    """PROD-WUL-01: the transport comes from `deploy_args`, and `wasm_build`
    travels whole, `features` included — the caller transcribes the commit."""
    deployment = _deployment("PROD-WUL-01", 673, _CONTINUOUS_UPLOADER_ARGS)
    clients = [
        {"ip": "1.1.1.1", "role": "upload", "region": "lon1", "client_index": 1},
        {"ip": "2.2.2.2", "role": "download", "region": "fra1", "client_index": 1},
    ]

    _, report = _run(
        _FakeRepo(active=deployment), _args(name="PROD-WUL-01", json_out="-"), clients, capsys
    )

    assert report["client_kind"] == "wasm"
    assert report["wasm_build"]["commit"] == "abc123def456"
    assert report["wasm_build"]["features"] == "browser-wasm"
    assert report["wasm_build"]["repo_owner"] == "WithAutonomi"
    # The upload unit is the one running the bundle on this kind; the verifier
    # stays native. The flag says which is which without this command
    # re-deriving a transport rule the upgrade commands already own.
    assert [c["is_upload"] for c in report["clients"]] == [True, False]


def test_a_deployment_with_no_wasm_build_reports_null_not_an_absent_key(capsys):
    """A missing key would make a caller's `jq` emit `null` anyway, but an
    explicit null says "nothing recorded" rather than "field not supported"."""
    deployment = _deployment("PROD-DL-01", 674, _NATIVE_DOWNLOADER_ARGS)

    _, report = _run(
        _FakeRepo(active=deployment),
        _args(name="PROD-DL-01", json_out="-"),
        [{"ip": "9.9.9.9", "role": "download", "region": "lon1", "client_index": 1}],
        capsys,
    )

    assert "wasm_build" in report
    assert report["wasm_build"] is None
    assert report["client_kind"] is None
    assert report["clients"][0]["unit"] == "ant-prod-downloader"


# ---------------------------------------------------------------------------
# Upgrade events
# ---------------------------------------------------------------------------


def test_the_newest_event_of_each_type_is_reported_newest_first(capsys):
    """Per type, not the newest N: a failure and a success are different facts
    and a reader needs both timestamps to order them."""
    events = [
        _event("created", 1, event_id=1),
        _event("ant_binary_upgrade_started", 2, {"binary_url": "https://x/ant"}, event_id=2),
        _event("ant_binary_upgrade_completed", 2, {"binary_url": "https://x/ant"}, event_id=3),
        _event("wasm_client_upgrade_failed", 3, {"failed_hosts": ["1.1.1.1"]}, event_id=4),
        _event("wasm_client_upgraded", 4, {"new_commit": "deadbeef"}, event_id=5),
        # An older duplicate of a type already seen must not displace it.
        _event("wasm_client_upgraded", 1, {"new_commit": "0ldc0mm1t"}, event_id=6),
    ]
    deployment = _deployment("PROD-WUL-01", 673, _CONTINUOUS_UPLOADER_ARGS, events=events)

    _, report = _run(
        _FakeRepo(active=deployment),
        _args(name="PROD-WUL-01", json_out="-"),
        [{"ip": "1.1.1.1", "role": "upload", "region": "lon1", "client_index": 1}],
        capsys,
    )

    reported = [e["event_type"] for e in report["upgrade_events"]]
    assert reported == [
        "wasm_client_upgraded",
        "wasm_client_upgrade_failed",
        "ant_binary_upgrade_completed",
        "ant_binary_upgrade_started",
    ]
    # `created` is not an upgrade event, so it is absent entirely.
    assert "created" not in reported
    newest = report["upgrade_events"][0]
    assert newest["details"] == {"new_commit": "deadbeef"}
    assert newest["created_at"] == "2026-10-04T12:00:00+00:00"


def test_a_deployment_with_no_upgrade_events_yet_reports_an_empty_list(capsys):
    """Emitted, not omitted — "never upgraded" is an answer the arming uses."""
    deployment = _deployment(
        "PROD-WDL-01",
        675,
        {**_NATIVE_DOWNLOADER_ARGS, "client_kind": "wasm"},
        events=[_event("created", 1)],
    )

    out, report = _run(
        _FakeRepo(active=deployment),
        _args(name="PROD-WDL-01", json_out="-"),
        [{"ip": "8.8.8.8", "role": "download", "region": "lon1", "client_index": 1}],
        capsys,
    )

    assert report["upgrade_events"] == []
    assert out.count("{") >= 1


# ---------------------------------------------------------------------------
# No credential, ever
# ---------------------------------------------------------------------------


def test_no_client_state_or_deploy_args_secret_reaches_the_output(capsys):
    """The reason this is a new command and not `deployments print`.

    Both outputs are checked: the JSON a caller parses and the human tables an
    operator reads, since either could be pasted into an issue.
    """
    events = [
        _event(
            "wasm_client_upgraded",
            4,
            {"new_commit": "deadbeef", "influx_token": INFLUX_TOKEN},
        )
    ]
    deployment = _deployment("PROD-WUL-01", 673, _CONTINUOUS_UPLOADER_ARGS, events=events)
    clients = [
        {
            "ip": "1.1.1.1",
            "role": "upload",
            "region": "lon1",
            "client_index": 1,
            "wallet_address": "0xpublic",
            "wallet_secret_key": WALLET_SECRET,
        }
    ]
    repo = _FakeRepo(active=deployment)

    json_text, report = _run(repo, _args(name="PROD-WUL-01", json_out="-"), clients, capsys)
    human_text, _ = _run(repo, _args(name="PROD-WUL-01"), clients, capsys)

    for text in (json_text, human_text):
        assert WALLET_SECRET not in text
        assert INFLUX_TOKEN not in text
    # The allowlist is what keeps it out, so the key itself is absent too.
    assert "wallet_secret_key" not in json_text
    assert report["clients"][0]["ip"] == "1.1.1.1"
    # A secret-looking detail key is redacted rather than dropped, so a reader
    # can see that the event carried one.
    assert report["upgrade_events"][0]["details"]["influx_token"] == pus.REDACTED
    assert report["upgrade_events"][0]["details"]["new_commit"] == "deadbeef"
    # The allowlist again: a `deploy_args` key nobody named is not a field.
    assert "influx_token" not in report


def test_the_command_never_reads_monitoring_state():
    """A regression guard on the mechanism, not on one output.

    `deployments print` leaks the InfluxDB token because it renders monitoring
    state. This command must not acquire a reason to load it — the credential
    property then survives a later edit that adds a field.
    """
    import inspect

    source = inspect.getsource(pus)
    assert "load_monitoring_state" not in source
    assert "load_elastic_state" not in source


# ---------------------------------------------------------------------------
# Resolution and refusals
# ---------------------------------------------------------------------------


def test_name_follows_the_live_row_and_falls_back_once_destroyed(capsys):
    live = _deployment("PROD-WUL-01", 673, _CONTINUOUS_UPLOADER_ARGS)
    old = _deployment("PROD-WUL-01", 611, _CONTINUOUS_UPLOADER_ARGS, status="destroyed")
    clients = [{"ip": "1.1.1.1", "role": "upload", "region": "lon1", "client_index": 1}]

    repo = _FakeRepo(active=live, by_name=old)
    _, report = _run(repo, _args(name="PROD-WUL-01", json_out="-"), clients, capsys)
    assert report["id"] == 673
    assert repo.by_name_calls == []

    repo = _FakeRepo(active=None, by_name=old)
    _, report = _run(repo, _args(name="PROD-WUL-01", json_out="-"), clients, capsys)
    assert report["id"] == 611
    assert report["status"] == "destroyed"


def test_id_reads_that_exact_row(capsys):
    old = _deployment("PROD-WUL-01", 611, _CONTINUOUS_UPLOADER_ARGS, status="destroyed")
    repo = _FakeRepo(
        active=_deployment("PROD-WUL-01", 673, _CONTINUOUS_UPLOADER_ARGS), by_id={611: old}
    )

    _, report = _run(
        repo,
        _args(deployment_id=611, json_out="-"),
        [{"ip": "1.1.1.1", "role": "upload", "region": "lon1", "client_index": 1}],
        capsys,
    )

    assert repo.get_calls == [611]
    assert repo.active_calls == []
    assert report["id"] == 611


def test_missing_client_state_is_reported_and_still_exits_0(capsys):
    """A destroyed deployment keeps its versions and its upgrade history, and
    `clients: []` beside the reason says why there are no VMs."""
    deployment = _deployment("PROD-WUL-01", 611, _CONTINUOUS_UPLOADER_ARGS, status="destroyed")

    _, report = _run(
        _FakeRepo(active=None, by_name=deployment),
        _args(name="PROD-WUL-01", json_out="-"),
        [],
        capsys,
        client_state_error="No client state found for 'PROD-WUL-01'.",
    )

    assert report["clients"] == []
    assert "No client state found" in report["client_state_error"]


def test_an_unreadable_client_state_refuses(capsys):
    """ "Could not look" is not "nothing there": an empty VM list taken from a
    transport error reads as a deployment with nothing to upgrade."""
    deployment = _deployment("PROD-WUL-01", 673, _CONTINUOUS_UPLOADER_ARGS)

    with (
        patch.object(pus, "get_repository", return_value=_FakeRepo(active=deployment)),
        patch.object(pus, "load_client_state", side_effect=OSError("connection reset by peer")),
    ):
        with pytest.raises(SystemExit) as exc:
            cmd_print_upgrade_state(_args(name="PROD-WUL-01", json_out="-"))

    assert exc.value.code == 1
    captured = capsys.readouterr()
    # The refusal goes to stderr, so stdout stays parseable (here: empty).
    assert captured.out == ""
    assert "connection reset" in captured.err


def test_a_local_kind_is_refused_by_name(capsys):
    deployment = _deployment("LOCAL-01", 700, {"kind": "prod-local-uploads"})

    with patch.object(pus, "get_repository", return_value=_FakeRepo(active=deployment)):
        with pytest.raises(SystemExit) as exc:
            cmd_print_upgrade_state(_args(name="LOCAL-01"))

    assert exc.value.code == 1
    assert "prod-local-uploads" in capsys.readouterr().err


def test_a_kind_with_no_known_units_is_refused_rather_than_half_answered(capsys):
    """Guessing the downloader unit for a testnet would name a service that
    does not exist on its VMs, which is the failure `prod_client_units` exists
    to prevent."""
    deployment = _deployment("DEV-01", 701, {"kind": "testnet"})

    with patch.object(pus, "get_repository", return_value=_FakeRepo(active=deployment)):
        with pytest.raises(SystemExit) as exc:
            cmd_print_upgrade_state(_args(name="DEV-01"))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "testnet" in err
    assert "prod-continuous-uploader" in err


def test_no_registry_refuses_naming_database_url(capsys):
    with patch.object(pus, "get_repository", return_value=None):
        with pytest.raises(SystemExit) as exc:
            cmd_print_upgrade_state(_args(name="PROD-WUL-01"))

    assert exc.value.code == 1
    assert "DATABASE_URL" in capsys.readouterr().err


def test_an_unknown_name_refuses_naming_the_name(capsys):
    with patch.object(pus, "get_repository", return_value=_FakeRepo()):
        with pytest.raises(SystemExit) as exc:
            cmd_print_upgrade_state(_args(name="NO-SUCH-DEPLOYMENT"))

    assert exc.value.code == 1
    assert "NO-SUCH-DEPLOYMENT" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The parser — built inside `main()`, so exercised through the CLI
# ---------------------------------------------------------------------------


def test_the_parser_registers_both_sources_and_json_out():
    result = subprocess.run(
        ["uv", "run", "saorsa-deploy", "deployments", "print-upgrade-state", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--name" in result.stdout
    assert "--id" in result.stdout
    assert "--json-out" in result.stdout


def test_the_parser_rejects_both_at_once():
    result = subprocess.run(
        [
            "uv",
            "run",
            "saorsa-deploy",
            "deployments",
            "print-upgrade-state",
            "--name",
            "PROD-WUL-01",
            "--id",
            "673",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "not allowed with argument" in result.stderr or "mutually exclusive" in result.stderr


def test_the_parser_rejects_neither():
    result = subprocess.run(
        ["uv", "run", "saorsa-deploy", "deployments", "print-upgrade-state"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "one of the arguments" in result.stderr
