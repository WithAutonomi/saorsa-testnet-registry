"""Tests for manifest loading, extends-merge, --set/--name overrides, validation."""

import textwrap

import pytest

from saorsa_deploy.config import ManifestError, load_manifest, merge_manifest_dict
from saorsa_deploy.config.models import (
    DEFAULT_LOG_LEVEL,
    VALID_LOG_LEVELS,
    log_level_to_v_flag,
    log_level_to_verbosity,
)


@pytest.mark.parametrize(
    ("level", "verbosity", "flag"),
    [("info", 1, "-v"), ("debug", 2, "-vv"), ("trace", 3, "-vvv")],
)
def test_log_level_helpers(level, verbosity, flag):
    assert log_level_to_verbosity(level) == verbosity
    assert log_level_to_v_flag(level) == flag


def test_log_level_defaults_and_valid_set():
    assert DEFAULT_LOG_LEVEL == "info"
    assert VALID_LOG_LEVELS == ("info", "debug", "trace")


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(textwrap.dedent(body))
    return p


PROD_UL = """\
    name: PROD-UL-01
    kind: prod-uploaders
    purpose: "weather check"
    prod_uploaders:
      binary: { version: "0.2.4" }
      uploaders:
        - { region: lon1, vm_size: s-2vcpu-4gb, urls: /tmp/list.txt, payment: merkle }
"""


def test_load_valid_prod_uploaders(tmp_path):
    p = _write(tmp_path, "ul.yaml", PROD_UL)
    m = load_manifest(p)
    assert m.kind == "prod-uploaders"
    assert m.name == "PROD-UL-01"
    assert m.prod_uploaders.uploaders[0].region == "lon1"
    assert m.prod_uploaders.secret_key_env == "ARBITRUM_ONE_SECRET_KEY"  # default


def test_numeric_funding_coerced_to_str(tmp_path):
    body = """\
        name: T
        kind: testnet
        purpose: p
        testnet:
          binaries: { ant_node: { version: "1" }, ant: { version: "1" } }
          nodes:
            per_vm: 5
            providers: { digital_ocean: { vm_count: 1 } }
          clients:
            uploaders:
              - { provider: vultr, region: ewr, file_size_mb: 10, ant: 1000, eth: 0.1 }
            downloaders:
              - { provider: digital_ocean, region: lon1 }
    """
    m = load_manifest(_write(tmp_path, "t.yaml", body))
    up = m.testnet.clients.uploaders[0]
    assert up.ant == "1000" and up.eth == "0.1"


# --------------------------------------------------------------------------- #
# extends
# --------------------------------------------------------------------------- #
BASE = """\
    kind: prod-uploaders
    purpose: "base purpose"
    ssh_key_path: ~/.ssh/ant_deploy
    prod_uploaders:
      binary: { version: "0.2.4" }
      monitoring_volume_size: 50
      uploaders:
        - { region: lon1, vm_size: s-2vcpu-4gb, urls: /tmp/a.txt }
        - { region: nyc1, vm_size: s-2vcpu-4gb, urls: /tmp/b.txt }
"""


def test_extends_deep_merge_and_scalar_override(tmp_path):
    _write(tmp_path, "base.yaml", BASE)
    child = """\
        extends: base.yaml
        name: PROD-UL-09
        purpose: "child override"
        prod_uploaders:
          monitoring_volume_size: 100
    """
    m = load_manifest(_write(tmp_path, "child.yaml", child))
    assert m.name == "PROD-UL-09"
    assert m.purpose == "child override"  # scalar overridden
    assert m.ssh_key_path == "~/.ssh/ant_deploy"  # inherited
    assert m.prod_uploaders.monitoring_volume_size == 100  # deep-merged scalar
    assert m.prod_uploaders.binary.version == "0.2.4"  # inherited nested
    assert len(m.prod_uploaders.uploaders) == 2  # inherited list


def test_extends_list_is_replaced_not_appended(tmp_path):
    _write(tmp_path, "base.yaml", BASE)
    child = """\
        extends: base.yaml
        name: PROD-UL-10
        prod_uploaders:
          uploaders:
            - { region: sgp1, vm_size: s-2vcpu-4gb, urls: /tmp/only.txt }
    """
    m = load_manifest(_write(tmp_path, "child.yaml", child))
    # list-replace semantics: the single child entry fully replaces the base's two
    assert len(m.prod_uploaders.uploaders) == 1
    assert m.prod_uploaders.uploaders[0].region == "sgp1"


def test_extends_circular_raises(tmp_path):
    _write(tmp_path, "a.yaml", "extends: b.yaml\nname: A\nkind: prod-uploaders\npurpose: p\n")
    _write(tmp_path, "b.yaml", "extends: a.yaml\nname: B\nkind: prod-uploaders\npurpose: p\n")
    with pytest.raises(ManifestError, match="circular"):
        load_manifest(tmp_path / "a.yaml")


# --------------------------------------------------------------------------- #
# --set / --name
# --------------------------------------------------------------------------- #
def test_set_dotted_scalar_and_type_coercion(tmp_path):
    body = """\
        name: T
        kind: testnet
        purpose: p
        testnet:
          binaries: { ant_node: { version: "1" } }
          nodes:
            per_vm: 5
            nat_percent: 30
            providers: { digital_ocean: { vm_count: 1 } }
    """
    p = _write(tmp_path, "t.yaml", body)
    m = load_manifest(p, sets=["testnet.nodes.nat_percent=0", "testnet.nodes.per_vm=20"])
    assert m.testnet.nodes.nat_percent == 0  # coerced to int, not "0"
    assert m.testnet.nodes.per_vm == 20


def test_set_creates_nested_path(tmp_path):
    p = _write(tmp_path, "ul.yaml", PROD_UL)
    d = merge_manifest_dict(p, sets=["prod_uploaders.monitoring_volume_size=200"])
    assert d["prod_uploaders"]["monitoring_volume_size"] == 200


def test_name_override(tmp_path):
    p = _write(tmp_path, "ul.yaml", PROD_UL)
    m = load_manifest(p, name="PROD-UL-99")
    assert m.name == "PROD-UL-99"


def test_set_without_equals_raises(tmp_path):
    p = _write(tmp_path, "ul.yaml", PROD_UL)
    with pytest.raises(ManifestError, match="KEY=VALUE"):
        load_manifest(p, sets=["bad_no_equals"])


# --------------------------------------------------------------------------- #
# validation failures
# --------------------------------------------------------------------------- #
def test_unknown_kind_rejected(tmp_path):
    body = "name: X\nkind: not-a-kind\npurpose: p\n"
    with pytest.raises(ManifestError, match="unknown kind"):
        load_manifest(_write(tmp_path, "x.yaml", body))


def test_kind_without_matching_block_rejected(tmp_path):
    body = "name: X\nkind: prod-uploaders\npurpose: p\n"
    with pytest.raises(ManifestError, match="requires a top-level `prod_uploaders:`"):
        load_manifest(_write(tmp_path, "x.yaml", body))


def test_binary_version_and_branch_mutually_exclusive(tmp_path):
    body = """\
        name: X
        kind: prod-uploaders
        purpose: p
        prod_uploaders:
          binary: { version: "0.2.4", branch: feat-x }
          uploaders:
            - { region: lon1, vm_size: s-2vcpu-4gb, urls: /tmp/a.txt }
    """
    with pytest.raises(ManifestError, match="either `version` or `branch`"):
        load_manifest(_write(tmp_path, "x.yaml", body))


def test_unknown_key_rejected(tmp_path):
    body = """\
        name: X
        kind: prod-uploaders
        purpose: p
        prod_uplaoders:        # typo
          binary: { version: "0.2.4" }
    """
    with pytest.raises(ManifestError):
        load_manifest(_write(tmp_path, "x.yaml", body))


def test_clients_require_both_roles(tmp_path):
    body = """\
        name: T
        kind: testnet
        purpose: p
        testnet:
          binaries: { ant_node: { version: "1" }, ant: { version: "1" } }
          nodes:
            per_vm: 5
            providers: { digital_ocean: { vm_count: 1 } }
          clients:
            uploaders:
              - { provider: vultr, region: ewr, file_size_mb: 10 }
    """
    with pytest.raises(ManifestError, match="uploader AND one downloader"):
        load_manifest(_write(tmp_path, "t.yaml", body))


def test_testnet_clients_require_ant_binary(tmp_path):
    body = """\
        name: T
        kind: testnet
        purpose: p
        testnet:
          binaries: { ant_node: { version: "1" } }
          nodes:
            per_vm: 5
            providers: { digital_ocean: { vm_count: 1 } }
          clients:
            uploaders:
              - { provider: vultr, region: ewr, file_size_mb: 10 }
            downloaders:
              - { provider: digital_ocean, region: lon1 }
    """
    with pytest.raises(ManifestError, match="binaries.ant is required"):
        load_manifest(_write(tmp_path, "t.yaml", body))


# --------------------------------------------------------------------------- #
# bootstrap_cache
# --------------------------------------------------------------------------- #
def test_bootstrap_cache_defaults_to_none_everywhere(tmp_path):
    m = load_manifest(_write(tmp_path, "ul.yaml", PROD_UL))
    assert m.prod_uploaders.uploaders[0].bootstrap_cache is None

    body = """\
        name: T
        kind: testnet
        purpose: p
        testnet:
          binaries: { ant_node: { version: "1" }, ant: { version: "1" } }
          nodes:
            per_vm: 5
            providers: { digital_ocean: { vm_count: 1 } }
          clients:
            uploaders:
              - { provider: vultr, region: ewr, file_size_mb: 10 }
            downloaders:
              - { provider: digital_ocean, region: lon1 }
    """
    m = load_manifest(_write(tmp_path, "t.yaml", body))
    assert m.testnet.clients.uploaders[0].bootstrap_cache is None
    assert m.testnet.clients.downloaders[0].bootstrap_cache is None


def test_bootstrap_cache_parsed_on_testnet_clients(tmp_path):
    body = """\
        name: T
        kind: testnet
        purpose: p
        testnet:
          binaries: { ant_node: { version: "1" }, ant: { version: "1" } }
          nodes:
            per_vm: 5
            providers: { digital_ocean: { vm_count: 1 } }
          clients:
            uploaders:
              - { provider: vultr, region: ewr, file_size_mb: 10, bootstrap_cache: false }
            downloaders:
              - { provider: digital_ocean, region: lon1, bootstrap_cache: true }
    """
    m = load_manifest(_write(tmp_path, "t.yaml", body))
    assert m.testnet.clients.uploaders[0].bootstrap_cache is False
    assert m.testnet.clients.downloaders[0].bootstrap_cache is True


def test_bootstrap_cache_parsed_on_prod_kinds(tmp_path):
    body = """\
        name: PROD-UL-01
        kind: prod-uploaders
        purpose: p
        prod_uploaders:
          binary: { version: "0.2.4" }
          uploaders:
            - { region: lon1, vm_size: s-2vcpu-4gb, urls: /tmp/l.txt, bootstrap_cache: false }
    """
    m = load_manifest(_write(tmp_path, "ul.yaml", body))
    assert m.prod_uploaders.uploaders[0].bootstrap_cache is False

    body = """\
        name: PROD-DL-01
        kind: prod-downloaders
        purpose: p
        prod_downloaders:
          spec: lon1
          binary: { version: "0.2.4" }
          bootstrap_cache: false
    """
    m = load_manifest(_write(tmp_path, "dl.yaml", body))
    assert m.prod_downloaders.bootstrap_cache is False

    body = """\
        name: PROD-CAN-01
        kind: prod-canary
        purpose: p
        prod_canary:
          binary: { version: "0.2.4" }
          upload_spec: lon1
          probes:
            - { region: lon1, count: 4 }
          urls_file: /tmp/urls.txt
          bootstrap_cache: false
    """
    m = load_manifest(_write(tmp_path, "can.yaml", body))
    assert m.prod_canary.bootstrap_cache is False


def test_bootstrap_cache_parsed_on_local_kinds(tmp_path):
    body = """\
        name: LOCAL-UL-01
        kind: prod-local-uploads
        purpose: p
        prod_local_uploads:
          binary: { version: "0.2.4" }
          urls: /tmp/l.txt
          bootstrap_cache: false
    """
    m = load_manifest(_write(tmp_path, "lu.yaml", body))
    assert m.prod_local_uploads.bootstrap_cache is False

    body = """\
        name: LOCAL-DL-01
        kind: prod-local-downloads
        purpose: p
        prod_local_downloads:
          binary: { version: "0.2.4" }
          addresses_path: /tmp/a.txt
          bootstrap_cache: false
    """
    m = load_manifest(_write(tmp_path, "ld.yaml", body))
    assert m.prod_local_downloads.bootstrap_cache is False


_TESTNET_KEEP = """\
    name: DEV-01
    kind: testnet
    purpose: p
    testnet:
      binaries:
        ant_node: {{ version: "1.0" }}
      bootstraps: {{ count: 1 }}
      nodes:
        per_vm: 5
        providers:
          digital_ocean: {{ vm_count: 2, regions: 3 }}
      elastic:
{elastic}
      monitoring:
{monitoring}
"""


def _keep_manifest(tmp_path, elastic: str, monitoring: str):
    return _write(
        tmp_path,
        "keep.yaml",
        _TESTNET_KEEP.format(elastic=elastic, monitoring=monitoring),
    )


def test_keep_on_destroy_defaults_false(tmp_path):
    p = _keep_manifest(tmp_path, "        enabled: true\n", "        enabled: true\n")
    m = load_manifest(p)
    assert m.testnet.elastic.keep_on_destroy is False
    assert m.testnet.monitoring.keep_on_destroy is False


def test_keep_on_destroy_parsed(tmp_path):
    p = _keep_manifest(
        tmp_path,
        "        enabled: true\n        keep_on_destroy: true\n",
        "        enabled: true\n        keep_on_destroy: true\n",
    )
    m = load_manifest(p)
    assert m.testnet.elastic.keep_on_destroy is True
    assert m.testnet.monitoring.keep_on_destroy is True


def test_elastic_keep_without_enabled_rejected(tmp_path):
    # elastic.enabled defaults False, so this would silently keep nothing.
    p = _keep_manifest(tmp_path, "        keep_on_destroy: true\n", "        enabled: true\n")
    with pytest.raises(ManifestError, match="elastic.keep_on_destroy requires"):
        load_manifest(p)


def test_monitoring_keep_without_enabled_rejected(tmp_path):
    p = _keep_manifest(
        tmp_path,
        "        enabled: true\n",
        "        enabled: false\n        keep_on_destroy: true\n",
    )
    with pytest.raises(ManifestError, match="monitoring.keep_on_destroy requires"):
        load_manifest(p)


def test_keep_on_destroy_on_extension(tmp_path):
    body = """\
        name: DEV-02
        kind: testnet-extension
        purpose: p
        extension:
          source: DEV-01
          binaries:
            ant_node: { version: "1.0" }
          nodes:
            per_vm: 5
            providers:
              digital_ocean: { vm_count: 2, regions: 3 }
          elastic: { enabled: true, keep_on_destroy: true }
          monitoring: { enabled: true, keep_on_destroy: true }
    """
    m = load_manifest(_write(tmp_path, "ext.yaml", body))
    assert m.extension.elastic.keep_on_destroy is True
    assert m.extension.monitoring.keep_on_destroy is True
