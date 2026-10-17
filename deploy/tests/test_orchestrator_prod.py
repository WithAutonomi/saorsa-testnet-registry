"""Orchestrator tests for the prod-* kinds.

The uploaders/downloaders cases reproduce the real reference runbooks
(``runbooks/PROD_UL_01-deploy.sh`` and ``runbooks/PROD_DL_01-versioned-deploy.sh``).
"""

import pytest
from pydantic import ValidationError

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
                        "clock_skew": "2h",
                    }
                ],
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    spec = argv[argv.index("--uploader") + 1]
    assert spec == "lon1:s-2vcpu-4gb:urls=/a.txt:payment=merkle:clock_skew=2h"


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


def test_prod_uploaders_log_level_appends_token():
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
                        "log_level": "debug",
                    }
                ],
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    spec = argv[argv.index("--uploader") + 1]
    assert spec == "lon1:s-2vcpu-4gb:urls=/a.txt:log_level=debug"


def test_prod_uploaders_log_level_omitted_by_default():
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
    assert "log_level" not in spec


def test_prod_uploaders_log_level_invalid_value_rejected():
    with pytest.raises(ValidationError):
        Manifest.model_validate(
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
                            "log_level": "verbose",
                        }
                    ],
                },
            }
        )


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


def test_prod_uploaders_allow_duplicate_files_emits_flag():
    m = Manifest.model_validate(
        {
            "name": "U",
            "kind": "prod-uploaders",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_uploaders": {
                "binary": {"version": "1"},
                "allow_duplicate_files": True,
                "uploaders": [{"region": "lon1", "vm_size": "s-2vcpu-4gb", "urls": "/a.txt"}],
            },
        }
    )
    assert "--allow-duplicate-files" in build_deploy_steps(m)[0].argv


def test_prod_uploaders_allow_duplicate_files_omitted_by_default():
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
    assert "--allow-duplicate-files" not in build_deploy_steps(m)[0].argv


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
        ],
        # The download kinds keep no deployment state, so the run's VM details
        # and address list exist nowhere locally unless this snapshots them.
        [
            "deployments",
            "generate-inventory",
            "--name",
            "PROD-DL-01",
            "--ssh-key-path",
            SSH,
        ],
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
                "clock_skew": "2h",
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert "--clock-skew" in argv
    assert argv[argv.index("--clock-skew") + 1] == "2h"


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
                "clock_skew": "-90m",
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert "--clock-skew" in argv
    assert argv[argv.index("--clock-skew") + 1] == "-90m"


def test_prod_continuous_downloader_daily_at():
    m = Manifest.model_validate(
        {
            "name": "CD",
            "kind": "prod-continuous-downloader",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_continuous_downloader": {
                "spec": "lon1",
                "daily_at": "02:00",
                "binary": {"branch": "feat-x", "repo_owner": "me"},
            },
        }
    )
    steps = build_deploy_steps(m)
    # branch binary -> a `build ant` step precedes the deploy
    assert steps[0].argv[:2] == ["build", "ant"]
    argv = steps[1].argv
    assert argv[:3] == ["prod", "continuous-downloader", "deploy"]
    assert argv[argv.index("--daily-at") + 1] == "02:00"
    # branch binary -> --branch-name/--repo-owner, not --ant-version
    assert "--branch-name" in argv and "--ant-version" not in argv


def test_prod_continuous_downloader_daily_at_is_always_emitted():
    """`plan` must show the anchor even when it is the default.

    The anchor is the deployment's whole cadence, so a plan that does not name
    it reads as a run with none — and the step journal compares argv, so an
    explicit value is also what pins it across a resume.
    """
    m = Manifest.model_validate(
        {
            "name": "CD",
            "kind": "prod-continuous-downloader",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_continuous_downloader": {"spec": "lon1", "binary": {"version": "0.3.1"}},
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert argv[argv.index("--daily-at") + 1] == "00:00"
    assert "--sleep-interval-secs" not in argv
    assert "--daily" not in argv


def test_prod_continuous_downloader_refuses_the_retired_sleep_interval():
    """Interval mode is gone; a manifest still carrying it names the key."""
    with pytest.raises(ValidationError, match="sleep_interval_secs"):
        Manifest.model_validate(
            {
                "name": "CD",
                "kind": "prod-continuous-downloader",
                "purpose": "p",
                "ssh_key_path": SSH,
                "prod_continuous_downloader": {
                    "spec": "lon1",
                    "sleep_interval_secs": 86400,
                    "binary": {"version": "0.3.1"},
                },
            }
        )


@pytest.mark.parametrize("value", ["00:00", "23:59", "02:00"])
def test_prod_continuous_downloader_daily_at_accepts_a_strict_hh_mm(value):
    m = Manifest.model_validate(
        {
            "name": "CD",
            "kind": "prod-continuous-downloader",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_continuous_downloader": {
                "spec": "lon1",
                "daily_at": value,
                "binary": {"version": "0.3.1"},
            },
        }
    )
    assert m.spec.daily_at == value


@pytest.mark.parametrize("value", ["24:00", "0:00", "00:00:00", "0000", "00:60", "ab:cd"])
def test_prod_continuous_downloader_daily_at_rejects_anything_else(value):
    with pytest.raises(ValidationError, match="daily_at must be HH:MM"):
        Manifest.model_validate(
            {
                "name": "CD",
                "kind": "prod-continuous-downloader",
                "purpose": "p",
                "ssh_key_path": SSH,
                "prod_continuous_downloader": {
                    "spec": "lon1",
                    "daily_at": value,
                    "binary": {"version": "0.3.1"},
                },
            }
        )


def test_prod_continuous_downloader_permanent_options_map_to_flags():
    m = Manifest.model_validate(
        {
            "name": "CD",
            "kind": "prod-continuous-downloader",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_continuous_downloader": {
                "spec": "fsn1:cpx41",
                "provider": "hetzner",
                "monitoring_provider": "ovh",
                "monitoring_region": "GRA9",
                "monitoring_droplet_size": "b3-8",
                "monitoring_volume_type": "high-speed",
                "influx_retention_days": 7,
                "log_retention_days": 7,
                "daily_at": "06:30",
                "binary": {"version": "0.3.1"},
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert argv[:3] == ["prod", "continuous-downloader", "deploy"]

    def _val(flag):
        return argv[argv.index(flag) + 1]

    assert _val("--provider") == "hetzner"
    assert _val("--monitoring-provider") == "ovh"
    assert _val("--monitoring-region") == "GRA9"
    assert _val("--monitoring-droplet-size") == "b3-8"
    assert _val("--monitoring-volume-type") == "high-speed"
    assert _val("--influx-retention-days") == "7"
    assert _val("--log-retention-days") == "7"
    assert _val("--daily-at") == "06:30"


def test_prod_continuous_downloader_omits_unset_permanent_options():
    m = Manifest.model_validate(
        {
            "name": "CD",
            "kind": "prod-continuous-downloader",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_continuous_downloader": {
                "spec": "lon1",
                "binary": {"version": "0.3.1"},
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    # Nothing set → the CLI defaults apply; no new flags are emitted.
    for flag in (
        "--provider",
        "--monitoring-provider",
        "--monitoring-region",
        "--monitoring-droplet-size",
        "--monitoring-volume-type",
        "--influx-retention-days",
        "--log-retention-days",
    ):
        assert flag not in argv


def test_prod_skip_builds_omits_branch_build_step():
    m = Manifest.model_validate(
        {
            "name": "CD",
            "kind": "prod-continuous-downloader",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_continuous_downloader": {
                "spec": "lon1",
                "binary": {"branch": "feat-x", "repo_owner": "me"},
            },
        }
    )
    default = build_deploy_steps(m)
    assert default[0].argv[:2] == ["build", "ant"]

    skipped = build_deploy_steps(m, skip_builds=True)
    assert skipped[0].argv[:2] != ["build", "ant"]
    # The deploy step(s) after the build are untouched.
    assert _argvs(skipped) == _argvs(default)[1:]


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
    assert "--log-level" not in argv
    assert "--ssh-key-path" not in argv  # runs locally
    assert build_destroy_steps(m) == []  # nothing to tear down


def test_prod_local_uploads_log_level_emits_flag():
    m = Manifest.model_validate(
        {
            "name": "LU",
            "kind": "prod-local-uploads",
            "purpose": "p",
            "prod_local_uploads": {
                "binary": {"version": "1"},
                "urls": "/data/urls.txt",
                "log_level": "debug",
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert argv[argv.index("--log-level") + 1] == "debug"
    assert "--verbose" not in argv


def test_prod_local_uploads_log_level_wins_over_verbose():
    m = Manifest.model_validate(
        {
            "name": "LU",
            "kind": "prod-local-uploads",
            "purpose": "p",
            "prod_local_uploads": {
                "binary": {"version": "1"},
                "urls": "/data/urls.txt",
                "log_level": "trace",
                "verbose": 2,
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert argv[argv.index("--log-level") + 1] == "trace"
    assert "--verbose" not in argv  # deprecated alias suppressed when log_level set


def test_prod_local_uploads_log_level_invalid_value_rejected():
    with pytest.raises(ValidationError):
        Manifest.model_validate(
            {
                "name": "LU",
                "kind": "prod-local-uploads",
                "purpose": "p",
                "prod_local_uploads": {
                    "binary": {"version": "1"},
                    "urls": "/data/urls.txt",
                    "log_level": "loud",
                },
            }
        )


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


# --------------------------------------------------------------------------- #
# branch binaries — a `build ant` step must precede the deploy (the prod
# commands resolve branch binaries from the name-scoped S3 custom build, which
# only `build ant` populates; reproduces the PROD-DL-02 failure)
# --------------------------------------------------------------------------- #
def test_prod_downloaders_branch_binary_prepends_build_step():
    m = Manifest.model_validate(
        {
            "name": "PROD-DL-02",
            "kind": "prod-downloaders",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_downloaders": {
                "spec": "lon1:s-2vcpu-4gb",
                "addresses_path": "/a.txt",
                "binary": {
                    "branch": "codex/remove-bootstrap-cache",
                    "repo_owner": "WithAutonomi",
                },
                "bootstrap_cache": True,
            },
        }
    )
    steps = build_deploy_steps(m)
    assert steps[0].argv == [
        "build",
        "ant",
        "--name",
        "PROD-DL-02",
        "--branch-name",
        "codex/remove-bootstrap-cache",
        "--repo-owner",
        "WithAutonomi",
        "--ssh-key-path",
        SSH,
    ]
    assert steps[1].argv[:3] == ["prod", "downloaders", "deploy"]
    # destroy is unaffected
    assert all(s.argv[:2] != ["build", "ant"] for s in build_destroy_steps(m))


def test_prod_uploaders_branch_binary_prepends_build_step_with_validation():
    m = Manifest.model_validate(
        {
            "name": "U",
            "kind": "prod-uploaders",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_uploaders": {
                "binary": {"branch": "feat-x", "repo_owner": "me", "validation": "marker"},
                "uploaders": [{"region": "lon1", "vm_size": "s-2vcpu-4gb", "urls": "/a.txt"}],
            },
        }
    )
    steps = build_deploy_steps(m)
    assert steps[0].argv[:2] == ["build", "ant"]
    assert steps[0].argv[steps[0].argv.index("--validation-string") + 1] == "marker"
    assert steps[1].argv[:3] == ["prod", "uploaders", "deploy"]


def test_prod_canary_branch_binary_prepends_build_step():
    m = Manifest.model_validate(
        {
            "name": "C",
            "kind": "prod-canary",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_canary": {
                "binary": {"branch": "feat-x", "repo_owner": "me"},
                "upload_spec": "lon1",
                "probes": [{"region": "lon1", "count": 4}],
                "urls_file": "/u.txt",
            },
        }
    )
    steps = build_deploy_steps(m)
    assert steps[0].argv[:2] == ["build", "ant"]
    assert steps[1].argv[:3] == ["prod", "canary", "deploy"]


def test_prod_local_kinds_branch_binary_prepends_build_step():
    # local kinds also fetch branch binaries from the name-scoped S3 build
    m = Manifest.model_validate(
        {
            "name": "LU",
            "kind": "prod-local-uploads",
            "purpose": "p",
            "prod_local_uploads": {
                "binary": {"branch": "feat-x", "repo_owner": "me"},
                "urls": "/l.txt",
            },
        }
    )
    steps = build_deploy_steps(m)
    assert steps[0].argv[:2] == ["build", "ant"]
    assert steps[1].argv[:2] == ["prod", "local-uploads"]

    m = Manifest.model_validate(
        {
            "name": "LD",
            "kind": "prod-local-downloads",
            "purpose": "p",
            "prod_local_downloads": {
                "binary": {"branch": "feat-x", "repo_owner": "me"},
                "addresses_path": "/a.txt",
            },
        }
    )
    steps = build_deploy_steps(m)
    assert steps[0].argv[:2] == ["build", "ant"]
    assert steps[1].argv[:2] == ["prod", "local-downloads"]


def test_prod_version_binary_has_no_build_step():
    m = Manifest.model_validate(
        {
            "name": "D",
            "kind": "prod-downloaders",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_downloaders": {"spec": "lon1", "binary": {"version": "0.2.6"}},
        }
    )
    assert all(s.argv[:2] != ["build", "ant"] for s in build_deploy_steps(m))


# --------------------------------------------------------------------------- #
# bootstrap_cache
# --------------------------------------------------------------------------- #
def test_prod_uploaders_bootstrap_cache_appends_token():
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
                        "bootstrap_cache": False,
                    }
                ],
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    spec = argv[argv.index("--uploader") + 1]
    assert spec == "lon1:s-2vcpu-4gb:urls=/a.txt:bootstrap_cache=false"


def test_prod_downloaders_bootstrap_cache_flag():
    m = Manifest.model_validate(
        {
            "name": "D",
            "kind": "prod-downloaders",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_downloaders": {
                "spec": "lon1",
                "binary": {"version": "1"},
                "bootstrap_cache": False,
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert "--no-bootstrap-cache" in argv
    assert "--bootstrap-cache" not in argv


def test_prod_continuous_downloader_bootstrap_cache_flag():
    m = Manifest.model_validate(
        {
            "name": "D",
            "kind": "prod-continuous-downloader",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_continuous_downloader": {
                "spec": "lon1",
                "binary": {"version": "1"},
                "bootstrap_cache": True,
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert "--bootstrap-cache" in argv
    assert "--no-bootstrap-cache" not in argv


def test_prod_canary_bootstrap_cache_flag():
    m = Manifest.model_validate(
        {
            "name": "C",
            "kind": "prod-canary",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_canary": {
                "binary": {"version": "1"},
                "upload_spec": "lon1",
                "probes": [{"region": "lon1", "count": 4}],
                "urls_file": "/u.txt",
                "bootstrap_cache": False,
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert "--no-bootstrap-cache" in argv


def test_prod_local_kinds_bootstrap_cache_flag():
    m = Manifest.model_validate(
        {
            "name": "LU",
            "kind": "prod-local-uploads",
            "purpose": "p",
            "prod_local_uploads": {
                "binary": {"version": "1"},
                "urls": "/l.txt",
                "bootstrap_cache": False,
            },
        }
    )
    assert "--no-bootstrap-cache" in build_deploy_steps(m)[0].argv

    m = Manifest.model_validate(
        {
            "name": "LD",
            "kind": "prod-local-downloads",
            "purpose": "p",
            "prod_local_downloads": {
                "binary": {"version": "1"},
                "addresses_path": "/a.txt",
                "bootstrap_cache": True,
            },
        }
    )
    assert "--bootstrap-cache" in build_deploy_steps(m)[0].argv


def test_bootstrap_cache_flag_omitted_when_unset():
    m = Manifest.model_validate(
        {
            "name": "D",
            "kind": "prod-downloaders",
            "purpose": "p",
            "ssh_key_path": SSH,
            "prod_downloaders": {"spec": "lon1", "binary": {"version": "1"}},
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert "--bootstrap-cache" not in argv
    assert "--no-bootstrap-cache" not in argv


# --------------------------------------------------------------------------- #
# prod-nodes — heaptrack profiling nodes against production
# --------------------------------------------------------------------------- #
def _prod_nodes_manifest(prod_nodes: dict) -> Manifest:
    return Manifest.model_validate(
        {
            "name": "PROF-NODE-01",
            "kind": "prod-nodes",
            "purpose": "leak chase",
            "ssh_key_path": SSH,
            "prod_nodes": prod_nodes,
        }
    )


def test_prod_nodes_debug_build_then_deploy():
    m = _prod_nodes_manifest(
        {
            "binary": {"branch": "main", "repo_owner": "WithAutonomi", "debug": True},
            "provider": "hetzner",
            "region": "fsn1",
            "vm_size": "cpx42",
            "vm_count": 1,
            "node_count": 15,
            "volume_size": 100,
            "heaptrack": True,
            "monitoring": True,
            "grafana": False,
            "monitoring_volume_size": 50,
            "log_retention_days": 7,
            "log_level": "info",
        }
    )
    steps = _argvs(build_deploy_steps(m))
    # A debug ANT-NODE build precedes the deploy (not `build ant`).
    assert steps[0] == [
        "build",
        "ant-node",
        "--name",
        "PROF-NODE-01",
        "--branch-name",
        "main",
        "--repo-owner",
        "WithAutonomi",
        "--ssh-key-path",
        SSH,
        "--debug",
    ]
    assert steps[1] == [
        "prod",
        "nodes",
        "deploy",
        "--name",
        "PROF-NODE-01",
        "--purpose",
        "leak chase",
        "--region",
        "fsn1",
        "--provider",
        "hetzner",
        "--vm-size",
        "cpx42",
        "--vm-count",
        "1",
        "--node-count",
        "15",
        "--volume-size",
        "100",
        "--heaptrack",
        "--monitoring",
        "--no-grafana",
        "--monitoring-volume-size",
        "50",
        "--log-retention-days",
        "7",
        "--log-level",
        "info",
        "--branch-name",
        "main",
        "--repo-owner",
        "WithAutonomi",
        "--debug",
        "--ssh-key-path",
        SSH,
    ]


def test_prod_nodes_release_has_no_build_step():
    m = _prod_nodes_manifest({"binary": {"version": "0.11.6"}, "region": "lon1"})
    steps = _argvs(build_deploy_steps(m))
    # A pinned release ships prebuilt — no build step, just the deploy.
    assert len(steps) == 1
    assert steps[0][:3] == ["prod", "nodes", "deploy"]
    assert "--node-version" in steps[0]
    assert steps[0][steps[0].index("--node-version") + 1] == "0.11.6"
    # No build step means no --branch-name in the (single) deploy step's source.
    assert "--branch-name" not in steps[0]


def test_prod_nodes_no_heaptrack_flag_emitted():
    m = _prod_nodes_manifest(
        {"binary": {"version": "0.11.6"}, "region": "lon1", "heaptrack": False}
    )
    argv = build_deploy_steps(m)[0].argv
    assert "--no-heaptrack" in argv
    assert "--heaptrack" not in argv


def test_prod_nodes_destroy_default_and_keep_monitoring():
    base = {"binary": {"version": "0.11.6"}, "region": "lon1"}
    m = _prod_nodes_manifest(base)
    assert _argvs(build_destroy_steps(m)) == [
        ["prod", "nodes", "destroy", "--name", "PROF-NODE-01", "--ssh-key-path", SSH]
    ]
    m_keep = _prod_nodes_manifest({**base, "keep_monitoring": True})
    assert build_destroy_steps(m_keep)[0].argv[-1] == "--keep-monitoring"


def test_prod_nodes_debug_requires_branch():
    with pytest.raises(ValidationError):
        _prod_nodes_manifest({"binary": {"version": "0.11.6", "debug": True}, "region": "lon1"})


def test_prod_nodes_elastic_emits_forward_logs_and_sizing():
    m = _prod_nodes_manifest(
        {
            "binary": {"version": "0.11.6"},
            "region": "lon1",
            "log_level": "debug",
            "elastic": {
                "enabled": True,
                "volume_size": 200,
                "retention_days": 14,
                "droplet_size": "s-8vcpu-16gb",
                "shards": 3,
            },
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert "--forward-logs" in argv
    assert argv[argv.index("--elastic-volume-size") + 1] == "200"
    assert argv[argv.index("--elastic-retention-days") + 1] == "14"
    assert argv[argv.index("--elastic-droplet-size") + 1] == "s-8vcpu-16gb"
    assert argv[argv.index("--elastic-shards") + 1] == "3"
    # No explicit drop override given → the flag is omitted (CLI derives it from
    # --log-level at deploy time).
    assert "--elastic-drop-log-levels" not in argv


def test_prod_nodes_elastic_omitted_by_default():
    # No `elastic:` block → no forward-logs flags at all (byte-identical argv to
    # before this feature, so existing `up` journal/plan hashes still match).
    m = _prod_nodes_manifest({"binary": {"version": "0.11.6"}, "region": "lon1"})
    argv = build_deploy_steps(m)[0].argv
    assert "--forward-logs" not in argv
    assert not any(a.startswith("--elastic-") for a in argv)


def test_prod_nodes_elastic_drop_log_levels_override_emitted():
    m = _prod_nodes_manifest(
        {
            "binary": {"version": "0.11.6"},
            "region": "lon1",
            "log_level": "trace",
            "elastic": {"enabled": True, "drop_log_levels": "DEBUG,TRACE"},
        }
    )
    argv = build_deploy_steps(m)[0].argv
    assert argv[argv.index("--elastic-drop-log-levels") + 1] == "DEBUG,TRACE"


def test_prod_nodes_keep_elastic_emits_flag():
    base = {"binary": {"version": "0.11.6"}, "region": "lon1"}
    m = _prod_nodes_manifest({**base, "keep_elastic": True})
    assert "--keep-elastic" in build_destroy_steps(m)[0].argv
    # Default (no keep_elastic) does NOT emit the flag.
    m_default = _prod_nodes_manifest(base)
    assert "--keep-elastic" not in build_destroy_steps(m_default)[0].argv
