"""Tests for the standalone ``elasticsearch`` deployment kind (V2-1016).

The instance this kind produces is reachable from the public internet by
machines we do not operate, which is what separates it from every other
Elasticsearch stack in this repo. Most of what is asserted here is therefore
about what the instance does NOT do: no plaintext port, no anonymous access, no
read privilege on a community credential — and, just as importantly, that none
of that leaks into the existing testnet/prod stacks.
"""

import pytest
from pydantic import ValidationError

from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator import build_deploy_steps, build_destroy_steps
from saorsa_deploy.provisioning.elastic import (
    BETA_INDEX_PATTERN,
    BETA_INGEST_ROLE,
    ElasticProvisioner,
)
from saorsa_deploy.provisioning.es_ingest_proxy import (
    build_caddyfile,
    dns_preflight_message,
)

SSH = "/ssh/key"
HOST = "logs.autonomi.com"


def _manifest(**spec) -> Manifest:
    base = {"public_ingest": True, "ingest_hostname": HOST}
    base.update(spec)
    return Manifest.model_validate(
        {
            "name": "BETA-LOGS-01",
            "kind": "elasticsearch",
            "purpose": "beta-channel community node logs",
            "ssh_key_path": SSH,
            "elasticsearch": base,
        }
    )


def _argvs(steps):
    return [s.argv for s in steps]


# --------------------------------------------------------------------------- #
# manifest schema
# --------------------------------------------------------------------------- #
def test_minimal_manifest_defaults():
    m = _manifest()
    assert m.spec.provider == "digitalocean"
    # 14 days: the beta soak is 5, so this is comfortably more than the window
    # the verdict skill reads over.
    assert m.spec.retention_days == 14
    assert m.spec.keep_on_destroy is False


def test_public_ingest_requires_hostname():
    # ACME cannot issue for an IP, so this cannot come up at all — better to
    # fail at validate time than after a VM exists and is billing.
    with pytest.raises(ValidationError, match="requires ingest_hostname"):
        Manifest.model_validate(
            {
                "name": "X",
                "kind": "elasticsearch",
                "purpose": "p",
                "elasticsearch": {"public_ingest": True},
            }
        )


def test_hostname_without_public_ingest_is_refused():
    # Otherwise the operator believes they configured TLS for that name and
    # nothing is listening for it.
    with pytest.raises(ValidationError, match="requires public_ingest"):
        Manifest.model_validate(
            {
                "name": "X",
                "kind": "elasticsearch",
                "purpose": "p",
                "elasticsearch": {"ingest_hostname": HOST},
            }
        )


def test_reserved_ip_is_digitalocean_only():
    # Silently ignoring it elsewhere would promise a stable address the
    # deployment does not have — and the DNS record depends on that promise.
    with pytest.raises(ValidationError, match="only supported on provider"):
        Manifest.model_validate(
            {
                "name": "X",
                "kind": "elasticsearch",
                "purpose": "p",
                "elasticsearch": {
                    "provider": "ovh",
                    "reserved_ip": "1.2.3.4",
                    "public_ingest": True,
                    "ingest_hostname": HOST,
                },
            }
        )


def test_kind_rejects_a_foreign_block():
    with pytest.raises(ValidationError):
        Manifest.model_validate(
            {
                "name": "X",
                "kind": "elasticsearch",
                "purpose": "p",
                "elasticsearch": {},
                "testnet": {},
            }
        )


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def test_deploy_steps():
    m = _manifest(droplet_size="s-4vcpu-8gb", volume_size=100, reserved_ip="1.2.3.4")
    assert _argvs(build_deploy_steps(m)) == [
        [
            "elastic", "infra", "--name", "BETA-LOGS-01", "--standalone",
            "--provider", "digitalocean",
            "--droplet-size", "s-4vcpu-8gb",
            "--volume-size", "100",
            "--public-ingest",
            "--reserved-ip", "1.2.3.4",
        ],
        [
            "elastic", "provision", "--name", "BETA-LOGS-01",
            "--ssh-key-path", SSH,
            "--retention-days", "14",
            "--public-ingest", "--ingest-hostname", HOST,
        ],
    ]


def test_deploy_is_two_steps_so_a_dns_failure_resumes():
    # The DNS record can only be set once infra has produced an address, and the
    # certificate is issued during provision. Keeping them as separate journal
    # entries is what lets `up --resume` retry provisioning alone instead of
    # recreating the VM.
    steps = build_deploy_steps(_manifest())
    assert len(steps) == 2
    assert steps[0].argv[:2] == ["elastic", "infra"]
    assert steps[1].argv[:2] == ["elastic", "provision"]


def test_no_binary_build_or_inventory_step():
    # The prod dispatch path prepends `build ant` and appends
    # `generate-inventory`; neither means anything for an ES-only deployment,
    # and `build ant` would fail outright since the spec has no `binary`.
    argvs = _argvs(build_deploy_steps(_manifest()))
    assert not any(a[0] == "build" for a in argvs)
    assert not any("generate-inventory" in a for a in argvs)


def test_private_instance_omits_the_public_flags():
    m = _manifest(public_ingest=False, ingest_hostname=None)
    infra, provision = build_deploy_steps(m)
    assert "--public-ingest" not in infra.argv
    assert "--public-ingest" not in provision.argv
    assert "--ingest-hostname" not in provision.argv


def test_staging_ca_flag_is_threaded():
    m = _manifest(letsencrypt_staging=True)
    assert "--letsencrypt-staging" in build_deploy_steps(m)[1].argv


def test_destroy_forces_to_avoid_a_prompt():
    # `elastic destroy` prompts without --force, which would hang a non-TTY run.
    assert _argvs(build_destroy_steps(_manifest())) == [
        ["elastic", "destroy", "--name", "BETA-LOGS-01", "--force"]
    ]


def test_slack_post_tail_still_applies():
    m = Manifest.model_validate(
        {
            "name": "BETA-LOGS-01",
            "kind": "elasticsearch",
            "purpose": "p",
            "slack_post": True,
            "elasticsearch": {"public_ingest": True, "ingest_hostname": HOST},
        }
    )
    assert build_deploy_steps(m)[-1].argv == [
        "deployments", "slack-post", "--name", "BETA-LOGS-01",
    ]


# --------------------------------------------------------------------------- #
# provisioner — public-ingest hardening
# --------------------------------------------------------------------------- #
def _pub(**kw) -> ElasticProvisioner:
    return ElasticProvisioner(
        "10.0.0.1", SSH, "BETA-LOGS-01",
        public_ingest=True, ingest_hostname=HOST, **kw,
    )


def _config_lines(yml: str) -> list[str]:
    return [
        line for line in yml.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_public_instance_binds_loopback_only():
    # Belt and braces with the firewall: a firewall mistake alone must not be
    # enough to expose an unauthenticated plaintext Elasticsearch.
    assert "network.host: 127.0.0.1" in _config_lines(_pub()._build_elasticsearch_yml())


def test_public_instance_disables_anonymous_access():
    # The testnet default grants anonymous callers `remote_monitoring_collector`.
    # On a public endpoint that is an unauthenticated window into the cluster.
    lines = _config_lines(_pub()._build_elasticsearch_yml())
    assert not any("anonymous" in line or "authc" in line for line in lines)


def test_ingest_role_is_append_only():
    script = _pub()._build_setup_script()
    assert BETA_INGEST_ROLE in script
    assert "'create_doc', 'auto_configure'" in script
    # The role must never gain `read`: V2-1016 says writes only, and a reader
    # could pull every other participant's logs.
    role_block = script.split(BETA_INGEST_ROLE)[-1]
    assert "'read'" not in role_block
    assert "'manage'" not in role_block


def test_beta_index_template_is_created():
    script = _pub()._build_setup_script()
    assert BETA_INDEX_PATTERN in script
    assert "beta-node-logs" in script
    # One bad field from one community machine must not 400 a bulk batch and
    # take down ingest for everyone else in it.
    assert "'index.mapping.ignore_malformed': True" in script


def test_public_setup_script_is_valid_python():
    compile(_pub()._build_setup_script(), "setup", "exec")


def test_public_ingest_requires_a_hostname_in_the_provisioner_too():
    with pytest.raises(ValueError, match="requires ingest_hostname"):
        ElasticProvisioner("10.0.0.1", SSH, "X", public_ingest=True)


# --------------------------------------------------------------------------- #
# no regression for the existing (testnet/prod) stacks
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("root_disk", [False, True])
def test_private_instance_keeps_its_existing_config(root_disk):
    priv = ElasticProvisioner("10.0.0.1", SSH, "STG-01", root_disk=root_disk)
    yml = priv._build_elasticsearch_yml()
    # Unchanged from before this kind existed: world-bound and anonymous, which
    # is fine for a stack only our own VMs can reach for a few hours.
    assert "network.host: 0.0.0.0" in yml
    assert "roles: remote_monitoring_collector" in yml
    script = priv._build_setup_script()
    assert BETA_INGEST_ROLE not in script
    assert BETA_INDEX_PATTERN not in script
    compile(script, "setup", "exec")


# --------------------------------------------------------------------------- #
# ingest proxy
# --------------------------------------------------------------------------- #
def test_caddyfile_exposes_only_write_endpoints():
    cfg = build_caddyfile(HOST)
    assert f"{HOST} {{" in cfg
    assert "reverse_proxy 127.0.0.1:9200" in cfg
    # An allowlist, so a new endpoint in a future Elasticsearch version is
    # unreachable by default rather than newly exposed.
    assert "method POST PUT" in cfg
    assert "_search" not in cfg
    assert "respond \"not found\" 404" in cfg


def test_caddyfile_trims_the_bulk_response():
    # An unfiltered _bulk response is roughly the size of the request that
    # produced it — pure egress, on every batch. Forcing the filter here means
    # it holds regardless of how a community forwarder is configured.
    assert "uri query +filter_path errors,items.*.error" in build_caddyfile(HOST)


def test_caddyfile_disables_the_http_redirect():
    # Port 80 is closed by the firewall, so a redirect listener would only fail
    # to bind; TLS-ALPN-01 on 443 is what issues the certificate.
    assert "auto_https disable_redirects" in build_caddyfile(HOST)


def test_staging_ca_is_opt_in():
    assert "acme-staging" not in build_caddyfile(HOST)
    assert "acme-staging" in build_caddyfile(HOST, letsencrypt_staging=True)


def test_request_body_is_capped():
    assert "max_size" in build_caddyfile(HOST)


# --------------------------------------------------------------------------- #
# DNS preflight
# --------------------------------------------------------------------------- #
def test_dns_preflight_passes_when_the_record_matches():
    assert dns_preflight_message(HOST, ["1.2.3.4"], "1.2.3.4") is None


def test_dns_preflight_reports_the_wrong_address():
    msg = dns_preflight_message(HOST, ["9.9.9.9"], "1.2.3.4")
    assert "9.9.9.9" in msg and "1.2.3.4" in msg and HOST in msg


def test_dns_preflight_reports_a_missing_record():
    msg = dns_preflight_message(HOST, [], "1.2.3.4")
    assert "does not resolve" in msg
    # The proxied-record trap is the one an operator will actually hit, so the
    # message has to name it rather than leaving them to discover it.
    assert "grey cloud" in msg or "DNS only" in msg


def test_dns_preflight_accepts_a_multi_record_name():
    assert dns_preflight_message(HOST, ["9.9.9.9", "1.2.3.4"], "1.2.3.4") is None
