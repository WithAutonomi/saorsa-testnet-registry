"""Unit tests for the ``prod nodes`` profiling-node command helpers."""

import pytest

from saorsa_deploy.cmd.prod_nodes import (
    _PROVIDER_INFO,
    _parse_prod_bootstrap_nodes,
)
from saorsa_deploy.prod_bootstraps import ProdBootstrapsError


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
