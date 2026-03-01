import os

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.binary_source import BUILDS_BUCKET, BUILDS_KEY_PREFIX


class SaorsaNodeBuilder:
    """Builds saorsa-node from source on a remote host and uploads to S3."""

    def __init__(
        self,
        ip: str,
        ssh_key_path: str,
        repo_owner: str,
        branch_name: str,
        console: Console | None = None,
    ):
        self.ip = ip
        self.ssh_key_path = ssh_key_path
        self.repo_owner = repo_owner
        self.branch_name = branch_name
        self.console = console or Console()
        self.s3_key = f"{BUILDS_KEY_PREFIX}/{repo_owner}/{branch_name}/saorsa-node"

    def execute(self) -> str:
        """Build saorsa-node and upload to S3. Returns the S3 URL of the uploaded binary."""
        aws_access_key = os.environ.get("SAORSA_BUILD_AWS_ACCESS_KEY_ID")
        aws_secret_key = os.environ.get("SAORSA_BUILD_AWS_SECRET_ACCESS_KEY")
        if not aws_access_key or not aws_secret_key:
            raise RuntimeError(
                "SAORSA_BUILD_AWS_ACCESS_KEY_ID and SAORSA_BUILD_AWS_SECRET_ACCESS_KEY must be set"
            )

        self.console.print(f"Connecting to {self.ip} as root...")
        inventory = Inventory(
            (
                [
                    (
                        self.ip,
                        {"ssh_user": "root", "ssh_key": self.ssh_key_path},
                    ),
                ],
                {},
            ),
        )
        state = State(inventory=inventory, config=Config())
        connect_all(state)

        try:
            add_op(
                state,
                server.shell,
                name="Wait for cloud-init to finish",
                commands=["cloud-init status --wait"],
            )

            add_op(
                state,
                server.shell,
                name="Install build dependencies",
                commands=[
                    "apt-get update -qq && apt-get install -y -qq "
                    "curl build-essential pkg-config libssl-dev git unzip"
                ],
            )

            add_op(
                state,
                server.shell,
                name="Install AWS CLI v2",
                commands=[
                    'curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" '
                    "-o /tmp/awscliv2.zip && "
                    "unzip -q /tmp/awscliv2.zip -d /tmp && "
                    "/tmp/aws/install && "
                    "rm -rf /tmp/awscliv2.zip /tmp/aws"
                ],
            )

            add_op(
                state,
                server.shell,
                name="Install Rust toolchain",
                commands=[
                    "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"
                ],
            )

            add_op(
                state,
                server.shell,
                name=f"Clone {self.repo_owner}/saorsa-node ({self.branch_name})",
                commands=[
                    f"git clone --branch {self.branch_name} --depth 1 "
                    f"https://github.com/{self.repo_owner}/saorsa-node.git /root/saorsa-node"
                ],
            )

            add_op(
                state,
                server.shell,
                name="Build saorsa-node (release)",
                commands=[
                    "cd /root/saorsa-node && "
                    "/root/.cargo/bin/cargo build --release --bin saorsa-node"
                ],
            )

            add_op(
                state,
                server.shell,
                name="Upload binary to S3",
                commands=[
                    f"AWS_ACCESS_KEY_ID={aws_access_key} "
                    f"AWS_SECRET_ACCESS_KEY={aws_secret_key} "
                    f"aws s3 cp /root/saorsa-node/target/release/saorsa-node "
                    f"s3://{BUILDS_BUCKET}/{self.s3_key}"
                ],
            )

            self.console.print("Running build operations...")
            run_ops(state)
        finally:
            disconnect_all(state)

        from saorsa_deploy.binary_source import get_custom_build_url

        return get_custom_build_url(self.repo_owner, self.branch_name)
