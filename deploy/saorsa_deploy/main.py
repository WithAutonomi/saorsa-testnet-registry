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

    def _add_print_file_ops_args(p):
        p.add_argument(
            "--name",
            type=str,
            required=True,
            help="Deployment name (used to locate the monitoring stack)",
        )
        p.add_argument(
            "--monitoring-name",
            type=str,
            default=None,
            help=(
                "Monitoring stack name to load Influx credentials from "
                "(defaults to --name)."
            ),
        )
        p.add_argument(
            "--influx-url",
            type=str,
            default=None,
            help="InfluxDB URL (overrides monitoring state lookup)",
        )
        p.add_argument(
            "--influx-token",
            type=str,
            default=None,
            help="InfluxDB API token (overrides monitoring state lookup)",
        )
        p.add_argument(
            "--influx-org",
            type=str,
            default=None,
            help="InfluxDB organization (default: from monitoring state, else 'ant')",
        )
        p.add_argument(
            "--influx-bucket",
            type=str,
            default=None,
            help=(
                "InfluxDB bucket (default: from monitoring state, else "
                "'testnet-metrics')"
            ),
        )
        p.add_argument(
            "--window",
            type=str,
            default="-30d",
            help="Flux range start (e.g. -30d, -7d, -24h). Default: -30d",
        )
        mode = p.add_mutually_exclusive_group()
        mode.add_argument(
            "--recent",
            action="store_true",
            help="Only show the 10 most recent operations per client",
        )
        mode.add_argument(
            "--summary",
            action="store_true",
            help=(
                "Print a per-host aggregate summary (start time, elapsed, "
                "total/success/failure counts, and average duration) instead "
                "of per-operation tables."
            ),
        )

    def _add_prod_upload_query_args(p):
        p.add_argument(
            "--name",
            type=str,
            required=True,
            help="Deployment name (used to locate the monitoring stack).",
        )
        p.add_argument(
            "--monitoring-name",
            type=str,
            default=None,
            help=(
                "Monitoring stack name to load Influx credentials from "
                "(defaults to --name)."
            ),
        )
        p.add_argument(
            "--influx-url",
            type=str,
            default=None,
            help="InfluxDB URL (overrides monitoring state lookup).",
        )
        p.add_argument(
            "--influx-token",
            type=str,
            default=None,
            help="InfluxDB API token (overrides monitoring state lookup).",
        )
        p.add_argument(
            "--influx-org",
            type=str,
            default=None,
            help="InfluxDB organization (default: from monitoring state, else 'ant').",
        )
        p.add_argument(
            "--influx-bucket",
            type=str,
            default=None,
            help=(
                "InfluxDB bucket (default: from monitoring state, else "
                "'testnet-metrics')."
            ),
        )

    # =========================================================================
    # === bootstraps group (clean, destroy, infra, provision) ===
    # =========================================================================
    bootstraps_parser = subparsers.add_parser(
        "bootstraps",
        help="Bootstrap-VM lifecycle: clean, destroy, infra, provision.",
    )
    bootstraps_subparsers = bootstraps_parser.add_subparsers(
        dest="bootstraps_subcommand", required=True
    )

    # bootstraps clean
    bootstraps_clean_parser = bootstraps_subparsers.add_parser(
        "clean",
        help=(
            "Remove ant-bootstrap-* services + data/log/binary from bootstrap "
            "VMs without destroying the VMs (covers genesis too)"
        ),
    )
    bootstraps_clean_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help=(
            "Deployment name (loads bootstrap IPs from deployment state). "
            "Required unless --host-ips-file is provided."
        ),
    )
    bootstraps_clean_parser.add_argument(
        "--host-ips-file",
        type=str,
        default=None,
        help="Path to a text file containing one IP address per line",
    )
    bootstraps_clean_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa)",
    )
    bootstraps_clean_parser.add_argument(
        "--ssh-user",
        type=str,
        default="root",
        help="SSH username (default: root)",
    )

    # bootstraps destroy
    bootstraps_destroy_parser = bootstraps_subparsers.add_parser(
        "destroy",
        help="Destroy bootstrap VMs (refuses if nodes are still up)",
    )
    bootstraps_destroy_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )
    bootstraps_destroy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name to destroy bootstraps for",
    )

    # bootstraps infra
    bootstraps_infra_parser = bootstraps_subparsers.add_parser(
        "infra",
        help="Create only the bootstrap VMs for a deployment",
    )
    bootstraps_infra_parser.add_argument(
        "--force",
        action="store_true",
        help="Destroy any orphan bootstrap Terraform state and proceed",
    )
    bootstraps_infra_parser.add_argument(
        "--attached-volume-size",
        type=int,
        default=20,
        help="Size of attached volume in GB (default: 20)",
    )
    bootstraps_infra_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (used as prefix in VM names, e.g. DEV-01)",
    )
    bootstraps_infra_parser.add_argument(
        "--bootstrap-count",
        type=int,
        default=1,
        help="Number of bootstrap nodes (1-7, default: 1). "
        "Node #1 is the genesis node. Additional nodes are geographically "
        "distributed across Digital Ocean regions. "
        "Ignored if --do-regions-list is set.",
    )
    bootstraps_infra_parser.add_argument(
        "--do-regions-list",
        type=str,
        default=None,
        help="Comma-separated list of specific Digital Ocean regions to use "
        "for bootstrap VMs (e.g. lon1,nyc1,ams3). Overrides --bootstrap-count "
        "and the default ordering; no fallback substitution. List length "
        "becomes the bootstrap count (1-7).",
    )

    # bootstraps provision
    bootstraps_provision_parser = bootstraps_subparsers.add_parser(
        "provision",
        help="Provision additional bootstrap nodes (index 2+) after genesis",
    )
    bootstraps_provision_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built binary from this branch (requires --repo-owner)",
    )
    bootstraps_provision_parser.add_argument(
        "--log-level",
        type=str,
        help="Logging level the nodes will run with",
    )
    bootstraps_provision_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with `bootstraps infra`)",
    )
    bootstraps_provision_parser.add_argument(
        "--node-version",
        type=str,
        help="Specific release version to deploy (e.g., 0.2.0)",
    )
    bootstraps_provision_parser.add_argument(
        "--port",
        type=int,
        required=True,
        help="Port the bootstrap nodes will run with",
    )
    bootstraps_provision_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built binary (requires --branch-name)",
    )
    bootstraps_provision_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )
    bootstraps_provision_parser.add_argument(
        "--testnet",
        action="store_true",
        help="Run nodes with --network-mode testnet",
    )
    bootstraps_provision_parser.add_argument(
        "--upgrade-channel",
        type=str,
        choices=["beta", "stable"],
        help="Upgrade channel for ant-node (beta or stable)",
    )
    bootstraps_provision_parser.add_argument(
        "--rewards-address",
        type=str,
        default="0x03B770D9cD32077cC0bF330c13C114a87643B124",
        help="Arbitrum wallet address for node rewards "
        "(default: 0x03B770D9cD32077cC0bF330c13C114a87643B124)",
    )

    # =========================================================================
    # === build group (ant, ant-node) ===
    # =========================================================================
    build_parser = subparsers.add_parser(
        "build",
        help="Build ant / ant-node binaries from source and upload to S3.",
    )
    build_subparsers = build_parser.add_subparsers(
        dest="build_subcommand", required=True
    )

    # build ant
    build_ant_parser = build_subparsers.add_parser(
        "ant", help="Build ant client binary from source and upload to S3"
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
    build_ant_parser.add_argument(
        "--validation-string",
        type=str,
        default=None,
        help="String to search for in the compiled binary using `strings`",
    )
    build_ant_parser.add_argument(
        "--build-vm-size",
        type=str,
        default=None,
        help="DigitalOcean droplet size for the build VM (default: c-32)",
    )

    # build ant-node
    build_ant_node_parser = build_subparsers.add_parser(
        "ant-node", help="Build ant-node from source and upload to S3"
    )
    build_ant_node_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (used to isolate build artifacts)",
    )
    build_ant_node_parser.add_argument(
        "--branch-name",
        type=str,
        required=True,
        help="Git branch to build from",
    )
    build_ant_node_parser.add_argument(
        "--repo-owner",
        type=str,
        required=True,
        help="GitHub repository owner (e.g., WithAutonomi)",
    )
    build_ant_node_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning the build VM (default: ~/.ssh/id_rsa)",
    )
    build_ant_node_parser.add_argument(
        "--validation-string",
        type=str,
        default=None,
        help="String to search for in the compiled binary using `strings`",
    )
    build_ant_node_parser.add_argument(
        "--build-vm-size",
        type=str,
        default=None,
        help="DigitalOcean droplet size for the build VM (default: c-32)",
    )

    # =========================================================================
    # === clients group (clean, destroy, infra, provision, start, stop) ===
    # =========================================================================
    clients_parser = subparsers.add_parser(
        "clients",
        help="Client-VM lifecycle: clean, destroy, infra, provision, start, stop.",
    )
    clients_subparsers = clients_parser.add_subparsers(
        dest="clients_subcommand", required=True
    )

    # clients clean
    clients_clean_parser = clients_subparsers.add_parser(
        "clean",
        help=(
            "Remove ant-uploader/ant-downloader services + data/binary from "
            "client VMs without destroying the VMs"
        ),
    )
    clients_clean_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help=(
            "Deployment name (loads client IPs from client state). "
            "Required unless --host-ips-file is provided."
        ),
    )
    clients_clean_parser.add_argument(
        "--host-ips-file",
        type=str,
        default=None,
        help="Path to a text file containing one IP address per line",
    )
    clients_clean_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa)",
    )
    clients_clean_parser.add_argument(
        "--ssh-user",
        type=str,
        default="root",
        help="SSH username (default: root)",
    )

    # clients destroy
    clients_destroy_parser = clients_subparsers.add_parser(
        "destroy", help="Destroy client VM infrastructure"
    )
    clients_destroy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with `clients infra`)",
    )
    clients_destroy_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )

    # clients infra
    clients_infra_parser = clients_subparsers.add_parser(
        "infra", help="Create client VMs on Digital Ocean"
    )
    clients_infra_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (used as prefix in VM names)",
    )
    clients_infra_parser.add_argument(
        "--uploader",
        type=str,
        action="append",
        default=[],
        help="Uploader VM spec: 'region:file_size_mb[:vm_size]'. "
        "Repeat for multiple uploaders. Default vm_size=s-2vcpu-4gb. "
        "Example: --uploader 'lon1:50' --uploader 'nyc1:100:s-4vcpu-8gb'",
    )
    clients_infra_parser.add_argument(
        "--downloader",
        type=str,
        action="append",
        default=[],
        help="Downloader VM spec: 'region[:vm_size]'. Repeat for multiple "
        "downloaders. Default vm_size=s-2vcpu-4gb. Example: "
        "--downloader 'lon1' --downloader 'nyc1:s-4vcpu-8gb'",
    )
    clients_infra_parser.add_argument(
        "--force",
        action="store_true",
        help="Delete stale Terraform state files and proceed",
    )

    # clients provision
    clients_provision_parser = clients_subparsers.add_parser(
        "provision",
        help="Provision client VMs with uploader/downloader services",
    )
    clients_provision_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with `clients infra`)",
    )
    clients_provision_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built ant binary from this branch (requires --repo-owner)",
    )
    clients_provision_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built ant binary (requires --branch-name)",
    )
    clients_provision_parser.add_argument(
        "--ant-version",
        type=str,
        help="Specific release version of ant to deploy (e.g., 0.3.0)",
    )
    clients_provision_parser.add_argument(
        "--influx-url",
        type=str,
        default=None,
        help="InfluxDB URL for metrics (e.g., http://1.2.3.4:8086)",
    )
    clients_provision_parser.add_argument(
        "--influx-token",
        type=str,
        default=None,
        help="InfluxDB API token for metrics",
    )
    clients_provision_parser.add_argument(
        "--influx-org",
        type=str,
        default="ant",
        help="InfluxDB organization (default: ant)",
    )
    clients_provision_parser.add_argument(
        "--influx-bucket",
        type=str,
        default="testnet-metrics",
        help="InfluxDB bucket (default: testnet-metrics)",
    )
    clients_provision_parser.add_argument(
        "--monitoring-name",
        type=str,
        default=None,
        help="Load InfluxDB config from monitoring state with this name "
        "(alternative to --influx-url/--influx-token)",
    )
    clients_provision_parser.add_argument(
        "--wallet-secret-key",
        type=str,
        default=None,
        help="EVM wallet private key for uploads. "
        "Falls back to ANT_DEPLOY_SECRET_KEY env var if not provided.",
    )
    clients_provision_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )
    clients_provision_parser.add_argument(
        "--addresses-file",
        type=str,
        default=None,
        help="Local path to a newline-separated list of public file addresses. "
        "When provided, the contents are written to /var/lib/ant-client/addresses.txt "
        "on every download-role client VM, and the downloader reads from this file "
        "instead of fetching from S3. Has no effect on upload-role VMs.",
    )
    clients_provision_parser.add_argument(
        "--prod",
        action="store_true",
        help="Point the client VMs at the canonical Autonomi production network. "
        "Fetches the bootstrap peers list from the WithAutonomi/ant-client repo "
        "at runtime, skips the usual testnet deployment-state bootstrap lookup, "
        "and relaxes wallet-secret-key / client-AWS-creds requirements (download-only).",
    )

    # clients start
    clients_start_parser = clients_subparsers.add_parser(
        "start", help="Start uploader/downloader services on client VMs"
    )
    clients_start_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with `clients infra`)",
    )
    clients_start_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa)",
    )

    # clients stop
    clients_stop_parser = clients_subparsers.add_parser(
        "stop", help="Stop uploader/downloader services on client VMs"
    )
    clients_stop_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with `clients infra`)",
    )
    clients_stop_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa)",
    )

    # =========================================================================
    # === deployments group ===
    # === extend, generate-inventory, ls, print-downloads, print-uploads, ===
    # === record-file-results, record-results, set-results-summary, unextend ===
    # =========================================================================
    deployments_parser = subparsers.add_parser(
        "deployments",
        help="Deployment lifecycle and result-recording operations.",
    )
    deployments_subparsers = deployments_parser.add_subparsers(
        dest="deployments_subcommand", required=True
    )

    # deployments extend
    deployments_extend_parser = deployments_subparsers.add_parser(
        "extend",
        help="Seed an extension testnet's state from an existing testnet's bootstraps",
    )
    deployments_extend_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Name of the new extension testnet (e.g. DEV-02)",
    )
    deployments_extend_parser.add_argument(
        "--source",
        type=str,
        required=True,
        help="Name of the existing testnet whose bootstraps will be reused "
        "(e.g. DEV-01). Must already have bootstraps recorded in state.",
    )
    deployments_extend_parser.add_argument(
        "--force",
        action="store_true",
        help="Repoint an existing extension at a different source",
    )

    # deployments generate-inventory
    deployments_generate_inventory_parser = deployments_subparsers.add_parser(
        "generate-inventory",
        help="Generate a schema-version-4 inventory.json for a deployment",
    )
    deployments_generate_inventory_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name",
    )
    deployments_generate_inventory_parser.add_argument(
        "--output",
        type=str,
        default=None,
        help=(
            "Output file path (default: ~/.autonomi/testnets/<name>/inventory.json)"
        ),
    )
    deployments_generate_inventory_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="SSH key path recorded per VM in the inventory (default: ~/.ssh/id_rsa)",
    )
    deployments_generate_inventory_parser.add_argument(
        "--node-count",
        type=int,
        default=None,
        help=(
            "Number of node services per VM. Defaults to the node_count saved by the "
            "provision command."
        ),
    )
    deployments_generate_inventory_parser.add_argument(
        "--wallet-secret-key",
        type=str,
        default=None,
        help="EVM wallet private key embedded in devnet-manifest.json. "
        "Falls back to ANT_DEPLOY_SECRET_KEY env var if not provided.",
    )

    # deployments ls
    deployments_ls_parser = deployments_subparsers.add_parser(
        "ls",
        help="List deployments registered in the database",
    )
    deployments_ls_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Show only deployments whose name contains this substring "
        "(case-insensitive).",
    )
    deployments_ls_parser.add_argument(
        "--recent",
        action="store_true",
        help="Show only deployments created in the last 7 days.",
    )
    deployments_ls_status_group = deployments_ls_parser.add_mutually_exclusive_group()
    deployments_ls_status_group.add_argument(
        "--status",
        type=str,
        default=None,
        help="Show only deployments with this status (pending, running, "
        "stopped, failed, destroying, destroyed).",
    )
    deployments_ls_status_group.add_argument(
        "--active",
        action="store_true",
        help="Show only active deployments (pending, running, destroying).",
    )

    # deployments print-downloads
    deployments_print_downloads_parser = deployments_subparsers.add_parser(
        "print-downloads",
        help=(
            "Print every download operation recorded in the testnet's InfluxDB, "
            "one table per client host."
        ),
    )
    _add_print_file_ops_args(deployments_print_downloads_parser)

    # deployments print-uploads
    deployments_print_uploads_parser = deployments_subparsers.add_parser(
        "print-uploads",
        help=(
            "Print every upload operation recorded in the testnet's InfluxDB, "
            "one table per client host."
        ),
    )
    _add_print_file_ops_args(deployments_print_uploads_parser)

    # deployments record-file-results
    deployments_record_file_results_parser = deployments_subparsers.add_parser(
        "record-file-results",
        help=(
            "Read per-client upload/download aggregates from the testnet's "
            "InfluxDB and upsert them into the deployment_file_results table."
        ),
    )
    deployments_record_file_results_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match a row in the registry deployments table)",
    )
    deployments_record_file_results_parser.add_argument(
        "--monitoring-name",
        type=str,
        default=None,
        help=(
            "Monitoring stack name to load Influx credentials from "
            "(defaults to --name)."
        ),
    )
    deployments_record_file_results_parser.add_argument(
        "--influx-url",
        type=str,
        default=None,
        help="InfluxDB URL (overrides monitoring state lookup)",
    )
    deployments_record_file_results_parser.add_argument(
        "--influx-token",
        type=str,
        default=None,
        help="InfluxDB API token (overrides monitoring state lookup)",
    )
    deployments_record_file_results_parser.add_argument(
        "--influx-org",
        type=str,
        default=None,
        help="InfluxDB organization (default: from monitoring state, else 'ant')",
    )
    deployments_record_file_results_parser.add_argument(
        "--influx-bucket",
        type=str,
        default=None,
        help=(
            "InfluxDB bucket (default: from monitoring state, else "
            "'testnet-metrics')"
        ),
    )
    deployments_record_file_results_parser.add_argument(
        "--window",
        type=str,
        default="-30d",
        help="Flux range start (e.g. -30d, -7d, -24h). Default: -30d",
    )

    # deployments record-results
    deployments_record_results_parser = deployments_subparsers.add_parser(
        "record-results",
        help=(
            "Read per-host CPU/memory/bandwidth aggregates from the testnet's "
            "InfluxDB and upsert them into the deployment_host_results table, "
            "optionally with a freeform summary of the test run."
        ),
    )
    deployments_record_results_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match a row in the registry deployments table)",
    )
    deployments_record_results_parser.add_argument(
        "--monitoring-name",
        type=str,
        default=None,
        help=(
            "Monitoring stack name to load Influx credentials from "
            "(defaults to --name)."
        ),
    )
    deployments_record_results_parser.add_argument(
        "--influx-url",
        type=str,
        default=None,
        help="InfluxDB URL (overrides monitoring state lookup)",
    )
    deployments_record_results_parser.add_argument(
        "--influx-token",
        type=str,
        default=None,
        help="InfluxDB API token (overrides monitoring state lookup)",
    )
    deployments_record_results_parser.add_argument(
        "--influx-org",
        type=str,
        default=None,
        help="InfluxDB organization (default: from monitoring state, else 'ant')",
    )
    deployments_record_results_parser.add_argument(
        "--influx-bucket",
        type=str,
        default=None,
        help=(
            "InfluxDB bucket (default: from monitoring state, else "
            "'testnet-metrics')"
        ),
    )
    deployments_record_results_parser.add_argument(
        "--window",
        type=str,
        default="-6h",
        help="Flux range start (e.g. -6h, -24h, -7d). Default: -6h",
    )
    deployments_record_results_parser.add_argument(
        "--summary",
        type=str,
        default=None,
        help=(
            "Freeform summary text describing the testnet run outcome. "
            "If omitted, any existing summary on the row is preserved."
        ),
    )

    # deployments set-results-summary
    deployments_set_results_summary_parser = deployments_subparsers.add_parser(
        "set-results-summary",
        help=(
            "Update the freeform summary on an existing deployment_host_results "
            "row without re-querying InfluxDB."
        ),
    )
    deployments_set_results_summary_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match a row in the registry deployments table)",
    )
    deployments_set_results_summary_parser.add_argument(
        "--summary",
        type=str,
        required=True,
        help="Freeform summary text describing the testnet run outcome.",
    )

    # deployments unextend
    deployments_unextend_parser = deployments_subparsers.add_parser(
        "unextend",
        help="Tear down an extension testnet's state (does not touch the source)",
    )
    deployments_unextend_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Name of the extension testnet (must have been created with `deployments extend`)",
    )
    deployments_unextend_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the interactive confirmation prompt",
    )

    # =========================================================================
    # === elastic group (clean, destroy, info, infra, provision, provision-telegraf) ===
    # =========================================================================
    elastic_parser = subparsers.add_parser(
        "elastic",
        help="Elasticsearch (log forwarding) lifecycle.",
    )
    elastic_subparsers = elastic_parser.add_subparsers(
        dest="elastic_subcommand", required=True
    )

    # elastic clean
    elastic_clean_parser = elastic_subparsers.add_parser(
        "clean",
        help=(
            "Remove telegraf-ant-elasticsearch log forwarder from node VMs "
            "(leaves telegraf deb, metrics sidecar, and ant-node untouched)"
        ),
    )
    elastic_clean_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help=(
            "Deployment name (loads IPs from deployment state). Required "
            "unless --host-ips-file or --inventory-path is provided."
        ),
    )
    elastic_clean_parser.add_argument(
        "--inventory-path",
        type=str,
        default=None,
        help=(
            "Path to an inventory JSON file. Reads ip, ssh_user, and ssh_key "
            "from each entry in the node_vms array."
        ),
    )
    elastic_clean_parser.add_argument(
        "--host-ips-file",
        type=str,
        default=None,
        help=(
            "Path to a text file containing one IP address per line "
            "(alternative to loading IPs from deployment state)"
        ),
    )
    elastic_clean_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa)",
    )
    elastic_clean_parser.add_argument(
        "--ssh-user",
        type=str,
        default="root",
        help="SSH username (default: root)",
    )

    # elastic destroy
    elastic_destroy_parser = elastic_subparsers.add_parser(
        "destroy", help="Destroy Elasticsearch infrastructure"
    )
    elastic_destroy_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )
    elastic_destroy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Elasticsearch stack name to destroy",
    )

    # elastic info
    elastic_info_parser = elastic_subparsers.add_parser(
        "info", help="Print Elasticsearch stack connection details"
    )
    elastic_info_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Elasticsearch stack name",
    )

    # elastic infra
    elastic_infra_parser = elastic_subparsers.add_parser(
        "infra", help="Create Elasticsearch VM infrastructure"
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

    # elastic provision
    elastic_provision_parser = elastic_subparsers.add_parser(
        "provision", help="Provision Elasticsearch on Elasticsearch VM"
    )
    elastic_provision_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Elasticsearch stack name (must match the name used with `elastic infra`)",
    )
    elastic_provision_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )
    elastic_provision_parser.add_argument(
        "--retention-days",
        type=int,
        default=7,
        help=(
            "ILM retention in days before log indices are deleted "
            "(default: 7). Re-run `elastic provision` to change."
        ),
    )
    elastic_provision_parser.add_argument(
        "--shards",
        type=int,
        default=4,
        help=(
            "Number of primary shards per daily ant-node-logs index "
            "(default: 4). Each shard pins to one indexing thread on "
            "the ES VM, so set to the vCPU count of the droplet: 4 for "
            "s-4vcpu-8gb (the `elastic infra` default), 8 for "
            "s-8vcpu-16gb. Only takes effect for indices created *after* "
            "`elastic provision` runs — existing indices retain their "
            "original shard count."
        ),
    )

    # elastic provision-telegraf (was elastic provision-telegraf)
    elastic_provision_telegraf_parser = elastic_subparsers.add_parser(
        "provision-telegraf",
        help="Install and configure Telegraf for Elasticsearch log forwarding",
    )
    elastic_provision_telegraf_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Deployment name (must match the name used with `nodes infra`). "
        "Required unless --host-ips-file is provided.",
    )
    elastic_provision_telegraf_parser.add_argument(
        "--elasticsearch-url",
        type=str,
        default=None,
        help="Elasticsearch endpoint URL (auto-loaded from elastic state if --name is provided)",
    )
    elastic_provision_telegraf_parser.add_argument(
        "--elasticsearch-username",
        type=str,
        default=None,
        help="Elasticsearch username (auto-loaded from elastic state if --name is provided)",
    )
    elastic_provision_telegraf_parser.add_argument(
        "--elasticsearch-password",
        type=str,
        default=None,
        help="Elasticsearch password (auto-loaded from elastic state if --name is provided)",
    )
    elastic_provision_telegraf_parser.add_argument(
        "--node-version",
        type=str,
        default=None,
        help="Version of the deployed ant-node binary",
    )
    elastic_provision_telegraf_parser.add_argument(
        "--branch-commit",
        type=str,
        default=None,
        help="Git branch or commit of the deployed binary",
    )
    elastic_provision_telegraf_parser.add_argument(
        "--repo-owner",
        type=str,
        default="maidsafe",
        help="Repository owner for branch builds (default: maidsafe)",
    )
    elastic_provision_telegraf_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )
    elastic_provision_telegraf_parser.add_argument(
        "--ssh-user",
        type=str,
        default="root",
        help="SSH username for provisioning (default: root)",
    )
    elastic_provision_telegraf_parser.add_argument(
        "--host-ips-file",
        type=str,
        default=None,
        help="Path to a text file containing one IP address per line "
        "(alternative to loading IPs from deployment state)",
    )
    elastic_provision_telegraf_parser.add_argument(
        "--inventory-path",
        type=str,
        default=None,
        help="Path to an inventory JSON file. Reads ip, ssh_user, and ssh_key "
        "from each entry in the node_vms array.",
    )
    elastic_provision_telegraf_parser.add_argument(
        "--ant-log-dir",
        type=str,
        default=None,
        help=(
            "Override the ant-node log directory that telegraf tails. "
            "Defaults to the production layout "
            "(/mnt/ant-node-storage/log). Use a different path for "
            "non-standard deployments such as bootstrap nodes that log to "
            "/mnt/ant-node-storage/logs."
        ),
    )
    elastic_provision_telegraf_tail_group = (
        elastic_provision_telegraf_parser.add_mutually_exclusive_group()
    )
    elastic_provision_telegraf_tail_group.add_argument(
        "--from-start",
        dest="from_beginning",
        action="store_true",
        default=True,
        help=(
            "Telegraf reads each log file from the beginning (default). "
            "Use this for fresh deployments where startup logs matter."
        ),
    )
    elastic_provision_telegraf_tail_group.add_argument(
        "--only-new-logs",
        dest="from_beginning",
        action="store_false",
        help=(
            "Telegraf tails from the end of each existing log file (no "
            "backfill). Use this for post-hoc attachment to a running "
            "testnet to avoid re-ingesting historical logs."
        ),
    )
    elastic_provision_telegraf_parser.add_argument(
        "--drop-log-levels",
        type=str,
        default="DEBUG,TRACE",
        help=(
            "Comma-separated ant-node log levels Telegraf drops before "
            "shipping to Elasticsearch (default: DEBUG,TRACE). INFO is "
            "kept by default because its breadcrumbs are needed for "
            "post-hoc investigation. Use --drop-log-levels \"\" to keep "
            "every line, or --drop-log-levels DEBUG,TRACE,INFO to trim "
            "the loudest levels when ES capacity is constrained."
        ),
    )
    elastic_provision_telegraf_parser.add_argument(
        "--buffer-limit",
        type=int,
        default=None,
        help=(
            "Size of Telegraf's output buffer (metric_buffer_limit) in "
            "lines. Default 2,000,000 gives ~50 s of headroom at 40k "
            "lines/sec for a 900-node testnet; drop to 200,000 for "
            "tiny/short runs. When full Telegraf evicts oldest-first — "
            "that's how DEV-01 lost all node-side forwarding at 01:30."
        ),
    )
    elastic_provision_telegraf_parser.add_argument(
        "--max-undelivered-lines",
        type=int,
        default=None,
        help=(
            "Size of the inputs.tail buffer in lines. Defaults to 1/4 of "
            "--buffer-limit. Rarely needs tuning unless log files rotate "
            "faster than Telegraf can drain."
        ),
    )

    # =========================================================================
    # === monitoring group (clean, destroy, info, infra, provision, provision-telegraf) ===
    # =========================================================================
    monitoring_parser = subparsers.add_parser(
        "monitoring",
        help="Monitoring stack (InfluxDB + Grafana) lifecycle.",
    )
    monitoring_subparsers = monitoring_parser.add_subparsers(
        dest="monitoring_subcommand", required=True
    )

    # monitoring clean
    monitoring_clean_parser = monitoring_subparsers.add_parser(
        "clean",
        help=(
            "Remove the telegraf-ant-metrics sidecar from node VMs (leaves "
            "telegraf deb, log forwarder, ant-node, and the monitoring VM "
            "itself untouched)"
        ),
    )
    monitoring_clean_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help=(
            "Deployment name (loads IPs from deployment state). Required "
            "unless --host-ips-file or --inventory-path is provided."
        ),
    )
    monitoring_clean_parser.add_argument(
        "--inventory-path",
        type=str,
        default=None,
        help=(
            "Path to an inventory JSON file. Reads ip, ssh_user, and ssh_key "
            "from each entry in the node_vms array."
        ),
    )
    monitoring_clean_parser.add_argument(
        "--host-ips-file",
        type=str,
        default=None,
        help="Path to a text file containing one IP address per line",
    )
    monitoring_clean_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa)",
    )
    monitoring_clean_parser.add_argument(
        "--ssh-user",
        type=str,
        default="root",
        help="SSH username (default: root)",
    )

    # monitoring destroy
    monitoring_destroy_parser = monitoring_subparsers.add_parser(
        "destroy", help="Destroy monitoring infrastructure"
    )
    monitoring_destroy_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )
    monitoring_destroy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Monitoring stack name to destroy",
    )

    # monitoring info
    monitoring_info_parser = monitoring_subparsers.add_parser(
        "info", help="Print monitoring stack connection details"
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

    # monitoring infra
    monitoring_infra_parser = monitoring_subparsers.add_parser(
        "infra", help="Create monitoring VM infrastructure"
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
    monitoring_infra_parser.add_argument(
        "--droplet-size",
        type=str,
        default="s-2vcpu-4gb",
        help="Droplet size slug (default: s-2vcpu-4gb). "
        "Use s-4vcpu-8gb or larger for large testnets with many "
        "metric-forwarding VMs.",
    )

    # monitoring provision
    monitoring_provision_parser = monitoring_subparsers.add_parser(
        "provision", help="Provision InfluxDB + Grafana on monitoring VM"
    )
    monitoring_provision_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Monitoring stack name (must match the name used with `monitoring infra`)",
    )
    monitoring_provision_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )

    # monitoring provision-telegraf (was monitoring provision-telegraf)
    monitoring_provision_telegraf_parser = monitoring_subparsers.add_parser(
        "provision-telegraf",
        help="Install and configure Telegraf for InfluxDB metrics forwarding",
    )
    monitoring_provision_telegraf_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Deployment name (loads VM IPs from deployment state)",
    )
    monitoring_provision_telegraf_parser.add_argument(
        "--host-ips",
        type=str,
        default=None,
        help="Comma-separated list of host IPs to provision (alternative to --name)",
    )
    monitoring_provision_telegraf_parser.add_argument(
        "--influxdb-url",
        type=str,
        default=None,
        help="InfluxDB URL (auto-loaded from monitoring state if --name is provided)",
    )
    monitoring_provision_telegraf_parser.add_argument(
        "--influxdb-token",
        type=str,
        default=None,
        help="InfluxDB API token (auto-loaded from monitoring state if --name is provided)",
    )
    monitoring_provision_telegraf_parser.add_argument(
        "--influxdb-org",
        type=str,
        default="ant",
        help="InfluxDB organization (default: ant)",
    )
    monitoring_provision_telegraf_parser.add_argument(
        "--influxdb-bucket",
        type=str,
        default="testnet-metrics",
        help="InfluxDB bucket (default: testnet-metrics)",
    )
    monitoring_provision_telegraf_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )
    monitoring_provision_telegraf_parser.add_argument(
        "--ssh-user",
        type=str,
        default="root",
        help="SSH username for provisioning (default: root)",
    )
    monitoring_provision_telegraf_parser.add_argument(
        "--host-ips-file",
        type=str,
        default=None,
        help="Path to a text file containing one IP address per line "
        "(alternative to --host-ips or loading IPs from deployment state)",
    )
    monitoring_provision_telegraf_parser.add_argument(
        "--inventory-path",
        type=str,
        default=None,
        help="Path to an inventory JSON file. Reads ip, ssh_user, and ssh_key "
        "from each entry in the node_vms array.",
    )

    # =========================================================================
    # === nodes group (clean, destroy, infra, provision, provision-genesis, start) ===
    # =========================================================================
    nodes_parser = subparsers.add_parser(
        "nodes",
        help="Node-VM lifecycle: clean, destroy, infra, provision, provision-genesis, start.",
    )
    nodes_subparsers = nodes_parser.add_subparsers(
        dest="nodes_subcommand", required=True
    )

    # nodes clean
    nodes_clean_parser = nodes_subparsers.add_parser(
        "clean",
        help=(
            "Remove ant-node-* services + data/log/binary from node VMs "
            "without destroying the VMs"
        ),
    )
    nodes_clean_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help=(
            "Deployment name (loads node IPs from deployment state). "
            "Required unless --host-ips-file is provided."
        ),
    )
    nodes_clean_parser.add_argument(
        "--region",
        type=str,
        default=None,
        help=(
            "Clean only one region (e.g., digitalocean/lon1). "
            "Only used with --name."
        ),
    )
    nodes_clean_parser.add_argument(
        "--host-ips-file",
        type=str,
        default=None,
        help="Path to a text file containing one IP address per line",
    )
    nodes_clean_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa)",
    )
    nodes_clean_parser.add_argument(
        "--ssh-user",
        type=str,
        default="root",
        help="SSH username (default: root)",
    )

    # nodes destroy
    nodes_destroy_parser = nodes_subparsers.add_parser(
        "destroy",
        help="Destroy node (main) VMs, leaving bootstraps alone",
    )
    nodes_destroy_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )
    nodes_destroy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name to destroy nodes for",
    )

    # nodes infra
    nodes_infra_parser = nodes_subparsers.add_parser(
        "infra",
        help="Create the node (main) VMs for a deployment. Requires bootstraps to already exist.",
    )
    nodes_infra_parser.add_argument(
        "--force",
        action="store_true",
        help="Destroy any orphan main-VM Terraform state and proceed",
    )
    nodes_infra_parser.add_argument(
        "--attached-volume-size",
        type=int,
        default=20,
        help="Size of attached volume in GB (default: 20)",
    )
    nodes_infra_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with `bootstraps infra`)",
    )
    nodes_infra_parser.add_argument(
        "--do-vm-count",
        type=int,
        default=0,
        help="Number of VMs per region on Digital Ocean (default: 0, provider not used)",
    )
    nodes_infra_parser.add_argument(
        "--do-regions",
        type=int,
        default=3,
        help="Number of Digital Ocean regions to use (default: 3). "
        "Ignored if --do-regions-list is set.",
    )
    nodes_infra_parser.add_argument(
        "--do-regions-list",
        type=str,
        default=None,
        help="Comma-separated list of specific Digital Ocean regions to use "
        "(e.g. lon1,nyc1,ams3). Overrides --do-regions and the default "
        "ordering; no fallback substitution.",
    )
    nodes_infra_parser.add_argument(
        "--do-droplet-size",
        type=str,
        default="s-2vcpu-4gb",
        help="Digital Ocean droplet size (default: s-2vcpu-4gb)",
    )
    nodes_infra_parser.add_argument(
        "--hetzner-vm-count",
        type=int,
        default=0,
        help="Number of VMs per region on Hetzner (default: 0, provider not used)",
    )
    nodes_infra_parser.add_argument(
        "--hetzner-regions",
        type=int,
        default=3,
        help="Number of Hetzner regions to use (default: 3). "
        "Ignored if --hetzner-regions-list is set.",
    )
    nodes_infra_parser.add_argument(
        "--hetzner-regions-list",
        type=str,
        default=None,
        help="Comma-separated list of specific Hetzner regions to use "
        "(e.g. fsn1,nbg1,hel1). Overrides --hetzner-regions and the "
        "default ordering; no fallback substitution.",
    )
    nodes_infra_parser.add_argument(
        "--hetzner-server-type",
        type=str,
        default="cpx22",
        help="Hetzner server type (default: cpx22). Alternatives: cx23, cax11",
    )
    nodes_infra_parser.add_argument(
        "--vultr-vm-count",
        type=int,
        default=0,
        help="Number of VMs per region on Vultr (default: 0, provider not used)",
    )
    nodes_infra_parser.add_argument(
        "--vultr-plan",
        type=str,
        default="vc2-2c-4gb",
        help="Vultr instance plan (default: vc2-2c-4gb)",
    )
    nodes_infra_parser.add_argument(
        "--vultr-regions",
        type=int,
        default=3,
        help="Number of Vultr regions to use (default: 3). "
        "Ignored if --vultr-regions-list is set.",
    )
    nodes_infra_parser.add_argument(
        "--vultr-regions-list",
        type=str,
        default=None,
        help="Comma-separated list of specific Vultr regions to use "
        "(e.g. ewr,ord,lax). Overrides --vultr-regions and the default "
        "ordering; no fallback substitution.",
    )

    # nodes provision
    nodes_provision_parser = nodes_subparsers.add_parser(
        "provision", help="Provision ant-node services on node VMs"
    )
    nodes_provision_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built binary from this branch (requires --repo-owner)",
    )
    nodes_provision_parser.add_argument(
        "--log-level",
        type=str,
        help="Logging level the nodes will run with",
    )
    nodes_provision_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with `nodes infra`)",
    )
    nodes_provision_parser.add_argument(
        "--node-count",
        type=int,
        required=True,
        help="Number of node services per VM",
    )
    nodes_provision_parser.add_argument(
        "--node-version",
        type=str,
        help="Specific release version to deploy (e.g., 0.2.0)",
    )
    nodes_provision_parser.add_argument(
        "--port",
        type=int,
        help="Beginning of a port range from PORT to PORT+N (omit for random ports)",
    )
    nodes_provision_parser.add_argument(
        "--region",
        type=str,
        help="Provision only VMs in this region (e.g., digitalocean/lon1)",
    )
    nodes_provision_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built binary (requires --branch-name)",
    )
    nodes_provision_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )
    nodes_provision_parser.add_argument(
        "--testnet",
        action="store_true",
        help="Run nodes with --network-mode testnet",
    )
    nodes_provision_parser.add_argument(
        "--upgrade-channel",
        type=str,
        choices=["beta", "stable"],
        help="Upgrade channel for ant-node (beta or stable)",
    )
    nodes_provision_parser.add_argument(
        "--rewards-address",
        type=str,
        default="0x03B770D9cD32077cC0bF330c13C114a87643B124",
        help="Arbitrum wallet address for node rewards "
        "(default: 0x03B770D9cD32077cC0bF330c13C114a87643B124)",
    )
    nodes_provision_parser.add_argument(
        "--nat-percent",
        type=int,
        default=0,
        help="Percentage of node VMs to run behind simulated port-restricted "
        "NAT (0-100, default: 0 = disabled). Selection is 'one per region "
        "first, then round-robin' and is persisted to deployment state on "
        "first run.",
    )
    nodes_provision_parser.add_argument(
        "--force-nat-reselect",
        action="store_true",
        help="Re-compute the NAT VM selection from --nat-percent even if "
        "deployment state already has one recorded. Use with caution — this "
        "can flip a VM between NAT and non-NAT between runs.",
    )

    # nodes provision-genesis
    nodes_provision_genesis_parser = nodes_subparsers.add_parser(
        "provision-genesis", help="Provision the genesis node"
    )
    nodes_provision_genesis_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built binary from this branch (requires --repo-owner)",
    )
    nodes_provision_genesis_parser.add_argument(
        "--log-level",
        type=str,
        help="Logging level the node will run with",
    )
    nodes_provision_genesis_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with `bootstraps infra`)",
    )
    nodes_provision_genesis_parser.add_argument(
        "--node-version",
        type=str,
        help="Specific release version to deploy (e.g., 0.2.0)",
    )
    nodes_provision_genesis_parser.add_argument(
        "--port",
        type=int,
        required=True,
        help="Port the genesis node will run with (required for bootstrap address)",
    )
    nodes_provision_genesis_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built binary (requires --branch-name)",
    )
    nodes_provision_genesis_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )
    nodes_provision_genesis_parser.add_argument(
        "--testnet",
        action="store_true",
        help="Run node with --network-mode testnet",
    )
    nodes_provision_genesis_parser.add_argument(
        "--upgrade-channel",
        type=str,
        choices=["beta", "stable"],
        help="Upgrade channel for ant-node (beta or stable)",
    )
    nodes_provision_genesis_parser.add_argument(
        "--rewards-address",
        type=str,
        default="0x03B770D9cD32077cC0bF330c13C114a87643B124",
        help="Arbitrum wallet address for node rewards "
        "(default: 0x03B770D9cD32077cC0bF330c13C114a87643B124)",
    )

    # nodes start
    nodes_start_parser = nodes_subparsers.add_parser(
        "start",
        help="Start ant-node services on node VMs (required after `nodes provision`)",
    )
    nodes_start_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match the name used with `nodes infra`)",
    )
    nodes_start_parser.add_argument(
        "--region",
        type=str,
        help="Start only VMs in this region (e.g., digitalocean/lon1)",
    )
    nodes_start_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa)",
    )

    # =========================================================================
    # === prod (nested: prod <subsystem> <action>) ===
    # =========================================================================
    prod_parser = subparsers.add_parser(
        "prod",
        help="Production-network operations (e.g. prod continuous-downloader deploy).",
    )
    prod_subparsers = prod_parser.add_subparsers(
        dest="prod_subcommand", required=True
    )

    # --- prod bootstrap (deploy / destroy) — was prod bootstrap deploy / prod bootstrap destroy ---
    prod_bootstrap_parser = prod_subparsers.add_parser(
        "bootstrap",
        help="Production bootstrap network: deploy or destroy.",
    )
    prod_bootstrap_subparsers = prod_bootstrap_parser.add_subparsers(
        dest="prod_bootstrap_action", required=True
    )

    prod_bootstrap_deploy_parser = prod_bootstrap_subparsers.add_parser(
        "deploy",
        help="Deploy production bootstrap nodes across multiple providers",
    )
    prod_bootstrap_deploy_parser.add_argument(
        "--name",
        type=str,
        default="ant-production",
        help="Deployment name (default: ant-production)",
    )
    prod_bootstrap_deploy_binary = (
        prod_bootstrap_deploy_parser.add_mutually_exclusive_group()
    )
    prod_bootstrap_deploy_binary.add_argument(
        "--node-version",
        type=str,
        default=None,
        help="Specific ant-node release version (e.g., 0.3.0). Uses latest if not specified.",
    )
    prod_bootstrap_deploy_binary.add_argument(
        "--path",
        type=str,
        default=None,
        help="Path to a local ant-node binary to upload to nodes.",
    )
    prod_bootstrap_deploy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/ant-production",
        help="Path to SSH private key for provisioning (default: ~/.ssh/ant-production)",
    )
    prod_bootstrap_deploy_parser.add_argument(
        "--rewards-address",
        type=str,
        required=True,
        help="Arbitrum wallet address for node rewards",
    )
    prod_bootstrap_deploy_parser.add_argument(
        "--evm-network",
        type=str,
        default="arbitrum-one",
        choices=["arbitrum-one", "arbitrum-sepolia"],
        help="EVM network for payments (default: arbitrum-one)",
    )
    prod_bootstrap_deploy_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompts and force overwrite stale state",
    )

    prod_bootstrap_destroy_parser = prod_bootstrap_subparsers.add_parser(
        "destroy",
        help="Destroy production bootstrap network infrastructure",
    )
    prod_bootstrap_destroy_parser.add_argument(
        "--name",
        type=str,
        default="ant-production",
        help="Deployment name (default: ant-production)",
    )
    prod_bootstrap_destroy_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )

    # --- prod continuous-downloader ---
    prod_cd_parser = prod_subparsers.add_parser(
        "continuous-downloader",
        help="Continuously download from the Autonomi production network "
        "to measure performance.",
    )
    prod_cd_subparsers = prod_cd_parser.add_subparsers(
        dest="prod_cd_action", required=True
    )

    prod_cd_deploy_parser = prod_cd_subparsers.add_parser(
        "deploy",
        help="Deploy a prod continuous-downloader client VM and its monitoring stack.",
    )
    prod_cd_deploy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (also used for the paired monitoring stack).",
    )
    prod_cd_deploy_parser.add_argument(
        "--spec",
        type=str,
        required=True,
        help="Downloader VM spec 'region[:vm_size]' (single client VM).",
    )
    prod_cd_deploy_parser.add_argument(
        "--addresses-file",
        type=str,
        default=None,
        help="Local path to a newline-separated list of public file addresses. "
        "Defaults to the list shipped in resources/prod_addresses.txt.",
    )
    prod_cd_deploy_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built ant binary from this branch (requires --repo-owner).",
    )
    prod_cd_deploy_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built ant binary (requires --branch-name).",
    )
    prod_cd_deploy_parser.add_argument(
        "--ant-version",
        type=str,
        help="Specific release version of ant to install (e.g., 0.3.0).",
    )
    prod_cd_deploy_parser.add_argument(
        "--monitoring-volume-size",
        type=int,
        default=50,
        help="Monitoring VM attached volume size in GB (default: 50).",
    )
    prod_cd_deploy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa).",
    )

    prod_cd_destroy_parser = prod_cd_subparsers.add_parser(
        "destroy",
        help="Destroy a prod continuous-downloader deployment "
        "(clients and, optionally, monitoring).",
    )
    prod_cd_destroy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name to tear down.",
    )
    prod_cd_destroy_parser.add_argument(
        "--keep-monitoring",
        action="store_true",
        help="Leave the paired monitoring VM running so recorded metrics "
        "remain queryable after the client VM is gone. "
        "Default: the monitoring VM is destroyed alongside the client.",
    )
    prod_cd_destroy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa).",
    )

    # --- prod uploaders ---
    prod_up_parser = prod_subparsers.add_parser(
        "uploaders",
        help=(
            "Deploy multiple uploader VMs that push real public files to the "
            "Autonomi production network."
        ),
    )
    prod_up_subparsers = prod_up_parser.add_subparsers(
        dest="prod_up_action", required=True
    )

    prod_up_deploy_parser = prod_up_subparsers.add_parser(
        "deploy",
        help="Deploy prod uploader VMs and their paired monitoring stack.",
    )
    prod_up_deploy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (also used for the paired monitoring stack).",
    )
    prod_up_deploy_parser.add_argument(
        "--uploader",
        action="append",
        default=[],
        metavar="REGION:VM_SIZE:urls=PATH",
        help=(
            "Uploader spec, repeatable. Example: "
            "lon1:s-2vcpu-4gb:urls=/path/to/list.txt"
        ),
    )
    prod_up_deploy_parser.add_argument(
        "--secret-key",
        type=str,
        default=None,
        help=(
            "Arbitrum One wallet secret key for prod uploads (with or without "
            "0x prefix). Falls back to ARBITRUM_ONE_SECRET_KEY env var. "
            "Must be a funded mainnet wallet — uploads cost real ETH; this is "
            "deliberately distinct from the testnet ANT_DEPLOY_SECRET_KEY."
        ),
    )
    prod_up_deploy_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built ant binary from this branch (requires --repo-owner).",
    )
    prod_up_deploy_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built ant binary (requires --branch-name).",
    )
    prod_up_deploy_parser.add_argument(
        "--ant-version",
        type=str,
        help="Specific release version of ant to install (e.g., 0.3.0).",
    )
    prod_up_deploy_parser.add_argument(
        "--monitoring-volume-size",
        type=int,
        default=50,
        help="Monitoring VM attached volume size in GB (default: 50).",
    )
    prod_up_deploy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa).",
    )

    prod_up_destroy_parser = prod_up_subparsers.add_parser(
        "destroy",
        help=(
            "Destroy a prod uploaders deployment "
            "(uploader VMs and, optionally, monitoring)."
        ),
    )
    prod_up_destroy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name to tear down.",
    )
    prod_up_destroy_parser.add_argument(
        "--keep-monitoring",
        action="store_true",
        help=(
            "Leave the paired monitoring VM running so recorded metrics "
            "remain queryable after the uploader VMs are gone. "
            "Default: the monitoring VM is destroyed alongside the uploaders."
        ),
    )
    prod_up_destroy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa).",
    )

    # --- prod print-upload-results ---
    prod_pur_parser = prod_subparsers.add_parser(
        "print-upload-results",
        help=(
            "Print every prod_upload record stored in the deployment's "
            "InfluxDB, one table per uploader VM."
        ),
    )
    _add_prod_upload_query_args(prod_pur_parser)
    prod_pur_parser.add_argument(
        "--details",
        action="store_true",
        help=(
            "Print one full record per upload with no truncation — full "
            "addresses, full file names, source URL, raw byte counts. "
            "Use when you need to grab values verbatim."
        ),
    )

    # --- prod record-upload-results ---
    prod_rur_parser = prod_subparsers.add_parser(
        "record-upload-results",
        help=(
            "Read every prod_upload record from the deployment's InfluxDB and "
            "upsert per-file rows into the deployment_prod_upload_results table."
        ),
    )
    _add_prod_upload_query_args(prod_rur_parser)

    # --- prod extend-unmanaged-volumes ---
    prod_euv_parser = prod_subparsers.add_parser(
        "extend-unmanaged-volumes",
        help="Grow the LVM-backed data volume on every node VM in a production "
        "inventory, sequentially and without stopping ant-node services.",
    )
    prod_euv_parser.add_argument(
        "--inventory-path",
        type=str,
        required=True,
        help="Path to the production inventory JSON (schema_version 3).",
    )
    prod_euv_parser.add_argument(
        "--size",
        type=int,
        required=True,
        help="Target volume size in GB (must exceed current size to do work).",
    )
    prod_euv_parser.add_argument(
        "--provider",
        type=str,
        default=None,
        choices=["aws", "digitalocean", "hetzner", "vultr"],
        help="Restrict to a single provider (default: every provider in the inventory).",
    )
    prod_euv_parser.add_argument(
        "--limit-to",
        type=str,
        default=None,
        help="Comma-separated VM names to restrict the run to (matched against "
        "inventory 'name' field). Combine with --provider for fine-grained tests.",
    )
    prod_euv_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default=None,
        help="Override the SSH key for every VM (default: each VM's inventory ssh_key).",
    )
    prod_euv_parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue past per-VM failures instead of stopping at the first error.",
    )
    prod_euv_parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )

    # =========================================================================
    # === track-upgrades (existing group, NO CHANGE) ===
    # =========================================================================
    track_upgrades_parser = subparsers.add_parser(
        "track-upgrades",
        help=(
            "Track ant-node auto-upgrade progress by polling Elasticsearch "
            "and writing per-node state to a local SQLite DB."
        ),
    )
    track_upgrades_sub = track_upgrades_parser.add_subparsers(
        dest="track_upgrades_verb", required=True
    )

    def _add_common_es_args(p):
        p.add_argument(
            "--name",
            type=str,
            default=None,
            help=(
                "Elasticsearch stack name — if set, credentials are loaded "
                "from the elastic state (recommended)."
            ),
        )
        p.add_argument("--es-url", type=str, default=None)
        p.add_argument("--es-username", type=str, default=None)
        p.add_argument("--es-password", type=str, default=None)
        p.add_argument("--es-api-key", type=str, default=None)
        p.add_argument(
            "--es-index-pattern",
            type=str,
            default=None,
            help=(
                "ES index pattern to search (default derived from --name "
                "or testnet_name)."
            ),
        )
        p.add_argument(
            "--testnet-name",
            type=str,
            default=None,
            help=(
                "The tag.testnet_name.keyword value that was set on the "
                "telegraf global_tags at provisioning time. Defaults to "
                "--name."
            ),
        )

    # init
    ti_init = track_upgrades_sub.add_parser(
        "init",
        help="Seed the SQLite DB from an inventory + baseline/target versions",
    )
    _add_common_es_args(ti_init)
    ti_init.add_argument(
        "--inventory-path", type=str, required=True,
        help="Path to inventory JSON (node_vms[].{name, services/nodes}).",
    )
    ti_init.add_argument(
        "--from-version", type=str, required=True,
        help="Baseline version currently running on every node.",
    )
    ti_init.add_argument(
        "--to-version", type=str, required=True,
        help="Target version we expect every node to upgrade to.",
    )
    ti_init.add_argument(
        "--db", type=str, default=None,
        help=(
            "Path to the SQLite DB file. Default: "
            "~/.saorsa-deploy/upgrades/<testnet>.db"
        ),
    )
    ti_init.add_argument(
        "--since", type=str, default=None,
        help=(
            "Ignore ES logs older than this ISO timestamp. Default: now "
            "(captures only logs emitted after init)."
        ),
    )
    ti_init.add_argument(
        "--reset", action="store_true",
        help="Wipe existing rows and metadata before seeding.",
    )

    # sync
    ti_sync = track_upgrades_sub.add_parser(
        "sync",
        help="Incrementally pull upgrade logs from ES and update the DB.",
    )
    ti_sync.add_argument("--db", type=str, required=True)
    ti_sync.add_argument(
        "--watch", action="store_true",
        help="Loop forever, polling every --interval. Ctrl-C to stop.",
    )
    ti_sync.add_argument(
        "--interval", type=str, default="5m",
        help="Polling interval in watch mode (e.g. 30s, 5m, 1h). Default 5m.",
    )

    # summary
    ti_summary = track_upgrades_sub.add_parser(
        "summary",
        help="Compact progress dashboard.",
    )
    ti_summary.add_argument("--db", type=str, required=True)

    # details
    ti_details = track_upgrades_sub.add_parser(
        "details",
        help="Per-node status table (hostname, service, versions, schedule).",
    )
    ti_details.add_argument("--db", type=str, required=True)

    # =========================================================================
    # === utils group (hetzner-availability, list-regions, vultr-availability) ===
    # =========================================================================
    utils_parser = subparsers.add_parser(
        "utils",
        help="Provider/region introspection helpers.",
    )
    utils_subparsers = utils_parser.add_subparsers(
        dest="utils_subcommand", required=True
    )

    # utils hetzner-availability
    utils_hetzner_avail_parser = utils_subparsers.add_parser(
        "hetzner-availability",
        help="Show which Hetzner server types are currently available per region",
    )
    utils_hetzner_avail_parser.add_argument(
        "--server-type",
        type=str,
        default=None,
        help="Filter the table to a single server type (e.g. cpx41)",
    )
    utils_hetzner_avail_parser.add_argument(
        "--region",
        type=str,
        default=None,
        help="Comma-separated list of regions to show (e.g. fsn1,nbg1,hel1)",
    )

    # utils list-regions
    utils_subparsers.add_parser(
        "list-regions",
        help="List the supported regions for each cloud provider",
    )

    # utils vultr-availability
    utils_vultr_avail_parser = utils_subparsers.add_parser(
        "vultr-availability",
        help="Show which Vultr plans are offered in each region",
    )
    utils_vultr_avail_parser.add_argument(
        "--plan",
        type=str,
        default=None,
        help="Filter the table to a single plan (e.g. vc2-8c-32gb)",
    )
    utils_vultr_avail_parser.add_argument(
        "--region",
        type=str,
        default=None,
        help="Comma-separated list of regions to show (e.g. ewr,lhr,fra)",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "bootstraps":
        if args.bootstraps_subcommand == "clean":
            from saorsa_deploy.cmd.clean_bootstraps import cmd_clean_bootstraps

            cmd_clean_bootstraps(args)
        elif args.bootstraps_subcommand == "destroy":
            from saorsa_deploy.cmd.destroy_bootstraps import cmd_destroy_bootstraps

            cmd_destroy_bootstraps(args)
        elif args.bootstraps_subcommand == "infra":
            from saorsa_deploy.cmd.infra_bootstraps import cmd_infra_bootstraps

            cmd_infra_bootstraps(args)
        elif args.bootstraps_subcommand == "provision":
            from saorsa_deploy.cmd.provision_bootstraps import cmd_provision_bootstraps

            cmd_provision_bootstraps(args)
    elif args.command == "build":
        if args.build_subcommand == "ant":
            from saorsa_deploy.cmd.build_ant import cmd_build_ant

            cmd_build_ant(args)
        elif args.build_subcommand == "ant-node":
            from saorsa_deploy.cmd.build import cmd_build

            cmd_build(args)
    elif args.command == "clients":
        if args.clients_subcommand == "clean":
            from saorsa_deploy.cmd.clean_clients import cmd_clean_clients

            cmd_clean_clients(args)
        elif args.clients_subcommand == "destroy":
            from saorsa_deploy.cmd.destroy_clients import cmd_destroy_clients

            cmd_destroy_clients(args)
        elif args.clients_subcommand == "infra":
            from saorsa_deploy.cmd.infra_clients import cmd_infra_clients

            cmd_infra_clients(args)
        elif args.clients_subcommand == "provision":
            from saorsa_deploy.cmd.provision_clients import cmd_provision_clients

            cmd_provision_clients(args)
        elif args.clients_subcommand == "start":
            from saorsa_deploy.cmd.start_clients import cmd_start_clients

            cmd_start_clients(args)
        elif args.clients_subcommand == "stop":
            from saorsa_deploy.cmd.stop_clients import cmd_stop_clients

            cmd_stop_clients(args)
    elif args.command == "deployments":
        if args.deployments_subcommand == "extend":
            from saorsa_deploy.cmd.extend import cmd_extend

            cmd_extend(args)
        elif args.deployments_subcommand == "generate-inventory":
            from saorsa_deploy.cmd.generate_inventory import cmd_generate_inventory

            cmd_generate_inventory(args)
        elif args.deployments_subcommand == "ls":
            from saorsa_deploy.cmd.ls import cmd_ls

            cmd_ls(args)
        elif args.deployments_subcommand == "print-downloads":
            from saorsa_deploy.cmd.print_file_ops import cmd_print_downloads

            cmd_print_downloads(args)
        elif args.deployments_subcommand == "print-uploads":
            from saorsa_deploy.cmd.print_file_ops import cmd_print_uploads

            cmd_print_uploads(args)
        elif args.deployments_subcommand == "record-file-results":
            from saorsa_deploy.cmd.record_file_results import cmd_record_file_results

            cmd_record_file_results(args)
        elif args.deployments_subcommand == "record-results":
            from saorsa_deploy.cmd.record_results import cmd_record_results

            cmd_record_results(args)
        elif args.deployments_subcommand == "set-results-summary":
            from saorsa_deploy.cmd.set_results_summary import cmd_set_results_summary

            cmd_set_results_summary(args)
        elif args.deployments_subcommand == "unextend":
            from saorsa_deploy.cmd.unextend import cmd_unextend

            cmd_unextend(args)
    elif args.command == "elastic":
        if args.elastic_subcommand == "clean":
            from saorsa_deploy.cmd.clean_elastic import cmd_clean_elastic

            cmd_clean_elastic(args)
        elif args.elastic_subcommand == "destroy":
            from saorsa_deploy.cmd.destroy_elastic import cmd_destroy_elastic

            cmd_destroy_elastic(args)
        elif args.elastic_subcommand == "info":
            from saorsa_deploy.cmd.elastic_info import cmd_elastic_info

            cmd_elastic_info(args)
        elif args.elastic_subcommand == "infra":
            from saorsa_deploy.cmd.elastic_infra import cmd_elastic_infra

            cmd_elastic_infra(args)
        elif args.elastic_subcommand == "provision":
            from saorsa_deploy.cmd.provision_elastic import cmd_provision_elastic

            cmd_provision_elastic(args)
        elif args.elastic_subcommand == "provision-telegraf":
            from saorsa_deploy.cmd.provision_telegraf import cmd_provision_telegraf

            cmd_provision_telegraf(args)
    elif args.command == "monitoring":
        if args.monitoring_subcommand == "clean":
            from saorsa_deploy.cmd.clean_monitoring import cmd_clean_monitoring

            cmd_clean_monitoring(args)
        elif args.monitoring_subcommand == "destroy":
            from saorsa_deploy.cmd.destroy_monitoring import cmd_destroy_monitoring

            cmd_destroy_monitoring(args)
        elif args.monitoring_subcommand == "info":
            from saorsa_deploy.cmd.monitoring_info import cmd_monitoring_info

            cmd_monitoring_info(args)
        elif args.monitoring_subcommand == "infra":
            from saorsa_deploy.cmd.monitoring_infra import cmd_monitoring_infra

            cmd_monitoring_infra(args)
        elif args.monitoring_subcommand == "provision":
            from saorsa_deploy.cmd.provision_monitoring import cmd_provision_monitoring

            cmd_provision_monitoring(args)
        elif args.monitoring_subcommand == "provision-telegraf":
            from saorsa_deploy.cmd.provision_telegraf_metrics import (
                cmd_provision_telegraf_metrics,
            )

            cmd_provision_telegraf_metrics(args)
    elif args.command == "nodes":
        if args.nodes_subcommand == "clean":
            from saorsa_deploy.cmd.clean_nodes import cmd_clean_nodes

            cmd_clean_nodes(args)
        elif args.nodes_subcommand == "destroy":
            from saorsa_deploy.cmd.destroy_nodes import cmd_destroy_nodes

            cmd_destroy_nodes(args)
        elif args.nodes_subcommand == "infra":
            from saorsa_deploy.cmd.infra_nodes import cmd_infra_nodes

            cmd_infra_nodes(args)
        elif args.nodes_subcommand == "provision":
            from saorsa_deploy.cmd.provision_nodes import cmd_provision_nodes

            cmd_provision_nodes(args)
        elif args.nodes_subcommand == "provision-genesis":
            from saorsa_deploy.cmd.provision_genesis import cmd_provision_genesis

            cmd_provision_genesis(args)
        elif args.nodes_subcommand == "start":
            from saorsa_deploy.cmd.start_nodes import cmd_start_nodes

            cmd_start_nodes(args)
    elif args.command == "prod":
        if args.prod_subcommand == "bootstrap" and args.prod_bootstrap_action == "deploy":
            from saorsa_deploy.cmd.bootstrap_production import cmd_bootstrap_production

            cmd_bootstrap_production(args)
        elif (
            args.prod_subcommand == "bootstrap"
            and args.prod_bootstrap_action == "destroy"
        ):
            from saorsa_deploy.cmd.destroy_bootstrap_production import (
                cmd_destroy_bootstrap_production,
            )

            cmd_destroy_bootstrap_production(args)
        elif (
            args.prod_subcommand == "continuous-downloader"
            and args.prod_cd_action == "deploy"
        ):
            from saorsa_deploy.cmd.prod_continuous_downloader import (
                cmd_prod_continuous_downloader_deploy,
            )

            cmd_prod_continuous_downloader_deploy(args)
        elif (
            args.prod_subcommand == "continuous-downloader"
            and args.prod_cd_action == "destroy"
        ):
            from saorsa_deploy.cmd.prod_continuous_downloader import (
                cmd_prod_continuous_downloader_destroy,
            )

            cmd_prod_continuous_downloader_destroy(args)
        elif (
            args.prod_subcommand == "uploaders"
            and args.prod_up_action == "deploy"
        ):
            from saorsa_deploy.cmd.prod_uploaders import cmd_prod_uploaders_deploy

            cmd_prod_uploaders_deploy(args)
        elif (
            args.prod_subcommand == "uploaders"
            and args.prod_up_action == "destroy"
        ):
            from saorsa_deploy.cmd.prod_uploaders import cmd_prod_uploaders_destroy

            cmd_prod_uploaders_destroy(args)
        elif args.prod_subcommand == "print-upload-results":
            from saorsa_deploy.cmd.prod_print_upload_results import (
                cmd_prod_print_upload_results,
            )

            cmd_prod_print_upload_results(args)
        elif args.prod_subcommand == "record-upload-results":
            from saorsa_deploy.cmd.prod_record_upload_results import (
                cmd_prod_record_upload_results,
            )

            cmd_prod_record_upload_results(args)
        elif args.prod_subcommand == "extend-unmanaged-volumes":
            from saorsa_deploy.cmd.prod_extend_unmanaged_volumes import (
                cmd_prod_extend_unmanaged_volumes,
            )

            cmd_prod_extend_unmanaged_volumes(args)
    elif args.command == "track-upgrades":
        verb = args.track_upgrades_verb
        if verb == "init":
            from saorsa_deploy.cmd.track_upgrades.init_cmd import (
                cmd_track_upgrades_init,
            )
            cmd_track_upgrades_init(args)
        elif verb == "sync":
            from saorsa_deploy.cmd.track_upgrades.sync_cmd import (
                cmd_track_upgrades_sync,
            )
            cmd_track_upgrades_sync(args)
        elif verb == "summary":
            from saorsa_deploy.cmd.track_upgrades.summary_cmd import (
                cmd_track_upgrades_summary,
            )
            cmd_track_upgrades_summary(args)
        elif verb == "details":
            from saorsa_deploy.cmd.track_upgrades.details_cmd import (
                cmd_track_upgrades_details,
            )
            cmd_track_upgrades_details(args)
    elif args.command == "utils":
        if args.utils_subcommand == "hetzner-availability":
            from saorsa_deploy.cmd.hetzner_availability import cmd_hetzner_availability

            cmd_hetzner_availability(args)
        elif args.utils_subcommand == "list-regions":
            from saorsa_deploy.cmd.list_regions import cmd_list_regions

            cmd_list_regions(args)
        elif args.utils_subcommand == "vultr-availability":
            from saorsa_deploy.cmd.vultr_availability import cmd_vultr_availability

            cmd_vultr_availability(args)


if __name__ == "__main__":
    main()
