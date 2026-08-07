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


def _add_telegraf_fold_args(p):
    """Shared Telegraf-fold args for the node-tier provision commands.

    When ``--with-telegraf`` is set, the command installs the Elasticsearch
    log-forwarder and/or InfluxDB host-metrics Telegraf (whichever the
    deployment has state for) in the SAME pyinfra pass it uses to provision the
    ant-node services — avoiding a second pass over the node VMs.
    """
    p.add_argument(
        "--with-telegraf",
        action="store_true",
        help="Install Telegraf (ES log forwarding + InfluxDB host metrics, "
        "whichever the deployment has elastic/monitoring state for) in the same "
        "pass as node provisioning.",
    )
    p.add_argument(
        "--drop-log-levels",
        type=str,
        default=None,
        help="Comma-separated ant-node log levels Telegraf drops before "
        'shipping to Elasticsearch (only with --with-telegraf). Use "" to keep '
        "every line; omit to use the default (DEBUG,TRACE).",
    )
    p.add_argument(
        "--buffer-limit",
        type=int,
        default=None,
        help="Telegraf ES output buffer (metric_buffer_limit) in lines "
        "(only with --with-telegraf).",
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
    _add_telegraf_fold_args(bootstraps_provision_parser)
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
        default=None,
        help=(
            "SSH key used to stop the ant-uploader service on each uploader VM "
            "before draining its wallet, so the drain isn't racing a live "
            "spender. Best-effort: unreachable VMs are skipped. Defaults to the "
            "key recorded in client state at provision time, falling back to "
            "~/.ssh/id_rsa."
        ),
    )
    clients_destroy_parser.add_argument(
        "--skip-stop-services",
        action="store_true",
        help=(
            "Skip SSHing into uploader VMs to stop ant-uploader before "
            "draining. Use on a retry after a partial destroy failure when "
            "the remaining VMs are unreachable and the stop step would just "
            "stall waiting on connection timeouts."
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
        "[:clock_skew=DURATION][:bootstrap_cache=BOOL][:network=PROFILE]'. "
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
        "[:network=PROFILE]"
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
    clients_provision_parser.add_argument(
        "--forward-buffer-limit",
        type=int,
        default=None,
        help="Size (in lines) of the client→Elasticsearch log forwarder's "
        "Telegraf output buffer (metric_buffer_limit), for clients with "
        "forward_logs set. Defaults to 200,000 — bump it (e.g. to match the "
        "node forwarder's --buffer-limit) when a client logs at TRACE so a "
        "high-volume run doesn't overflow the buffer and drop forwarded lines. "
        "No effect unless at least one client opted into log forwarding.",
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

    # deployments provision-telegraf
    deployments_provision_telegraf_parser = deployments_subparsers.add_parser(
        "provision-telegraf",
        help=(
            "Install Telegraf for Elasticsearch log forwarding AND InfluxDB host "
            "metrics in a single pass per host group. A backend is provisioned "
            "only when its credentials resolve (explicit flags, or whatever "
            "elastic/monitoring state --name has)."
        ),
    )
    deployments_provision_telegraf_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Deployment name. Required unless --host-ips-file/--inventory-path "
        "is provided. Loads elastic + monitoring credentials and node IPs from state.",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--host-ips-file",
        type=str,
        default=None,
        help="Path to a text file containing one IP address per line "
        "(alternative to loading IPs from deployment state).",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--inventory-path",
        type=str,
        default=None,
        help="Path to an inventory JSON file (reads ip/ssh_user/ssh_key/role per entry).",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--host-role",
        type=str,
        default=None,
        choices=["GENESIS", "BOOTSTRAP", "WAN_NODE", "CLIENT"],
        help="Force this host_role tag for every targeted host (retrofit modes). "
        "CLIENT additionally routes host metrics to the client-metrics bucket.",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa)",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--ssh-user",
        type=str,
        default="root",
        help="SSH username for provisioning (default: root)",
    )
    # --- Elasticsearch backend ---
    deployments_provision_telegraf_parser.add_argument(
        "--elasticsearch-url",
        type=str,
        default=None,
        help="Elasticsearch endpoint URL (auto-loaded from elastic state if --name).",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--elasticsearch-username",
        type=str,
        default=None,
        help="Elasticsearch username (auto-loaded from elastic state if --name).",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--elasticsearch-password",
        type=str,
        default=None,
        help="Elasticsearch password (auto-loaded from elastic state if --name).",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--node-version",
        type=str,
        default=None,
        help="Version of the deployed ant-node binary (ES global tag).",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--branch-commit",
        type=str,
        default=None,
        help="Git branch or commit of the deployed binary (ES global tag).",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--repo-owner",
        type=str,
        default="maidsafe",
        help="Repository owner for branch builds (default: maidsafe).",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--ant-log-dir",
        type=str,
        default=None,
        help="Override the ant-node log directory that Telegraf tails "
        "(default: /mnt/ant-node-storage/log).",
    )
    deployments_provision_telegraf_tail_group = (
        deployments_provision_telegraf_parser.add_mutually_exclusive_group()
    )
    deployments_provision_telegraf_tail_group.add_argument(
        "--from-start",
        dest="from_beginning",
        action="store_true",
        default=True,
        help="Telegraf reads each log file from the beginning (default).",
    )
    deployments_provision_telegraf_tail_group.add_argument(
        "--only-new-logs",
        dest="from_beginning",
        action="store_false",
        help="Telegraf tails from the end of each existing log file (no backfill).",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--skip-metrics",
        action="store_true",
        help="Provision only the Elasticsearch log forwarder, leaving the "
        "InfluxDB host-metrics forwarder untouched. Use when retrofitting a "
        "log-forwarding change: --name is required for the ES index name but "
        "otherwise also pulls monitoring state, which would regenerate and "
        "restart telegraf-ant-metrics on every targeted host.",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--drop-log-levels",
        type=str,
        default="DEBUG,TRACE",
        help="Comma-separated ant-node log levels Telegraf drops before shipping "
        'to Elasticsearch (default: DEBUG,TRACE). Use "" to keep every line.',
    )
    deployments_provision_telegraf_parser.add_argument(
        "--buffer-limit",
        type=int,
        default=None,
        help="Size of Telegraf's ES output buffer (metric_buffer_limit) in lines "
        "(default 2,000,000).",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--max-undelivered-lines",
        type=int,
        default=None,
        help="Size of the inputs.tail buffer in lines (defaults to 1/4 of --buffer-limit).",
    )
    # --- InfluxDB backend ---
    deployments_provision_telegraf_parser.add_argument(
        "--influxdb-url",
        type=str,
        default=None,
        help="InfluxDB URL (auto-loaded from monitoring state if --name).",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--influxdb-token",
        type=str,
        default=None,
        help="InfluxDB API token (auto-loaded from monitoring state if --name).",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--influxdb-org",
        type=str,
        default="ant",
        help="InfluxDB organization (default: ant).",
    )
    deployments_provision_telegraf_parser.add_argument(
        "--influxdb-bucket",
        type=str,
        default="testnet-metrics",
        help="InfluxDB bucket for node host metrics (default: testnet-metrics).",
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
    deployments_ls_parser.add_argument(
        "--has-results",
        action="store_true",
        help="Show only deployments for which results were recorded.",
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
        default="0",
        help=(
            "Flux range start. Default '0' records the entire run (from the "
            "Unix epoch, i.e. all data in the bucket). Pass a duration like "
            "-6h, -24h, or -7d to record only a narrower window."
        ),
    )
    deployments_record_analysis_parser.add_argument(
        "--bucket-secs",
        type=int,
        default=300,
        help=(
            "Width in seconds of the time buckets written to "
            "deployment_host_metric_buckets, which let an arbitrary sub-window "
            "of the run be re-derived later from Postgres alone. Default 300 "
            "(5 minutes). Re-aggregating a window is exact at any width, so "
            "this trades only how finely a window edge can be placed (and "
            "trend-plot resolution) against stored rows. Pass 0 to skip "
            "bucket capture and record only the whole-run rollup."
        ),
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
    deployments_record_analysis_parser.add_argument(
        "--no-derived",
        dest="derived",
        action="store_false",
        default=True,
        help=(
            "Skip computing the normalised per-GB metrics (outbound bytes per "
            "GB of user traffic, disk consumed per GB stored) into "
            "deployment_derived_metrics. They are computed by default because "
            "the raw data they need is destroyed with the monitoring stack."
        ),
    )

    # deployments derive-metrics
    deployments_derive_metrics_parser = deployments_subparsers.add_parser(
        "derive-metrics",
        help=(
            "Re-derive the normalised per-GB metrics for an arbitrary window "
            "from the stored host metric buckets and per-op records. Works "
            "after the monitoring stack is destroyed; use it to slice out the "
            "steady-state phase for a like-for-like comparison."
        ),
    )
    # Exactly one of --id / --name. A name resolves to the most recently created
    # row that bears it, which is fine for the run you just finished and useless
    # for any earlier one -- and staging names are reused every train. Without
    # --id a windowed comparison against a baseline is unreachable: only the
    # newest run of a given name could ever be given a window label, while
    # `deployments compare` requires the label on both sides.
    deployments_derive_metrics_target = (
        deployments_derive_metrics_parser.add_mutually_exclusive_group(required=True)
    )
    deployments_derive_metrics_target.add_argument(
        "--id",
        type=int,
        default=None,
        help=(
            "Deployment id. Prefer this for any run that is not the latest of "
            "its name -- a baseline, in particular."
        ),
    )
    deployments_derive_metrics_target.add_argument(
        "--name",
        type=str,
        default=None,
        help=(
            "Deployment name. Resolves to the most recent row with that name, so "
            "use --id to target an earlier run of a reused name."
        ),
    )
    deployments_derive_metrics_parser.add_argument(
        "--window-start",
        type=str,
        default=None,
        help=(
            "ISO-8601 UTC start of the window, e.g. 2026-07-25T10:00:00. "
            "Defaults to the first stored bucket."
        ),
    )
    deployments_derive_metrics_parser.add_argument(
        "--window-end",
        type=str,
        default=None,
        help=(
            "ISO-8601 UTC end of the window (exclusive). Defaults to the last "
            "stored bucket."
        ),
    )
    deployments_derive_metrics_parser.add_argument(
        "--label",
        type=str,
        default="steady-state",
        help=(
            "Name for this window, stored alongside the values so several "
            "windows can coexist for one run (default: steady-state). "
            "Re-running with the same label replaces that window's values. "
            "'full-run' is written by record-analysis from exact raw counters "
            "-- reusing it here would overwrite an exact value with an "
            "approximate one."
        ),
    )
    deployments_derive_metrics_parser.add_argument(
        "--bucket-secs",
        type=int,
        default=0,
        help=(
            "Override the bucket width used to integrate per-second averages "
            "into byte totals. Default 0 infers it from the stored buckets, "
            "which is almost always what you want -- the width is not recorded "
            "on the rows, and a wrong value scales every byte total by exactly "
            "that factor."
        ),
    )

    # deployments compare
    deployments_compare_parser = deployments_subparsers.add_parser(
        "compare",
        help=(
            "Deterministically compare a run against a baseline run on their "
            "recorded metrics, apply the versioned gate thresholds, and emit a "
            "per-gate PASS/FAIL/GRAY verdict as JSON plus a fixed-format "
            "markdown report. Evaluates only what the registry stores: the two "
            "success floors and the eight soft gates. Data integrity, crashes, "
            "mixed-version chunk storage and the production canaries are "
            "observational and are NOT covered -- the report names them so "
            "their absence never reads as a pass."
        ),
    )
    deployments_compare_parser.add_argument(
        "--id",
        type=int,
        required=True,
        help=(
            "Deployment id of the candidate run. An id, not a name: staging "
            "names are reused every train and a name lookup would silently "
            "resolve to whichever run was most recent."
        ),
    )
    deployments_compare_parser.add_argument(
        "--baseline-id",
        dest="baseline_id",
        type=int,
        required=True,
        help=(
            "Deployment id of the baseline run -- by the process, the staging "
            "run the last shipped release passed on."
        ),
    )
    deployments_compare_parser.add_argument(
        "--window-label",
        dest="window_label",
        type=str,
        default="full-run",
        help=(
            "Which stored window to compare on both sides (default: full-run). "
            "Use the label given to `deployments derive-metrics --label` to "
            "compare a steady-state slice. Both runs must carry the same label "
            "and the same derived-metric source."
        ),
    )
    deployments_compare_parser.add_argument(
        "--thresholds",
        type=str,
        default=None,
        help=(
            "Path to a threshold config YAML. Defaults to the versioned one "
            "shipped with the package (resources/gates/staging-thresholds.yaml)."
        ),
    )
    deployments_compare_parser.add_argument(
        "--history",
        type=str,
        default=None,
        help=(
            "Comma-separated deployment ids of prior ACCEPTED runs, used for "
            "the 2-sigma noise criterion. Which runs count as accepted is a "
            "judgment call and is yours to make; only the arithmetic lives in "
            "this command. Below the configured minimum the percentage "
            "threshold stands alone and the report says so."
        ),
    )
    deployments_compare_parser.add_argument(
        "--json-out",
        dest="json_out",
        type=str,
        default=None,
        help="Write the machine-readable verdict to this path.",
    )
    deployments_compare_parser.add_argument(
        "--markdown-out",
        dest="markdown_out",
        type=str,
        default=None,
        help="Write the fixed-format markdown report to this path.",
    )
    deployments_compare_parser.add_argument(
        "--print-markdown",
        dest="print_markdown",
        action="store_true",
        help="Print the markdown report to stdout as well as the summary table.",
    )
    deployments_compare_parser.add_argument(
        "--no-persist",
        dest="no_persist",
        action="store_true",
        help=(
            "Do not store the verdict in the registry. By default it is upserted "
            "against (candidate, baseline, window) so a re-run replaces its "
            "previous answer rather than leaving two."
        ),
    )

    # deployments prune-host-buckets
    deployments_prune_buckets_parser = deployments_subparsers.add_parser(
        "prune-host-buckets",
        help=(
            "Delete old rows from deployment_host_metric_buckets. Never "
            "touches deployment_host_results, so a pruned deployment degrades "
            "to the whole-run rollup view rather than losing its analysis."
        ),
    )
    deployments_prune_buckets_parser.add_argument(
        "--older-than",
        type=int,
        required=True,
        help=(
            "Delete buckets whose bucket_start is more than this many days "
            "old. Use 0 to delete every bucket row."
        ),
    )
    deployments_prune_buckets_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help=(
            "Restrict the prune to one deployment (default: every deployment)."
        ),
    )
    deployments_prune_buckets_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be deleted without deleting anything.",
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

    # deployments archive
    deployments_archive_parser = deployments_subparsers.add_parser(
        "archive",
        help=(
            "Archive a deployment's InfluxDB results into Postgres for long-term "
            "history. Thin wrapper over `record-results` intended to be run "
            "from the machine that owns the registry DB (DATABASE_URL set). "
            "Influx retention is bounded (~7 days), so run this at least that "
            "often. One-shot by default (cron / scheduled-agent friendly); pass "
            "--loop to keep archiving on an interval."
        ),
    )
    deployments_archive_parser.add_argument(
        "--name", type=str, required=True,
        help="Deployment name (must match a row in the registry).",
    )
    deployments_archive_parser.add_argument(
        "--monitoring-name", type=str, default=None,
        help="Monitoring stack name to load Influx creds from (defaults to --name).",
    )
    deployments_archive_parser.add_argument(
        "--window", type=str, default="-7d",
        help=(
            "Flux range start for the aggregate rollup (default: -7d, matching "
            "the default Influx retention). Per-op records are always pulled "
            "full-range and idempotently upserted regardless of this value."
        ),
    )
    deployments_archive_parser.add_argument(
        "--loop", action="store_true",
        help=(
            "Keep archiving forever, sleeping --interval-secs between runs, "
            "instead of doing a single pass and exiting. A transient failure in "
            "one iteration is logged and the loop continues."
        ),
    )
    deployments_archive_parser.add_argument(
        "--interval-secs", type=int, default=21600,
        help="With --loop: seconds to sleep between archive runs (default: 21600 = 6h).",
    )
    deployments_archive_parser.add_argument(
        "--influx-url", type=str, default=None,
        help="InfluxDB URL (overrides monitoring state lookup).",
    )
    deployments_archive_parser.add_argument(
        "--influx-token", type=str, default=None,
        help="InfluxDB API token (overrides monitoring state lookup).",
    )
    deployments_archive_parser.add_argument(
        "--influx-org", type=str, default=None,
        help="InfluxDB organization (default: from monitoring state, else 'ant').",
    )
    deployments_archive_parser.add_argument(
        "--influx-bucket", type=str, default=None,
        help="InfluxDB bucket (default: from monitoring state, else 'testnet-metrics').",
    )
    deployments_archive_parser.add_argument(
        "--update-slack", action="store_true",
        help="Forwarded to record-results: thread a results summary onto the "
        "deployment's Slack message after each archive run (best-effort).",
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

    # deployments print
    deployments_print_parser = deployments_subparsers.add_parser(
        "print",
        help=(
            "Print a deployment's summary (the same information as "
            "`deployments slack-post`) to the terminal instead of posting it "
            "to Slack. Identified by ID rather than name."
        ),
    )
    deployments_print_parser.add_argument(
        "--id",
        type=int,
        required=True,
        help="Deployment ID (must exist in the registry).",
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

    # deployments upgrade-ant-binary
    deployments_upgrade_ant_binary_parser = deployments_subparsers.add_parser(
        "upgrade-ant-binary",
        help=(
            "Force-install the ant binary in place on a running prod "
            "downloader deployment's VM(s) and restart the service. "
            "Supports prod-downloaders and prod-continuous-downloader only; "
            "no infrastructure is touched."
        ),
    )
    deployments_upgrade_ant_binary_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (must have client state in S3).",
    )
    deployments_upgrade_ant_binary_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built ant binary from this branch (requires --repo-owner).",
    )
    deployments_upgrade_ant_binary_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for custom-built ant binary (requires --branch-name).",
    )
    deployments_upgrade_ant_binary_parser.add_argument(
        "--ant-version",
        type=str,
        help="Specific release version of ant to install (e.g., 0.3.0). "
        "Default: latest release.",
    )
    deployments_upgrade_ant_binary_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa).",
    )
    deployments_upgrade_ant_binary_parser.add_argument(
        "--restart",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Restart the ant-prod-downloader service after swapping the "
            "binary (default: on). --no-restart leaves the new binary on "
            "disk without restarting — useful for a completed one-shot "
            "'prod-downloaders' run, since restarting it re-walks the "
            "entire address list from scratch (no checkpointing)."
        ),
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
    # === elastic group (clean, destroy, info, infra, provision) ===
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
        choices=["digitalocean", "ovh", "contabo"],
        help="Cloud provider for the Elasticsearch VM (default: digitalocean). "
        "'ovh' requires OVH_CLOUD_PROJECT_SERVICE + OVH API credentials; "
        "'contabo' requires the CNTB_OAUTH2_* credentials and --contabo-image-id "
        "(root-disk only, no volume).",
    )
    elastic_infra_parser.add_argument(
        "--contabo-image-id",
        type=str,
        default=None,
        help="Ubuntu image UUID for a Contabo Elasticsearch VM (required with "
        "--provider contabo; resolve via `cntb get images`).",
    )
    elastic_infra_parser.add_argument(
        "--contabo-ssh-public-key-path",
        type=str,
        default="~/.ssh/id_rsa.pub",
        help="Operator SSH PUBLIC key path registered as a Contabo secret "
        "(default: ~/.ssh/id_rsa.pub).",
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
    elastic_infra_parser.add_argument(
        "--keep-on-destroy",
        action="store_true",
        help="Record in this stack's state that `deployments down` must leave it "
        "standing, so the testnet's logs stay queryable for a post-mortem after "
        "the nodes are gone. Honoured by both `down --config` and `down --name`. "
        "Clean the stack up afterwards with `elastic destroy --name <name> "
        "--force`, which is never gated by this flag.",
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

    # =========================================================================
    # === monitoring group (clean, destroy, info, infra, provision) ===
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
        choices=["digitalocean", "ovh", "contabo"],
        help="Cloud provider for the monitoring VMs (default: digitalocean). "
        "'ovh' requires OVH_CLOUD_PROJECT_SERVICE + OVH API credentials; "
        "'contabo' requires the CNTB_OAUTH2_* credentials and --contabo-image-id "
        "(root-disk only, no volume).",
    )
    monitoring_infra_parser.add_argument(
        "--contabo-image-id",
        type=str,
        default=None,
        help="Ubuntu image UUID for Contabo monitoring VM(s) (required with "
        "--provider contabo; resolve via `cntb get images`).",
    )
    monitoring_infra_parser.add_argument(
        "--contabo-ssh-public-key-path",
        type=str,
        default="~/.ssh/id_rsa.pub",
        help="Operator SSH PUBLIC key path registered as a Contabo secret "
        "(default: ~/.ssh/id_rsa.pub).",
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
        "for digitalocean, d2-4 for ovh). Only used with --grafana.",
    )
    monitoring_infra_parser.add_argument(
        "--grafana",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Create the separate Grafana VM (default: off — InfluxDB only). "
        "Pass --grafana to opt in; --no-grafana is the explicit default.",
    )
    monitoring_infra_parser.add_argument(
        "--keep-on-destroy",
        action="store_true",
        help="Record in this stack's state that `deployments down` must leave it "
        "standing, so the testnet's metrics stay queryable for a post-mortem after "
        "the nodes are gone. Honoured by both `down --config` and `down --name`. "
        "Clean the stack up afterwards with `monitoring destroy --name <name> "
        "--force`, which is never gated by this flag.",
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
        choices=["hetzner", "ovh", "contabo"],
        help="Destination provider.",
    )
    migration_plan_parser.add_argument(
        "--target-server-type",
        type=str,
        required=True,
        help=(
            "Destination VM type/flavor (e.g. cpx42 for hetzner, b3-16 for ovh, "
            "or a Contabo product id like V99 for contabo)."
        ),
    )
    migration_plan_parser.add_argument(
        "--target-volume-size",
        type=int,
        default=0,
        help=(
            "Attached data volume size in GB (e.g. 50). Required for "
            "hetzner/ovh; ignored for contabo, which is root-disk only."
        ),
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
            "Provision destination VMs for a migration (Terraform for "
            "hetzner/ovh; the Contabo REST API for contabo)."
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
            "fsn1=3,hel1=3,nbg1=3; for contabo use region codes like "
            "US-central=3). Sum must equal the number of VMs still in "
            "'planned' status."
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
    migration_infra_parser.add_argument(
        "--contabo-image-id",
        type=str,
        default="d64d5c6c-9dda-4e38-8174-0ee282474d8a",
        help=(
            "Contabo image UUID (contabo targets only). Defaults to the "
            "standard ubuntu-24.04 image; re-verify with `cntb get images "
            "--imageType standard` as UUIDs can change."
        ),
    )
    migration_infra_parser.add_argument(
        "--contabo-ssh-public-key-path",
        type=str,
        default="~/.ssh/id_rsa.pub",
        help=(
            "Path to the operator SSH public key (contabo targets only); "
            "registered as a Contabo secret so the new VMs allow root login."
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
        help="Default attached-volume size in GB for volume-backed providers "
        "(default: 20). Per-provider --<provider>-attached-volume-size overrides "
        "it. Not applicable to ovh_3az (3-AZ regions have no block storage).",
    )
    # Per-provider attached-volume overrides. Default None => fall back to the
    # global --attached-volume-size. No ovh_3az flag: 3-AZ has no block storage,
    # so ovh_3az nodes use the root disk.
    for _p in ("do", "hetzner", "vultr", "ovh"):
        nodes_infra_parser.add_argument(
            f"--{_p}-attached-volume-size",
            type=int,
            default=None,
            help=f"Attached-volume size in GB for {_p} nodes "
            "(overrides --attached-volume-size).",
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
    # OVH 3-AZ regions (EU-WEST-PAR, EU-SOUTH-MIL) use a separate provider that
    # creates instances via OpenStack/Nova (resources/ovh-3az) to sidestep the
    # OVH-native API's 500 on user_data/availability_zone in 3-AZ regions. Same
    # OVH credentials as --ovh-*.
    nodes_infra_parser.add_argument(
        "--ovh-3az-vm-count",
        type=int,
        default=0,
        help="Number of VMs per region on OVH 3-AZ regions "
        "(default: 0, provider not used)",
    )
    nodes_infra_parser.add_argument(
        "--ovh-3az-flavor",
        type=str,
        default="b3-16",
        help="OVH 3-AZ instance flavor (default: b3-16)",
    )
    nodes_infra_parser.add_argument(
        "--ovh-3az-regions",
        type=int,
        default=1,
        help="Number of OVH 3-AZ regions to use (default: 1). "
        "Ignored if --ovh-3az-regions-list is set.",
    )
    nodes_infra_parser.add_argument(
        "--ovh-3az-regions-list",
        type=str,
        default=None,
        help="Comma-separated list of specific OVH 3-AZ regions to use "
        "(EU-WEST-PAR, EU-SOUTH-MIL). Overrides --ovh-3az-regions.",
    )
    # Contabo: root-disk only (no attachable block storage), so no
    # --contabo-attached-volume-size flag. Images are referenced by UUID and the
    # operator public key is registered as a contabo_secret.
    nodes_infra_parser.add_argument(
        "--contabo-vm-count",
        type=int,
        default=0,
        help="Number of VMs per region on Contabo (default: 0, provider not used)",
    )
    nodes_infra_parser.add_argument(
        "--contabo-product-id",
        type=str,
        default="V92",
        help="Contabo product id / plan (default: V92). Verify current ids via "
        "`cntb get products`.",
    )
    nodes_infra_parser.add_argument(
        "--contabo-regions",
        type=int,
        default=1,
        help="Number of Contabo regions to use (default: 1). "
        "Ignored if --contabo-regions-list is set.",
    )
    nodes_infra_parser.add_argument(
        "--contabo-regions-list",
        type=str,
        default=None,
        help="Comma-separated list of specific Contabo regions to use "
        "(EU, US-central, US-east, US-west, UK, SIN, AUS, JPN, IND). "
        "Overrides --contabo-regions.",
    )
    nodes_infra_parser.add_argument(
        "--contabo-image-id",
        type=str,
        default=None,
        help="Ubuntu image UUID for Contabo VMs (required with --contabo-vm-count; "
        "resolve via `cntb get images`).",
    )
    nodes_infra_parser.add_argument(
        "--contabo-ssh-public-key-path",
        type=str,
        default="~/.ssh/id_rsa.pub",
        help="Operator SSH PUBLIC key path registered as a Contabo secret "
        "(default: ~/.ssh/id_rsa.pub).",
    )

    # nodes provision
    nodes_provision_parser = nodes_subparsers.add_parser(
        "provision", help="Provision ant-node services on node VMs"
    )
    _add_telegraf_fold_args(nodes_provision_parser)
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
    _add_telegraf_fold_args(nodes_provision_genesis_parser)
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
        "--network",
        type=str,
        default=None,
        metavar="PROFILE",
        help="Simulate a residential connection on the client VM: a built-in "
        "profile name (home-adsl, home-calibrated, home-fttc, home-fttp, "
        "mobile-4g) or a comma-separated set such as "
        "'rate_down=30mbit,rate_up=5mbit,delay=25ms,loss=0.2'. Traffic to the "
        "monitoring/Elasticsearch endpoints and SSH is exempt so instrumentation "
        "never competes with the transfer. Omit for an unshaped cloud link.",
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
        "--grafana",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Also create a Grafana VM for dashboards (default: off — "
        "InfluxDB only).",
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
    prod_cd_deploy_parser.add_argument(
        "--daily",
        action="store_true",
        help=(
            "Convenience for a permanent once-a-day cadence: sets "
            "--sleep-interval-secs to 86400 (24h between full passes). "
            "Overrides --sleep-interval-secs when both are given."
        ),
    )
    prod_cd_deploy_parser.add_argument(
        "--provider",
        type=str,
        default="digitalocean",
        choices=["digitalocean", "hetzner", "vultr", "ovh"],
        help=(
            "Cloud provider for the downloader CLIENT VM (default: "
            "digitalocean). Non-DO providers are cheaper for a permanent run "
            "and cut DigitalOcean egress. Requires the provider's credentials "
            "in the environment (HCLOUD_TOKEN / VULTR_API_KEY / the OVH_* vars)."
        ),
    )
    prod_cd_deploy_parser.add_argument(
        "--monitoring-provider",
        type=str,
        default="digitalocean",
        choices=["digitalocean", "ovh"],
        help=(
            "Cloud provider for the paired MONITORING VM (InfluxDB/Grafana). "
            "Only digitalocean and ovh have monitoring Terraform modules "
            "(default: digitalocean)."
        ),
    )
    prod_cd_deploy_parser.add_argument(
        "--monitoring-region",
        type=str,
        default=None,
        help=(
            "Region for the monitoring VM. Default: lon1 on digitalocean, the "
            "provider default (e.g. GRA9) on ovh."
        ),
    )
    prod_cd_deploy_parser.add_argument(
        "--monitoring-droplet-size",
        type=str,
        default=None,
        help=(
            "InfluxDB VM size/flavor. Default: s-2vcpu-4gb on digitalocean, the "
            "provider default on ovh."
        ),
    )
    prod_cd_deploy_parser.add_argument(
        "--monitoring-volume-type",
        type=str,
        default=None,
        help="OVH only: monitoring data volume type (default: high-speed).",
    )
    prod_cd_deploy_parser.add_argument(
        "--influx-retention-days",
        type=int,
        default=7,
        help=(
            "InfluxDB bucket retention in days for the monitoring stack "
            "(default: 7). Pass 0 for infinite retention. Applied at first "
            "monitoring provision."
        ),
    )
    prod_cd_deploy_parser.add_argument(
        "--log-retention-days",
        type=int,
        default=7,
        help=(
            "Days to keep per-op download logs under /var/log/ant-prod-downloader "
            "on the client VM before a daily systemd timer prunes them "
            "(default: 7). Pass 0 to disable pruning (keep logs forever)."
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
        "--network",
        type=str,
        default=None,
        metavar="PROFILE",
        help="Simulate a residential connection on the client VM: a built-in "
        "profile name (home-adsl, home-calibrated, home-fttc, home-fttp, "
        "mobile-4g) or a comma-separated set such as "
        "'rate_down=30mbit,rate_up=5mbit,delay=25ms,loss=0.2'. Traffic to the "
        "monitoring/Elasticsearch endpoints and SSH is exempt so instrumentation "
        "never competes with the transfer. Omit for an unshaped cloud link.",
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
        "--grafana",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Also create a Grafana VM for dashboards (default: off — "
        "InfluxDB only).",
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

    # --- prod nodes (heaptrack profiling nodes against production) ---
    prod_nodes_parser = prod_subparsers.add_parser(
        "nodes",
        help="Profiling ant-node VMs that join the PRODUCTION network, running "
        "a debug build wrapped in heaptrack (for chasing the node memory leak).",
    )
    prod_nodes_subparsers = prod_nodes_parser.add_subparsers(
        dest="prod_nodes_action", required=True
    )

    prod_nodes_deploy_parser = prod_nodes_subparsers.add_parser(
        "deploy",
        help="Create node VM(s) joining production, provision ant-node services "
        "wrapped in heaptrack, and start them.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name (also used for the paired monitoring stack).",
    )
    prod_nodes_deploy_parser.add_argument(
        "--provider",
        type=str,
        default="digitalocean",
        choices=["digitalocean", "hetzner", "vultr", "ovh"],
        help="Cloud provider for the node VM(s) (default: digitalocean).",
    )
    prod_nodes_deploy_parser.add_argument(
        "--region",
        type=str,
        required=True,
        help="Provider region for the node VM(s) (e.g. lon1, fsn1, lhr, GRA9).",
    )
    prod_nodes_deploy_parser.add_argument(
        "--vm-size",
        type=str,
        default=None,
        help="Provider VM size/plan/flavor. Defaults to a prod-appropriate size "
        "per provider (DO s-4vcpu-8gb, Hetzner cpx42, Vultr vhf-4c-16gb, OVH b3-16).",
    )
    prod_nodes_deploy_parser.add_argument(
        "--vm-count",
        type=int,
        default=1,
        help="Number of node VMs to create in the region (default: 1).",
    )
    prod_nodes_deploy_parser.add_argument(
        "--node-count",
        type=int,
        default=15,
        help="ant-node services per VM (default: 15, matching the prod fleet).",
    )
    prod_nodes_deploy_parser.add_argument(
        "--volume-size",
        type=int,
        default=100,
        help="Attached data-volume size in GB per node VM (default: 100). Node "
        "data plus growing heaptrack traces live here, not the root disk.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--heaptrack",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Wrap every ant-node service with heaptrack memory profiling "
        "(default: on — the whole point of this command). --no-heaptrack runs "
        "plain prod nodes.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--monitoring",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Also stand up a paired InfluxDB VM and forward host/per-service "
        "RSS metrics (default: off). Pair with --grafana for dashboards.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--grafana",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="With --monitoring, also create a Grafana VM (default: off — "
        "InfluxDB only).",
    )
    prod_nodes_deploy_parser.add_argument(
        "--monitoring-volume-size",
        type=int,
        default=50,
        help="Monitoring VM attached volume size in GB (default: 50).",
    )
    prod_nodes_deploy_parser.add_argument(
        "--forward-logs",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Stand up a dedicated Elasticsearch VM (named after this deployment) "
        "and forward the node JSON logs to it (default: off). Keeps verbose "
        "debug-build logs off the always-on ant-prod-01 ES.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--elastic-volume-size",
        type=int,
        default=100,
        help="Data-volume size in GB for the dedicated Elasticsearch VM "
        "(default: 100). Only used with --forward-logs.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--elastic-retention-days",
        type=int,
        default=7,
        help="ILM retention for the dedicated Elasticsearch index (default: 7). "
        "Only used with --forward-logs.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--elastic-droplet-size",
        type=str,
        default=None,
        help="VM size for the dedicated Elasticsearch box (default: per-provider "
        "elastic default, e.g. DO s-4vcpu-8gb). Only used with --forward-logs.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--elastic-shards",
        type=int,
        default=2,
        help="Primary shard count for the dedicated Elasticsearch index "
        "(default: 2). Only used with --forward-logs.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--elastic-drop-log-levels",
        type=str,
        default=None,
        help="Override the node→ES level drop filter (comma-separated, e.g. "
        "'DEBUG,TRACE'). Default: derived from --log-level (trace keeps all, "
        "debug drops TRACE, info/unset uses the DEBUG,TRACE default).",
    )
    prod_nodes_deploy_parser.add_argument(
        "--log-retention-days",
        type=int,
        default=7,
        help="Install a daily timer that deletes heaptrack traces older than N "
        "days (default: 7). 0 keeps traces forever.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--rewards-address",
        type=str,
        default="0x03B770D9cD32077cC0bF330c13C114a87643B124",
        help="EVM rewards address for the prod node services "
        "(default: 0x03B770D9cD32077cC0bF330c13C114a87643B124).",
    )
    prod_nodes_deploy_parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        help="ant-node --log-level (single level: info/debug/trace). ant-node has "
        "no per-module env filter, so one global level applies.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--port",
        type=int,
        default=10000,
        help="Initial ant-node listen port; services use port..port+node_count-1 "
        "(default: 10000).",
    )
    prod_nodes_deploy_parser.add_argument(
        "--branch-name",
        type=str,
        help="Use custom-built ant-node binary from this branch (requires "
        "--repo-owner). Build it first with `build ant-node --debug` for a "
        "profiling-friendly debug build.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repo owner for the custom-built ant-node binary "
        "(requires --branch-name).",
    )
    prod_nodes_deploy_parser.add_argument(
        "--node-version",
        type=str,
        help="Specific release version of ant-node to install (e.g. 0.11.6). "
        "Mutually exclusive with --branch-name/--repo-owner.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--debug",
        action="store_true",
        help="Informational: record that the resolved branch binary is a debug "
        "build so `deployments slack-post` surfaces it. The actual debug build is "
        "produced by `build ant-node --debug`; this flag does not change the fetch.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--purpose",
        type=str,
        required=True,
        help="Free-text description of what this profiling run is for. Persisted "
        "into deploy_args and surfaced by `deployments slack-post`.",
    )
    prod_nodes_deploy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key for provisioning (default: ~/.ssh/id_rsa).",
    )

    prod_nodes_destroy_parser = prod_nodes_subparsers.add_parser(
        "destroy",
        help="Destroy a prod profiling-nodes deployment (node VMs and, by "
        "default, the paired monitoring stack).",
    )
    prod_nodes_destroy_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Deployment name to tear down.",
    )
    prod_nodes_destroy_parser.add_argument(
        "--keep-monitoring",
        action="store_true",
        help="Leave the paired monitoring VM running so recorded metrics remain "
        "queryable after the node VMs are gone. Default: monitoring is destroyed "
        "alongside the nodes.",
    )
    prod_nodes_destroy_parser.add_argument(
        "--keep-elastic",
        action="store_true",
        help="Leave the dedicated Elasticsearch VM running (only created when the "
        "deploy used --forward-logs) so forwarded node logs remain queryable. "
        "Default: it is destroyed alongside the nodes.",
    )
    prod_nodes_destroy_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa).",
    )

    # --- prod debug-nodes (crash-debug nodes against production) ---
    prod_dbg_parser = prod_subparsers.add_parser(
        "debug-nodes",
        help="Permanent crash-debug ant-node VMs joining PRODUCTION: "
        "RUST_BACKTRACE, core dumps, DEBUG logs, and a dedicated small "
        "InfluxDB + short-retention Elasticsearch. Node and monitoring/logging "
        "VMs each choose their own provider (incl. Contabo).",
    )
    prod_dbg_subparsers = prod_dbg_parser.add_subparsers(
        dest="prod_debug_nodes_action", required=True
    )

    prod_dbg_deploy = prod_dbg_subparsers.add_parser(
        "deploy",
        help="Create node VM(s) joining production configured for crash "
        "debugging, provision the ant-node services, and start them.",
    )
    prod_dbg_deploy.add_argument("--name", type=str, required=True,
        help="Deployment name (also used for the paired monitoring/ES stacks).")
    prod_dbg_deploy.add_argument(
        "--provider", type=str, default="digitalocean",
        choices=["digitalocean", "hetzner", "vultr", "ovh", "contabo"],
        help="Cloud provider for the node VM(s) (default: digitalocean).")
    prod_dbg_deploy.add_argument("--region", type=str, required=True,
        help="Provider region for the node VM(s) (e.g. lon1, fsn1, GRA9, EU).")
    prod_dbg_deploy.add_argument("--vm-size", type=str, default=None,
        help="Provider VM size/plan/flavor/product-id. Defaults per provider "
        "(DO s-4vcpu-8gb, Hetzner cpx42, Vultr vhf-4c-16gb, OVH b3-16, "
        "Contabo V10 VDS).")
    prod_dbg_deploy.add_argument("--vm-count", type=int, default=1,
        help="Number of node VMs to create in the region (default: 1).")
    prod_dbg_deploy.add_argument("--node-count", type=int, default=4,
        help="ant-node services per VM (default: 4). Deliberately NOT the prod "
        "fleet's 15: at --log-level debug each service emits ~758 log lines/s, "
        "and one telegraf forwarder tops out around 2700 lines/s, so 15 (or "
        "even 10) services produce far more than can reach Elasticsearch. "
        "Measured on ant-prod-debug-01 2026-08-02: 10 services produced 7577 "
        "lines/s of which only ~36% was forwarded. Raise it only if you accept "
        "that ES holds a partial sample and the on-disk logs are the record.")
    prod_dbg_deploy.add_argument("--volume-size", type=int, default=100,
        help="Attached data-volume size in GB per node VM (default: 100). "
        "Ignored on providers with no attachable volume (Contabo: root disk).")
    prod_dbg_deploy.add_argument("--log-level", type=str, default="debug",
        help="ant-node --log-level (single level: info/debug/trace; default "
        "debug). ant-node has no per-module env filter, so one global level "
        "applies — the requested per-module targeting is not achievable.")
    prod_dbg_deploy.add_argument("--rust-backtrace", type=str, default="full",
        help="RUST_BACKTRACE value set on every service (default: full; use '1' "
        "for a terser trace, or '' to disable). Services also always get "
        "COLORBT_SHOW_HIDDEN=1 so ant-node's color-backtrace prints real frames "
        "instead of <empty backtrace>.")
    prod_dbg_deploy.add_argument("--coredumps", action=argparse.BooleanOptionalAction,
        default=True, help="Enable core dumps (LimitCORE=infinity + a global "
        "kernel.core_pattern under the data path) so a panic leaves an "
        "inspectable core (default: on).")
    prod_dbg_deploy.add_argument("--coredump-retention-days", type=int, default=7,
        help="Install a daily timer deleting core dumps older than N days "
        "(default: 7). 0 keeps them forever.")
    prod_dbg_deploy.add_argument("--log-retention-days", type=int, default=2,
        help="Install a daily timer deleting rotated ant-node debug logs "
        "under /mnt/ant-node-storage/log older than N days (default: 2). "
        "Do NOT assume Elasticsearch is a complete copy: forwarding is "
        "lossy whenever the services out-produce the single telegraf "
        "forwarder, so the on-disk logs are the authoritative record and "
        "this timer is what discards them. 2 days is what the disk affords "
        "at the default --node-count alongside the chunk store. 0 keeps "
        "them forever.")
    prod_dbg_deploy.add_argument("--heaptrack", action=argparse.BooleanOptionalAction,
        default=False, help="Also wrap services in heaptrack memory profiling "
        "(default: off — use `prod nodes` for the heaptrack-focused kind).")
    prod_dbg_deploy.add_argument("--monitoring", action=argparse.BooleanOptionalAction,
        default=True, help="Stand up a dedicated small InfluxDB VM and forward "
        "host/per-service metrics (default: on).")
    prod_dbg_deploy.add_argument("--monitoring-provider", type=str,
        default="digitalocean", choices=["digitalocean", "ovh", "contabo"],
        help="Provider for the monitoring VM (default: digitalocean).")
    prod_dbg_deploy.add_argument("--monitoring-region", type=str, default=None,
        help="Region for the monitoring VM (default: per-provider default).")
    prod_dbg_deploy.add_argument("--monitoring-droplet-size", type=str, default=None,
        help="VM size for the InfluxDB box (default: per-provider default).")
    prod_dbg_deploy.add_argument("--monitoring-volume-size", type=int, default=50,
        help="Monitoring VM attached volume size in GB (default: 50; ignored on "
        "Contabo).")
    prod_dbg_deploy.add_argument("--grafana", action=argparse.BooleanOptionalAction,
        default=False, help="With --monitoring, also create a Grafana VM "
        "(default: off — InfluxDB only).")
    prod_dbg_deploy.add_argument("--influx-retention-days", type=int, default=7,
        help="InfluxDB bucket retention in days (default: 7; 0 = infinite).")
    prod_dbg_deploy.add_argument("--forward-logs", action=argparse.BooleanOptionalAction,
        default=True, help="Stand up a dedicated Elasticsearch VM and forward the "
        "node JSON logs to it (default: on). Keeps verbose debug logs off the "
        "always-on prod ES.")
    prod_dbg_deploy.add_argument("--elastic-provider", type=str,
        default="digitalocean", choices=["digitalocean", "ovh", "contabo"],
        help="Provider for the Elasticsearch VM (default: digitalocean).")
    prod_dbg_deploy.add_argument("--elastic-region", type=str, default=None,
        help="Region for the Elasticsearch VM (default: per-provider default).")
    prod_dbg_deploy.add_argument("--elastic-droplet-size", type=str, default=None,
        help="VM size for the Elasticsearch box (default: per-provider default).")
    prod_dbg_deploy.add_argument("--elastic-volume-size", type=int, default=100,
        help="Data-volume size in GB for the Elasticsearch VM (default: 100; "
        "ignored on Contabo).")
    prod_dbg_deploy.add_argument("--elastic-retention-days", type=int, default=3,
        help="ILM retention for the dedicated Elasticsearch index (default: 3).")
    prod_dbg_deploy.add_argument("--elastic-shards", type=int, default=1,
        help="Primary shard count for the Elasticsearch index (default: 1).")
    prod_dbg_deploy.add_argument("--elastic-drop-log-levels", type=str, default=None,
        help="Override the node→ES level drop filter. Default: derived from "
        "--log-level (debug drops only TRACE so DEBUG reaches ES).")
    prod_dbg_deploy.add_argument("--rewards-address", type=str,
        default="0x03B770D9cD32077cC0bF330c13C114a87643B124",
        help="EVM rewards address for the prod node services.")
    prod_dbg_deploy.add_argument("--port", type=int, default=10000,
        help="Initial ant-node listen port (default: 10000).")
    prod_dbg_deploy.add_argument("--branch-name", type=str,
        help="Use a custom-built ant-node binary from this branch (requires "
        "--repo-owner). Build it first with `build ant-node --debug`.")
    prod_dbg_deploy.add_argument("--repo-owner", type=str,
        help="GitHub repo owner for the custom-built ant-node binary.")
    prod_dbg_deploy.add_argument("--node-version", type=str,
        help="Specific release version of ant-node to install (e.g. 0.11.6).")
    prod_dbg_deploy.add_argument("--debug", action="store_true",
        help="Informational: record that the resolved branch binary is a debug "
        "build. The actual debug build comes from `build ant-node --debug`.")
    prod_dbg_deploy.add_argument("--purpose", type=str, required=True,
        help="Free-text description of what this debug run is for.")
    prod_dbg_deploy.add_argument("--ssh-key-path", type=str, default="~/.ssh/id_rsa",
        help="Path to SSH private key for provisioning (default: ~/.ssh/id_rsa).")
    prod_dbg_deploy.add_argument("--contabo-image-id", type=str, default=None,
        help="Ubuntu image UUID for Contabo VMs (required when any Contabo "
        "provider is used; resolve via `cntb get images`).")
    prod_dbg_deploy.add_argument("--contabo-ssh-public-key-path", type=str,
        default="~/.ssh/id_rsa.pub",
        help="Operator SSH PUBLIC key path registered as a Contabo secret "
        "(default: ~/.ssh/id_rsa.pub; the .pub of --ssh-key-path).")

    prod_dbg_destroy = prod_dbg_subparsers.add_parser(
        "destroy",
        help="Destroy a prod debug-nodes deployment (node VMs and, by default, "
        "the paired monitoring + Elasticsearch stacks).",
    )
    prod_dbg_destroy.add_argument("--name", type=str, required=True,
        help="Deployment name to tear down.")
    prod_dbg_destroy.add_argument("--keep-monitoring", action="store_true",
        help="Leave the monitoring VM running (default: destroyed with nodes).")
    prod_dbg_destroy.add_argument("--keep-elastic", action="store_true",
        help="Leave the Elasticsearch VM running (default: destroyed with nodes).")
    prod_dbg_destroy.add_argument("--ssh-key-path", type=str, default="~/.ssh/id_rsa",
        help="Path to SSH key (default: ~/.ssh/id_rsa).")

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
            "REGION:VM_SIZE:urls=PATH[:payment=MODE][:clock_skew=DURATION][:network=PROFILE]"
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
        "--grafana",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Also create a Grafana VM for dashboards (default: off — "
        "InfluxDB only).",
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
    prod_up_destroy_parser.add_argument(
        "--skip-results-check",
        action="store_true",
        help="Skip the warning about results that were never recorded to "
        "Postgres. Destroying the monitoring VM destroys its InfluxDB, and "
        "anything unrecorded goes with it permanently.",
    )

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
        help="Local path to a newline-separated list of public source URLs. "
        "Exactly one of --urls-file or --pool is required; the command checks "
        "that itself, so this is not argparse-required.",
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
        "--grafana",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Also create a Grafana VM for dashboards (default: off — "
        "InfluxDB only). Canary datasets are usually inspected via Grafana, "
        "so consider --grafana for long-lived canary runs.",
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
    prod_canary_destroy_parser.add_argument(
        "--skip-results-check",
        action="store_true",
        help="Skip the warning about results that were never recorded to "
        "Postgres. Destroying the monitoring VM destroys its InfluxDB, and "
        "anything unrecorded goes with it permanently.",
    )

    # --- prod canary report ---
    prod_canary_report_parser = prod_canary_subparsers.add_parser(
        "report",
        help=(
            "Render the canary digest locally, to the terminal. Runs the same "
            "analysis as the on-VM reporter but posts nothing to Slack, and "
            "keeps its own state file so the daily message's deltas are "
            "unaffected."
        ),
    )
    prod_canary_report_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Canary deployment name (also the monitoring stack name).",
    )
    prod_canary_report_parser.add_argument(
        "--no-save-state",
        action="store_true",
        help=(
            "Don't update the local snapshot, so the next run still compares "
            "against the same baseline."
        ),
    )

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

    # These belong to `canary deploy`, not to the `canary` group: an option on
    # the group parser has to be written BEFORE the subcommand, so `prod canary
    # deploy --pool …` — exactly what the orchestrator emits — was rejected.
    prod_canary_deploy_parser.add_argument(
        "--pool",
        type=str,
        metavar="CLASS:COUNT",
        help="Draw the canary's files from the managed pool instead of "
        "--urls-file, e.g. tiny:40,small:230,medium:110. Selected files have "
        "never been uploaded to production — which is what makes a loss "
        "measurement meaningful.",
    )
    prod_canary_deploy_parser.add_argument(
        "--pool-min-size",
        type=str,
        metavar="SIZE",
        help="With --pool, take only files of at least this size, e.g. 5MB. "
        "Narrows every requested class without changing the counts. The chunk "
        "floor is 4, so a 14KB file buys the same minimum loss observation as a "
        "5MB one — this is how a canary avoids spending pool entries on it. "
        "Units are binary and a bare number is bytes.",
    )
    prod_canary_deploy_parser.add_argument(
        "--pool-max-size",
        type=str,
        metavar="SIZE",
        help="With --pool, take only files below this size, e.g. 500MB. "
        "Exclusive, so --pool-min-size 10MB --pool-max-size 100MB is exactly "
        "the `small` class.",
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
        "--from-pool",
        type=str,
        metavar="CLASS:COUNT",
        help="Draw LOCAL files from the managed pool, e.g. tiny:10,small:2. "
        "Selected files have never been uploaded to production. A third input "
        "source alongside --urls and --local-dir-path — exactly one is required.",
    )
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

    prod_lu_parser.add_argument(
        "--exclude-partial",
        action="store_true",
        help="With --from-pool, draw only files with no chunks already on the "
        "network. A partially uploaded file has ant skip what is stored, so it "
        "does not exercise a full upload.",
    )
    prod_lu_parser.add_argument(
        "--pool-min-size",
        type=str,
        metavar="SIZE",
        help="With --from-pool, take only files of at least this size, e.g. "
        "5MB. Narrows every requested class without changing the counts. Units "
        "are binary and a bare number is bytes.",
    )
    prod_lu_parser.add_argument(
        "--pool-max-size",
        type=str,
        metavar="SIZE",
        help="With --from-pool, take only files below this size, e.g. 500MB. "
        "Exclusive, so --pool-min-size 10MB --pool-max-size 100MB is exactly "
        "the `small` class.",
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

    # --- prod set-volume-reserve ---
    prod_svr_parser = prod_subparsers.add_parser(
        "set-volume-reserve",
        help="Lower the ext4 reserved-block percentage (default 5%%) on every "
        "node VM's data volume in a production inventory, reclaiming the "
        "otherwise-unusable reserve. Sequential, no ant-node restart.",
    )
    prod_svr_parser.add_argument(
        "--inventory-path",
        type=str,
        required=True,
        help="Path to the production inventory JSON (schema_version 3 or 4).",
    )
    prod_svr_parser.add_argument(
        "--reserve-percent",
        type=int,
        default=1,
        help="ext4 reserved-block percentage to set (default: 1).",
    )
    prod_svr_parser.add_argument(
        "--provider",
        type=str,
        default=None,
        choices=["aws", "digitalocean", "hetzner", "ovh", "vultr", "contabo"],
        help="Restrict to a single provider (default: every provider in the inventory).",
    )
    prod_svr_parser.add_argument(
        "--limit-to",
        type=str,
        default=None,
        help="Comma-separated VM names to restrict the run to (matched against "
        "inventory 'name' field). Combine with --provider for fine-grained tests.",
    )
    prod_svr_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default=None,
        help="Override the SSH key for every VM (default: each VM's inventory ssh_key).",
    )
    prod_svr_parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue past per-VM failures instead of stopping at the first error.",
    )
    prod_svr_parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )

    # --- prod services (ls/rm/add a single ant-node service on one host) ---
    prod_services_parser = prod_subparsers.add_parser(
        "services",
        help="List, remove, or add a single ant-node systemd service on one "
        "host — for freeing disk space on a quota-constrained node VM "
        "(e.g. Hetzner) ahead of a volume-size increase, and adding a "
        "service back once it lands.",
    )
    prod_services_subparsers = prod_services_parser.add_subparsers(
        dest="prod_services_action", required=True
    )

    def _add_prod_services_common_args(parser, *, service_name_help):
        parser.add_argument(
            "--inventory-path",
            type=str,
            required=True,
            help="Path to the inventory JSON (schema_version 3 or 4) containing the host.",
        )
        parser.add_argument(
            "--name",
            type=str,
            required=True,
            help="Host name, matched against the inventory's 'name' field.",
        )
        parser.add_argument(
            "--service-name",
            type=str,
            required=True,
            help=service_name_help,
        )
        parser.add_argument(
            "--ssh-key-path",
            type=str,
            default=None,
            help="Override the SSH key (default: the host's inventory ssh_key).",
        )

    prod_services_ls_parser = prod_services_subparsers.add_parser(
        "ls",
        help="List every ant-node-* service on the host with its active/enabled "
        "state and on-disk data size.",
    )
    prod_services_ls_parser.add_argument(
        "--inventory-path",
        type=str,
        required=True,
        help="Path to the inventory JSON (schema_version 3 or 4) containing the host.",
    )
    prod_services_ls_parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Host name, matched against the inventory's 'name' field.",
    )
    prod_services_ls_parser.add_argument(
        "--service-name",
        type=str,
        default=None,
        help="Restrict output to a single service, e.g. ant-node-7 "
        "(default: list every ant-node-* service on the host).",
    )
    prod_services_ls_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default=None,
        help="Override the SSH key (default: the host's inventory ssh_key).",
    )

    prod_services_rm_parser = prod_services_subparsers.add_parser(
        "rm",
        help="Stop, disable, and delete one ant-node service's unit + data/log "
        "dirs, freeing its disk space. Never touches sibling services.",
    )
    _add_prod_services_common_args(
        prod_services_rm_parser,
        service_name_help="Service to remove, e.g. ant-node-7.",
    )

    prod_services_add_parser = prod_services_subparsers.add_parser(
        "add",
        help="Add a new ant-node service at --service-name, cloning its config "
        "from an existing running sibling service on the same host.",
    )
    _add_prod_services_common_args(
        prod_services_add_parser,
        service_name_help="Service to add, e.g. ant-node-7 (must not already exist).",
    )

    prod_services_reset_parser = prod_services_subparsers.add_parser(
        "reset-data",
        help="Stop one ant-node service, wipe its data dir EXCEPT the identity "
        "key (node_identity.key), then start it — so it re-bootstraps with the "
        "SAME peer ID (unchanged XOR range/responsibility), reclaiming the "
        "record-store bytes while keeping its identity. Leaves the unit, port, "
        "log dir, and siblings untouched.",
    )
    _add_prod_services_common_args(
        prod_services_reset_parser,
        service_name_help="Service to reset, e.g. ant-node-7 (must already exist).",
    )

    prod_services_survey_parser = prod_services_subparsers.add_parser(
        "disk-survey",
        help="Survey disk usage across EVERY host in a production inventory: "
        "all ant-node-* services by data-dir size (ascending = deletion "
        "candidates first) plus each host's free space. Stores the survey in "
        "the registry DB (best-effort) for historical comparison.",
    )
    prod_services_survey_parser.add_argument(
        "--inventory-path",
        type=str,
        required=True,
        help="Path to the production inventory JSON (all hosts to survey).",
    )
    prod_services_survey_parser.add_argument(
        "--provider",
        type=str,
        default=None,
        choices=["aws", "digitalocean", "hetzner", "ovh", "vultr", "contabo"],
        help="Restrict the survey to a single provider (default: every provider).",
    )
    prod_services_survey_parser.add_argument(
        "--limit-to",
        type=str,
        default=None,
        help="Comma-separated VM names to restrict the survey to.",
    )
    prod_services_survey_parser.add_argument(
        "--ssh-key-path",
        type=str,
        default=None,
        help="Override the SSH key for every host (default: each host's inventory ssh_key).",
    )
    prod_services_survey_parser.add_argument(
        "--no-store",
        action="store_true",
        help="Print the survey but do NOT persist it to the registry DB.",
    )

    prod_services_survey_report_parser = prod_services_subparsers.add_parser(
        "survey-report",
        help="Compare the two most recent stored disk surveys for an inventory "
        "(today vs yesterday): per-host free-disk delta, total node-data delta, "
        "and the services added/removed/reset between them from the audit trail. "
        "Requires DATABASE_URL.",
    )
    prod_services_survey_report_parser.add_argument(
        "--inventory-path",
        type=str,
        required=True,
        help="Path to the production inventory JSON (its testnet_id keys the surveys).",
    )

    # --- prod files ---
    # =========================================================================
    # === prod pool — the catalogue of real files for production uploads ===
    # =========================================================================
    prod_pool_parser = prod_subparsers.add_parser(
        "pool",
        help="Manage the pool of real files available for production uploads: "
        "public URLs for remote uploader VMs, local paths for local uploads "
        "and the desktop application. A pooled file is never handed out twice.",
    )
    prod_pool_subparsers = prod_pool_parser.add_subparsers(
        dest="prod_pool_action", required=True
    )

    prod_pool_add_parser = prod_pool_subparsers.add_parser(
        "add",
        help="Catalogue files from URLs or the local filesystem.",
    )
    prod_pool_add_parser.add_argument(
        "--url",
        action="append",
        help="A public URL to catalogue. Repeatable.",
    )
    prod_pool_add_parser.add_argument(
        "--urls-file",
        type=str,
        help="File of newline-separated URLs (# comments allowed).",
    )
    prod_pool_add_parser.add_argument(
        "--dir",
        type=str,
        help="Directory to catalogue recursively, as local entries.",
    )
    prod_pool_add_parser.add_argument(
        "--path",
        type=str,
        help="A single local file to catalogue.",
    )
    prod_pool_add_parser.add_argument(
        "--tag",
        action="append",
        help="Label to attach for grouping (e.g. video, corpus). Repeatable.",
    )
    prod_pool_add_parser.add_argument(
        "--provenance",
        type=str,
        help="Where these files came from, kept so a curated pool stays auditable.",
    )
    prod_pool_add_parser.add_argument(
        "--allow-unknown-size",
        action="store_true",
        help="Catalogue a URL whose host publishes no Content-Length. Such an "
        "entry is INVISIBLE to size-class selection until `pool verify` "
        "resolves its size.",
    )
    prod_pool_add_parser.add_argument(
        "--hash",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Compute SHA1+SHA256 for local files (default on). SHA1 is what "
        "the Internet Archive publishes, and is the only way a local file and "
        "a remote URL can be recognised as the same content.",
    )
    prod_pool_add_parser.add_argument(
        "--materialise",
        type=str,
        metavar="DIR",
        help="Download each URL into DIR and catalogue the local copy instead "
        "of the URL, so one piece of content is one entry.",
    )
    prod_pool_add_parser.add_argument(
        "--from-listing",
        type=str,
        metavar="KIND:ID",
        help="Bulk-catalogue a public collection, e.g. "
        "internet-archive:<item-id>. Entries arrive pre-sized and pre-hashed "
        "from the source's own metadata — nothing is downloaded, and the real "
        "checksums are what let these be matched against local copies. A "
        "canary run consumes ~350 files, so this is the practical way to keep "
        "the pool stocked.",
    )
    prod_pool_add_parser.add_argument(
        "--size",
        choices=["tiny", "small", "medium", "large", "xlarge"],
        help="With --from-listing, take only files in this size class.",
    )
    prod_pool_add_parser.add_argument(
        "--min-size",
        type=str,
        metavar="SIZE",
        help="With --from-listing, take only files of at least this size, e.g. "
        "5MB. Combines with --size, and expresses what a class cannot: 'tiny "
        "but at least 5MB' is the band a canary wants, because the chunk floor "
        "is 4 and a 14KB file buys the same minimum observation. Units are "
        "binary; a bare number is bytes.",
    )
    prod_pool_add_parser.add_argument(
        "--max-size",
        type=str,
        metavar="SIZE",
        help="With --from-listing, take only files below this size (exclusive), "
        "e.g. 1GB.",
    )
    prod_pool_add_parser.add_argument(
        "--limit",
        type=int,
        help="With --from-listing, take at most this many files.",
    )
    prod_pool_add_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --from-listing, show what would be catalogued.",
    )
    prod_pool_add_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print a line per file instead of a progress bar. Readable for a "
        "small directory; unusable for a large one.",
    )
    prod_pool_add_parser.add_argument(
        "--path-prefix",
        type=str,
        metavar="PATH",
        help="With --from-listing, take only files under this directory within "
        "the item. Percent-decoded first, so it can be pasted from a browser "
        "URL. An item's file names are its full internal paths, so a large "
        "item can be catalogued a directory at a time.",
    )
    prod_pool_add_parser.add_argument(
        "--originals-only",
        action="store_true",
        help="With --from-listing, restrict to files the source marks as "
        "originals. On Internet Archive media items this usually EXCLUDES the "
        "real content — the ingested media is marked 'derivative' and the only "
        "originals are metadata and a torrent — so it is off by default.",
    )

    prod_pool_list_parser = prod_pool_subparsers.add_parser(
        "list",
        help="List catalogued files and their state.",
    )
    prod_pool_list_parser.add_argument(
        "--status",
        choices=["available", "claimed", "uploaded", "partial"],
        help="available = never handed out; claimed = handed out but not "
        "confirmed on the network; uploaded = confirmed, with a datamap "
        "address; partial = an upload stored some chunks and stopped. A "
        "claimed file is NOT on the network. A partial file IS still "
        "available (completing it is useful work), but is excluded from "
        "canary selection because chunks of it are already stored.",
    )
    prod_pool_list_parser.add_argument(
        "--kind", choices=["url", "local"], help="Restrict to one inventory."
    )
    prod_pool_list_parser.add_argument(
        "--size",
        choices=["tiny", "small", "medium", "large", "xlarge"],
        help="Restrict to one size class.",
    )
    prod_pool_list_parser.add_argument(
        "--min-size",
        type=str,
        metavar="SIZE",
        help="Restrict to files of at least this size, e.g. 5MB. The same "
        "bounds a manifest's pool_min_size applies, so inventory can be checked "
        "against the exact selection a run will make.",
    )
    prod_pool_list_parser.add_argument(
        "--max-size",
        type=str,
        metavar="SIZE",
        help="Restrict to files below this size (exclusive), e.g. 100MB.",
    )
    prod_pool_list_parser.add_argument(
        "--tag", action="append", help="Restrict to entries carrying a tag."
    )
    prod_pool_list_parser.add_argument(
        "--since", type=str, help="Only entries added since (ISO date, or 7d / 24h)."
    )
    prod_pool_list_parser.add_argument("--limit", type=int, help="Cap the rows shown.")
    prod_pool_list_parser.add_argument(
        "--all", action="store_true", help="Include retired (inactive) entries."
    )

    prod_pool_show_parser = prod_pool_subparsers.add_parser(
        "show",
        help="Show one file in full, including its upload result if it has one.",
    )
    prod_pool_show_parser.add_argument(
        "file_id", type=int, nargs="?", help="Pool file id."
    )
    prod_pool_show_parser.add_argument(
        "--address",
        type=str,
        help="Reverse lookup: find the file that produced this datamap address.",
    )

    prod_pool_subparsers.add_parser(
        "stats",
        help="Availability per size class, split by kind. The two kinds are "
        "separate inventories and are never summed.",
    )

    prod_pool_checkout_parser = prod_pool_subparsers.add_parser(
        "checkout",
        help="Take a file out of the pool by hand — for the desktop "
        "application, or anything else with no link to this database. The "
        "checkout is what spends the file, not the upload.",
    )
    prod_pool_checkout_parser.add_argument(
        "--size",
        required=True,
        choices=["tiny", "small", "medium", "large", "xlarge"],
        help="Which size class to draw from.",
    )
    prod_pool_checkout_parser.add_argument(
        "--count", type=int, default=1, help="How many files (default 1)."
    )
    prod_pool_checkout_parser.add_argument(
        "--kind",
        choices=["local", "url"],
        default="local",
        help="Default local, since the desktop application reads from disk.",
    )
    prod_pool_checkout_parser.add_argument(
        "--consumer",
        type=str,
        help="Free-text label so you can find these claims again "
        "(e.g. desktop-app). Defaults to 'manual'.",
    )
    prod_pool_checkout_parser.add_argument(
        "--reuse-open",
        action="store_true",
        help="Show the claims this consumer already holds instead of spending "
        "another file.",
    )

    prod_pool_claims_parser = prod_pool_subparsers.add_parser(
        "claims",
        help="Show what is currently checked out. Open claims by default — "
        "this is how you find a file you took earlier and never resolved.",
    )
    prod_pool_claims_parser.add_argument(
        "--consumer", type=str, help="Only claims carrying this label."
    )
    prod_pool_claims_parser.add_argument(
        "--source",
        choices=["prod-uploaders", "prod-local-uploads", "prod-canary", "manual"],
        help="Only claims taken by this consumer type.",
    )
    prod_pool_claims_parser.add_argument(
        "--status",
        choices=["claimed", "uploaded", "released", "partial"],
        help="Filter by claim status (default: open claims only).",
    )
    prod_pool_claims_parser.add_argument(
        "--stale",
        type=str,
        metavar="AGE",
        help="Only claims open longer than AGE (e.g. 7d, 24h). Excludes "
        "deployment-backed claims, which are pending rather than forgotten.",
    )
    prod_pool_claims_parser.add_argument(
        "--include-deployments",
        action="store_true",
        help="With --stale, also show deployment-backed claims.",
    )
    prod_pool_claims_parser.add_argument(
        "--unresolvable",
        action="store_true",
        help="Only claims no record-results run can ever close, because their "
        "deployment — and its InfluxDB — is gone.",
    )
    prod_pool_claims_parser.add_argument(
        "--all", action="store_true", help="Include resolved and released claims."
    )

    prod_pool_verify_parser = prod_pool_subparsers.add_parser(
        "verify",
        help="Re-check entries against reality: URLs that have rotted, local "
        "files that moved, sizes that changed. Finding out mid-deploy on a "
        "funded mainnet wallet is the expensive way.",
    )
    prod_pool_verify_parser.add_argument(
        "--kind", choices=["url", "local"], help="Restrict to one inventory."
    )
    prod_pool_verify_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print a line per file instead of a progress bar. Readable for a "
        "small directory; unusable for a large one.",
    )
    prod_pool_verify_parser.add_argument(
        "--limit", type=int, help="Check at most this many entries."
    )
    prod_pool_verify_parser.add_argument(
        "--rehash",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Re-hash a local file whose size changed (default on).",
    )

    prod_pool_import_parser = prod_pool_subparsers.add_parser(
        "import-history",
        help="Mark pooled files that are already on the production network as "
        "spent, from BOTH recorded upload tables (prod-uploaders results AND "
        "canary uploads). Idempotent — re-run it whenever uploads happen "
        "outside the pool.",
    )
    prod_pool_import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be marked spent without writing anything.",
    )
    prod_pool_import_parser.add_argument(
        "--catalogue-missing",
        action="store_true",
        help="Also CREATE pool entries for recorded uploads the pool does not "
        "hold, already marked spent. Built entirely from the registry — the "
        "source URL, name and byte size were recorded at upload time — so it "
        "needs no network access and works for URLs that have since died. "
        "Without this, such uploads are only counted as unmatched.",
    )
    prod_pool_import_parser.add_argument(
        "--deployment",
        type=str,
        metavar="NAME",
        help="Only consider uploads recorded against this deployment.",
    )
    prod_pool_import_parser.add_argument(
        "--tag",
        action="append",
        help="Tag to apply to entries created by --catalogue-missing. Repeatable.",
    )

    prod_pool_history_parser = prod_pool_subparsers.add_parser(
        "history", help="Every claim ever made against one file."
    )
    prod_pool_history_parser.add_argument("file_id", type=int)

    prod_pool_release_parser = prod_pool_subparsers.add_parser(
        "release",
        help="Return claimed file(s) to the pool. Never automatic — a claim "
        "that might correspond to a real upload must not be freed by a sweep.",
    )
    prod_pool_release_parser.add_argument("file_ids", type=int, nargs="+")
    prod_pool_release_parser.add_argument("--note", type=str, help="Why.")

    prod_pool_mark_parser = prod_pool_subparsers.add_parser(
        "mark-uploaded",
        help="Record that a checked-out file reached the network.",
    )
    prod_pool_mark_parser.add_argument("file_id", type=int)
    prod_pool_mark_parser.add_argument(
        "--address", type=str, help="The datamap address the upload produced."
    )
    prod_pool_mark_parser.add_argument("--chunks-stored", type=int)
    prod_pool_mark_parser.add_argument("--chunks-failed", type=int)
    prod_pool_mark_parser.add_argument("--total-chunks", type=int)
    prod_pool_mark_parser.add_argument(
        "--storage-cost-atto",
        type=str,
        help="Storage cost in atto (a whole number, not ANT).",
    )
    prod_pool_mark_parser.add_argument(
        "--gas-cost-wei",
        type=str,
        help="Gas cost in wei (a whole number, not ETH).",
    )
    prod_pool_mark_parser.add_argument("--note", type=str)

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
        elif args.deployments_subcommand == "provision-telegraf":
            from saorsa_deploy.cmd.provision_telegraf_combined import (
                cmd_provision_telegraf_combined,
            )

            cmd_provision_telegraf_combined(args)
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
        elif args.deployments_subcommand == "derive-metrics":
            from saorsa_deploy.cmd.derive_metrics import cmd_derive_metrics

            cmd_derive_metrics(args)
        elif args.deployments_subcommand == "compare":
            from saorsa_deploy.cmd.compare_runs import cmd_compare_runs

            cmd_compare_runs(args)
        elif args.deployments_subcommand == "prune-host-buckets":
            from saorsa_deploy.cmd.prune_host_buckets import cmd_prune_host_buckets

            cmd_prune_host_buckets(args)
        elif args.deployments_subcommand == "record-results":
            from saorsa_deploy.cmd.record_results import cmd_record_results

            cmd_record_results(args)
        elif args.deployments_subcommand == "archive":
            from saorsa_deploy.cmd.archive_results import cmd_archive_results

            cmd_archive_results(args)
        elif args.deployments_subcommand == "slack-post":
            from saorsa_deploy.cmd.slack_post import cmd_slack_post

            cmd_slack_post(args)
        elif args.deployments_subcommand == "print":
            from saorsa_deploy.cmd.print_deployment import cmd_print

            cmd_print(args)
        elif args.deployments_subcommand == "unextend":
            from saorsa_deploy.cmd.unextend import cmd_unextend

            cmd_unextend(args)
        elif args.deployments_subcommand == "upgrade-ant-binary":
            from saorsa_deploy.cmd.upgrade_ant_binary import cmd_upgrade_ant_binary

            cmd_upgrade_ant_binary(args)
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
            args.prod_subcommand == "nodes"
            and args.prod_nodes_action == "deploy"
        ):
            from saorsa_deploy.cmd.prod_nodes import cmd_prod_nodes_deploy

            cmd_prod_nodes_deploy(args)
        elif (
            args.prod_subcommand == "nodes"
            and args.prod_nodes_action == "destroy"
        ):
            from saorsa_deploy.cmd.prod_nodes import cmd_prod_nodes_destroy

            cmd_prod_nodes_destroy(args)
        elif (
            args.prod_subcommand == "debug-nodes"
            and args.prod_debug_nodes_action == "deploy"
        ):
            from saorsa_deploy.cmd.prod_debug_nodes import (
                cmd_prod_debug_nodes_deploy,
            )

            cmd_prod_debug_nodes_deploy(args)
        elif (
            args.prod_subcommand == "debug-nodes"
            and args.prod_debug_nodes_action == "destroy"
        ):
            from saorsa_deploy.cmd.prod_debug_nodes import (
                cmd_prod_debug_nodes_destroy,
            )

            cmd_prod_debug_nodes_destroy(args)
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
            and args.prod_canary_action == "report"
        ):
            from saorsa_deploy.cmd.prod_canary_report import (
                cmd_prod_canary_report,
            )

            cmd_prod_canary_report(args)
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
        elif args.prod_subcommand == "set-volume-reserve":
            from saorsa_deploy.cmd.set_volume_reserve import cmd_set_volume_reserve

            cmd_set_volume_reserve(args)
        elif args.prod_subcommand == "services" and args.prod_services_action == "ls":
            from saorsa_deploy.cmd.prod_services import cmd_prod_services_ls

            cmd_prod_services_ls(args)
        elif args.prod_subcommand == "services" and args.prod_services_action == "rm":
            from saorsa_deploy.cmd.prod_services import cmd_prod_services_rm

            cmd_prod_services_rm(args)
        elif args.prod_subcommand == "services" and args.prod_services_action == "add":
            from saorsa_deploy.cmd.prod_services import cmd_prod_services_add

            cmd_prod_services_add(args)
        elif (
            args.prod_subcommand == "services"
            and args.prod_services_action == "reset-data"
        ):
            from saorsa_deploy.cmd.prod_services import cmd_prod_services_reset_data

            cmd_prod_services_reset_data(args)
        elif (
            args.prod_subcommand == "services"
            and args.prod_services_action == "disk-survey"
        ):
            from saorsa_deploy.cmd.prod_services import cmd_prod_services_disk_survey

            cmd_prod_services_disk_survey(args)
        elif (
            args.prod_subcommand == "services"
            and args.prod_services_action == "survey-report"
        ):
            from saorsa_deploy.cmd.prod_services import cmd_prod_services_survey_report

            cmd_prod_services_survey_report(args)
        elif args.prod_subcommand == "pool" and args.prod_pool_action == "add":
            from saorsa_deploy.cmd.prod_pool import cmd_prod_pool_add

            cmd_prod_pool_add(args)
        elif args.prod_subcommand == "pool" and args.prod_pool_action == "list":
            from saorsa_deploy.cmd.prod_pool import cmd_prod_pool_list

            cmd_prod_pool_list(args)
        elif args.prod_subcommand == "pool" and args.prod_pool_action == "show":
            from saorsa_deploy.cmd.prod_pool import cmd_prod_pool_show

            cmd_prod_pool_show(args)
        elif args.prod_subcommand == "pool" and args.prod_pool_action == "stats":
            from saorsa_deploy.cmd.prod_pool import cmd_prod_pool_stats

            cmd_prod_pool_stats(args)
        elif args.prod_subcommand == "pool" and args.prod_pool_action == "checkout":
            from saorsa_deploy.cmd.prod_pool import cmd_prod_pool_checkout

            cmd_prod_pool_checkout(args)
        elif args.prod_subcommand == "pool" and args.prod_pool_action == "claims":
            from saorsa_deploy.cmd.prod_pool import cmd_prod_pool_claims

            cmd_prod_pool_claims(args)
        elif (
            args.prod_subcommand == "pool"
            and args.prod_pool_action == "import-history"
        ):
            from saorsa_deploy.cmd.prod_pool import cmd_prod_pool_import_history

            cmd_prod_pool_import_history(args)
        elif args.prod_subcommand == "pool" and args.prod_pool_action == "verify":
            from saorsa_deploy.cmd.prod_pool import cmd_prod_pool_verify

            cmd_prod_pool_verify(args)
        elif args.prod_subcommand == "pool" and args.prod_pool_action == "history":
            from saorsa_deploy.cmd.prod_pool import cmd_prod_pool_history

            cmd_prod_pool_history(args)
        elif args.prod_subcommand == "pool" and args.prod_pool_action == "release":
            from saorsa_deploy.cmd.prod_pool import cmd_prod_pool_release

            cmd_prod_pool_release(args)
        elif (
            args.prod_subcommand == "pool"
            and args.prod_pool_action == "mark-uploaded"
        ):
            from saorsa_deploy.cmd.prod_pool import cmd_prod_pool_mark_uploaded

            cmd_prod_pool_mark_uploaded(args)
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
