"""TLS-terminating ingest proxy for a public-facing Elasticsearch instance.

The testnet Elasticsearch stacks talk plaintext on 9200 with anonymous access
enabled, which is fine when the only callers are our own VMs inside a run that
lasts hours. An instance that community machines forward to is a different
proposition: the credential is on the open internet on every request, so TLS is
not optional, and the ES REST surface should not be reachable at all beyond the
few endpoints ingest actually needs.

Caddy fronts Elasticsearch because it obtains and renews a Let's Encrypt
certificate with no cron, no certbot and no renewal hook — which is what
"permanent operation" demands of a box nobody is watching.

Everything here is a pure function returning a shell command or a config string,
mirroring the rest of ``provisioning/`` so the rendering can be tested without a
VM.
"""

from __future__ import annotations

CADDY_CONFIG_PATH = "/etc/caddy/Caddyfile"

#: Cap on a single ingest request body. Bulk requests are the only large writes
#: and a forwarder batching more than this is misconfigured; without a cap one
#: caller can drive the heap into a circuit breaker.
DEFAULT_MAX_BODY = "50MB"

#: Trim the ``_bulk`` response to what a forwarder actually acts on.
#: Elasticsearch otherwise answers with a per-document result object (~200 bytes
#: each), so an unfiltered response is roughly the size of the request that
#: produced it — pure egress, on every batch, forever. Forcing it here rather
#: than documenting it means it holds regardless of how a community forwarder is
#: configured.
#:
#: ``items.*.status`` is included and must stay. Filtering to ``items.*.error``
#: alone is smaller still (measured: 16 bytes vs 13 KB for a 500-document batch,
#: against 100 KB unfiltered) but Elasticsearch **collapses the items array to
#: only the failing entries**, so their positions no longer line up with the
#: submitted documents. A client cannot then tell which document a per-item 429
#: belongs to and has to retry the whole batch — duplicating everything that
#: already succeeded, since these are auto-ID ``create`` writes. Keeping
#: ``status`` gives every document an entry, preserving the correlation, and
#: still saves ~87% of the response.
BULK_FILTER_PATH = "errors,items.*.status,items.*.error"

LETSENCRYPT_STAGING_CA = "https://acme-staging-v02.api.letsencrypt.org/directory"


def build_caddy_install_command() -> str:
    """Install Caddy from its official apt repository (idempotent)."""
    return (
        "dpkg -s caddy >/dev/null 2>&1 && echo 'CADDY:SKIP' || ("
        "apt-get update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "debian-keyring debian-archive-keyring apt-transport-https curl gpg && "
        "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' "
        "| gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg && "
        "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' "
        "> /etc/apt/sources.list.d/caddy-stable.list && "
        "apt-get update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy && "
        "echo 'CADDY:INSTALLED')"
    )


def build_caddyfile(
    hostname: str,
    *,
    letsencrypt_staging: bool = False,
    max_body: str = DEFAULT_MAX_BODY,
    filter_bulk_response: bool = True,
) -> str:
    """Render the Caddyfile fronting Elasticsearch on ``hostname``."""
    global_opts = [
        "{",
        # Port 80 is closed by the firewall, so there is no cleartext listener
        # to redirect from and no HTTP-01 challenge to serve. Caddy falls back
        # to TLS-ALPN-01 on 443, which is why 80 can stay shut.
        "\tauto_https disable_redirects",
    ]
    if letsencrypt_staging:
        global_opts.append(f"\tacme_ca {LETSENCRYPT_STAGING_CA}")
    global_opts.append("}")

    lines = [
        *global_opts,
        "",
        f"{hostname} {{",
        # Allowlist, not denylist: a new Elasticsearch endpoint appearing in a
        # future version is then unreachable by default rather than newly
        # exposed. Ingest needs bulk writes and single-document writes; nothing
        # else, and notably no reads.
        "\t@ingest {",
        "\t\tmethod POST PUT",
        "\t\tpath /_bulk /*/_bulk /*/_doc /*/_doc/* /*/_create/*",
        "\t}",
        "",
        "\thandle @ingest {",
        "\t\trequest_body {",
        f"\t\t\tmax_size {max_body}",
        "\t\t}",
    ]
    # Strip any client-supplied `pipeline`. Belt and braces with the index's
    # final_pipeline (which already cannot be overridden): naming a pipeline is
    # the documented way to bypass a `default_pipeline`, and removing the
    # parameter at the edge means no client can select ingest processing at all.
    lines.append("\t\turi query -pipeline")
    if filter_bulk_response:
        # `uri query +key value` appends without disturbing an existing query
        # string — string-concatenating "&..." would produce a malformed URI on
        # the (usual) requests that carry no query at all.
        lines.append(f"\t\turi query +filter_path {BULK_FILTER_PATH}")
    lines += [
        "\t\treverse_proxy 127.0.0.1:9200",
        "\t}",
        "",
        # A bare 404 for everything else, with no hint that Elasticsearch is
        # behind it.
        "\thandle {",
        '\t\trespond "not found" 404',
        "\t}",
        "}",
        "",
    ]
    return "\n".join(lines)


def build_write_caddyfile_command(caddyfile: str) -> str:
    """Write the Caddyfile, validate it, then reload.

    Validation runs before anything is restarted, so a bad directive fails the
    provision with Caddy's own error rather than silently leaving the endpoint
    down — the failure mode that matters here, since nothing else would notice
    an ingest endpoint that stopped accepting writes.
    """
    return (
        f"cat > {CADDY_CONFIG_PATH} << 'CADDYFILE_EOF'\n{caddyfile}CADDYFILE_EOF\n"
        f"caddy fmt --overwrite {CADDY_CONFIG_PATH} && "
        f"caddy validate --config {CADDY_CONFIG_PATH} && "
        "systemctl enable caddy && "
        "systemctl restart caddy"
    )


def dns_preflight_message(hostname: str, resolved: list[str], expected_ip: str) -> str | None:
    """``None`` when DNS is correct, else an operator-actionable explanation.

    The certificate is issued during provisioning, so a record that does not yet
    point here fails ACME. Saying exactly which record to change — and what it
    currently says — turns a confusing certificate error into a two-minute fix.
    """
    if expected_ip in resolved:
        return None
    if not resolved:
        return (
            f"'{hostname}' does not resolve.\n"
            f"Create an A record:  {hostname}  ->  {expected_ip}\n"
            "In Cloudflare it must be DNS only (grey cloud): a proxied record "
            "terminates TLS at Cloudflare, so the certificate cannot be issued "
            "here."
        )
    return (
        f"'{hostname}' resolves to {', '.join(resolved)}, not {expected_ip}.\n"
        f"Update the A record:  {hostname}  ->  {expected_ip}\n"
        "If the current value is a Cloudflare address the record is proxied "
        "(orange cloud); set it to DNS only so the certificate can be issued "
        "here."
    )
