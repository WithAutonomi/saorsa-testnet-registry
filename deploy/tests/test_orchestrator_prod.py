"""Orchestrator tests for the prod-* kinds.

The uploaders/downloaders cases reproduce the real reference runbooks
(``runbooks/PROD_UL_01-deploy.sh`` and ``runbooks/PROD_DL_01-versioned-deploy.sh``).
"""

from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator import build_deploy_steps, build_destroy_steps

SSH = "/ssh/key"


def _argvs(steps):
    return [s.argv for s in steps]


# --------------------------------------------------------------------------- #
# prod-uploaders — reproduces runbooks/PROD_UL_01-deploy.sh
# --------------------------------------------------------------------------- #
def test_prod_uploaders_matches_runbook():
    m = Manifest.model_validate(
        {
            "name": "PROD-UL-01",
            "kind": "prod-uploaders",
            "purpose": "weather check",
            "ssh_key_path": SSH,
            "prod_uploaders": {
                "binary": {"version": "0.2.4"},
                "uploaders": [
                    {
                        "region": "lon1",
                        "vm_size": "s-2vcpu-4gb",
                        "urls": "/home/chris/downloads/panama_racing_links11.txt",
                        "payment": "merkle",
                    }
                ],
            },
        }
    )
    assert _argvs(build_deploy_steps(m)) == [
        [
            "prod",
            "uploaders",
            "deploy",
            "--name",
            "PROD-UL-01",
            "--purpose",
            "weather check",
            "--uploader",
            "lon1:s-2vcpu-4gb:urls=/home/chris/downloads/panama_racing_links11.txt:payment=merkle",
            "--ant-version",
            "0.2.4",
            "--ssh-key-path",
            SSH,
        ]
    ]
    # destroy: no --force; default tears down monitoring
    assert _argvs(build_destroy_steps(m)) == [
        ["prod", "uploaders", "destroy", "--name", "PROD-UL-01", "--ssh-key-path", SSH]
    ]


def test_prod_uploaders_clock_skew_appends_token():
    m = Manifest.model_validate(
        {
            "name": "U",
            "kind": "prod-uploaders",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_uploaders": {
                "binary": {"version": "1"},
                "uploaders": [
                    {
                        "region": "lon1",
                        "vm_size": "s-2vcpu-4gb",
                        "urls": "/a.txt",
                        "payment": "merkle",
                        "clock_skew": True,
                    }
                ],
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    spec = argv[argv.index("--uploader") + 1]
    assert spec == "lon1:s-2vcpu-4gb:urls=/a.txt:payment=merkle:clock_skew=true"


def test_prod_uploaders_clock_skew_omitted_by_default():
    m = Manifest.model_validate(
        {
            "name": "U",
            "kind": "prod-uploaders",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_uploaders": {
                "binary": {"version": "1"},
                "uploaders": [{"region": "lon1", "vm_size": "s-2vcpu-4gb", "urls": "/a.txt"}],
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    spec = argv[argv.index("--uploader") + 1]
    assert "clock_skew" not in spec


def test_prod_uploaders_slack_post_appends_final_step():
    m = Manifest.model_validate(
        {
            "name": "PROD-UL-01",
            "kind": "prod-uploaders",
            "purpose": "p",
            "ssh_key_path": SSH,
            "slack_post": True,
            "prod_uploaders": {
                "binary": {"version": "1"},
                "uploaders": [{"region": "lon1", "vm_size": "s-2vcpu-4gb", "urls": "/a.txt"}],
            },
        }
    )
    steps = build_deploy_steps(m)
    assert steps[-1].argv == ["deployments", "slack-post", "--name", "PROD-UL-01"]
    assert steps[-1].best_effort is True  # a Slack failure must not fail the run
    # only one slack-post step, and the deploy step still leads
    assert sum(s.argv[:2] == ["deployments", "slack-post"] for s in steps) == 1
    assert steps[0].argv[:3] == ["prod", "uploaders", "deploy"]
    # destroy is unaffected by slack_post
    assert all(s.argv[:2] != ["deployments", "slack-post"] for s in build_destroy_steps(m))


def test_prod_uploaders_no_slack_post_by_default():
    m = Manifest.model_validate(
        {
            "name": "U",
            "kind": "prod-uploaders",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_uploaders": {
                "binary": {"version": "1"},
                "uploaders": [{"region": "lon1", "vm_size": "s-2vcpu-4gb", "urls": "/a.txt"}],
            },
        }
    )
    assert all(s.argv[:2] != ["deployments", "slack-post"] for s in build_deploy_steps(m))


def test_prod_uploaders_keep_monitoring_emits_flag():
    m = Manifest.model_validate(
        {
            "name": "U",
            "kind": "prod-uploaders",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_uploaders": {
                "binary": {"version": "1"},
                "keep_monitoring": True,
                "uploaders": [{"region": "lon1", "vm_size": "s-2vcpu-4gb", "urls": "/a.txt"}],
            },
        }
    )
    assert "--keep-monitoring" in build_destroy_steps(m)[0].argv


# --------------------------------------------------------------------------- #
# prod-downloaders — reproduces runbooks/PROD_DL_01-versioned-deploy.sh
# --------------------------------------------------------------------------- #
def test_prod_downloaders_matches_runbook():
    m = Manifest.model_validate(
        {
            "name": "PROD-DL-01",
            "kind": "prod-downloaders",
            "purpose": "reproduce timeouts",
            "ssh_key_path": SSH,
            "prod_downloaders": {
                "spec": "lon1:s-1vcpu-2gb",
                "addresses_path": "/home/chris/downloads/canary02-addresses.txt",
                "binary": {"version": "0.2.4"},
            },
        }
    )
    assert _argvs(build_deploy_steps(m)) == [
        [
            "prod",
            "downloaders",
            "deploy",
            "--name",
            "PROD-DL-01",
            "--purpose",
            "reproduce timeouts",
            "--spec",
            "lon1:s-1vcpu-2gb",
            "--addresses-path",
            "/home/chris/downloads/canary02-addresses.txt",
            "--ant-version",
            "0.2.4",
            "--ssh-key-path",
            SSH,
        ]
    ]


def test_prod_downloaders_clock_skew_appends_flag():
    m = Manifest.model_validate(
        {
            "name": "PROD-DL-01",
            "kind": "prod-downloaders",
            "purpose": "test clock skew",
            "ssh_key_path": SSH,
            "prod_downloaders": {
                "spec": "lon1:s-2vcpu-4gb",
                "addresses_path": "/a.txt",
                "binary": {"version": "0.2.6-rc.2"},
                "clock_skew": True,
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert "--clock-skew" in argv


def test_prod_downloaders_clock_skew_omitted_by_default():
    m = Manifest.model_validate(
        {
            "name": "PROD-DL-01",
            "kind": "prod-downloaders",
            "purpose": "no skew",
            "ssh_key_path": SSH,
            "prod_downloaders": {
                "spec": "lon1",
                "binary": {"version": "0.2.4"},
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert "--clock-skew" not in argv


def test_prod_continuous_downloader_clock_skew_appends_flag():
    m = Manifest.model_validate(
        {
            "name": "CD",
            "kind": "prod-continuous-downloader",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_continuous_downloader": {
                "spec": "lon1",
                "binary": {"version": "0.2.4"},
                "clock_skew": True,
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert "--clock-skew" in argv


def test_prod_continuous_downloader_sleep_interval():
    m = Manifest.model_validate(
        {
            "name": "CD",
            "kind": "prod-continuous-downloader",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_continuous_downloader": {
                "spec": "lon1",
                "sleep_interval_secs": 0,
                "binary": {"branch": "feat-x", "repo_owner": "me"},
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert argv[:3] == ["prod", "continuous-downloader", "deploy"]
    assert "--sleep-interval-secs" in argv and argv[argv.index("--sleep-interval-secs") + 1] == "0"
    # branch binary -> --branch-name/--repo-owner, not --ant-version
    assert "--branch-name" in argv and "--ant-version" not in argv


# --------------------------------------------------------------------------- #
# prod-canary
# --------------------------------------------------------------------------- #
def test_prod_canary_probes_and_upload_spec():
    m = Manifest.model_validate(
        {
            "name": "CAN-01",
            "kind": "prod-canary",
            "purpose": "loss",
            "ssh_key_path": SSH,
            "prod_canary": {
                "binary": {"version": "0.2.4"},
                "upload_spec": "lon1",
                "urls_file": "/urls.txt",
                "probe_concurrency": 3,
                "probes": [
                    {"region": "nyc1", "count": 2},
                    {"region": "sgp1", "vm_size": "s-2vcpu-4gb", "count": 1},
                ],
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert argv[:3] == ["prod", "canary", "deploy"]
    assert "--upload-spec" in argv and argv[argv.index("--upload-spec") + 1] == "lon1"
    # two repeatable probe specs
    probe_idxs = [i for i, t in enumerate(argv) if t == "--probe-spec"]
    assert [argv[i + 1] for i in probe_idxs] == ["nyc1:count=2", "sgp1:s-2vcpu-4gb:count=1"]
    assert "--urls-file" in argv


# --------------------------------------------------------------------------- #
# local kinds — no infra, no destroy, no ssh key
# --------------------------------------------------------------------------- #
def test_prod_local_uploads_local_dir_and_verbose():
    m = Manifest.model_validate(
        {
            "name": "LU",
            "kind": "prod-local-uploads",
            "purpose": "p",
            "prod_local_uploads": {
                "binary": {"version": "1"},
                "local_dir": "/data/files",
                "payment": "no-merkle",
                "verbose": 2,
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert argv[:2] == ["prod", "local-uploads"]
    assert "--local-dir-path" in argv and argv[argv.index("--local-dir-path") + 1] == "/data/files"
    assert "--urls" not in argv
    assert argv.count("--verbose") == 2
    assert "--ssh-key-path" not in argv  # runs locally
    assert build_destroy_steps(m) == []  # nothing to tear down


def test_prod_local_downloads_keep_output_dir():
    m = Manifest.model_validate(
        {
            "name": "LD",
            "kind": "prod-local-downloads",
            "purpose": "p",
            "prod_local_downloads": {
                "binary": {"version": "1"},
                "addresses_path": "/addrs.txt",
                "keep_output_dir": "/out",
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert argv[:2] == ["prod", "local-downloads"]
    assert argv[argv.index("--addresses-path") + 1] == "/addrs.txt"
    assert argv[argv.index("--keep-output-dir") + 1] == "/out"
    assert build_destroy_steps(m) == []
