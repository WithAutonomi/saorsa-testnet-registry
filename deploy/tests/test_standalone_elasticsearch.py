"""Tests for the standalone ``elasticsearch`` deployment kind (V2-1016).

The instance this kind produces is reachable from the public internet by
machines we do not operate, which is what separates it from every other
Elasticsearch stack in this repo. Most of what is asserted here is therefore
about what the instance does NOT do: no plaintext port, no anonymous access, no
read privilege on a community credential — and, just as importantly, that none
of that leaks into the existing testnet/prod stacks.
"""

import json

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
    BULK_FILTER_PATH,
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
        [
            "deployments", "generate-inventory", "--name", "BETA-LOGS-01",
            "--ssh-key-path", SSH,
        ],
    ]


def test_infra_and_provision_are_separate_so_a_dns_failure_resumes():
    # The DNS record can only be set once infra has produced an address, and the
    # certificate is issued during provision. Keeping them as separate journal
    # entries is what lets `up --resume` retry provisioning alone instead of
    # recreating the VM.
    steps = build_deploy_steps(_manifest())
    assert steps[0].argv[:2] == ["elastic", "infra"]
    assert steps[1].argv[:2] == ["elastic", "provision"]
    assert steps[0].best_effort is False and steps[1].best_effort is False


def test_no_binary_build_step():
    # The prod dispatch path prepends `build ant`, which is meaningless for an
    # ES-only deployment and would fail outright since the spec has no `binary`.
    argvs = _argvs(build_deploy_steps(_manifest()))
    assert not any(a[0] == "build" for a in argvs)


def test_private_instance_omits_the_public_flags():
    m = _manifest(public_ingest=False, ingest_hostname=None)
    infra, provision = build_deploy_steps(m)[:2]
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
    assert "uri query +filter_path errors,items." in build_caddyfile(HOST)


def test_bulk_filter_keeps_per_item_status():
    # Load-bearing, and measured against ES 8.17.4: filtering to items.*.error
    # alone makes Elasticsearch COLLAPSE the items array to just the failures,
    # so their positions no longer match the submitted documents. A client then
    # cannot tell which document a per-item 429 refers to and must retry the
    # whole batch — duplicating everything that already succeeded, because these
    # are auto-ID `create` writes. `status` gives every document an entry.
    assert "items.*.status" in BULK_FILTER_PATH
    assert "items.*.status" in build_caddyfile(HOST)


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


# --------------------------------------------------------------------------- #
# server-side log-level filtering
#
# Community forwarders are not ours to configure, so the level policy is
# enforced at ingest. The behaviour asserted here was verified against a real
# Elasticsearch 8.17.4 container: `final_pipeline` survives a client passing
# `?pipeline=_none`, whereas `default_pipeline` does not.
# --------------------------------------------------------------------------- #
from saorsa_deploy.provisioning.es_log_filter import (  # noqa: E402
    LOG_FILTER_PIPELINE,
    build_drop_condition,
    build_pipeline_body,
    levels_to_drop,
)


@pytest.mark.parametrize(
    "min_level,expected",
    [
        ("info", ["TRACE", "DEBUG"]),
        ("debug", ["TRACE"]),
        ("trace", []),
    ],
)
def test_levels_to_drop(min_level, expected):
    # Same derivation as the node telegraf forwarder's --drop-log-levels: drop
    # only what is strictly more verbose than the configured minimum.
    assert levels_to_drop(min_level) == expected


def test_warn_and_error_are_never_dropped():
    for level in ("info", "debug", "trace"):
        dropped = levels_to_drop(level)
        assert "WARN" not in dropped and "ERROR" not in dropped


def test_invalid_min_level_is_refused():
    with pytest.raises(ValueError, match="must be one of"):
        levels_to_drop("warn")


def test_drop_condition_is_case_insensitive():
    # A forwarder emitting "debug" rather than "DEBUG" would otherwise sail
    # straight past the filter this exists to enforce.
    assert "toUpperCase()" in build_drop_condition(["DEBUG"])


def test_drop_condition_keeps_documents_with_no_level():
    # Losing logs of an unanticipated shape is worse than keeping a few we did
    # not mean to, so the condition is null-guarded.
    assert "ctx.level != null" in build_drop_condition(["DEBUG"])


def test_trace_keeps_the_pipeline_but_drops_nothing():
    # The index template names the pipeline, and an index whose final_pipeline
    # refers to a missing pipeline rejects every write — so "keep everything"
    # must still leave a pipeline in place. It also still strips the host and
    # stamps attribution; only the level filter is conditional.
    body = build_pipeline_body("trace")
    assert not any("drop" in p for p in body["processors"])
    assert body["processors"], "the pipeline must still exist and do its other work"


def test_pipeline_is_attached_as_final_not_default():
    # default_pipeline is overridden by a client passing ?pipeline=, so it
    # enforces nothing against a forwarder we do not control. Verified against
    # ES 8.17.4: the bypass works on default_pipeline and fails on final.
    script = _pub()._build_setup_script()
    assert "'index.final_pipeline': LOG_FILTER_PIPELINE" in script
    assert "default_pipeline" not in script


def test_setup_script_creates_the_pipeline_before_the_template():
    script = _pub()._build_setup_script()
    assert script.index(f"'{LOG_FILTER_PIPELINE}'") < script.index("beta-node-logs")


@pytest.mark.parametrize("level", ["info", "debug", "trace"])
def test_setup_script_renders_for_every_level(level):
    p = ElasticProvisioner(
        "10.0.0.1", SSH, "B", public_ingest=True, ingest_hostname=HOST,
        min_log_level=level,
    )
    compile(p._build_setup_script(), "setup", "exec")


def test_bad_min_level_fails_before_any_vm_is_touched():
    with pytest.raises(ValueError, match="must be one of"):
        ElasticProvisioner(
            "10.0.0.1", SSH, "B", public_ingest=True, ingest_hostname=HOST,
            min_log_level="verbose",
        )


def test_private_instance_gets_no_ingest_pipeline():
    script = ElasticProvisioner("10.0.0.1", SSH, "STG-01")._build_setup_script()
    assert LOG_FILTER_PIPELINE not in script
    assert "final_pipeline" not in script


def test_min_log_level_is_threaded_into_the_provision_step():
    m = _manifest(min_log_level="debug")
    assert "--min-log-level" in build_deploy_steps(m)[1].argv


def test_min_log_level_omitted_when_unset():
    # Keeps the argv (and so the up journal) stable for a manifest that does not
    # set it.
    assert "--min-log-level" not in build_deploy_steps(_manifest())[1].argv


def test_proxy_strips_a_client_supplied_pipeline():
    # Belt and braces with final_pipeline: naming a pipeline is the documented
    # way to bypass a default_pipeline, so the parameter never reaches ES.
    assert "uri query -pipeline" in build_caddyfile(HOST)


# --------------------------------------------------------------------------- #
# the ingest contract the beta log-forwarding daemon (V2-1021) writes against
#
# Every assertion here was verified against a real Elasticsearch 8.17.4 with
# security enabled, using a key minted from the same role_descriptors the
# `elastic mint-key` command sends.
# --------------------------------------------------------------------------- #
def test_documents_are_timestamped_with_at_timestamp():
    # `@timestamp`, not `timestamp`: the Elasticsearch convention, Kibana's
    # default time field, and mandatory if this index ever becomes a data
    # stream. Renaming it after community forwarders exist would break them all
    # at once, so it is pinned before anything ships.
    script = _pub()._build_setup_script()
    assert "'@timestamp': {'type': 'date'}" in script


def test_daemon_identity_fields_are_keywords():
    # Mapped explicitly rather than left to dynamic mapping, which would make
    # them text+keyword subfields; `beta-health-verdict` aggregates on these.
    script = _pub()._build_setup_script()
    for field in ("node_id", "service", "binary_version", "channel", "os", "arch"):
        assert f"'{field}': {{'type': 'keyword'}}" in script


def test_ingest_role_permits_create_but_not_index():
    # Verified: with `create_doc`, a bulk `index` action returns a per-item 403
    # security_exception while `create` returns 201. The daemon must use
    # `create`. Granting `index` would also permit silently overwriting another
    # participant's document by _id.
    script = _pub()._build_setup_script()
    role_block = script.split(BETA_INGEST_ROLE)[-1]
    assert "'create_doc'" in role_block
    assert "'index'" not in role_block
    assert "'write'" not in role_block


# --------------------------------------------------------------------------- #
# attribution and PII, enforced in the ingest pipeline
#
# Verified against ES 8.17.4: a key whose metadata says "mallory", sending
# `beta_user: alice` and a hostname, produced a document reading "mallory" with
# no host field.
# --------------------------------------------------------------------------- #
def _processors(min_level="info"):
    return [list(p)[0] for p in build_pipeline_body(min_level)["processors"]]


def test_hostname_is_stripped_at_ingest():
    # Community machines' hostnames routinely contain the owner's real name, and
    # a forwarder we do not control cannot be relied on to withhold it — so this
    # is enforced, not requested.
    body = build_pipeline_body("info")
    removes = [p["remove"]["field"] for p in body["processors"] if "remove" in p]
    assert "host" in removes


def test_client_supplied_attribution_is_removed_before_being_stamped():
    # Overwriting alone suffices only while every key carries metadata; a key
    # minted without it would leave the client's claimed value standing, i.e.
    # silently forgeable. Removing first fails closed.
    order = _processors()
    assert order.index("remove") < order.index("set_security_user")


def test_attribution_comes_from_the_api_key():
    body = build_pipeline_body("info")
    sets = {p["set"]["field"]: p["set"]["copy_from"] for p in body["processors"] if "set" in p}
    assert sets["beta_user"] == "_sec.api_key.metadata.beta_user"
    assert sets["beta_key_id"] == "_sec.api_key.id"


def test_the_security_scratch_field_is_removed_again():
    # Otherwise every document carries a nested _sec blob forever.
    body = build_pipeline_body("info")
    removes = [p["remove"]["field"] for p in body["processors"] if "remove" in p]
    assert "_sec" in removes
    assert _processors().index("set_security_user") < len(_processors()) - 1


@pytest.mark.parametrize("min_level", ["info", "debug", "trace"])
def test_pii_and_attribution_apply_at_every_level(min_level):
    # Only the drop processor is conditional on the level; stripping the host and
    # stamping attribution must happen regardless.
    order = _processors(min_level)
    assert "set_security_user" in order
    assert order.count("remove") >= 3


def _beta_template_block(script: str) -> str:
    """Just the beta index template.

    Scoped deliberately: the same script also writes the testnet node/client
    templates, which legitimately map `host` — asserting over the whole script
    would match those and prove nothing about the beta index.
    """
    return script.split("# 5b.")[1].split("# 5c.")[0]


def test_host_is_not_mapped_since_it_is_stripped():
    beta = _beta_template_block(_pub()._build_setup_script())
    assert "'host'" not in beta
    assert "'beta_key_id': {'type': 'keyword'}" in beta


def test_the_testnet_templates_still_map_host():
    # The pre-existing node/client templates are unaffected by the beta index's
    # privacy handling — their hosts are our own VMs.
    script = _pub()._build_setup_script()
    assert "'host': {'type': 'keyword'}" in script


def test_read_key_covers_the_beta_index_on_a_public_instance():
    # The beta index is the only data a public-ingest instance holds, and
    # `beta-health-verdict` queries it with this key — a key that cannot see it
    # is useless. Found live: the key was scoped to the testnet patterns only.
    assert "beta-nodes-*" in _pub()._read_index_patterns()


def test_read_key_is_unchanged_on_a_private_instance():
    priv = ElasticProvisioner("10.0.0.1", SSH, "STG-01")
    assert priv._read_index_patterns() == [
        "telegraf-ant-nodes-*",
        "telegraf-ant-clients-*",
    ]


# --------------------------------------------------------------------------- #
# inventory generation
# --------------------------------------------------------------------------- #
from saorsa_deploy.cmd.generate_inventory import (  # noqa: E402
    INVENTORY_SCHEMA_VERSION,
    build_elasticsearch_config,
    build_elasticsearch_inventory,
)
from saorsa_deploy.inventory import SUPPORTED_SCHEMA_VERSIONS  # noqa: E402

_STATE = {
    "name": "ant-prod-beta-01",
    "region": "lon1",
    "provider": "digitalocean",
    "vm_ip": "203.0.113.10",
    "public_ingest": True,
    "elasticsearch_url": "https://logs.autonomi.com",
    "elasticsearch_ingest_hostname": "logs.autonomi.com",
    "elasticsearch_index_pattern": "beta-nodes-*",
    "elasticsearch_api_key": "cmVhZC1rZXk=",
    "elasticsearch_password": "SUPER-SECRET-ADMIN",
    "elasticsearch_username": "ant-log-writer",
    "elasticsearch_retention_days": 14,
    "elasticsearch_min_log_level": "info",
}


def _inv():
    return build_elasticsearch_inventory(
        "ant-prod-beta-01", elastic_state=_STATE, ssh_key=SSH
    )


def test_inventory_describes_the_es_vm():
    inv = _inv()
    assert inv["schema_version"] == INVENTORY_SCHEMA_VERSION
    assert inv["node_vms"] == [] and inv["client_vms"] == []
    vms = inv["observability_vms"]
    assert len(vms) == 1 and vms[0]["role"] == "elasticsearch"
    assert vms[0]["ip"] == "203.0.113.10"


def test_inventory_never_carries_the_admin_password():
    # This file is shared with other agents. The admin password is effectively
    # the superuser's (the provisioner sets both from one secret), so it must
    # not travel in it — anyone entitled to it has SSH and `elastic info`.
    assert "SUPER-SECRET-ADMIN" not in json.dumps(_inv())


def test_inventory_carries_the_read_key():
    assert _inv()["elasticsearch_config"]["read"]["api_key"] == "cmVhZC1rZXk="


def test_read_section_does_not_point_at_the_public_url():
    # Verified live: the read key against the public URL returns 404 at the
    # proxy, which reads as a missing index rather than a blocked path. Pointing
    # a consumer there would send them debugging the wrong thing.
    read = _inv()["elasticsearch_config"]["read"]
    assert read["url"] == "http://localhost:9200"
    assert "logs.autonomi.com" not in read["url"]
    assert "ssh" in read["tunnel_command"]
    assert "CANNOT" in read["note"]


def test_ingest_section_states_the_create_only_rule():
    ingest = _inv()["elasticsearch_config"]["ingest"]
    assert ingest["bulk_action"] == "create"
    assert ingest["time_field"] == "@timestamp"
    assert ingest["url"] == "https://logs.autonomi.com"


def test_inventory_reports_the_instances_own_policy():
    cfg = _inv()["elasticsearch_config"]
    assert cfg["retention_days"] == 14
    assert cfg["ingest"]["min_log_level"] == "info"


def test_policy_fields_absent_rather_than_guessed_on_older_state():
    # An instance provisioned before those fields were recorded must not have
    # the CLI defaults reported back as though they were its actual settings.
    old = {k: v for k, v in _STATE.items() if not k.startswith("elasticsearch_retention")}
    old.pop("elasticsearch_min_log_level")
    cfg = build_elasticsearch_config(old, SSH)
    assert cfg["retention_days"] is None
    assert cfg["ingest"]["min_log_level"] == ""


def test_loader_accepts_the_new_schema_version():
    assert INVENTORY_SCHEMA_VERSION in SUPPORTED_SCHEMA_VERSIONS


def test_up_generates_an_inventory_as_a_best_effort_final_step():
    steps = build_deploy_steps(_manifest())
    assert steps[-1].argv[:2] == ["deployments", "generate-inventory"]
    # The instance is already up and billing by then; failing to describe it
    # must not fail the run.
    assert steps[-1].best_effort is True
