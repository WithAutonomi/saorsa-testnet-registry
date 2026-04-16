import argparse
import sys
from importlib.metadata import version


def main():
    parser = argparse.ArgumentParser(
        prog="saorsa-deploy",
        description="Deploy testnets for ant-node using Terraform and Pyinfra",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('saorsa-deploy')}",
    )
    subparsers = parser.add_subparsers(dest="command")

    # === build-ant-node-binary ===
    build_parser = subparsers.add_parser(
        "build-ant-node-binary", help="Build ant-node from source and upload to S3"
    )
    build_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (used to isolate build artifacts)",
    )
    build_parser.add_argument(
        "--branch-name",
        type=str,
        required=True,
        help="Git branch to build from",
    )
    build_parser.add_argument(
        "--repo-owner",
        type=str,
        required=True,
        help="GitHub repository owner (e.g., WithAutonomi)",
    )
    build_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning the build VM (default: ~/.ssh/id_rsa)",
    )

    # === build-ant-binary ===
    build_ant_parser = subparsers.add_parser(
        "build-ant-binary", help="Build ant client binary from source and upload to S3"
    )
    build_ant_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (used to isolate build artifacts)",
    )
    build_ant_parser.add_argument(
        "--branch-name",
        type=str,
        required=True,
        help="Git branch to build from",
    )
    build_ant_parser.add_argument(
        "--repo-owner",
        type=str,
        required=True,
        help="GitHub repository owner (e.g., WithAutonomi)",
    )
    build_ant_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning the build VM (default: ~/.ssh/id_rsa)",
    )

    # === destroy ===
    destroy_parser = subparsers.add_parser("destroy", help="Destroy testnet infrastructure")
    destroy_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )
    destroy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name to destroy",
    )

    # === infra ===
    infra_parser = subparsers.add_parser("infra", help="Manage testnet infrastructure")
    infra_parser.add_argument(
        "--force",
        action="store_true",
        help="Delete stale Terraform state files and proceed",
    )
    infra_parser.add_argument(
        "--attached-volume-size",
        type=int,
        default=20,
        help="Size of attached volume in GB (default: 20)",
    )
    infra_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (used as prefix in VM names, e.g. DEV-01)",
    )
    infra_parser.add_argument(
        "--do-vm-count",
        type=int,
        default=0,
        help="Number of VMs per region on Digital Ocean (default: 0, provider not used)",
    )
    infra_parser.add_argument(
        "--do-regions",
        type=int,
        default=3,
        help="Number of Digital Ocean regions to use (default: 3)",
    )
    infra_parser.add_argument(
        "--do-droplet-size",
        type=str,
        default="s-2vcpu-4gb",
        help="Digital Ocean droplet size (default: s-2vcpu-4gb)",
    )
    infra_parser.add_argument(
        "--hetzner-vm-count",
        type=int,
        default=0,
        help="Number of VMs per region on Hetzner (default: 0, provider not used)",
    )
    infra_parser.add_argument(
        "--hetzner-regions",
        type=int,
        default=3,
        help="Number of Hetzner regions to use (default: 3)",
    )
    infra_parser.add_argument(
        "--hetzner-server-type",
        type=str,
        default="cpx22",
        help="Hetzner server type (default: cpx22). Alternatives: cx23, cax11",
    )
    infra_parser.add_argument(
        "--vultr-vm-count",
        type=int,
        default=0,
        help="Number of VMs per region on Vultr (default: 0, provider not used)",
    )
    infra_parser.add_argument(
        "--vultr-plan",
        type=str,
        default="vc2-2c-4gb",
        help="Vultr instance plan (default: vc2-2c-4gb)",
    )
    infra_parser.add_argument(
        "--vultr-regions",
        type=int,
        default=3,
        help="Number of Vultr regions to use (default: 3)",
    )
    infra_parser.add_argument(
        "--bootstrap-count",
        type=int,
        default=1,
        help="Number of bootstrap nodes (1-7, default: 1). "
        "Node #1 is the genesis node. Additional nodes are geographically "
        "distributed across Digital Ocean regions.",
    )

    # === infra-client ===
    infra_client_parser = subparsers.add_parser(
        "infra-client", help="Create client VMs on Digital Ocean"
    )
    infra_client_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (used as prefix in VM names)",
    )
    infra_client_parser.add_argument(
        "--client",
        type=str,
        action="append",
        help="Client VM spec: 'region:file_size_mb[:vm_size]'. "
        "Repeat for multiple clients. Default VM size: s-2vcpu-4gb. "
        "Example: --client 'lon1:50' --client 'nyc1:100:s-4vcpu-8gb'",
    )
    infra_client_parser.add_argument(
        "--force",
        action="store_true",
        help="Delete stale Terraform state files and proceed",
    )

    # === provision ===
    provision_parser = subparsers.add_parser("provision", help="Provision nodes on all VMs")
    provision_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built binary from this branch (requires --repo-owner)",
    )
    provision_parser.add_argument(
        "--log-level",
        type=str,
        help="Logging level the nodes will run with",
    )
    provision_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with infra command)",
    )
    provision_parser.add_argument(
        "--node-count",
        type=int,
        required=True,
        help="Number of node services per VM",
    )
    provision_parser.add_argument(
        "--node-version",
        type=str,
        help="Specific release version to deploy (e.g., 0.2.0)",
    )
    provision_parser.add_argument(
        "--port",
        type=int,
        help="Beginning of a port range from PORT to PORT+N (omit for random ports)",
    )
    provision_parser.add_argument(
        "--region",
        type=str,
        help="Provision only VMs in this region (e.g., digitalocean/lon1)",
    )
    provision_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built binary (requires --branch-name)",
    )
    provision_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )
    provision_parser.add_argument(
        "--testnet",
        action="store_true",
        help="Run nodes with --network-mode testnet",
    )
    provision_parser.add_argument(
        "--upgrade-channel",
        type=str,
        choices=["beta", "stable"],
        help="Upgrade channel for ant-node (beta or stable)",
    )
    provision_parser.add_argument(
        "--rewards-address",
        type=str,
        default="0x03B770D9cD32077cC0bF330c13C114a87643B124",
        help="Arbitrum wallet address for node rewards "
        "(default: 0x03B770D9cD32077cC0bF330c13C114a87643B124)",
    )

    # === provision-genesis ===
    provision_genesis_parser = subparsers.add_parser(
        "provision-genesis", help="Provision the genesis node"
    )
    provision_genesis_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built binary from this branch (requires --repo-owner)",
    )
    provision_genesis_parser.add_argument(
        "--log-level",
        type=str,
        help="Logging level the node will run with",
    )
    provision_genesis_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with infra command)",
    )
    provision_genesis_parser.add_argument(
        "--node-version",
        type=str,
        help="Specific release version to deploy (e.g., 0.2.0)",
    )
    provision_genesis_parser.add_argument(
        "--port",
        type=int,
        required=True,
        help="Port the genesis node will run with (required for bootstrap address)",
    )
    provision_genesis_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built binary (requires --branch-name)",
    )
    provision_genesis_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )
    provision_genesis_parser.add_argument(
        "--testnet",
        action="store_true",
        help="Run node with --network-mode testnet",
    )
    provision_genesis_parser.add_argument(
        "--upgrade-channel",
        type=str,
        choices=["beta", "stable"],
        help="Upgrade channel for ant-node (beta or stable)",
    )
    provision_genesis_parser.add_argument(
        "--rewards-address",
        type=str,
        default="0x03B770D9cD32077cC0bF330c13C114a87643B124",
        help="Arbitrum wallet address for node rewards "
        "(default: 0x03B770D9cD32077cC0bF330c13C114a87643B124)",
    )

    # === provision-bootstraps ===
    provision_bootstraps_parser = subparsers.add_parser(
        "provision-bootstraps",
        help="Provision additional bootstrap nodes (index 2+) after genesis",
    )
    provision_bootstraps_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built binary from this branch (requires --repo-owner)",
    )
    provision_bootstraps_parser.add_argument(
        "--log-level",
        type=str,
        help="Logging level the nodes will run with",
    )
    provision_bootstraps_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with infra command)",
    )
    provision_bootstraps_parser.add_argument(
        "--node-version",
        type=str,
        help="Specific release version to deploy (e.g., 0.2.0)",
    )
    provision_bootstraps_parser.add_argument(
        "--port",
        type=int,
        required=True,
        help="Port the bootstrap nodes will run with",
    )
    provision_bootstraps_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built binary (requires --branch-name)",
    )
    provision_bootstraps_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )
    provision_bootstraps_parser.add_argument(
        "--testnet",
        action="store_true",
        help="Run nodes with --network-mode testnet",
    )
    provision_bootstraps_parser.add_argument(
        "--upgrade-channel",
        type=str,
        choices=["beta", "stable"],
        help="Upgrade channel for ant-node (beta or stable)",
    )
    provision_bootstraps_parser.add_argument(
        "--rewards-address",
        type=str,
        default="0x03B770D9cD32077cC0bF330c13C114a87643B124",
        help="Arbitrum wallet address for node rewards "
        "(default: 0x03B770D9cD32077cC0bF330c13C114a87643B124)",
    )

    # === provision-client ===
    provision_client_parser = subparsers.add_parser(
        "provision-client",
        help="Provision client VMs with uploader/downloader services",
    )
    provision_client_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with infra-client)",
    )
    provision_client_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built ant binary from this branch (requires --repo-owner)",
    )
    provision_client_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built ant binary (requires --branch-name)",
    )
    provision_client_parser.add_argument(
        "--ant-version",
        type=str,
        help="Specific release version of ant to deploy (e.g., 0.3.0)",
    )
    provision_client_parser.add_argument(
        "--influx-url",
        type=str,
        default=None,
        help="InfluxDB URL for metrics (e.g., http://1.2.3.4:8086)",
    )
    provision_client_parser.add_argument(
        "--influx-token",
        type=str,
        default=None,
        help="InfluxDB API token for metrics",
    )
    provision_client_parser.add_argument(
        "--influx-org",
        type=str,
        default="ant",
        help="InfluxDB organization (default: ant)",
    )
    provision_client_parser.add_argument(
        "--influx-bucket",
        type=str,
        default="testnet-metrics",
        help="InfluxDB bucket (default: testnet-metrics)",
    )
    provision_client_parser.add_argument(
        "--monitoring-name",
        type=str,
        default=None,
        help="Load InfluxDB config from monitoring state with this name "
        "(alternative to --influx-url/--influx-token)",
    )
    provision_client_parser.add_argument(
        "--wallet-secret-key",
        type=str,
        default=None,
        help="EVM wallet private key for uploads. "
        "Falls back to ANT_DEPLOY_SECRET_KEY env var if not provided.",
    )
    provision_client_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )

    # === start-clients ===
    start_clients_parser = subparsers.add_parser(
        "start-clients", help="Start uploader/downloader services on client VMs"
    )
    start_clients_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with infra-client)",
    )
    start_clients_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa)",
    )

    # === stop-clients ===
    stop_clients_parser = subparsers.add_parser(
        "stop-clients", help="Stop uploader/downloader services on client VMs"
    )
    stop_clients_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with infra-client)",
    )
    stop_clients_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa)",
    )

    # === provision-telegraf-logs (formerly provision-telegraf) ===
    telegraf_parser = subparsers.add_parser(
        "provision-telegraf-logs",
        help="Install and configure Telegraf for Elasticsearch log forwarding",
    )
    telegraf_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Deployment name (must match the name used with infra command). "
        "Required unless --host-ips-file is provided.",
    )
    telegraf_parser.add_argument(
        "--elasticsearch-url",
        type=str,
        default=None,
        help="Elasticsearch endpoint URL (auto-loaded from elastic state if --name is provided)",
    )
    telegraf_parser.add_argument(
        "--elasticsearch-username",
        type=str,
        default=None,
        help="Elasticsearch username (auto-loaded from elastic state if --name is provided)",
    )
    telegraf_parser.add_argument(
        "--elasticsearch-password",
        type=str,
        default=None,
        help="Elasticsearch password (auto-loaded from elastic state if --name is provided)",
    )
    telegraf_parser.add_argument(
        "--node-version",
        type=str,
        default=None,
        help="Version of the deployed ant-node binary",
    )
    telegraf_parser.add_argument(
        "--branch-commit",
        type=str,
        default=None,
        help="Git branch or commit of the deployed binary",
    )
    telegraf_parser.add_argument(
        "--repo-owner",
        type=str,
        default="maidsafe",
        help="Repository owner for branch builds (default: maidsafe)",
    )
    telegraf_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )
    telegraf_parser.add_argument(
        "--ssh-user",
        type=str,
        default="root",
        help="SSH username for provisioning (default: root)",
    )
    telegraf_parser.add_argument(
        "--host-ips-file",
        type=str,
        default=None,
        help="Path to a text file containing one IP address per line "
        "(alternative to loading IPs from deployment state)",
    )
    telegraf_parser.add_argument(
        "--inventory-path",
        type=str,
        default=None,
        help="Path to an inventory JSON file. Reads ip, ssh_user, and ssh_key "
        "from each entry in the node_vms array.",
    )

    # === destroy-clients ===
    destroy_clients_parser = subparsers.add_parser(
        "destroy-clients", help="Destroy client VM infrastructure"
    )
    destroy_clients_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with infra-client)",
    )
    destroy_clients_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )

    # === destroy-elastic ===
    destroy_elastic_parser = subparsers.add_parser(
        "destroy-elastic", help="Destroy Elasticsearch infrastructure"
    )
    destroy_elastic_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )
    destroy_elastic_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Elasticsearch stack name to destroy",
    )

    # === destroy-monitoring ===
    destroy_monitoring_parser = subparsers.add_parser(
        "destroy-monitoring", help="Destroy monitoring infrastructure"
    )
    destroy_monitoring_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )
    destroy_monitoring_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Monitoring stack name to destroy",
    )

    # === elastic-infra ===
    elastic_infra_parser = subparsers.add_parser(
        "elastic-infra", help="Create Elasticsearch VM infrastructure"
    )
    elastic_infra_parser.add_argument(
        "--force",
        action="store_true",
        help="Delete stale Terraform state files and proceed",
    )
    elastic_infra_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Elasticsearch stack name (used as prefix in VM names)",
    )
    elastic_infra_parser.add_argument(
        "--region",
        type=str,
        default="lon1",
        help="Region for the Elasticsearch VM (default: lon1)",
    )
    elastic_infra_parser.add_argument(
        "--volume-size",
        type=int,
        default=100,
        help="Size of attached volume in GB for Elasticsearch data (default: 100)",
    )
    elastic_infra_parser.add_argument(
        "--droplet-size",
        type=str,
        default="s-4vcpu-8gb",
        help="Droplet size slug (default: s-4vcpu-8gb). "
        "Use s-8vcpu-16gb or larger for 5000+ node testnets.",
    )

    # === elastic-info ===
    elastic_info_parser = subparsers.add_parser(
        "elastic-info", help="Print Elasticsearch stack connection details"
    )
    elastic_info_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Elasticsearch stack name",
    )

    # === monitoring-infra ===
    monitoring_infra_parser = subparsers.add_parser(
        "monitoring-infra", help="Create monitoring VM infrastructure"
    )
    monitoring_infra_parser.add_argument(
        "--force",
        action="store_true",
        help="Delete stale Terraform state files and proceed",
    )
    monitoring_infra_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Monitoring stack name (used as prefix in VM names)",
    )
    monitoring_infra_parser.add_argument(
        "--region",
        type=str,
        default="lon1",
        help="Region for the monitoring VM (default: lon1)",
    )
    monitoring_infra_parser.add_argument(
        "--volume-size",
        type=int,
        default=50,
        help="Size of attached volume in GB for InfluxDB data (default: 50)",
    )

    # === monitoring-info ===
    monitoring_info_parser = subparsers.add_parser(
        "monitoring-info", help="Print monitoring stack connection details"
    )
    monitoring_info_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Monitoring stack name",
    )
    monitoring_info_parser.add_argument(
        "--telegraf-config",
        action="store_true",
        help="Emit a complete Telegraf TOML config to stdout",
    )

    # === provision-elastic ===
    provision_elastic_parser = subparsers.add_parser(
        "provision-elastic", help="Provision Elasticsearch on Elasticsearch VM"
    )
    provision_elastic_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Elasticsearch stack name (must match the name used with elastic-infra)",
    )
    provision_elastic_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )

    # === provision-monitoring ===
    provision_monitoring_parser = subparsers.add_parser(
        "provision-monitoring", help="Provision InfluxDB + Grafana on monitoring VM"
    )
    provision_monitoring_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Monitoring stack name (must match the name used with monitoring-infra)",
    )
    provision_monitoring_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )

    # === provision-telegraf-metrics ===
    telegraf_metrics_parser = subparsers.add_parser(
        "provision-telegraf-metrics",
        help="Install and configure Telegraf for InfluxDB metrics forwarding",
    )
    telegraf_metrics_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Deployment name (loads VM IPs from deployment state)",
    )
    telegraf_metrics_parser.add_argument(
        "--host-ips",
        type=str,
        default=None,
        help="Comma-separated list of host IPs to provision (alternative to --name)",
    )
    telegraf_metrics_parser.add_argument(
        "--influxdb-url",
        type=str,
        default=None,
        help="InfluxDB URL (auto-loaded from monitoring state if --name is provided)",
    )
    telegraf_metrics_parser.add_argument(
        "--influxdb-token",
        type=str,
        default=None,
        help="InfluxDB API token (auto-loaded from monitoring state if --name is provided)",
    )
    telegraf_metrics_parser.add_argument(
        "--influxdb-org",
        type=str,
        default="ant",
        help="InfluxDB organization (default: ant)",
    )
    telegraf_metrics_parser.add_argument(
        "--influxdb-bucket",
        type=str,
        default="testnet-metrics",
        help="InfluxDB bucket (default: testnet-metrics)",
    )
    telegraf_metrics_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )
    telegraf_metrics_parser.add_argument(
        "--ssh-user",
        type=str,
        default="root",
        help="SSH username for provisioning (default: root)",
    )
    telegraf_metrics_parser.add_argument(
        "--host-ips-file",
        type=str,
        default=None,
        help="Path to a text file containing one IP address per line "
        "(alternative to --host-ips or loading IPs from deployment state)",
    )
    telegraf_metrics_parser.add_argument(
        "--inventory-path",
        type=str,
        default=None,
        help="Path to an inventory JSON file. Reads ip, ssh_user, and ssh_key "
        "from each entry in the node_vms array.",
    )

    # === bootstrap-production ===
    bootstrap_prod_parser = subparsers.add_parser(
        "bootstrap-production",
        help="Deploy production bootstrap nodes across multiple providers",
    )
    bootstrap_prod_parser.add_argument(
        "--name",
        type=str,
        default="ant-production",
        help="Deployment name (default: ant-production)",
    )
    bootstrap_prod_binary = bootstrap_prod_parser.add_mutually_exclusive_group()
    bootstrap_prod_binary.add_argument(
        "--node-version",
        type=str,
        default=None,
        help="Specific ant-node release version (e.g., 0.3.0). Uses latest if not specified.",
    )
    bootstrap_prod_binary.add_argument(
        "--path",
        type=str,
        default=None,
        help="Path to a local ant-node binary to upload to nodes.",
    )
    bootstrap_prod_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/ant-production",
        help="Path to SSH private key for provisioning (default: ~/.ssh/ant-production)",
    )
    bootstrap_prod_parser.add_argument(
        "--rewards-address",
        type=str,
        required=True,
        help="Arbitrum wallet address for node rewards",
    )
    bootstrap_prod_parser.add_argument(
        "--evm-network",
        type=str,
        default="arbitrum-one",
        choices=["arbitrum-one", "arbitrum-sepolia"],
        help="EVM network for payments (default: arbitrum-one)",
    )
    bootstrap_prod_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompts and force overwrite stale state",
    )

    # === destroy-bootstrap-production ===
    destroy_prod_parser = subparsers.add_parser(
        "destroy-bootstrap-production",
        help="Destroy production bootstrap network infrastructure",
    )
    destroy_prod_parser.add_argument(
        "--name",
        type=str,
        default="ant-production",
        help="Deployment name (default: ant-production)",
    )
    destroy_prod_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "build-ant-node-binary":
        from saorsa_deploy.cmd.build import cmd_build

        cmd_build(args)
    elif args.command == "build-ant-binary":
        from saorsa_deploy.cmd.build_ant import cmd_build_ant

        cmd_build_ant(args)
    elif args.command == "destroy":
        from saorsa_deploy.cmd.destroy import cmd_destroy

        cmd_destroy(args)
    elif args.command == "infra":
        from saorsa_deploy.cmd.infra import cmd_infra

        cmd_infra(args)
    elif args.command == "infra-client":
        from saorsa_deploy.cmd.infra_client import cmd_infra_client

        cmd_infra_client(args)
    elif args.command == "provision":
        from saorsa_deploy.cmd.provision import cmd_provision

        cmd_provision(args)
    elif args.command == "provision-genesis":
        from saorsa_deploy.cmd.provision_genesis import cmd_provision_genesis

        cmd_provision_genesis(args)
    elif args.command == "provision-bootstraps":
        from saorsa_deploy.cmd.provision_bootstraps import cmd_provision_bootstraps

        cmd_provision_bootstraps(args)
    elif args.command == "provision-client":
        from saorsa_deploy.cmd.provision_client import cmd_provision_client

        cmd_provision_client(args)
    elif args.command == "start-clients":
        from saorsa_deploy.cmd.start_clients import cmd_start_clients

        cmd_start_clients(args)
    elif args.command == "stop-clients":
        from saorsa_deploy.cmd.stop_clients import cmd_stop_clients

        cmd_stop_clients(args)
    elif args.command == "provision-telegraf-logs":
        from saorsa_deploy.cmd.provision_telegraf import cmd_provision_telegraf

        cmd_provision_telegraf(args)
    elif args.command == "destroy-clients":
        from saorsa_deploy.cmd.destroy_clients import cmd_destroy_clients

        cmd_destroy_clients(args)
    elif args.command == "destroy-elastic":
        from saorsa_deploy.cmd.destroy_elastic import cmd_destroy_elastic

        cmd_destroy_elastic(args)
    elif args.command == "elastic-infra":
        from saorsa_deploy.cmd.elastic_infra import cmd_elastic_infra

        cmd_elastic_infra(args)
    elif args.command == "elastic-info":
        from saorsa_deploy.cmd.elastic_info import cmd_elastic_info

        cmd_elastic_info(args)
    elif args.command == "provision-elastic":
        from saorsa_deploy.cmd.provision_elastic import cmd_provision_elastic

        cmd_provision_elastic(args)
    elif args.command == "destroy-monitoring":
        from saorsa_deploy.cmd.destroy_monitoring import cmd_destroy_monitoring

        cmd_destroy_monitoring(args)
    elif args.command == "monitoring-infra":
        from saorsa_deploy.cmd.monitoring_infra import cmd_monitoring_infra

        cmd_monitoring_infra(args)
    elif args.command == "monitoring-info":
        from saorsa_deploy.cmd.monitoring_info import cmd_monitoring_info

        cmd_monitoring_info(args)
    elif args.command == "provision-monitoring":
        from saorsa_deploy.cmd.provision_monitoring import cmd_provision_monitoring

        cmd_provision_monitoring(args)
    elif args.command == "provision-telegraf-metrics":
        from saorsa_deploy.cmd.provision_telegraf_metrics import (
            cmd_provision_telegraf_metrics,
        )

        cmd_provision_telegraf_metrics(args)
    elif args.command == "bootstrap-production":
        from saorsa_deploy.cmd.bootstrap_production import cmd_bootstrap_production

        cmd_bootstrap_production(args)
    elif args.command == "destroy-bootstrap-production":
        from saorsa_deploy.cmd.destroy_bootstrap_production import (
            cmd_destroy_bootstrap_production,
        )

        cmd_destroy_bootstrap_production(args)


if __name__ == "__main__":
    main()
