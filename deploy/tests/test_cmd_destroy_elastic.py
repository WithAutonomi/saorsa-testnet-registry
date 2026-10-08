from types import SimpleNamespace
from unittest.mock import patch

from saorsa_deploy.cmd.destroy_elastic import cmd_destroy_elastic


def _args(name="DEV-01", force=True):
    return SimpleNamespace(name=name, force=force)


def test_no_op_when_elastic_state_missing():
    with (
        patch(
            "saorsa_deploy.cmd.destroy_elastic.load_elastic_state",
            side_effect=RuntimeError("No Elasticsearch state for 'DEV-01'"),
        ),
        patch("saorsa_deploy.cmd.destroy_elastic.execute_terraform_runs") as execute,
    ):
        cmd_destroy_elastic(_args())

    execute.assert_not_called()
