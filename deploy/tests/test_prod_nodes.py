"""Unit tests for the ``prod nodes`` profiling-node command helpers."""

from types import SimpleNamespace

import pytest

from saorsa_deploy.cmd.prod_nodes import (
    _PROVIDER_INFO,
    _build_node_infra_args,
    _parse_prod_bootstrap_nodes,
    _resolve_node_drop_log_levels,
)
from saorsa_deploy.prod_bootstraps import ProdBootstrapsError


def _deploy_args(provider, vm_size=None):
    return SimpleNamespace(
        name="PN",
        provider=provider,
        region="ord",
        vm_size=vm_size,
        vm_count=1,
        volume_size=100,
    )


@pytest.mark.parametrize("provider", ["digitalocean", "hetzner", "vultr", "ovh"])
def test_build_node_infra_args_no_duplicate_count(provider):
    # Regression: SimpleNamespace previously got the chosen provider's
    # *_vm_count both as an explicit zero and via the spread, raising
    # "got multiple values for keyword argument". Construction must succeed
    # and the chosen provider's count must win.
    ns = _build_node_infra_args(_deploy_args(provider, vm_size="custom-size"))
    prefix, size_var, _ = _PROVIDER_INFO[provider]
    # Every provider count attribute exists; only the chosen one is non-zero.
    counts = {
        p: getattr(ns, f"{pre}_vm_count")
        for p, (pre, _, _) in _PROVIDER_INFO.items()
    }
    assert getattr(ns, f"{prefix}_vm_count") == 1
    assert sum(counts.values()) == 1  # all others are 0
    assert getattr(ns, f"{prefix}_regions_list") == "ord"
    assert getattr(ns, size_var) == "custom-size"
    assert ns.attached_volume_size == 100
    assert ns.force is False


def test_build_node_infra_args_defaults_vm_size_per_provider():
    ns = _build_node_infra_args(_deploy_args("vultr", vm_size=None))
    _, size_var, default_size = _PROVIDER_INFO["vultr"]
    assert getattr(ns, size_var) == default_size


def test_parse_prod_bootstrap_nodes_basic():
    nodes, port = _parse_prod_bootstrap_nodes(["1.2.3.4:10000", "5.6.7.8:20000"])
    assert port == 10000
    assert nodes == [
        {"ip": "1.2.3.4", "port": 10000, "region": "prod", "bootstrap_index": 1},
        {"ip": "5.6.7.8", "port": 20000, "region": "prod", "bootstrap_index": 2},
    ]


def test_parse_prod_bootstrap_nodes_ipv6_uses_last_colon():
    # rpartition on the last ':' keeps an IPv6 host intact.
    nodes, port = _parse_prod_bootstrap_nodes(["2001:db8::1:10000"])
    assert port == 10000
    assert nodes[0]["ip"] == "2001:db8::1"
    assert nodes[0]["port"] == 10000


@pytest.mark.parametrize("bad", ["nohostport", "1.2.3.4:", "1.2.3.4:abc", ":10000"])
def test_parse_prod_bootstrap_nodes_rejects_malformed(bad):
    with pytest.raises(ProdBootstrapsError):
        _parse_prod_bootstrap_nodes([bad])


def test_provider_info_covers_all_four_providers():
    assert set(_PROVIDER_INFO) == {"digitalocean", "hetzner", "vultr", "ovh"}
    for prefix, size_var, default in _PROVIDER_INFO.values():
        assert isinstance(prefix, str) and prefix
        assert size_var.endswith(("_droplet_size", "_server_type", "_plan", "_flavor"))
        assert isinstance(default, str) and default


def _drop_args(log_level=None, override=None):
    return SimpleNamespace(log_level=log_level, elastic_drop_log_levels=override)


@pytest.mark.parametrize(
    "level,expected",
    [
        ("trace", ""),       # forward everything
        ("TRACE", ""),       # case-insensitive
        ("debug", "TRACE"),  # keep DEBUG, drop the noisier TRACE
        ("info", None),      # omit → forwarder default DEBUG,TRACE
        (None, None),        # unset → omit
    ],
)
def test_resolve_node_drop_log_levels_derives_from_log_level(level, expected):
    assert _resolve_node_drop_log_levels(_drop_args(log_level=level)) == expected


@pytest.mark.parametrize("override", ["", "DEBUG,TRACE", "TRACE"])
def test_resolve_node_drop_log_levels_explicit_override_wins(override):
    # An explicit --elastic-drop-log-levels beats any log_level-derived value,
    # including the empty-string "keep everything" escape hatch.
    assert (
        _resolve_node_drop_log_levels(
            _drop_args(log_level="info", override=override)
        )
        == override
    )
