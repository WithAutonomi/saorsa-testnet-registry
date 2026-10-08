import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest


class TestCmdBuild:
    @patch("saorsa_deploy.cmd.build.validate_branch_exists")
    @patch("saorsa_deploy.cmd.build.clear_known_hosts")
    @patch("saorsa_deploy.cmd.build.AntNodeBuilder")
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
        mock_validate,
    ):
        os.environ["ANT_BUILD_AWS_ACCESS_KEY_ID"] = "test-key"
        os.environ["ANT_BUILD_AWS_SECRET_ACCESS_KEY"] = "test-secret"
        try:
            mock_create.return_value = {
                "droplet_id": 12345,
                "droplet_name": "saorsa-build-myorg-branch",
                "ip_address": "1.2.3.4",
            }
            mock_builder_cls.return_value.execute.return_value = "https://s3.example.com/binary"

            from saorsa_deploy.cmd.build import cmd_build

            args = SimpleNamespace(
                name="DEV-01",
                branch_name="feature-x",
                repo_owner="myorg",
                ssh_key_path="~/.ssh/id_rsa",
                validation_string=None,
                build_vm_size=None,
                musl=False,
            )
            cmd_build(args)

            mock_validate.assert_called_once_with("myorg", "ant-node", "feature-x")
            mock_create.assert_called_once_with("ant-node", "DEV-01", "myorg", "feature-x", None)
            mock_wait_ssh.assert_called_once_with("1.2.3.4")
            mock_clear_hosts.assert_called_once()
            mock_builder_cls.assert_called_once()
            mock_builder_cls.return_value.execute.assert_called_once()
            mock_destroy.assert_called_once_with(12345)
        finally:
            os.environ.pop("ANT_BUILD_AWS_ACCESS_KEY_ID", None)
            os.environ.pop("ANT_BUILD_AWS_SECRET_ACCESS_KEY", None)

    @patch("saorsa_deploy.cmd.build.create_build_vm")
    def test_exits_without_aws_credentials(self, mock_create):
        os.environ.pop("ANT_BUILD_AWS_ACCESS_KEY_ID", None)
        os.environ.pop("ANT_BUILD_AWS_SECRET_ACCESS_KEY", None)

        from saorsa_deploy.cmd.build import cmd_build

        args = SimpleNamespace(
            name="DEV-01",
            branch_name="feature-x",
            repo_owner="myorg",
            ssh_key_path="~/.ssh/id_rsa",
            validation_string=None,
            build_vm_size=None,
        )
        with pytest.raises(SystemExit):
            cmd_build(args)

        mock_create.assert_not_called()

    @patch("saorsa_deploy.cmd.build.create_build_vm")
    @patch("saorsa_deploy.cmd.build.validate_branch_exists")
    def test_exits_before_droplet_when_branch_missing(self, mock_validate, mock_create):
        os.environ["ANT_BUILD_AWS_ACCESS_KEY_ID"] = "test-key"
        os.environ["ANT_BUILD_AWS_SECRET_ACCESS_KEY"] = "test-secret"
        mock_validate.side_effect = ValueError("Branch 'nope' was not found")
        try:
            from saorsa_deploy.cmd.build import cmd_build

            args = SimpleNamespace(
                name="DEV-01",
                branch_name="nope",
                repo_owner="myorg",
                ssh_key_path="~/.ssh/id_rsa",
                validation_string=None,
                build_vm_size=None,
            )
            with pytest.raises(SystemExit):
                cmd_build(args)

            mock_validate.assert_called_once_with("myorg", "ant-node", "nope")
            mock_create.assert_not_called()
        finally:
            os.environ.pop("ANT_BUILD_AWS_ACCESS_KEY_ID", None)
            os.environ.pop("ANT_BUILD_AWS_SECRET_ACCESS_KEY", None)

    @patch("saorsa_deploy.cmd.build.validate_branch_exists")
    @patch("saorsa_deploy.cmd.build.clear_known_hosts")
    @patch("saorsa_deploy.cmd.build.AntNodeBuilder")
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
        mock_validate,
    ):
        os.environ["ANT_BUILD_AWS_ACCESS_KEY_ID"] = "test-key"
        os.environ["ANT_BUILD_AWS_SECRET_ACCESS_KEY"] = "test-secret"
        try:
            mock_create.return_value = {
                "droplet_id": 12345,
                "droplet_name": "saorsa-build-myorg-branch",
                "ip_address": "1.2.3.4",
            }
            mock_builder_cls.return_value.execute.side_effect = RuntimeError("build failed")

            from saorsa_deploy.cmd.build import cmd_build

            args = SimpleNamespace(
                name="DEV-01",
                branch_name="feature-x",
                repo_owner="myorg",
                ssh_key_path="~/.ssh/id_rsa",
                validation_string=None,
                build_vm_size=None,
                musl=False,
            )
            with pytest.raises(SystemExit):
                cmd_build(args)

            mock_destroy.assert_called_once_with(12345)
        finally:
            os.environ.pop("ANT_BUILD_AWS_ACCESS_KEY_ID", None)
            os.environ.pop("ANT_BUILD_AWS_SECRET_ACCESS_KEY", None)

    @patch("saorsa_deploy.cmd.build.validate_branch_exists")
    @patch("saorsa_deploy.cmd.build.clear_known_hosts")
    @patch("saorsa_deploy.cmd.build.AntNodeBuilder")
    @patch("saorsa_deploy.cmd.build.wait_for_ssh")
    @patch("saorsa_deploy.cmd.build.destroy_build_vm")
    @patch("saorsa_deploy.cmd.build.create_build_vm")
    def test_passes_validation_string_to_builder(
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
                "droplet_name": "saorsa-build-myorg-branch",
                "ip_address": "1.2.3.4",
            }
            mock_builder_cls.return_value.execute.return_value = "https://s3.example.com/binary"

            from saorsa_deploy.cmd.build import cmd_build

            args = SimpleNamespace(
                name="DEV-01",
                branch_name="feature-x",
                repo_owner="myorg",
                ssh_key_path="~/.ssh/id_rsa",
                validation_string="v0.42.0",
                build_vm_size=None,
                musl=False,
            )
            cmd_build(args)

            mock_builder_cls.assert_called_once_with(
                name="DEV-01",
                ip="1.2.3.4",
                ssh_key_path="~/.ssh/id_rsa",
                repo_owner="myorg",
                branch_name="feature-x",
                console=mock_builder_cls.call_args[1]["console"],
                validation_string="v0.42.0",
                musl=False,
                debug=False,
            )
        finally:
            os.environ.pop("ANT_BUILD_AWS_ACCESS_KEY_ID", None)
            os.environ.pop("ANT_BUILD_AWS_SECRET_ACCESS_KEY", None)


class TestAntNodeBuilderValidation:
    @patch("saorsa_deploy.provisioning.build.run_ops")
    @patch("saorsa_deploy.provisioning.build.disconnect_all")
    @patch("saorsa_deploy.provisioning.build.connect_all")
    @patch("saorsa_deploy.provisioning.build.add_op")
    @patch("saorsa_deploy.provisioning.build.State")
    @patch("saorsa_deploy.provisioning.build.Inventory")
    def test_adds_validation_step_when_string_provided(
        self,
        mock_inventory,
        mock_state,
        mock_add_op,
        mock_connect,
        mock_disconnect,
        mock_run_ops,
    ):
        os.environ["ANT_BUILD_AWS_ACCESS_KEY_ID"] = "test-key"
        os.environ["ANT_BUILD_AWS_SECRET_ACCESS_KEY"] = "test-secret"
        mock_state.return_value.failed_hosts = set()
        try:
            from saorsa_deploy.provisioning.build import AntNodeBuilder

            builder = AntNodeBuilder(
                name="DEV-01",
                ip="1.2.3.4",
                ssh_key_path="~/.ssh/id_rsa",
                repo_owner="myorg",
                branch_name="feature-x",
                validation_string="v0.42.0",
            )

            with patch(
                "saorsa_deploy.binary_source.get_custom_build_url",
                return_value="https://s3.example.com/binary",
            ):
                builder.execute()

            op_names = [c.kwargs["name"] for c in mock_add_op.call_args_list]
            assert "Validate binary" in op_names

            validate_call = [
                c for c in mock_add_op.call_args_list if c.kwargs.get("name") == "Validate binary"
            ][0]
            cmd = validate_call.kwargs["commands"][0]
            assert "strings /root/ant-node/target/release/ant-node" in cmd
            assert "grep -q 'v0.42.0'" in cmd
        finally:
            os.environ.pop("ANT_BUILD_AWS_ACCESS_KEY_ID", None)
            os.environ.pop("ANT_BUILD_AWS_SECRET_ACCESS_KEY", None)

    @patch("saorsa_deploy.provisioning.build.run_ops")
    @patch("saorsa_deploy.provisioning.build.disconnect_all")
    @patch("saorsa_deploy.provisioning.build.connect_all")
    @patch("saorsa_deploy.provisioning.build.add_op")
    @patch("saorsa_deploy.provisioning.build.State")
    @patch("saorsa_deploy.provisioning.build.Inventory")
    def test_skips_validation_when_no_string(
        self,
        mock_inventory,
        mock_state,
        mock_add_op,
        mock_connect,
        mock_disconnect,
        mock_run_ops,
    ):
        os.environ["ANT_BUILD_AWS_ACCESS_KEY_ID"] = "test-key"
        os.environ["ANT_BUILD_AWS_SECRET_ACCESS_KEY"] = "test-secret"
        mock_state.return_value.failed_hosts = set()
        try:
            from saorsa_deploy.provisioning.build import AntNodeBuilder

            builder = AntNodeBuilder(
                name="DEV-01",
                ip="1.2.3.4",
                ssh_key_path="~/.ssh/id_rsa",
                repo_owner="myorg",
                branch_name="feature-x",
            )

            with patch(
                "saorsa_deploy.binary_source.get_custom_build_url",
                return_value="https://s3.example.com/binary",
            ):
                builder.execute()

            op_names = [c.kwargs["name"] for c in mock_add_op.call_args_list]
            assert "Validate binary" not in op_names
        finally:
            os.environ.pop("ANT_BUILD_AWS_ACCESS_KEY_ID", None)
            os.environ.pop("ANT_BUILD_AWS_SECRET_ACCESS_KEY", None)

    @patch("saorsa_deploy.provisioning.build.run_ops")
    @patch("saorsa_deploy.provisioning.build.disconnect_all")
    @patch("saorsa_deploy.provisioning.build.connect_all")
    @patch("saorsa_deploy.provisioning.build.add_op")
    @patch("saorsa_deploy.provisioning.build.State")
    @patch("saorsa_deploy.provisioning.build.Inventory")
    def test_validation_failure_raises_with_details(
        self,
        mock_inventory,
        mock_state,
        mock_add_op,
        mock_connect,
        mock_disconnect,
        mock_run_ops,
    ):
        os.environ["ANT_BUILD_AWS_ACCESS_KEY_ID"] = "test-key"
        os.environ["ANT_BUILD_AWS_SECRET_ACCESS_KEY"] = "test-secret"
        mock_state.return_value.failed_hosts = set()

        mock_op_meta = SimpleNamespace(is_complete=lambda: True, did_error=lambda: True)
        mock_add_op.return_value = {"1.2.3.4": mock_op_meta}

        try:
            from saorsa_deploy.provisioning.build import AntNodeBuilder

            builder = AntNodeBuilder(
                name="DEV-01",
                ip="1.2.3.4",
                ssh_key_path="~/.ssh/id_rsa",
                repo_owner="myorg",
                branch_name="feature-x",
                validation_string="v0.42.0",
            )

            with pytest.raises(RuntimeError, match="Binary validation failed"):
                with patch(
                    "saorsa_deploy.binary_source.get_custom_build_url",
                    return_value="https://s3.example.com/binary",
                ):
                    builder.execute()
        finally:
            os.environ.pop("ANT_BUILD_AWS_ACCESS_KEY_ID", None)
            os.environ.pop("ANT_BUILD_AWS_SECRET_ACCESS_KEY", None)
