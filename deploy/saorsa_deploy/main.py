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
        "--region-counts",
        type=str,
        default="3",
        help="Comma-separated region counts per provider (default: 3)",
    )
    infra_parser.add_argument(
        "--testnet",
        action="store_true",
        help="Run in testnet mode (Digital Ocean only, lon1 region)",
    )
    infra_parser.add_argument(
        "--vm-count",
        type=int,
        required=True,
        help="Number of VMs per provider per region",
    )

    # === provision ===
    provision_parser = subparsers.add_parser("provision", help="Provision nodes on all VMs")
    provision_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built binary from this branch (requires --repo-owner)",
    )
    provision_parser.add_argument(
        "--ip-version",
        type=str,
        choices=["v4", "v6"],
        help="IP version the nodes will run with (v4 or v6)",
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
        help="Run the nodes with the --testnet flag",
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
        "--ip-version",
        type=str,
        choices=["v4", "v6"],
        help="IP version the node will run with (v4 or v6)",
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
        help="Run the node with the --testnet flag",
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

    # === provision-telegraf-logs (formerly provision-telegraf) ===
    telegraf_parser = subparsers.add_parser(
        "provision-telegraf-logs",
        help="Install and configure Telegraf for Elasticsearch log forwarding",
    )
    telegraf_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with infra command)",
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
    bootstrap_prod_parser.add_argument(
        "--node-version",
        type=str,
        default=None,
        help="Specific ant-node release version (e.g., 0.3.0). Uses latest if not specified.",
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
        "--ip-version",
        type=str,
        default=None,
        choices=["ipv4", "ipv6", "dual"],
        help="IP version for the node (default: dual). Use ipv4 to work around dual-stack bugs.",
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
    elif args.command == "destroy":
        from saorsa_deploy.cmd.destroy import cmd_destroy

        cmd_destroy(args)
    elif args.command == "infra":
        from saorsa_deploy.cmd.infra import cmd_infra

        cmd_infra(args)
    elif args.command == "provision":
        from saorsa_deploy.cmd.provision import cmd_provision

        cmd_provision(args)
    elif args.command == "provision-genesis":
        from saorsa_deploy.cmd.provision_genesis import cmd_provision_genesis

        cmd_provision_genesis(args)
    elif args.command == "provision-telegraf-logs":
        from saorsa_deploy.cmd.provision_telegraf import cmd_provision_telegraf

        cmd_provision_telegraf(args)
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
