import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _args(**overrides):
    base = dict(
        name="DEV-01",
        branch_name="feature-x",
        repo_owner="myorg",
        ssh_key_path="~/.ssh/id_rsa",
        validation_string=None,
        build_vm_size=None,
        musl=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestCmdBuildAnt:
    @patch("saorsa_deploy.cmd.build_ant.validate_branch_exists")
    @patch("saorsa_deploy.cmd.build_ant.clear_known_hosts")
    @patch("saorsa_deploy.cmd.build_ant.AntBuilder")
    @patch("saorsa_deploy.cmd.build_ant.wait_for_ssh")
    @patch("saorsa_deploy.cmd.build_ant.destroy_build_vm")
    @patch("saorsa_deploy.cmd.build_ant.create_build_vm")
    def test_validates_ant_client_branch_then_builds(
        self,
        mock_create,
        mock_destroy,
        mock_wait_ssh,
        mock_builder_cls,
        mock_clear_hosts,
        mock_validate,
    ):
        os.environ["ANT_BUILD_AWS_ACCESS_KEY_ID"] = "test-key"
        os.environ["ANT_BUILD_AWS_SECRET_ACCESS_KEY"] = "test-secret"
        try:
            mock_create.return_value = {
                "droplet_id": 12345,
                "droplet_name": "ant-build-myorg-branch",
                "ip_address": "1.2.3.4",
            }
            mock_builder_cls.return_value.execute.return_value = "https://s3.example.com/ant"

            from saorsa_deploy.cmd.build_ant import cmd_build_ant

            cmd_build_ant(_args())

            mock_validate.assert_called_once_with("myorg", "ant-client", "feature-x")
            mock_create.assert_called_once_with("ant", "DEV-01", "myorg", "feature-x", None)
            mock_builder_cls.return_value.execute.assert_called_once()
            mock_destroy.assert_called_once_with(12345)
        finally:
            os.environ.pop("ANT_BUILD_AWS_ACCESS_KEY_ID", None)
            os.environ.pop("ANT_BUILD_AWS_SECRET_ACCESS_KEY", None)

    @patch("saorsa_deploy.cmd.build_ant.create_build_vm")
    @patch("saorsa_deploy.cmd.build_ant.validate_branch_exists")
    def test_exits_before_droplet_when_branch_missing(self, mock_validate, mock_create):
        os.environ["ANT_BUILD_AWS_ACCESS_KEY_ID"] = "test-key"
        os.environ["ANT_BUILD_AWS_SECRET_ACCESS_KEY"] = "test-secret"
        mock_validate.side_effect = ValueError("Branch 'nope' was not found")
        try:
            from saorsa_deploy.cmd.build_ant import cmd_build_ant

            with pytest.raises(SystemExit):
                cmd_build_ant(_args(branch_name="nope"))

            mock_validate.assert_called_once_with("myorg", "ant-client", "nope")
            mock_create.assert_not_called()
        finally:
            os.environ.pop("ANT_BUILD_AWS_ACCESS_KEY_ID", None)
            os.environ.pop("ANT_BUILD_AWS_SECRET_ACCESS_KEY", None)

    @patch("saorsa_deploy.cmd.build_ant.create_build_vm")
    @patch("saorsa_deploy.cmd.build_ant.validate_branch_exists")
    def test_exits_before_validation_without_aws_credentials(self, mock_validate, mock_create):
        os.environ.pop("ANT_BUILD_AWS_ACCESS_KEY_ID", None)
        os.environ.pop("ANT_BUILD_AWS_SECRET_ACCESS_KEY", None)

        from saorsa_deploy.cmd.build_ant import cmd_build_ant

        with pytest.raises(SystemExit):
            cmd_build_ant(_args())

        mock_validate.assert_not_called()
        mock_create.assert_not_called()
