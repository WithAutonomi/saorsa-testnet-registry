"""`slack_post` on a ``kind: clients`` manifest (V2-1639).

A browser cohort brought up as a client extension was invisible in Slack
between the deployment's own post and the results comment: the manifest could
not carry ``slack_post`` at all. It now appends the extension's own
``clients slack-post`` step to both ``clients up`` and ``clients down``.

What is worth asserting is exactly what would be expensive to find on a run:

* the extension's steps are appended and nothing before them moves, because the
  step argv is what the journal compares — a changed earlier step turns a resume
  into a divergence report;
* ``deployments slack-post`` is never emitted for this kind, since it takes a
  DEPLOYMENT name and an extension has no registry row;
* the teardown step carries the VM summary as an argument, because
  ``clients down`` deletes the extension's state before the post runs.
"""

import textwrap

import pytest

from saorsa_deploy.config import load_manifest
from saorsa_deploy.orchestrator import build_deploy_steps, build_destroy_steps
from saorsa_deploy.orchestrator.clients import vm_summary

#: A 35-VM hot-read cohort, the shape the DEV-01 run uses.
COHORT = """\
    kind: clients
    name: DEV-01-browsers
    deployment: DEV-01
    purpose: "V2-1639: a hot-read cohort"
    ssh_key_path: ~/.ssh/ant_deploy
{slack}    clients:
      browser_downloaders:
        - provider: digital_ocean
          region: lon1
          count: 9
          instances: 4
          round_period_secs: 600
          download_timeout_secs: 300
        - provider: digital_ocean
          region: nyc1
          count: 9
          instances: 4
          round_period_secs: 600
          download_timeout_secs: 300
        - provider: digital_ocean
          region: sgp1
          count: 9
          instances: 4
          round_period_secs: 600
          download_timeout_secs: 300
        - provider: digital_ocean
          region: syd1
          count: 8
          instances: 4
          round_period_secs: 600
          download_timeout_secs: 300
"""

_EXPECTED_SUMMARY = "35 VMs — browser-download: lon1 ×9, nyc1 ×9, sgp1 ×9, syd1 ×8"


def _manifest(tmp_path, *, slack_post: bool):
    body = COHORT.format(slack="    slack_post: true\n" if slack_post else "")
    path = tmp_path / "cohort.yaml"
    path.write_text(textwrap.dedent(body))
    return load_manifest(path)


@pytest.fixture()
def with_slack(tmp_path):
    return _manifest(tmp_path, slack_post=True)


@pytest.fixture()
def without_slack(tmp_path):
    return _manifest(tmp_path, slack_post=False)


def test_deploy_ends_with_the_extension_slack_step(with_slack, without_slack):
    steps = build_deploy_steps(with_slack)
    assert steps[-1].argv == [
        "clients",
        "slack-post",
        "--name",
        "DEV-01",
        "--extension",
        "DEV-01-browsers",
        "--event",
        "up",
    ]
    assert steps[-1].best_effort is True
    # The three steps before it are byte-identical to the plan without the key:
    # the argv is what the step journal compares, so a resumed `up` must not see
    # a divergence just because the Slack tail was turned on.
    plain = build_deploy_steps(without_slack)
    assert [s.argv for s in steps[:-1]] == [s.argv for s in plain]


def test_destroy_ends_with_the_teardown_post_carrying_the_vm_summary(with_slack, without_slack):
    steps = build_destroy_steps(with_slack)
    assert steps[-1].argv == [
        "clients",
        "slack-post",
        "--name",
        "DEV-01",
        "--extension",
        "DEV-01-browsers",
        "--event",
        "down",
        "--vms",
        _EXPECTED_SUMMARY,
    ]
    assert steps[-1].best_effort is True
    plain = build_destroy_steps(without_slack)
    assert [s.argv for s in steps[:-1]] == [s.argv for s in plain]


def test_without_the_key_no_slack_step_at_all(without_slack):
    for steps in (build_deploy_steps(without_slack), build_destroy_steps(without_slack)):
        assert not [s for s in steps if "slack-post" in s.argv]


def test_deployments_slack_post_is_never_emitted_for_this_kind(with_slack):
    # It takes a DEPLOYMENT name and reads its registry row; handed the
    # extension's name it would exit 1 on "No deployment named ...".
    for steps in (build_deploy_steps(with_slack), build_destroy_steps(with_slack)):
        assert not [s for s in steps if s.argv[:2] == ["deployments", "slack-post"]]


def test_vm_summary_counts_regions_in_first_seen_order(with_slack):
    assert vm_summary(with_slack) == _EXPECTED_SUMMARY


def test_vm_summary_separates_roles(tmp_path):
    body = """\
        kind: clients
        name: mixed
        deployment: DEV-01
        purpose: p
        clients:
          uploaders:
            - { provider: digital_ocean, region: lon1, file_size_mb: 100 }
          downloaders:
            - { provider: digital_ocean, region: lon1 }
            - { provider: digital_ocean, region: nyc1 }
    """
    path = tmp_path / "mixed.yaml"
    path.write_text(textwrap.dedent(body))
    manifest = load_manifest(path)
    assert vm_summary(manifest) == "3 VMs — upload: lon1 ×1; download: lon1 ×1, nyc1 ×1"
