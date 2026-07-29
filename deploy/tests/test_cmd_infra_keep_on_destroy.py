"""`elastic infra` / `monitoring infra` persist the keep-on-destroy intent.

The intent has to reach the component's S3 state, because the name-only
`deployments down` has no manifest to read it from. The load-bearing case is the
**idempotent-reuse** branch: both commands return early when state already
records a `vm_ip`, *before* any state write, so without an explicit merge there
the flag is silently dropped on every re-run and on a resumed `up` — and the
teardown then destroys a stack the manifest asked to keep.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.elastic_infra import cmd_elastic_infra
from saorsa_deploy.cmd.monitoring_infra import cmd_monitoring_infra


def _elastic_args(keep_on_destroy=False):
    return SimpleNamespace(
        name="DEV-01",
        provider="digitalocean",
        region="lon1",
        volume_size=None,
        volume_type=None,
        droplet_size=None,
        force=False,
        keep_on_destroy=keep_on_destroy,
    )


def _monitoring_args(keep_on_destroy=False):
    return SimpleNamespace(
        name="DEV-01",
        provider="digitalocean",
        region="lon1",
        volume_size=None,
        volume_type=None,
        droplet_size=None,
        grafana_size=None,
        grafana=False,
        force=False,
        keep_on_destroy=keep_on_destroy,
    )


_EXISTING = {"name": "DEV-01", "region": "lon1", "vm_ip": "10.0.0.1"}


# --------------------------------------------------------------------------- #
# elastic
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "existing_keep,requested_keep,expected",
    [
        # a stack created before the option existed, now asked to be kept
        (None, True, True),
        # already kept, asked again — nothing to write
        (True, True, None),
        # kept, but the manifest no longer asks for it: desired-state wins
        (True, False, False),
        # never kept, still not asked for — nothing to write
        (False, False, None),
    ],
)
def test_elastic_reuse_path_syncs_keep_flag(existing_keep, requested_keep, expected):
    existing = dict(_EXISTING)
    if existing_keep is not None:
        existing["keep_on_destroy"] = existing_keep

    with (
        patch(
            "saorsa_deploy.cmd.elastic_infra.load_elastic_state", return_value=existing
        ),
        patch("saorsa_deploy.cmd.elastic_infra.update_elastic_state") as update,
        patch("saorsa_deploy.cmd.elastic_infra.execute_terraform_runs") as execute,
    ):
        cmd_elastic_infra(_elastic_args(keep_on_destroy=requested_keep))

    # the reuse branch must never re-apply terraform
    execute.assert_not_called()
    if expected is None:
        update.assert_not_called()
    else:
        update.assert_called_once_with("DEV-01", {"keep_on_destroy": expected})


def test_elastic_reuse_path_tolerates_a_failed_state_update():
    # A state-write failure on the reuse path must warn, not abort — the VM
    # already exists and the rest of the deploy should carry on.
    with (
        patch(
            "saorsa_deploy.cmd.elastic_infra.load_elastic_state",
            return_value=dict(_EXISTING),
        ),
        patch(
            "saorsa_deploy.cmd.elastic_infra.update_elastic_state",
            side_effect=Exception("s3 down"),
        ),
        patch("saorsa_deploy.cmd.elastic_infra.execute_terraform_runs"),
    ):
        cmd_elastic_infra(_elastic_args(keep_on_destroy=True))


def test_elastic_create_path_saves_keep_flag():
    with (
        patch(
            "saorsa_deploy.cmd.elastic_infra.load_elastic_state",
            side_effect=RuntimeError("absent"),
        ),
        patch(
            "saorsa_deploy.cmd.elastic_infra.check_terraform_state_exists",
            return_value=False,
        ),
        patch(
            "saorsa_deploy.cmd.elastic_infra.execute_terraform_runs",
            return_value=[
                SimpleNamespace(
                    success=True, outputs={"elastic_ip": "10.0.0.2"}, stderr=""
                )
            ],
        ),
        patch("saorsa_deploy.cmd.elastic_infra.save_elastic_state") as save,
    ):
        cmd_elastic_infra(_elastic_args(keep_on_destroy=True))

    assert save.call_args.kwargs["keep_on_destroy"] is True


def test_elastic_create_path_defaults_to_destroy():
    with (
        patch(
            "saorsa_deploy.cmd.elastic_infra.load_elastic_state",
            side_effect=RuntimeError("absent"),
        ),
        patch(
            "saorsa_deploy.cmd.elastic_infra.check_terraform_state_exists",
            return_value=False,
        ),
        patch(
            "saorsa_deploy.cmd.elastic_infra.execute_terraform_runs",
            return_value=[
                SimpleNamespace(
                    success=True, outputs={"elastic_ip": "10.0.0.2"}, stderr=""
                )
            ],
        ),
        patch("saorsa_deploy.cmd.elastic_infra.save_elastic_state") as save,
    ):
        cmd_elastic_infra(_elastic_args())

    assert save.call_args.kwargs["keep_on_destroy"] is False


# --------------------------------------------------------------------------- #
# monitoring
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "existing_keep,requested_keep,expected",
    [
        (None, True, True),
        (True, True, None),
        (True, False, False),
        (False, False, None),
    ],
)
def test_monitoring_reuse_path_syncs_keep_flag(existing_keep, requested_keep, expected):
    existing = dict(_EXISTING)
    if existing_keep is not None:
        existing["keep_on_destroy"] = existing_keep

    with (
        patch(
            "saorsa_deploy.cmd.monitoring_infra.load_monitoring_state",
            return_value=existing,
        ),
        patch("saorsa_deploy.cmd.monitoring_infra.update_monitoring_state") as update,
        patch("saorsa_deploy.cmd.monitoring_infra.execute_terraform_runs") as execute,
    ):
        cmd_monitoring_infra(_monitoring_args(keep_on_destroy=requested_keep))

    execute.assert_not_called()
    if expected is None:
        update.assert_not_called()
    else:
        update.assert_called_once_with("DEV-01", {"keep_on_destroy": expected})


def test_monitoring_create_path_saves_keep_flag():
    with (
        patch(
            "saorsa_deploy.cmd.monitoring_infra.load_monitoring_state",
            side_effect=RuntimeError("absent"),
        ),
        patch(
            "saorsa_deploy.cmd.monitoring_infra.check_terraform_state_exists",
            return_value=False,
        ),
        patch(
            "saorsa_deploy.cmd.monitoring_infra.execute_terraform_runs",
            return_value=[
                SimpleNamespace(
                    success=True,
                    outputs={"monitoring_ip": "10.0.0.3", "grafana_ip": ""},
                    stderr="",
                )
            ],
        ),
        patch("saorsa_deploy.cmd.monitoring_infra.save_monitoring_state") as save,
    ):
        cmd_monitoring_infra(_monitoring_args(keep_on_destroy=True))

    assert save.call_args.kwargs["keep_on_destroy"] is True


def test_monitoring_create_path_defaults_to_destroy():
    with (
        patch(
            "saorsa_deploy.cmd.monitoring_infra.load_monitoring_state",
            side_effect=RuntimeError("absent"),
        ),
        patch(
            "saorsa_deploy.cmd.monitoring_infra.check_terraform_state_exists",
            return_value=False,
        ),
        patch(
            "saorsa_deploy.cmd.monitoring_infra.execute_terraform_runs",
            return_value=[
                SimpleNamespace(
                    success=True,
                    outputs={"monitoring_ip": "10.0.0.3", "grafana_ip": ""},
                    stderr="",
                )
            ],
        ),
        patch("saorsa_deploy.cmd.monitoring_infra.save_monitoring_state") as save,
    ):
        cmd_monitoring_infra(_monitoring_args())

    assert save.call_args.kwargs["keep_on_destroy"] is False
