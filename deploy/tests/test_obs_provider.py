from types import SimpleNamespace

import pytest

from saorsa_deploy.cmd import obs_provider


def test_default_provider_is_digitalocean():
    assert obs_provider.resolve_provider(SimpleNamespace()) == "digitalocean"
    assert (
        obs_provider.resolve_provider(SimpleNamespace(provider=None))
        == "digitalocean"
    )


def test_ovh_provider_resolves():
    assert (
        obs_provider.resolve_provider(SimpleNamespace(provider="ovh")) == "ovh"
    )


def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        obs_provider.resolve_provider(SimpleNamespace(provider="aws"))


def test_state_key_abbrev_keeps_do_prefix():
    # Existing DigitalOcean tfstate keys must stay on the "do-" prefix.
    assert obs_provider.state_key_abbrev("digitalocean") == "do"
    assert obs_provider.state_key_abbrev("ovh") == "ovh"


def test_default_regions():
    assert obs_provider.default_region("digitalocean") == "lon1"
    assert obs_provider.default_region("ovh") == "GRA9"


def test_default_sizes():
    assert obs_provider.default_influxdb_size("digitalocean") == "s-4vcpu-8gb"
    assert obs_provider.default_influxdb_size("ovh") == "b3-8"
    assert obs_provider.default_grafana_size("digitalocean") == "s-1vcpu-2gb"
    assert obs_provider.default_grafana_size("ovh") == "d2-4"
    assert obs_provider.default_elastic_size("digitalocean") == "s-4vcpu-8gb"
    assert obs_provider.default_elastic_size("ovh") == "b3-8"


def test_require_ovh_service_name_present(monkeypatch):
    from rich.console import Console

    monkeypatch.setenv("OVH_CLOUD_PROJECT_SERVICE", "proj-123")
    assert obs_provider.require_ovh_service_name(Console()) == "proj-123"


def test_require_ovh_service_name_missing_exits(monkeypatch):
    from rich.console import Console

    monkeypatch.delenv("OVH_CLOUD_PROJECT_SERVICE", raising=False)
    with pytest.raises(SystemExit):
        obs_provider.require_ovh_service_name(Console())
