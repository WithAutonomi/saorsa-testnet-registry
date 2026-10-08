from types import SimpleNamespace
from unittest.mock import patch

from saorsa_deploy.cmd.destroy_monitoring import cmd_destroy_monitoring


def _args(name="DEV-01", force=True):
    return SimpleNamespace(name=name, force=force)


def test_no_op_when_monitoring_state_missing():
    with (
        patch(
            "saorsa_deploy.cmd.destroy_monitoring.load_monitoring_state",
            side_effect=RuntimeError("No monitoring state for 'DEV-01'"),
        ),
        patch("saorsa_deploy.cmd.destroy_monitoring.execute_terraform_runs") as execute,
    ):
        cmd_destroy_monitoring(_args())

    execute.assert_not_called()
