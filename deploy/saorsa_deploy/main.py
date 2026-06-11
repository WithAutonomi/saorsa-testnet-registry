import argparse
import sys
from importlib.metadata import version


def _add_manifest_args(p):
    """Shared --config/--set/--name args for the manifest-driven commands."""
    p.add_argument(
        "--config",
        type=str,
        required=True,
        help="Path to a deployment manifest (YAML).",
    )
    p.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override a manifest value by dotted key (repeatable), "
        "e.g. --set testnet.nodes.nat_percent=0",
    )
    p.add_argument(
        "--name",
        type=str,
        default=None,
        help="Override the manifest's deployment name.",
    )


def main(argv=None):
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
    bootstraps_infra_parser.add_argument(
        "--purpose",
        type=str,
        required=True,
        help="Free-text description of what this testnet is for. "
        "Persisted into deploy_args and surfaced by `deployments slack-post`.",
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
    build_ant_parser.add_argument(
        "--musl",
        action="store_true",
        help="Build against the x86_64-unknown-linux-musl target (statically linked)",
    )
    build_ant_parser.add_argument(
        "--debug",
        action="store_true",
        help="Build without --release (a debug build: larger, slower, but with full "
        "debug info — intended for memory profiling). The artifact overwrites the "
        "deployment's S3 build key as usual.",
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
    build_ant_node_parser.add_argument(
        "--musl",
        action="store_true",
        help="Build against the x86_64-unknown-linux-musl target (statically linked)",
    )
    build_ant_node_parser.add_argument(
        "--debug",
        action="store_true",
        help="Build without --release (a debug build: larger, slower, but with full "
        "debug info — intended for memory profiling). The artifact overwrites the "
        "deployment's S3 build key as usual.",
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
    clients_destroy_parser.add_argument(
        "--wallet-secret-key",
        type=str,
        default=None,
        help=(
            "Funding wallet secret key (Arbitrum Sepolia EOA). Used to drain "
            "per-uploader wallets before destruction. Falls back to "
            "ANT_DEPLOY_SECRET_KEY env var. Required only if uploaders have "
            "per-uploader wallets recorded in client state."
        ),
    )
    clients_destroy_parser.add_argument(
        "--skip-drain-failures",
        action="store_true",
        help=(
            "Continue with infrastructure teardown even if one or more wallet "
            "drains fail. Abandoned balances are reported on stdout. Use when "
            "a wallet has too little ETH left to cover the drain transaction "
            "and the abandoned dust is acceptable."
        ),
    )
    clients_destroy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help=(
            "SSH key used to stop the ant-uploader service on each uploader VM "
            "before draining its wallet, so the drain isn't racing a live "
            "spender. Best-effort: unreachable VMs are skipped "
            "(default: ~/.ssh/id_rsa)."
        ),
    )

    # clients infra
    clients_infra_parser = clients_subparsers.add_parser(
        "infra",
        help="Create client VMs on a cloud provider "
        "(digital_ocean, vultr, hetzner, ovh)",
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
        help="Uploader VM spec: "
        "'provider:region:file_size_mb[:vm_size][:payment=MODE][:ant=N][:eth=N]"
        "[:clock_skew=DURATION][:bootstrap_cache=BOOL]'. "
        "provider is mandatory: digital_ocean, vultr, hetzner, or ovh. "
        "Region names and VM sizes vary per provider and are passed through "
        "verbatim (supply valid strings). Repeat for multiple uploaders. "
        "Default vm_size=s-2vcpu-4gb (DO). Optional payment=merkle|no-merkle "
        "forces the payment mode for that uploader's `ant file upload` calls; "
        "omit for ant's default. clock_skew=DURATION disables time sync and "
        "shifts that VM's clock by a signed Go-style duration (e.g. 2h, -2h, "
        "30m; testing only). bootstrap_cache=true|false (default true) — when "
        "true ant runs WITHOUT --bootstrap (peer cache enabled, peers read from "
        "a provisioned bootstrap_peers.toml); false restores the legacy "
        "--bootstrap invocation. "
        "Example: --uploader 'digital_ocean:lon1:50' "
        "--uploader 'hetzner:fsn1:100:cpx32:payment=merkle'",
    )
    clients_infra_parser.add_argument(
        "--downloader",
        type=str,
        action="append",
        default=[],
        help="Downloader VM spec: 'provider:region[:vm_size][:clock_skew=DURATION]"
        "[:bootstrap_cache=BOOL]'. "
        "provider is "
        "mandatory: digital_ocean, vultr, hetzner, or ovh. Repeat for "
        "multiple downloaders. Default vm_size=s-2vcpu-4gb (DO). "
        "clock_skew=DURATION disables time sync and shifts that VM's clock by a "
        "signed Go-style duration (e.g. 2h, -2h, 30m; testing only). "
        "bootstrap_cache=true|false (default true) — when true ant runs WITHOUT "
        "--bootstrap (peer cache enabled, peers read from a provisioned "
        "bootstrap_peers.toml); false restores the legacy --bootstrap invocation. "
        "Example: --downloader 'digital_ocean:lon1' "
        "--downloader 'vultr:nrt:vc2-4c-8gb:clock_skew=-2h'",
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
    clients_provision_parser.add_argument(
        "--heaptrack",
        action="append",
        choices=["uploader", "downloader"],
        default=[],
        help="Wrap ant invocations with heaptrack for the named service. "
        "Repeatable: --heaptrack uploader --heaptrack downloader. Installs the "
        "heaptrack apt package and writes profiles to /var/log/ant-heaptrack/ "
        "on each matching VM (named upload-<seq>.gz / download-<seq>.gz). "
        "Fetch via scp before destroying the client VMs.",
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
    # === record-results, record-analysis, edit-analysis-summary, unextend ===
    # =========================================================================
    deployments_parser = subparsers.add_parser(
        "deployments",
        help="Deployment lifecycle and result-recording operations.",
    )
    deployments_subparsers = deployments_parser.add_subparsers(
        dest="deployments_subcommand", required=True
    )

    # deployments config
    deployments_config_parser = deployments_subparsers.add_parser(
        "config",
        help="Inspect declarative deployment manifests (read-only).",
    )
    deployments_config_subparsers = deployments_config_parser.add_subparsers(
        dest="config_subcommand", required=True
    )
    deployments_config_show_parser = deployments_config_subparsers.add_parser(
        "show",
        help="Print the fully-merged, validated manifest as YAML.",
    )
    _add_manifest_args(deployments_config_show_parser)
    deployments_config_validate_parser = deployments_config_subparsers.add_parser(
        "validate",
        help="Validate a manifest; non-zero exit if invalid.",
    )
    _add_manifest_args(deployments_config_validate_parser)

    # deployments down — destroy is name/state-driven, so --config is optional:
    # pass --config to mirror a manifest, or just --name to tear down whatever
    # exists in state for that deployment.
    deployments_down_parser = deployments_subparsers.add_parser(
        "down",
        help="Destroy a deployment. Pass --name (teardown derived from state) "
        "or --config <manifest>.",
    )
    deployments_down_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to a deployment manifest (YAML). Optional — omit and use "
        "--name to derive the teardown from S3 state.",
    )
    deployments_down_parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override a manifest value by dotted key (only with --config).",
    )
    deployments_down_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Deployment name. Used alone to destroy by state, or to override "
        "the manifest's name with --config.",
    )
    deployments_down_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the destroy plan without executing it.",
    )
    deployments_down_parser.add_argument(
        "--skip-drain-failures",
        action="store_true",
        help=(
            "Pass --skip-drain-failures to the clients-destroy step so teardown "
            "proceeds even if one or more per-uploader wallet drains fail. Use "
            "when a drain keeps erroring (e.g. nonce/balance issues) and you "
            "just need to tear the deployment down; abandoned dust is reported "
            "but no longer blocks the destroy."
        ),
    )

    # deployments edit-analysis-summary
    deployments_edit_analysis_summary_parser = deployments_subparsers.add_parser(
        "edit-analysis-summary",
        help=(
            "Update the freeform summary on an existing deployment_host_results "
            "row without re-querying InfluxDB."
        ),
    )
    deployments_edit_analysis_summary_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match a row in the registry deployments table)",
    )
    deployments_edit_analysis_summary_parser.add_argument(
        "--summary",
        type=str,
        required=True,
        help="Freeform summary text describing the testnet run outcome.",
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
    deployments_extend_parser.add_argument(
        "--purpose",
        type=str,
        required=True,
        help="Free-text description of what this testnet is for. "
        "Persisted into deploy_args and surfaced by `deployments slack-post`.",
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

    # deployments plan
    deployments_plan_parser = deployments_subparsers.add_parser(
        "plan",
        help="Print the deploy command sequence for a manifest (never executes).",
    )
    _add_manifest_args(deployments_plan_parser)
    deployments_plan_parser.add_argument(
        "--skip-builds",
        action="store_true",
        help="Omit the binary build steps from the printed sequence (see `up --skip-builds`).",
    )

    # deployments print-downloads
    deployments_print_downloads_parser = deployments_subparsers.add_parser(
        "print-downloads",
        help=(
            "Print every download operation from the deployment's live "
            "InfluxDB feed, one table per client host. Reads live results "
            "(monitoring stack must still be up); for the persisted "
            "Postgres view use `deployments print-recorded-results`. Works "
            "for testnet deployments and prod-continuous-downloader; "
            "rejects prod-uploaders and prod-local-uploads."
        ),
    )
    _add_print_file_ops_args(deployments_print_downloads_parser)

    # deployments print-recorded-results
    deployments_print_recorded_parser = deployments_subparsers.add_parser(
        "print-recorded-results",
        help=(
            "Print every result that has been persisted to Postgres for a "
            "deployment: the freeform analysis summary plus per-host "
            "metrics from deployment_host_results, and the kind-appropriate "
            "file results (deployment_file_results for testnet / "
            "prod-continuous-downloader; deployment_prod_upload_results "
            "for prod-uploaders / prod-local-uploads). Reads only Postgres, "
            "so it works after monitoring infra has been torn down."
        ),
    )
    deployments_print_recorded_parser.add_argument(
        "--id",
        dest="deployment_id",
        type=int,
        required=True,
        help=(
            "Deployment id from the registry. Names are not unique across "
            "destroyed/recreated deployments — use the id from "
            "`deployments ls` to point at a specific historical row."
        ),
    )
    deployments_print_recorded_parser.add_argument(
        "--details",
        action="store_true",
        help=(
            "For prod-uploaders / prod-local-uploads: print one full row "
            "per upload with no truncation. For testnet / "
            "prod-continuous-downloader: also print per-op rows from "
            "deployment_upload_records and deployment_download_records "
            "after the per-host aggregate table."
        ),
    )

    # deployments print-uploads
    deployments_print_uploads_parser = deployments_subparsers.add_parser(
        "print-uploads",
        help=(
            "Print every upload operation from the deployment's live "
            "InfluxDB feed, one table per client host. Reads live results "
            "(monitoring stack must still be up); for the persisted "
            "Postgres view use `deployments print-recorded-results`. Works "
            "for testnet deployments (file_ops measurement) and "
            "prod-uploaders (prod_upload measurement); rejects "
            "prod-continuous-downloader and prod-local-uploads."
        ),
    )
    _add_print_file_ops_args(deployments_print_uploads_parser)
    deployments_print_uploads_parser.add_argument(
        "--details",
        action="store_true",
        help=(
            "For prod-uploaders only: print one full record per upload with "
            "no truncation — full addresses, full file names, source URL, "
            "raw byte counts. Ignored for other kinds."
        ),
    )

    # deployments record-analysis
    deployments_record_analysis_parser = deployments_subparsers.add_parser(
        "record-analysis",
        help=(
            "Read per-host CPU/memory/bandwidth aggregates from the testnet's "
            "InfluxDB and upsert them into the deployment_host_results table, "
            "optionally with a freeform summary of the test run."
        ),
    )
    deployments_record_analysis_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must match a row in the registry deployments table)",
    )
    deployments_record_analysis_parser.add_argument(
        "--monitoring-name",
        type=str,
        default=None,
        help=(
            "Monitoring stack name to load Influx credentials from "
            "(defaults to --name)."
        ),
    )
    deployments_record_analysis_parser.add_argument(
        "--influx-url",
        type=str,
        default=None,
        help="InfluxDB URL (overrides monitoring state lookup)",
    )
    deployments_record_analysis_parser.add_argument(
        "--influx-token",
        type=str,
        default=None,
        help="InfluxDB API token (overrides monitoring state lookup)",
    )
    deployments_record_analysis_parser.add_argument(
        "--influx-org",
        type=str,
        default=None,
        help="InfluxDB organization (default: from monitoring state, else 'ant')",
    )
    deployments_record_analysis_parser.add_argument(
        "--influx-bucket",
        type=str,
        default=None,
        help=(
            "InfluxDB bucket (default: from monitoring state, else "
            "'testnet-metrics')"
        ),
    )
    deployments_record_analysis_parser.add_argument(
        "--window",
        type=str,
        default="-6h",
        help="Flux range start (e.g. -6h, -24h, -7d). Default: -6h",
    )
    deployments_record_analysis_parser.add_argument(
        "--summary",
        type=str,
        default=None,
        help=(
            "Freeform summary text describing the testnet run outcome. "
            "If omitted, any existing summary on the row is preserved."
        ),
    )

    # deployments record-results
    deployments_record_results_parser = deployments_subparsers.add_parser(
        "record-results",
        help=(
            "Persist file-operation results from a deployment's InfluxDB into "
            "Postgres. Dispatches on deploy_args.kind: testnet and "
            "prod-continuous-downloader → per-host aggregates in "
            "deployment_file_results; prod-uploaders → per-file rows in "
            "deployment_prod_upload_results (--window ignored); "
            "prod-local-uploads is rejected (already in Postgres)."
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
        default="-30d",
        help="Flux range start (e.g. -30d, -7d, -24h). Default: -30d",
    )
    deployments_record_results_parser.add_argument(
        "--update-slack",
        action="store_true",
        help=(
            "After recording, post a per-kind results summary as a threaded "
            "reply to the deployment's original `deployments slack-post` "
            "message. Requires that the original post was made via the Web API "
            "(ANT_DEPLOY_SLACK_BOT_TOKEN set) so its (channel, ts) was captured. "
            "Best-effort: a Slack failure warns but never fails the run."
        ),
    )

    # deployments slack-post
    deployments_slack_post_parser = deployments_subparsers.add_parser(
        "slack-post",
        help=(
            "Post a formatted summary of a deployment to Slack. With "
            "ANT_DEPLOY_SLACK_BOT_TOKEN set, posts via the Web API "
            "(chat.postMessage) and records the message (channel, ts) so "
            "`record-results --update-slack` can thread a results summary onto "
            "it; otherwise falls back to the incoming webhook "
            "(ANT_DEPLOY_SLACK_WEBHOOK_URL, no threading)."
        ),
    )
    deployments_slack_post_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must exist in the registry).",
    )
    deployments_slack_post_parser.add_argument(
        "--channel",
        type=str,
        default=None,
        help=(
            "Slack channel (ID or name) to post to via the Web API. Defaults to "
            "ANT_DEPLOY_SLACK_CHANNEL. Only used when ANT_DEPLOY_SLACK_BOT_TOKEN "
            "is set; ignored on the incoming-webhook path."
        ),
    )
    deployments_slack_post_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Slack payload as JSON instead of posting.",
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

    # deployments up
    deployments_up_parser = deployments_subparsers.add_parser(
        "up",
        help="Deploy from a manifest (runs the full ordered command sequence).",
    )
    _add_manifest_args(deployments_up_parser)
    deployments_up_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the deploy plan without executing it.",
    )
    deployments_up_parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "If a step journal exists from a previous failed run, skip the "
            "steps it records as completed without prompting (for "
            "non-interactive use; interactively a prompt offers the same "
            "choice)."
        ),
    )
    deployments_up_parser.add_argument(
        "--skip-builds",
        action="store_true",
        help=(
            "Omit the binary build steps (build ant-node / build ant from "
            "branch) from the sequence. Use when the branch binaries were "
            "already built and uploaded to S3 by a previous deployment, to "
            "skip the ~10-minute rebuild."
        ),
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
        "--provider",
        type=str,
        default="digitalocean",
        choices=["digitalocean", "ovh"],
        help="Cloud provider for the Elasticsearch VM (default: digitalocean). "
        "'ovh' requires OVH_CLOUD_PROJECT_SERVICE + OVH API credentials.",
    )
    elastic_infra_parser.add_argument(
        "--region",
        type=str,
        default=None,
        help="Region for the Elasticsearch VM (default: lon1 for "
        "digitalocean, GRA9 for ovh)",
    )
    elastic_infra_parser.add_argument(
        "--volume-size",
        type=int,
        default=None,
        help="Size of attached volume in GB for Elasticsearch data (default: 100)",
    )
    elastic_infra_parser.add_argument(
        "--volume-type",
        type=str,
        default=None,
        help="OVH block storage type for the Elasticsearch volume (default: "
        "high-speed). Ignored for digitalocean.",
    )
    elastic_infra_parser.add_argument(
        "--droplet-size",
        type=str,
        default=None,
        help="VM size for the Elasticsearch VM (default: s-4vcpu-8gb for "
        "digitalocean, b3-8 for ovh). Use a larger size for 5000+ node "
        "fleets.",
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
        "--host-role",
        type=str,
        default=None,
        choices=["GENESIS", "BOOTSTRAP", "WAN_NODE", "CLIENT"],
        help="Force this host_role tag for every targeted host. Use with "
        "auxiliary inventories like bootstrap-inventory.json, whose entries "
        "sit under node_vms and would otherwise be labelled WAN_NODE.",
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
        "--provider",
        type=str,
        default="digitalocean",
        choices=["digitalocean", "ovh"],
        help="Cloud provider for the monitoring VMs (default: digitalocean). "
        "'ovh' requires OVH_CLOUD_PROJECT_SERVICE + OVH API credentials.",
    )
    monitoring_infra_parser.add_argument(
        "--region",
        type=str,
        default=None,
        help="Region for the monitoring VMs (default: lon1 for digitalocean, "
        "GRA9 for ovh)",
    )
    monitoring_infra_parser.add_argument(
        "--volume-size",
        type=int,
        default=None,
        help="Size of attached volume in GB for InfluxDB data (default: 50)",
    )
    monitoring_infra_parser.add_argument(
        "--volume-type",
        type=str,
        default=None,
        help="OVH block storage type for the InfluxDB volume (default: "
        "high-speed). Ignored for digitalocean.",
    )
    monitoring_infra_parser.add_argument(
        "--droplet-size",
        type=str,
        default=None,
        help="VM size for the InfluxDB VM (default: s-4vcpu-8gb for "
        "digitalocean, b3-8 for ovh). Use a larger size for production "
        "fleets with many metric-forwarding VMs.",
    )
    monitoring_infra_parser.add_argument(
        "--grafana-size",
        type=str,
        default=None,
        help="VM size for the (separate) Grafana VM (default: s-1vcpu-2gb "
        "for digitalocean, d2-4 for ovh).",
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
    monitoring_provision_parser.add_argument(
        "--retention-days",
        type=int,
        default=0,
        help=(
            "InfluxDB bucket retention in days before data is dropped "
            "(default: 0 = keep forever, the dev default). Applied to both "
            "the node-tier and client-tier buckets at first provision; set "
            "e.g. 14 for a long-running production stack."
        ),
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
        "--host-role",
        type=str,
        default=None,
        choices=["GENESIS", "BOOTSTRAP", "WAN_NODE", "CLIENT"],
        help="Force this host_role tag for every targeted host. Use with "
        "auxiliary inventories like bootstrap-inventory.json, whose entries "
        "sit under node_vms and would otherwise be labelled WAN_NODE. "
        "CLIENT additionally routes host metrics to the client-metrics "
        "bucket.",
    )
    monitoring_provision_telegraf_parser.add_argument(
        "--inventory-path",
        type=str,
        default=None,
        help="Path to an inventory JSON file. Reads ip, ssh_user, and ssh_key "
        "from each entry in the node_vms array.",
    )

    # =========================================================================
    # === migration group (plan, infra, provision, start-nodes, status,     ===
    # === drain, destroy-source, complete, list)                            ===
    # =========================================================================
    migration_parser = subparsers.add_parser(
        "migration",
        help=(
            "Migrate the node VMs of an existing prod deployment to a "
            "different cloud provider."
        ),
    )
    migration_subparsers = migration_parser.add_subparsers(
        dest="migration_subcommand", required=True
    )

    # migration plan
    migration_plan_parser = migration_subparsers.add_parser(
        "plan",
        help=(
            "Record a migration's source VMs in the DB. Reads the prod "
            "inventory; does not touch infrastructure."
        ),
    )
    migration_plan_parser.add_argument(
        "--name", type=str, required=True, help="Migration name (unique)."
    )
    migration_plan_parser.add_argument(
        "--deployment-name",
        type=str,
        required=True,
        help="Existing deployment whose VMs are being migrated (e.g. ant-prod-01).",
    )
    migration_plan_parser.add_argument(
        "--inventory-path",
        type=str,
        default=None,
        help=(
            "Path to the prod inventory JSON. "
            "Defaults to ~/.autonomi/testnets/<deployment-name>/inventory.json."
        ),
    )
    migration_plan_parser.add_argument(
        "--source-provider",
        type=str,
        required=True,
        choices=["aws", "digitalocean", "hetzner", "vultr"],
        help="Provider of the source VMs to migrate.",
    )
    migration_plan_parser.add_argument(
        "--source-region",
        type=str,
        required=True,
        help="Region of the source VMs (e.g. eu-central-1).",
    )
    migration_plan_parser.add_argument(
        "--target-provider",
        type=str,
        required=True,
        choices=["hetzner", "ovh"],
        help="Destination provider.",
    )
    migration_plan_parser.add_argument(
        "--target-server-type",
        type=str,
        required=True,
        help=(
            "Destination VM type/flavor (e.g. cpx42 for hetzner, b3-16 for ovh)."
        ),
    )
    migration_plan_parser.add_argument(
        "--target-volume-size",
        type=int,
        required=True,
        help="Attached data volume size in GB (e.g. 50).",
    )
    migration_plan_parser.add_argument(
        "--node-count-per-vm",
        type=int,
        default=15,
        help="Nodes per VM (default 15).",
    )

    # migration infra
    migration_infra_parser = migration_subparsers.add_parser(
        "infra",
        help=(
            "Provision destination Hetzner VMs for a migration via Terraform."
        ),
    )
    migration_infra_parser.add_argument(
        "--name", type=str, required=True, help="Migration name."
    )
    migration_infra_parser.add_argument(
        "--locations",
        type=str,
        required=True,
        help=(
            "Comma-separated <location>=<count> map (e.g. "
            "fsn1=3,hel1=3,nbg1=3). Sum must equal the number of VMs still "
            "in 'planned' status."
        ),
    )
    migration_infra_parser.add_argument(
        "--inventory-path",
        type=str,
        default=None,
        help=(
            "Path to inventory.json — used to compute name index offsets so "
            "new VMs don't collide with existing prod names. Defaults to "
            "~/.autonomi/testnets/<deployment-name>/inventory.json."
        ),
    )

    # migration destroy-infra
    migration_destroy_infra_parser = migration_subparsers.add_parser(
        "destroy-infra",
        help=(
            "Tear down a migration's Hetzner target VMs (Terraform destroy + "
            "DB reset). Used to recover from a failed `migration infra`."
        ),
    )
    migration_destroy_infra_parser.add_argument(
        "--name", type=str, required=True, help="Migration name."
    )
    migration_destroy_infra_parser.add_argument(
        "--locations",
        type=str,
        default=None,
        help=(
            "Comma-separated list of locations to destroy. Defaults to every "
            "location with a Terraform state file for this migration."
        ),
    )
    migration_destroy_infra_parser.add_argument(
        "--inventory-path",
        type=str,
        default=None,
        help="Inventory path (used to compute name_index_offset for destroy).",
    )
    migration_destroy_infra_parser.add_argument(
        "--force",
        action="store_true",
        help="Destroy even if some VMs are past 'nodes_started'.",
    )

    # migration provision
    migration_provision_parser = migration_subparsers.add_parser(
        "provision",
        help=(
            "Install ant-node + write systemd units on the migration's "
            "target VMs. Services are enabled but not started."
        ),
    )
    migration_provision_parser.add_argument(
        "--name", type=str, required=True, help="Migration name."
    )
    migration_provision_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH private key (default: ~/.ssh/id_rsa).",
    )
    migration_provision_parser.add_argument(
        "--rewards-address",
        type=str,
        required=True,
        help="EVM rewards address for the new ant-node services.",
    )
    migration_provision_parser.add_argument(
        "--bootstrap-peers-file",
        type=str,
        default=None,
        help=(
            "Path to bootstrap_peers.toml (default: "
            "~/.config/ant/bootstrap_peers.toml)."
        ),
    )
    migration_provision_parser.add_argument(
        "--port",
        type=int,
        default=10000,
        help="Initial port for ant-node services (default: 10000).",
    )
    migration_provision_parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        help="ant-node --log-level (default: leave unset).",
    )
    migration_provision_parser.add_argument(
        "--node-version",
        type=str,
        default=None,
        help="Released ant-node version to install (e.g. 0.10.0).",
    )
    migration_provision_parser.add_argument(
        "--branch-name",
        type=str,
        default=None,
        help="Build from this branch (use with --repo-owner).",
    )
    migration_provision_parser.add_argument(
        "--repo-owner",
        type=str,
        default=None,
        help="GitHub owner for branch builds (use with --branch-name).",
    )

    # migration start-nodes
    migration_start_nodes_parser = migration_subparsers.add_parser(
        "start-nodes",
        help="systemctl start ant-node services on the migration's target VMs.",
    )
    migration_start_nodes_parser.add_argument(
        "--name", type=str, required=True, help="Migration name."
    )
    migration_start_nodes_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH private key (default: ~/.ssh/id_rsa).",
    )

    # migration drain
    migration_drain_parser = migration_subparsers.add_parser(
        "drain",
        help="systemctl stop ant-node services on the migration's source VMs in waves.",
    )
    migration_drain_parser.add_argument(
        "--name", type=str, required=True, help="Migration name."
    )
    migration_drain_parser.add_argument(
        "--interval-secs",
        type=int,
        default=60,
        help=(
            "Seconds between each per-service stop (default: 60). "
            "Each VM's 15 ant-node services are stopped one at a time at "
            "this cadence; the same interval applies across the VM "
            "boundary, so there is no extra wait between VMs."
        ),
    )
    migration_drain_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after this many VMs in the current invocation.",
    )
    migration_drain_parser.add_argument(
        "--soak-mins",
        type=int,
        default=120,
        help=(
            "Minimum minutes since the most-recently-started VM before "
            "draining is allowed (default: 120)."
        ),
    )
    migration_drain_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default=None,
        help=(
            "Path to SSH private key (default: per-VM source_ssh_key from "
            "the inventory at plan time)."
        ),
    )
    migration_drain_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the soak window check.",
    )

    # migration destroy-source
    migration_destroy_source_parser = migration_subparsers.add_parser(
        "destroy-source",
        help="Terminate the AWS source VMs of a fully-drained migration.",
    )
    migration_destroy_source_parser.add_argument(
        "--name", type=str, required=True, help="Migration name."
    )
    migration_destroy_source_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Destroy even if some VMs are not yet drained (skips the safety check)."
        ),
    )

    # migration complete
    migration_complete_parser = migration_subparsers.add_parser(
        "complete",
        help="Splice the migration's target VMs into the prod inventory.json.",
    )
    migration_complete_parser.add_argument(
        "--name", type=str, required=True, help="Migration name."
    )
    migration_complete_parser.add_argument(
        "--inventory-path",
        type=str,
        default=None,
        help=(
            "Path to inventory.json (default: "
            "~/.autonomi/testnets/<deployment-name>/inventory.json)."
        ),
    )
    migration_complete_parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if some VMs are not in 'source_destroyed' state.",
    )

    # migration status
    migration_status_parser = migration_subparsers.add_parser(
        "status",
        help="Print per-VM status for a migration.",
    )
    migration_status_parser.add_argument(
        "--name", type=str, required=True, help="Migration name."
    )

    # migration list
    migration_list_parser = migration_subparsers.add_parser(
        "list",
        help="List all migrations recorded in the DB.",
    )
    del migration_list_parser  # silence unused-name warning; parser is registered

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
    nodes_infra_parser.add_argument(
        "--ovh-vm-count",
        type=int,
        default=0,
        help="Number of VMs per region on OVH (default: 0, provider not used)",
    )
    nodes_infra_parser.add_argument(
        "--ovh-flavor",
        type=str,
        default="b3-16",
        help="OVH instance flavor (default: b3-16)",
    )
    nodes_infra_parser.add_argument(
        "--ovh-regions",
        type=int,
        default=3,
        help="Number of OVH regions to use (default: 3). "
        "Ignored if --ovh-regions-list is set.",
    )
    nodes_infra_parser.add_argument(
        "--ovh-regions-list",
        type=str,
        default=None,
        help="Comma-separated list of specific OVH regions to use "
        "(e.g. GRA9,UK1,DE1). Overrides --ovh-regions and the default "
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
    nodes_provision_parser.add_argument(
        "--clock-skew-count",
        type=int,
        default=0,
        help="Number of node VMs to inject intentional clock skew into "
        "(default: 0 = disabled). Selected hosts disable time sync and shift "
        "their clock by a few hours (direction varies per host). Selection is "
        "region-distributed and persisted to deployment state on first run.",
    )
    nodes_provision_parser.add_argument(
        "--force-clock-skew-reselect",
        action="store_true",
        help="Re-compute the clock-skew VM selection from --clock-skew-count "
        "even if deployment state already has one recorded. Use with caution — "
        "this can flip a VM between skewed and un-skewed between runs.",
    )
    nodes_provision_parser.add_argument(
        "--full-percent",
        type=int,
        default=0,
        help="Percentage of node VMs to make storage-full by pre-filling their "
        "data volume (0-100, default: 0 = disabled), so every ant-node on the "
        "VM sees no free space and behaves as full. The percentage is taken of "
        "the non-NAT VMs — the selection is always disjoint from --nat-percent. "
        "Selection is uniform random (NOT region-spread, so full nodes scatter "
        "across the keyspace), seeded by deployment name, and is persisted to "
        "deployment state on first run.",
    )
    nodes_provision_parser.add_argument(
        "--force-full-reselect",
        action="store_true",
        help="Re-compute the storage-full VM selection from --full-percent even "
        "if deployment state already has one recorded. Use with caution — this "
        "can flip a VM between full and non-full between runs.",
    )
    nodes_provision_parser.add_argument(
        "--full-headroom-mb",
        type=int,
        default=100,  # mirrors full_nodes.DEFAULT_FULL_HEADROOM_MB (kept literal
        # here to avoid importing pyinfra-heavy provisioning code at parse time)
        help="Free space (MiB) to leave on the data volume when making a VM "
        "storage-full (default: 100). Only meaningful with --full-percent > 0.",
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
        "--clock-skew",
        type=str,
        default=None,
        metavar="DURATION",
        help="Inject intentional clock skew on the downloader VM: disable time "
        "sync and shift the system clock by a signed Go-style duration (e.g. "
        "2h, -2h, 30m). For testing clock-independence of the network.",
    )
    prod_cd_deploy_parser.add_argument(
        "--bootstrap-cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable the ant peer cache (default): ant runs WITHOUT --bootstrap "
        "and reads a provisioned bootstrap_peers.toml. --no-bootstrap-cache "
        "restores the legacy --bootstrap invocation (cache disabled).",
    )
    prod_cd_deploy_parser.add_argument(
        "--forward-logs",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Forward the downloader's ant stdout+stderr to a dedicated "
        "Elasticsearch instance stood up by this command (index "
        "telegraf-ant-clients-<name>). Default off (current behaviour).",
    )
    prod_cd_deploy_parser.add_argument(
        "--rust-log",
        type=str,
        default=None,
        metavar="SPEC",
        help="Set RUST_LOG on the downloader service (e.g. "
        "'info,saorsa_core::dht_network_manager=debug'). Overrides the ant "
        "verbosity flag.",
    )
    prod_cd_deploy_parser.add_argument(
        "--memory-profiling",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Wrap the downloader's ant invocation with heaptrack (installs the "
        "heaptrack apt package); profiles → /var/log/ant-heaptrack/ on the VM. "
        "Default off.",
    )
    prod_cd_deploy_parser.add_argument(
        "--addresses-path",
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
        "--debug",
        action="store_true",
        help="Informational: record that the resolved branch binary is a debug "
        "build (built without --release) so `deployments slack-post` surfaces it. "
        "The actual debug build is driven by the manifest's binary `debug: true` "
        "via the separate `build ant` step; this flag does not change the fetch.",
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
    prod_cd_deploy_parser.add_argument(
        "--purpose",
        type=str,
        required=True,
        help="Free-text description of what this deployment is for. "
        "Persisted into deploy_args and surfaced by `deployments slack-post`.",
    )
    prod_cd_deploy_parser.add_argument(
        "--sleep-interval-secs",
        type=int,
        default=900,
        help=(
            "Seconds to sleep at the end of each full pass through the address "
            "list before starting the next cycle. Default: 900 (15 minutes). "
            "Pass 0 to disable inter-cycle sleeping (back-to-back cycling)."
        ),
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
        "--keep-elastic",
        action="store_true",
        help="Leave the dedicated client-log Elasticsearch VM running (only "
        "created when the deploy used --forward-logs). Default: it is destroyed "
        "alongside the client.",
    )
    prod_cd_destroy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa).",
    )

    # --- prod downloaders (one-shot) ---
    prod_dl_parser = prod_subparsers.add_parser(
        "downloaders",
        help="One-shot variant of continuous-downloader: walks the address "
        "list a single time and then exits.",
    )
    prod_dl_subparsers = prod_dl_parser.add_subparsers(
        dest="prod_dl_action", required=True
    )

    prod_dl_deploy_parser = prod_dl_subparsers.add_parser(
        "deploy",
        help="Deploy a one-shot prod downloaders client VM and its monitoring stack.",
    )
    prod_dl_deploy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (also used for the paired monitoring stack).",
    )
    prod_dl_deploy_parser.add_argument(
        "--spec",
        type=str,
        required=True,
        help="Downloader VM spec 'region[:vm_size]' (single client VM).",
    )
    prod_dl_deploy_parser.add_argument(
        "--clock-skew",
        type=str,
        default=None,
        metavar="DURATION",
        help="Inject intentional clock skew on the downloader VM: disable time "
        "sync and shift the system clock by a signed Go-style duration (e.g. "
        "2h, -2h, 30m). For testing clock-independence of the network.",
    )
    prod_dl_deploy_parser.add_argument(
        "--bootstrap-cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable the ant peer cache (default): ant runs WITHOUT --bootstrap "
        "and reads a provisioned bootstrap_peers.toml. --no-bootstrap-cache "
        "restores the legacy --bootstrap invocation (cache disabled).",
    )
    prod_dl_deploy_parser.add_argument(
        "--forward-logs",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Forward the downloader's ant stdout+stderr to a dedicated "
        "Elasticsearch instance stood up by this command (index "
        "telegraf-ant-clients-<name>). Default off (current behaviour).",
    )
    prod_dl_deploy_parser.add_argument(
        "--rust-log",
        type=str,
        default=None,
        metavar="SPEC",
        help="Set RUST_LOG on the downloader service (e.g. "
        "'info,saorsa_core::dht_network_manager=debug'). Overrides the ant "
        "verbosity flag.",
    )
    prod_dl_deploy_parser.add_argument(
        "--memory-profiling",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Wrap the downloader's ant invocation with heaptrack (installs the "
        "heaptrack apt package); profiles → /var/log/ant-heaptrack/ on the VM. "
        "Default off.",
    )
    prod_dl_deploy_parser.add_argument(
        "--addresses-path",
        type=str,
        default=None,
        help="Local path to a newline-separated list of public file addresses. "
        "Defaults to the list shipped in resources/prod_addresses.txt.",
    )
    prod_dl_deploy_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built ant binary from this branch (requires --repo-owner).",
    )
    prod_dl_deploy_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built ant binary (requires --branch-name).",
    )
    prod_dl_deploy_parser.add_argument(
        "--ant-version",
        type=str,
        help="Specific release version of ant to install (e.g., 0.3.0).",
    )
    prod_dl_deploy_parser.add_argument(
        "--debug",
        action="store_true",
        help="Informational: record that the resolved branch binary is a debug "
        "build (built without --release) so `deployments slack-post` surfaces it. "
        "The actual debug build is driven by the manifest's binary `debug: true` "
        "via the separate `build ant` step; this flag does not change the fetch.",
    )
    prod_dl_deploy_parser.add_argument(
        "--monitoring-volume-size",
        type=int,
        default=50,
        help="Monitoring VM attached volume size in GB (default: 50).",
    )
    prod_dl_deploy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa).",
    )
    prod_dl_deploy_parser.add_argument(
        "--purpose",
        type=str,
        required=True,
        help="Free-text description of what this deployment is for. "
        "Persisted into deploy_args and surfaced by `deployments slack-post`.",
    )

    prod_dl_destroy_parser = prod_dl_subparsers.add_parser(
        "destroy",
        help="Destroy a prod downloaders deployment "
        "(clients and, optionally, monitoring).",
    )
    prod_dl_destroy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name to tear down.",
    )
    prod_dl_destroy_parser.add_argument(
        "--keep-monitoring",
        action="store_true",
        help="Leave the paired monitoring VM running so recorded metrics "
        "remain queryable after the client VM is gone. "
        "Default: the monitoring VM is destroyed alongside the client.",
    )
    prod_dl_destroy_parser.add_argument(
        "--keep-elastic",
        action="store_true",
        help="Leave the dedicated client-log Elasticsearch VM running (only "
        "created when the deploy used --forward-logs). Default: it is destroyed "
        "alongside the client.",
    )
    prod_dl_destroy_parser.add_argument(
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
        metavar=(
            "REGION:VM_SIZE:urls=PATH[:payment=MODE][:clock_skew=DURATION]"
            "[:bootstrap_cache=BOOL]"
        ),
        help=(
            "Uploader spec, repeatable. Example: "
            "lon1:s-2vcpu-4gb:urls=/path/to/list.txt:payment=merkle. "
            "payment=merkle|no-merkle is optional and applies per-uploader; "
            "omit to let ant pick its default. clock_skew=DURATION disables time "
            "sync and shifts that VM's clock by a signed Go-style duration "
            "(e.g. 2h, -2h, 30m; testing only). bootstrap_cache=true|false "
            "(default true) — when true ant runs WITHOUT --bootstrap (peer "
            "cache enabled, peers read from a provisioned bootstrap_peers.toml); "
            "false restores the legacy --bootstrap invocation."
        ),
    )
    prod_up_deploy_parser.add_argument(
        "--allow-duplicate-files",
        action="store_true",
        help=(
            "Proceed even if a file to be uploaded already exists in the prod "
            "uploads list (matched by file name). By default the deploy is "
            "refused before any infrastructure is created. Override this to "
            "re-upload missing chunks or to test that already-stored chunks "
            "are not paid for again."
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
        "--debug",
        action="store_true",
        help="Informational: record that the resolved branch binary is a debug "
        "build (built without --release) so `deployments slack-post` surfaces it. "
        "The actual debug build is driven by the manifest's binary `debug: true` "
        "via the separate `build ant` step; this flag does not change the fetch.",
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
    prod_up_deploy_parser.add_argument(
        "--purpose",
        type=str,
        required=True,
        help="Free-text description of what this deployment is for. "
        "Persisted into deploy_args and surfaced by `deployments slack-post`.",
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
        "--keep-elastic",
        action="store_true",
        help="Leave the dedicated client-log Elasticsearch VM running (only "
        "created when the deploy used a forward_logs uploader). Default: it is "
        "destroyed alongside the uploaders.",
    )
    prod_up_destroy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa).",
    )

    # --- prod canary ---
    prod_canary_parser = prod_subparsers.add_parser(
        "canary",
        help=(
            "Deploy a canary set: an upload VM walks a URL list once and "
            "probe VMs continuously check the resulting public files for "
            "loss. The Influx + Postgres dataset survives destroy by default."
        ),
    )
    prod_canary_subparsers = prod_canary_parser.add_subparsers(
        dest="prod_canary_action", required=True
    )

    prod_canary_deploy_parser = prod_canary_subparsers.add_parser(
        "deploy",
        help="Deploy canary upload + probe VMs and their paired monitoring stack.",
    )
    prod_canary_deploy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (also used for the paired monitoring stack).",
    )
    prod_canary_deploy_parser.add_argument(
        "--upload-spec",
        type=str,
        required=True,
        metavar="REGION[:VM_SIZE]",
        help="Spec for the single upload VM (default vm_size: s-1vcpu-2gb).",
    )
    prod_canary_deploy_parser.add_argument(
        "--probe-spec",
        action="append",
        default=[],
        metavar="REGION[:VM_SIZE]:count=N",
        help=(
            "Probe spec, repeatable. Total probe VM count = sum of count=N "
            "values; each probe VM is assigned a sequential shard index. "
            "Default vm_size: s-2vcpu-4gb (probes need RAM headroom for "
            "concurrent large-file downloads)."
        ),
    )
    prod_canary_deploy_parser.add_argument(
        "--secret-key",
        type=str,
        default=None,
        help=(
            "Arbitrum One wallet secret key for the upload VM. Falls back to "
            "ARBITRUM_ONE_SECRET_KEY env var. Must be a funded mainnet wallet."
        ),
    )
    prod_canary_deploy_parser.add_argument(
        "--urls-file",
        type=str,
        required=True,
        help="Local path to a newline-separated list of public source URLs.",
    )
    prod_canary_deploy_parser.add_argument(
        "--upload-sleep-secs",
        type=int,
        default=0,
        help="Seconds to sleep between uploads (default: 0).",
    )
    prod_canary_deploy_parser.add_argument(
        "--probe-concurrency",
        type=int,
        default=3,
        help=(
            "Concurrent ant download invocations per probe VM (default: 3). "
            "Sized so 3 concurrent ~500 MB downloads fit in an s-2vcpu-4gb "
            "probe VM without OOM."
        ),
    )
    prod_canary_deploy_parser.add_argument(
        "--bootstrap-cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable the ant peer cache on the upload + probe VMs (default): "
        "ant runs WITHOUT --bootstrap and reads a provisioned "
        "bootstrap_peers.toml. --no-bootstrap-cache restores the legacy "
        "--bootstrap invocation (cache disabled).",
    )
    prod_canary_deploy_parser.add_argument(
        "--forward-logs",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Forward the canary upload + probe VMs' ant stdout+stderr to a "
        "dedicated Elasticsearch instance stood up by this command (index "
        "telegraf-ant-clients-<name>). Default off (current behaviour).",
    )
    prod_canary_deploy_parser.add_argument(
        "--rust-log",
        type=str,
        default=None,
        metavar="SPEC",
        help="Set RUST_LOG on the canary services (e.g. "
        "'info,saorsa_core::dht_network_manager=debug'). Overrides the ant "
        "verbosity flag.",
    )
    prod_canary_deploy_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built ant binary from this branch (requires --repo-owner).",
    )
    prod_canary_deploy_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built ant binary (requires --branch-name).",
    )
    prod_canary_deploy_parser.add_argument(
        "--ant-version",
        type=str,
        help="Specific release version of ant to install (e.g., 0.3.0).",
    )
    prod_canary_deploy_parser.add_argument(
        "--monitoring-volume-size",
        type=int,
        default=50,
        help="Monitoring VM attached volume size in GB (default: 50).",
    )
    prod_canary_deploy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa).",
    )
    prod_canary_deploy_parser.add_argument(
        "--purpose",
        type=str,
        required=True,
        help=(
            "Free-text description of what this canary is for. "
            "Persisted into deploy_args."
        ),
    )

    prod_canary_destroy_parser = prod_canary_subparsers.add_parser(
        "destroy",
        help=(
            "Destroy a canary deployment, including the paired monitoring "
            "stack. Pass --keep-monitoring if you want Influx + Grafana "
            "to survive (record-results should already have flushed any "
            "useful state into Postgres)."
        ),
    )
    prod_canary_destroy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name to tear down.",
    )
    prod_canary_destroy_parser.add_argument(
        "--keep-monitoring",
        action="store_true",
        default=False,
        help=(
            "Leave the paired monitoring VM (and its attached volume) "
            "running. Default OFF — the monitoring stack is treated as "
            "ephemeral; persisted state lives in Postgres."
        ),
    )
    prod_canary_destroy_parser.add_argument(
        "--no-keep-monitoring",
        dest="keep_monitoring",
        action="store_false",
        help=(
            "Tear down the paired monitoring VM along with the canary VMs. "
            "This is the default; the flag is retained for explicitness "
            "and back-compat with scripts."
        ),
    )
    prod_canary_destroy_parser.add_argument(
        "--keep-elastic",
        action="store_true",
        help="Leave the dedicated client-log Elasticsearch VM running (only "
        "created when the deploy used --forward-logs). Default: it is destroyed "
        "alongside the canary VMs.",
    )
    prod_canary_destroy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa).",
    )

    # --- prod canary provision-reporter ---
    prod_canary_pr_parser = prod_canary_subparsers.add_parser(
        "provision-reporter",
        help=(
            "Install a daily Slack reporter on the canary's monitoring VM. "
            "The reporter reads Influx locally and posts a Block Kit digest "
            "to the given incoming webhook."
        ),
    )
    prod_canary_pr_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Canary deployment name (also the monitoring stack name).",
    )
    prod_canary_pr_parser.add_argument(
        "--slack-webhook-url",
        type=str,
        default=None,
        help=(
            "Slack incoming webhook URL. Falls back to SLACK_WEBHOOK_URL "
            "in the environment."
        ),
    )
    prod_canary_pr_parser.add_argument(
        "--on-calendar",
        type=str,
        default="*-*-* 09:00:00",
        help=(
            "Systemd OnCalendar expression for the timer "
            "(default: '*-*-* 09:00:00' — daily at 09:00 UTC)."
        ),
    )
    prod_canary_pr_parser.add_argument(
        "--run-now",
        dest="run_now",
        action="store_true",
        default=True,
        help=(
            "Trigger one immediate run after install so you see a "
            "confirmation message in Slack (default)."
        ),
    )
    prod_canary_pr_parser.add_argument(
        "--no-run-now",
        dest="run_now",
        action="store_false",
        help="Don't trigger an immediate run after install.",
    )
    prod_canary_pr_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa).",
    )

    # --- prod canary unprovision-reporter ---
    prod_canary_upr_parser = prod_canary_subparsers.add_parser(
        "unprovision-reporter",
        help=(
            "Remove the reporter timer + script from the canary's "
            "monitoring VM. Leaves Influx + Grafana untouched."
        ),
    )
    prod_canary_upr_parser.add_argument(
        "--name", type=str, required=True,
        help="Canary deployment name.",
    )
    prod_canary_upr_parser.add_argument(
        "--purge-state",
        action="store_true",
        default=False,
        help=(
            "Also delete the persisted last-report.json snapshot. By "
            "default it is kept so a re-provision can resume delta "
            "computation."
        ),
    )
    prod_canary_upr_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa).",
    )

    # --- prod local-uploads ---
    prod_lu_parser = prod_subparsers.add_parser(
        "local-uploads",
        help=(
            "Run prod-network uploads from the local machine. Records "
            "per-file results directly into Postgres; no remote infra."
        ),
    )
    prod_lu_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name. Registered in the database; used to scope "
        "per-file results in deployment_prod_upload_results.",
    )
    prod_lu_input_group = prod_lu_parser.add_mutually_exclusive_group(required=True)
    prod_lu_input_group.add_argument(
        "--urls",
        type=str,
        default=None,
        metavar="FILE",
        help="Path to a newline-separated list of source URLs. Each URL is "
        "downloaded to a temp file, uploaded with `ant file upload --public`, "
        "then the temp file is deleted. Original URL targets are not touched.",
    )
    prod_lu_input_group.add_argument(
        "--local-dir-path",
        type=str,
        default=None,
        metavar="DIR",
        help="Path to a local directory whose files will each be uploaded "
        "with `ant file upload --public`. Subdirectories are walked "
        "recursively and every regular file is uploaded; each file is "
        "identified by its path relative to the given root. Source files "
        "are never modified or deleted.",
    )
    prod_lu_parser.add_argument(
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
    prod_lu_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built ant binary from this branch (requires --repo-owner).",
    )
    prod_lu_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built ant binary (requires --branch-name).",
    )
    prod_lu_parser.add_argument(
        "--ant-version",
        type=str,
        help="Specific release version of ant to install (e.g., 0.3.0).",
    )
    prod_lu_parser.add_argument(
        "--payment",
        type=str,
        default=None,
        metavar="MODE",
        help="Payment mode passed through to the ant CLI (e.g. 'merkle', "
        "'no-merkle'). Omit to let ant pick its default.",
    )
    from saorsa_deploy.config.models import VALID_LOG_LEVELS

    prod_lu_parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=VALID_LOG_LEVELS,
        metavar="LEVEL",
        help="ant log verbosity: info (default), debug, trace. Supersedes the "
        "deprecated -v/--verbose flag if both are given.",
    )
    prod_lu_parser.add_argument(
        # Deprecated alias for --log-level, kept for backwards compatibility.
        # Hidden from help; --log-level is the documented knob.
        "-v",
        "--verbose",
        action="count",
        default=0,
        help=argparse.SUPPRESS,
    )
    prod_lu_parser.add_argument(
        "--purpose",
        type=str,
        required=True,
        help="Free-text description of what this deployment is for. "
        "Persisted into deploy_args and surfaced by `deployments slack-post`.",
    )
    prod_lu_parser.add_argument(
        "--bootstrap-cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable the ant peer cache (default): ant runs WITHOUT --bootstrap "
        "and reads ~/.config/ant/bootstrap_peers.toml (created from the fetched "
        "prod list only when missing — an existing file is never overwritten). "
        "--no-bootstrap-cache restores the legacy --bootstrap invocation "
        "(cache disabled).",
    )

    # --- prod local-downloads ---
    prod_ld_parser = prod_subparsers.add_parser(
        "local-downloads",
        help=(
            "Run prod-network downloads from the local machine. Records "
            "per-address results directly into Postgres; no remote infra."
        ),
    )
    prod_ld_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name. Registered in the database; used to scope "
        "per-address results in deployment_prod_download_results.",
    )
    prod_ld_parser.add_argument(
        "--addresses-path",
        type=str,
        required=True,
        metavar="FILE",
        help="Path to a newline-separated list of addresses to download. "
        "Lines starting with '#' and blank lines are ignored.",
    )
    prod_ld_parser.add_argument(
        "--keep-output-dir",
        type=str,
        default=None,
        metavar="DIR",
        help="Directory to write downloaded files into. When set, files are "
        "kept after recording. When omitted, each download goes to a "
        "per-run temp directory and is deleted after the result row is "
        "recorded.",
    )
    prod_ld_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built ant binary from this branch (requires --repo-owner).",
    )
    prod_ld_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built ant binary (requires --branch-name).",
    )
    prod_ld_parser.add_argument(
        "--ant-version",
        type=str,
        help="Specific release version of ant to install (e.g., 0.3.0).",
    )
    prod_ld_parser.add_argument(
        "--purpose",
        type=str,
        required=True,
        help="Free-text description of what this deployment is for. "
        "Persisted into deploy_args and surfaced by `deployments slack-post`.",
    )
    prod_ld_parser.add_argument(
        "--bootstrap-cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable the ant peer cache (default): ant runs WITHOUT --bootstrap "
        "and reads ~/.config/ant/bootstrap_peers.toml (created from the fetched "
        "prod list only when missing — an existing file is never overwritten). "
        "--no-bootstrap-cache restores the legacy --bootstrap invocation "
        "(cache disabled).",
    )

    # --- prod extend-unmanaged-volumes ---
    prod_euv_parser = prod_subparsers.add_parser(
        "extend-unmanaged-volumes",
        help="Grow the data volume (LVM or raw ext4) on every node VM in a "
        "production inventory, sequentially and without stopping ant-node "
        "services.",
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
        choices=["aws", "digitalocean", "hetzner", "ovh", "vultr"],
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

    # --- prod files ---
    prod_files_parser = prod_subparsers.add_parser(
        "files",
        help="List every file uploaded to the production Autonomi network "
        "(from recorded prod-uploaders / prod-local-uploads results).",
    )
    prod_files_parser.add_argument(
        "--failed",
        action="store_true",
        help="Print only failed uploads instead of the default successful ones.",
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

    # reconcile
    ti_reconcile = track_upgrades_sub.add_parser(
        "reconcile",
        help=(
            "Repair stuck rows by re-deriving each non-terminal node's state "
            "from its complete Elasticsearch history. Cursor-independent — "
            "recovers events that incremental sync dropped/missed."
        ),
    )
    ti_reconcile.add_argument("--db", type=str, required=True)

    # =========================================================================
    # === release-candidates group (new, ls, show) ===
    # =========================================================================
    rc_parser = subparsers.add_parser(
        "release-candidates",
        help=(
            "Record and inspect release candidates cut by the release-candidate "
            "skill (per-crate PR history)."
        ),
    )
    rc_sub = rc_parser.add_subparsers(
        dest="release_candidates_verb", required=True
    )

    rc_new = rc_sub.add_parser(
        "new",
        help=(
            "Record a release candidate + its PR list. Reads a JSON payload "
            "from --from-file or stdin."
        ),
    )
    rc_new.add_argument(
        "--from-file",
        type=str,
        default=None,
        help="Path to a JSON payload file. Use '-' or omit to read stdin.",
    )

    rc_ls = rc_sub.add_parser(
        "ls",
        help=(
            "List recorded release candidates (parent rows with their "
            "constituent crates indented underneath)."
        ),
    )
    rc_ls.add_argument(
        "--binary-target", dest="binary_target", type=str, default=None,
        help="Filter by binary target (e.g. ant, ant-node).",
    )
    rc_ls.add_argument(
        "--limit", type=int, default=None,
        help="Cap the number of release candidates returned.",
    )

    rc_show = rc_sub.add_parser(
        "show",
        help="Show a single release candidate with all its crates + PRs.",
    )
    rc_show.add_argument(
        "--id", type=int, default=None,
        help="Look up by release-candidate id (preferred).",
    )
    rc_show.add_argument(
        "--binary-target", dest="binary_target", type=str, default=None,
        help="Look up by binary target (combine with --version).",
    )
    rc_show.add_argument(
        "--version", type=str, default=None,
        help="Look up by binary version (combine with --binary-target).",
    )

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

    args = parser.parse_args(argv)

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
        if args.deployments_subcommand == "config":
            if args.config_subcommand == "show":
                from saorsa_deploy.cmd.config_cmds import cmd_config_show

                cmd_config_show(args)
            elif args.config_subcommand == "validate":
                from saorsa_deploy.cmd.config_cmds import cmd_config_validate

                cmd_config_validate(args)
        elif args.deployments_subcommand == "down":
            from saorsa_deploy.cmd.deploy_cmds import cmd_down

            cmd_down(args)
        elif args.deployments_subcommand == "edit-analysis-summary":
            from saorsa_deploy.cmd.edit_analysis_summary import (
                cmd_edit_analysis_summary,
            )

            cmd_edit_analysis_summary(args)
        elif args.deployments_subcommand == "extend":
            from saorsa_deploy.cmd.extend import cmd_extend

            cmd_extend(args)
        elif args.deployments_subcommand == "generate-inventory":
            from saorsa_deploy.cmd.generate_inventory import cmd_generate_inventory

            cmd_generate_inventory(args)
        elif args.deployments_subcommand == "ls":
            from saorsa_deploy.cmd.ls import cmd_ls

            cmd_ls(args)
        elif args.deployments_subcommand == "plan":
            from saorsa_deploy.cmd.deploy_cmds import cmd_plan

            cmd_plan(args)
        elif args.deployments_subcommand == "print-downloads":
            from saorsa_deploy.cmd.print_file_ops import cmd_print_downloads

            cmd_print_downloads(args)
        elif args.deployments_subcommand == "print-recorded-results":
            from saorsa_deploy.cmd.print_recorded_results import (
                cmd_print_recorded_results,
            )

            cmd_print_recorded_results(args)
        elif args.deployments_subcommand == "print-uploads":
            from saorsa_deploy.cmd.print_file_ops import cmd_print_uploads

            cmd_print_uploads(args)
        elif args.deployments_subcommand == "record-analysis":
            from saorsa_deploy.cmd.record_analysis import cmd_record_analysis

            cmd_record_analysis(args)
        elif args.deployments_subcommand == "record-results":
            from saorsa_deploy.cmd.record_results import cmd_record_results

            cmd_record_results(args)
        elif args.deployments_subcommand == "slack-post":
            from saorsa_deploy.cmd.slack_post import cmd_slack_post

            cmd_slack_post(args)
        elif args.deployments_subcommand == "unextend":
            from saorsa_deploy.cmd.unextend import cmd_unextend

            cmd_unextend(args)
        elif args.deployments_subcommand == "up":
            from saorsa_deploy.cmd.deploy_cmds import cmd_up

            cmd_up(args)
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
    elif args.command == "migration":
        if args.migration_subcommand == "plan":
            from saorsa_deploy.cmd.migration_plan import cmd_migration_plan

            cmd_migration_plan(args)
        elif args.migration_subcommand == "infra":
            from saorsa_deploy.cmd.migration_infra import cmd_migration_infra

            cmd_migration_infra(args)
        elif args.migration_subcommand == "destroy-infra":
            from saorsa_deploy.cmd.migration_destroy_infra import (
                cmd_migration_destroy_infra,
            )

            cmd_migration_destroy_infra(args)
        elif args.migration_subcommand == "provision":
            from saorsa_deploy.cmd.migration_provision import (
                cmd_migration_provision,
            )

            cmd_migration_provision(args)
        elif args.migration_subcommand == "start-nodes":
            from saorsa_deploy.cmd.migration_start_nodes import (
                cmd_migration_start_nodes,
            )

            cmd_migration_start_nodes(args)
        elif args.migration_subcommand == "drain":
            from saorsa_deploy.cmd.migration_drain import cmd_migration_drain

            cmd_migration_drain(args)
        elif args.migration_subcommand == "destroy-source":
            from saorsa_deploy.cmd.migration_destroy_source import (
                cmd_migration_destroy_source,
            )

            cmd_migration_destroy_source(args)
        elif args.migration_subcommand == "complete":
            from saorsa_deploy.cmd.migration_complete import (
                cmd_migration_complete,
            )

            cmd_migration_complete(args)
        elif args.migration_subcommand == "status":
            from saorsa_deploy.cmd.migration_status import cmd_migration_status

            cmd_migration_status(args)
        elif args.migration_subcommand == "list":
            from saorsa_deploy.cmd.migration_list import cmd_migration_list

            cmd_migration_list(args)
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
            args.prod_subcommand == "downloaders"
            and args.prod_dl_action == "deploy"
        ):
            from saorsa_deploy.cmd.prod_downloaders import (
                cmd_prod_downloaders_deploy,
            )

            cmd_prod_downloaders_deploy(args)
        elif (
            args.prod_subcommand == "downloaders"
            and args.prod_dl_action == "destroy"
        ):
            from saorsa_deploy.cmd.prod_downloaders import (
                cmd_prod_downloaders_destroy,
            )

            cmd_prod_downloaders_destroy(args)
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
        elif (
            args.prod_subcommand == "canary"
            and args.prod_canary_action == "deploy"
        ):
            from saorsa_deploy.cmd.prod_canary import cmd_prod_canary_deploy

            cmd_prod_canary_deploy(args)
        elif (
            args.prod_subcommand == "canary"
            and args.prod_canary_action == "destroy"
        ):
            from saorsa_deploy.cmd.prod_canary import cmd_prod_canary_destroy

            cmd_prod_canary_destroy(args)
        elif (
            args.prod_subcommand == "canary"
            and args.prod_canary_action == "provision-reporter"
        ):
            from saorsa_deploy.cmd.prod_canary_reporter import (
                cmd_prod_canary_provision_reporter,
            )

            cmd_prod_canary_provision_reporter(args)
        elif (
            args.prod_subcommand == "canary"
            and args.prod_canary_action == "unprovision-reporter"
        ):
            from saorsa_deploy.cmd.prod_canary_reporter import (
                cmd_prod_canary_unprovision_reporter,
            )

            cmd_prod_canary_unprovision_reporter(args)
        elif args.prod_subcommand == "local-uploads":
            from saorsa_deploy.cmd.prod_local_uploads import (
                cmd_prod_local_uploads,
            )

            cmd_prod_local_uploads(args)
        elif args.prod_subcommand == "local-downloads":
            from saorsa_deploy.cmd.prod_local_downloads import (
                cmd_prod_local_downloads,
            )

            cmd_prod_local_downloads(args)
        elif args.prod_subcommand == "extend-unmanaged-volumes":
            from saorsa_deploy.cmd.prod_extend_unmanaged_volumes import (
                cmd_prod_extend_unmanaged_volumes,
            )

            cmd_prod_extend_unmanaged_volumes(args)
        elif args.prod_subcommand == "files":
            from saorsa_deploy.cmd.prod_files import cmd_prod_files

            cmd_prod_files(args)
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
        elif verb == "reconcile":
            from saorsa_deploy.cmd.track_upgrades.reconcile_cmd import (
                cmd_track_upgrades_reconcile,
            )
            cmd_track_upgrades_reconcile(args)
    elif args.command == "release-candidates":
        verb = args.release_candidates_verb
        if verb == "new":
            from saorsa_deploy.cmd.release_candidates.new_cmd import (
                cmd_release_candidates_new,
            )
            cmd_release_candidates_new(args)
        elif verb == "ls":
            from saorsa_deploy.cmd.release_candidates.ls_cmd import (
                cmd_release_candidates_ls,
            )
            cmd_release_candidates_ls(args)
        elif verb == "show":
            from saorsa_deploy.cmd.release_candidates.show_cmd import (
                cmd_release_candidates_show,
            )
            cmd_release_candidates_show(args)
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
