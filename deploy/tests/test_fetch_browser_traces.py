"""`clients fetch-browser-traces`, and the role config the report depends on.

The traces are the dataset the browser role exists to produce and they are
deleted with the VM, so the two things worth pinning are that an incomplete
archive cannot exit 0, and that a trace is linked to the round it came from
without the command ever inventing a row for one that has not been recorded yet.

The role config (`deploy_args.browser_downloaders`) is in the same file because
it is the other half of surviving teardown: the S3 client state that carries the
round period is deleted by `clients destroy`, and `analysis browser-reads` reads
Postgres alone by design.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.browser_records import (
    BROWSER_CONFIG_KEY,
    client_hostname,
    collect_browser_config,
    expected_instances,
    extension_by_host,
    record_browser_config,
    recorded_browser_config,
    round_period_from_config,
)
from saorsa_deploy.cmd.fetch_browser_traces import (
    browser_vms,
    cmd_fetch_browser_traces,
    local_traces_dir,
    parse_trace_name,
    trace_s3_key,
)

BROWSER = {
    "role": "browser-download",
    "region": "lon1",
    "client_index": 1,
    "instances": 4,
    "round_period_secs": 600,
    "download_timeout_secs": 300,
    "ip": "203.0.113.5",
}
NATIVE = {"role": "download", "region": "lon1", "client_index": 1, "ip": "203.0.113.9"}

EXTENSION_TIERS = [
    (None, {"clients": [NATIVE]}),
    ("browsers", {"clients": [dict(BROWSER)]}),
]

HOST = "DEV-03-browsers-ant-client-browser-download-lon1-1"


# ---------------------------------------------------------------------------
# The recorded role config
# ---------------------------------------------------------------------------


def test_the_hostname_matches_the_terraform_module_naming():
    """An extension's VMs are named `<deployment>-<extension>-ant-client-...`,
    which is the hostname the Influx `host` tag carries."""
    assert client_hostname("DEV-03", "browsers", BROWSER) == HOST
    assert client_hostname("DEV-03", None, BROWSER) == "DEV-03-ant-client-browser-download-lon1-1"


def test_only_browser_vms_are_collected():
    """A native downloader has no round period and no instances, so recording it
    here would give the report a VM whose expected instance count is nothing."""
    entries = collect_browser_config("DEV-03", EXTENSION_TIERS)
    assert [e["hostname"] for e in entries] == [HOST]
    assert entries[0]["extension"] == "browsers"
    assert entries[0]["instances"] == 4
    assert entries[0]["round_period_secs"] == 600


def test_the_config_is_sorted_so_two_provisions_write_the_same_bytes():
    tiers = [
        ("zulu", {"clients": [dict(BROWSER, client_index=2)]}),
        ("alpha", {"clients": [dict(BROWSER)]}),
    ]
    entries = collect_browser_config("DEV-03", tiers)
    assert [e["extension"] for e in entries] == ["alpha", "zulu"]
    assert collect_browser_config("DEV-03", list(reversed(tiers))) == entries


def test_no_ip_is_recorded_in_the_config():
    """The document outlives the VM, and an address released back to the
    provider and reassigned is worse than no address at all."""
    assert "ip" not in collect_browser_config("DEV-03", EXTENSION_TIERS)[0]


def test_nothing_is_written_for_a_deployment_with_no_browser_vms():
    """An ordinary testnet's deploy_args must be untouched."""
    with (
        patch(
            "saorsa_deploy.browser_records.client_tiers_with_extensions",
            return_value=[(None, {"clients": [NATIVE]})],
        ),
        patch("saorsa_deploy.registry.record_deploy_args_update") as update,
    ):
        assert record_browser_config("DEV-03") == []
    update.assert_not_called()


def test_the_config_is_merged_into_deploy_args():
    with (
        patch(
            "saorsa_deploy.browser_records.client_tiers_with_extensions",
            return_value=EXTENSION_TIERS,
        ),
        patch("saorsa_deploy.registry.record_deploy_args_update") as update,
    ):
        entries = record_browser_config("DEV-03")
    assert len(entries) == 1
    update.assert_called_once_with("DEV-03", {BROWSER_CONFIG_KEY: entries})


def test_the_round_period_is_read_back_from_the_config():
    entries = collect_browser_config("DEV-03", EXTENSION_TIERS)
    assert round_period_from_config(entries) == 600
    assert expected_instances(entries) == 4


def test_vms_on_different_periods_are_not_one_cohort():
    """Every instance derives its round id from the wall clock, so two VMs on
    different periods never read the same file at the same moment. None, rather
    than one of the two — the report says so and derives it from the rows."""
    entries = collect_browser_config(
        "DEV-03",
        [
            ("a", {"clients": [dict(BROWSER)]}),
            ("b", {"clients": [dict(BROWSER, round_period_secs=300)]}),
        ],
    )
    assert round_period_from_config(entries) is None


def test_expected_instances_sums_across_vms():
    entries = collect_browser_config(
        "DEV-03",
        [
            ("a", {"clients": [dict(BROWSER)]}),
            ("b", {"clients": [dict(BROWSER, instances=2)]}),
        ],
    )
    assert expected_instances(entries) == 6


def test_expected_instances_is_unknown_without_a_config():
    """None, never 0: a round compared against a zero expectation would be
    flagged on every deployment that recorded nothing."""
    assert expected_instances([]) is None
    assert round_period_from_config([]) is None


def test_recorded_config_tolerates_a_deploy_args_that_is_not_a_dict():
    assert recorded_browser_config(None) == []
    assert recorded_browser_config({"browser_downloaders": "nonsense"}) == []
    assert recorded_browser_config({"browser_downloaders": [{"hostname": "h"}, 7]}) == [
        {"hostname": "h"}
    ]


def test_only_extension_hosts_are_in_the_extension_map():
    """The deployment's own VMs carry NULL in the column, and a host the scan
    could not see must read as "not recorded" rather than as the deployment's
    own."""
    assert extension_by_host(EXTENSION_TIERS, "DEV-03") == {HOST: "browsers"}


# ---------------------------------------------------------------------------
# Resolving the VMs and the keys
# ---------------------------------------------------------------------------


def test_the_vms_come_from_the_client_state_with_their_addresses():
    """Not the local inventory: a browser cohort is normally an extension added
    hours after `up` wrote it, so the inventory would not name its VMs."""
    with patch(
        "saorsa_deploy.cmd.fetch_browser_traces.client_tiers_with_extensions",
        return_value=EXTENSION_TIERS,
    ):
        vms = browser_vms("DEV-03")
    assert vms == [
        {
            "hostname": HOST,
            "extension": "browsers",
            "ip": "203.0.113.5",
            "region": "lon1",
        }
    ]


def test_the_s3_key_names_the_round_and_instance():
    assert trace_s3_key("DEV-03", HOST, 2987191, 3) == (
        f"saorsa-deploy/results/DEV-03/browser-traces/{HOST}/2987191-3.jsonl"
    )


@pytest.mark.parametrize(
    "name, expected",
    [
        ("2987191-3.jsonl", (2987191, 3)),
        ("0-1.jsonl", (0, 1)),
        ("2987191-3.jsonl.gz", None),
        ("latest.jsonl", None),
        ("2987191.jsonl", None),
    ],
)
def test_only_the_harness_naming_is_recognised(name, expected):
    """The file name is how a trace is matched to its round, so anything else in
    the directory is ignored rather than uploaded under a guessed key."""
    assert parse_trace_name(name) == expected


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


class _Repo:
    """A registry that knows one round, so one trace links and one does not."""

    def __init__(self, known=((2987191, 1),)):
        self.deployment = SimpleNamespace(id=701, name="DEV-03")
        self.known = set(known)
        self.linked = {}

    def get_active_by_name(self, name):
        return self.deployment

    def get_by_name(self, name):
        return self.deployment

    def set_browser_download_trace_key(
        self, deployment_id, client_hostname, round_id, instance, key
    ):
        if (round_id, instance) not in self.known:
            return False
        self.linked[(client_hostname, round_id, instance)] = key
        return True


def _args(**extra):
    args = SimpleNamespace(name="DEV-03", ssh_key_path="~/.ssh/ant_deploy", ssh_user="root")
    for key, value in extra.items():
        setattr(args, key, value)
    return args


def _fake_scp(names, fail_for=()):
    """An scp that writes the given trace names, or fails for a host's IP."""

    def run(cmd, **kwargs):
        remote, target = cmd[-2], cmd[-1]
        if any(ip in remote for ip in fail_for):
            return SimpleNamespace(returncode=1, stderr="Connection timed out")
        from pathlib import Path

        for name in names:
            Path(target, name).write_text('{"header": true}\n')
        return SimpleNamespace(returncode=0, stderr="")

    return run


def _run_command(args, repo, scp, s3=None, tiers=EXTENSION_TIERS):
    uploaded = []

    class _S3:
        def upload_file(self, path, bucket, key):
            uploaded.append((bucket, key))

    with (
        patch(
            "saorsa_deploy.cmd.fetch_browser_traces.client_tiers_with_extensions",
            return_value=tiers,
        ),
        patch("saorsa_deploy.cmd.fetch_browser_traces.get_repository", return_value=repo),
        patch("saorsa_deploy.cmd.fetch_browser_traces.subprocess.run", side_effect=scp),
        patch(
            "saorsa_deploy.cmd.fetch_browser_traces.boto3.client",
            return_value=s3 or _S3(),
        ),
    ):
        try:
            cmd_fetch_browser_traces(args)
            return 0, uploaded
        except SystemExit as e:
            return e.code, uploaded


def test_each_trace_is_uploaded_and_linked_to_its_round():
    repo = _Repo(known=((2987191, 1), (2987191, 2)))
    code, uploaded = _run_command(_args(), repo, _fake_scp(["2987191-1.jsonl", "2987191-2.jsonl"]))
    assert code == 0
    assert sorted(key for _, key in uploaded) == [
        f"saorsa-deploy/results/DEV-03/browser-traces/{HOST}/2987191-1.jsonl",
        f"saorsa-deploy/results/DEV-03/browser-traces/{HOST}/2987191-2.jsonl",
    ]
    assert repo.linked[(HOST, 2987191, 2)].endswith("2987191-2.jsonl")


def test_a_trace_with_no_recorded_round_is_reported_not_invented():
    """Doing it the other way round would mean a row with no status, no duration
    and no first reads. The next `record-results` writes the row, and its upsert
    preserves a trace_s3_key that is already set."""
    repo = _Repo(known=((2987191, 1),))
    code, uploaded = _run_command(_args(), repo, _fake_scp(["2987191-1.jsonl", "2987192-1.jsonl"]))
    assert code == 0
    # Both are archived; only the recorded one is linked.
    assert len(uploaded) == 2
    assert list(repo.linked) == [(HOST, 2987191, 1)]


def test_a_file_the_harness_did_not_name_is_not_uploaded():
    repo = _Repo()
    _, uploaded = _run_command(_args(), repo, _fake_scp(["2987191-1.jsonl", "notes.txt"]))
    assert len(uploaded) == 1


def test_an_unreachable_vm_exits_two():
    """The command runs while the VMs are up, before `clients down`. A partial
    archive that exited 0 would be indistinguishable from a complete one, and
    after teardown there is no way to tell and no way to go back."""
    repo = _Repo()
    code, uploaded = _run_command(
        _args(), repo, _fake_scp(["2987191-1.jsonl"], fail_for=("203.0.113.5",))
    )
    assert code == 2
    assert uploaded == []


def test_a_vm_with_no_recorded_ip_exits_two():
    repo = _Repo()
    tiers = [("browsers", {"clients": [dict(BROWSER, ip="")]})]
    code, _ = _run_command(_args(), repo, _fake_scp([]), tiers=tiers)
    assert code == 2


def test_a_deployment_with_no_browser_vms_is_a_no_op():
    repo = _Repo()
    code, uploaded = _run_command(
        _args(), repo, _fake_scp([]), tiers=[(None, {"clients": [NATIVE]})]
    )
    assert code == 0
    assert uploaded == []


def test_without_a_registry_the_traces_are_still_fetched():
    """Getting the evidence off the VM before it is destroyed matters more than
    the pointer to it, so a missing DATABASE_URL warns rather than refusing."""
    code, uploaded = _run_command(_args(), None, _fake_scp(["2987191-1.jsonl"]))
    assert code == 0
    assert len(uploaded) == 1


def test_a_re_run_is_idempotent():
    """A completed round's trace never changes, so re-uploading it is a no-op in
    content — which is what lets the command be run again after more rounds."""
    repo = _Repo(known=((2987191, 1),))
    scp = _fake_scp(["2987191-1.jsonl"])
    first_code, first = _run_command(_args(), repo, scp)
    second_code, second = _run_command(_args(), repo, scp)
    assert (first_code, second_code) == (0, 0)
    assert first == second


def test_the_local_copy_sits_beside_the_inventory(local_inventory_root):
    """So the test suite's redirection of that root covers it, and an operator
    finds the traces where every other artefact of a deployment lives."""
    path = local_traces_dir("DEV-03", HOST)
    assert path == local_inventory_root / "DEV-03" / "browser-traces" / HOST
