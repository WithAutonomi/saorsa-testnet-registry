import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest


class TestCmdBuild:
    @patch("saorsa_deploy.cmd.build.clear_known_hosts")
    @patch("saorsa_deploy.cmd.build.SaorsaNodeBuilder")
    @patch("saorsa_deploy.cmd.build.wait_for_ssh")
    @patch("saorsa_deploy.cmd.build.destroy_build_vm")
    @patch("saorsa_deploy.cmd.build.create_build_vm")
    def test_full_build_flow(
        self,
        mock_create,
        mock_destroy,
        mock_wait_ssh,
        mock_builder_cls,
        mock_clear_hosts,
    ):
        os.environ["SAORSA_BUILD_AWS_ACCESS_KEY_ID"] = "test-key"
        os.environ["SAORSA_BUILD_AWS_SECRET_ACCESS_KEY"] = "test-secret"
        try:
            mock_create.return_value = {
                "droplet_id": 12345,
                "droplet_name": "saorsa-build-myorg-branch",
                "ip_address": "1.2.3.4",
            }
            mock_builder_cls.return_value.execute.return_value = "https://s3.example.com/binary"

            from saorsa_deploy.cmd.build import cmd_build

            args = SimpleNamespace(
                branch_name="feature-x",
                repo_owner="myorg",
                ssh_key_path="~/.ssh/id_rsa",
            )
            cmd_build(args)

            mock_create.assert_called_once_with("myorg", "feature-x")
            mock_wait_ssh.assert_called_once_with("1.2.3.4")
            mock_clear_hosts.assert_called_once()
            mock_builder_cls.assert_called_once()
            mock_builder_cls.return_value.execute.assert_called_once()
            mock_destroy.assert_called_once_with(12345)
        finally:
            os.environ.pop("SAORSA_BUILD_AWS_ACCESS_KEY_ID", None)
            os.environ.pop("SAORSA_BUILD_AWS_SECRET_ACCESS_KEY", None)

    @patch("saorsa_deploy.cmd.build.create_build_vm")
    def test_exits_without_aws_credentials(self, mock_create):
        os.environ.pop("SAORSA_BUILD_AWS_ACCESS_KEY_ID", None)
        os.environ.pop("SAORSA_BUILD_AWS_SECRET_ACCESS_KEY", None)

        from saorsa_deploy.cmd.build import cmd_build

        args = SimpleNamespace(
            branch_name="feature-x",
            repo_owner="myorg",
            ssh_key_path="~/.ssh/id_rsa",
        )
        with pytest.raises(SystemExit):
            cmd_build(args)

        mock_create.assert_not_called()

    @patch("saorsa_deploy.cmd.build.clear_known_hosts")
    @patch("saorsa_deploy.cmd.build.SaorsaNodeBuilder")
    @patch("saorsa_deploy.cmd.build.wait_for_ssh")
    @patch("saorsa_deploy.cmd.build.destroy_build_vm")
    @patch("saorsa_deploy.cmd.build.create_build_vm")
    def test_destroys_droplet_on_failure(
        self,
        mock_create,
        mock_destroy,
        mock_wait_ssh,
        mock_builder_cls,
        mock_clear_hosts,
    ):
        os.environ["SAORSA_BUILD_AWS_ACCESS_KEY_ID"] = "test-key"
        os.environ["SAORSA_BUILD_AWS_SECRET_ACCESS_KEY"] = "test-secret"
        try:
            mock_create.return_value = {
                "droplet_id": 12345,
                "droplet_name": "saorsa-build-myorg-branch",
                "ip_address": "1.2.3.4",
            }
            mock_builder_cls.return_value.execute.side_effect = RuntimeError("build failed")

            from saorsa_deploy.cmd.build import cmd_build

            args = SimpleNamespace(
                branch_name="feature-x",
                repo_owner="myorg",
                ssh_key_path="~/.ssh/id_rsa",
            )
            with pytest.raises(SystemExit):
                cmd_build(args)

            mock_destroy.assert_called_once_with(12345)
        finally:
            os.environ.pop("SAORSA_BUILD_AWS_ACCESS_KEY_ID", None)
            os.environ.pop("SAORSA_BUILD_AWS_SECRET_ACCESS_KEY", None)
