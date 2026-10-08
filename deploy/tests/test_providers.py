import pytest

from saorsa_deploy.providers import ProviderConfig, ProviderSpec, resolve_regions


@pytest.fixture
def single_provider():
    return {
        "digitalocean": ProviderConfig(
            name="digitalocean",
            regions=["lon1", "nyc1", "ams3", "sfo3", "sgp1"],
            default_region="lon1",
            tf_dir="digitalocean",
            state_key_prefix="saorsa-deploy/do",
        ),
    }


@pytest.fixture
def three_providers():
    return {
        "digitalocean": ProviderConfig(
            name="digitalocean",
            regions=["lon1", "nyc1", "ams3", "sfo3", "sgp1"],
            default_region="lon1",
            tf_dir="digitalocean",
            state_key_prefix="saorsa-deploy/do",
        ),
        "hetzner": ProviderConfig(
            name="hetzner",
            regions=["fsn1", "nbg1", "hel1", "ash", "hil"],
            default_region="fsn1",
            tf_dir="hetzner",
            state_key_prefix="saorsa-deploy/hz",
        ),
        "vultr": ProviderConfig(
            name="vultr",
            regions=["ewr", "ord", "dfw", "lax", "ams", "lhr"],
            default_region="ewr",
            tf_dir="vultr",
            state_key_prefix="saorsa-deploy/vl",
        ),
    }


class TestResolveRegionsSingleProvider:
    def test_selects_requested_number_of_regions(self, single_provider):
        specs = [ProviderSpec("digitalocean", vm_count=5, region_count=3)]
        result = resolve_regions(specs, providers=single_provider)
        assert result == [
            ("digitalocean", "lon1"),
            ("digitalocean", "nyc1"),
            ("digitalocean", "ams3"),
        ]

    def test_single_region(self, single_provider):
        specs = [ProviderSpec("digitalocean", vm_count=5, region_count=1)]
        result = resolve_regions(specs, providers=single_provider)
        assert result == [("digitalocean", "lon1")]

    def test_all_regions(self, single_provider):
        specs = [ProviderSpec("digitalocean", vm_count=5, region_count=5)]
        result = resolve_regions(specs, providers=single_provider)
        assert len(result) == 5

    def test_too_many_regions_raises(self, single_provider):
        specs = [ProviderSpec("digitalocean", vm_count=5, region_count=10)]
        with pytest.raises(ValueError, match="only 5 are available"):
            resolve_regions(specs, providers=single_provider)


class TestResolveRegionsMultiProvider:
    def test_multiple_providers_with_different_region_counts(self, three_providers):
        specs = [
            ProviderSpec("digitalocean", vm_count=10, region_count=3),
            ProviderSpec("hetzner", vm_count=5, region_count=2),
            ProviderSpec("vultr", vm_count=8, region_count=4),
        ]
        result = resolve_regions(specs, providers=three_providers)
        do_regions = [(p, r) for p, r in result if p == "digitalocean"]
        hz_regions = [(p, r) for p, r in result if p == "hetzner"]
        vl_regions = [(p, r) for p, r in result if p == "vultr"]
        assert len(do_regions) == 3
        assert len(hz_regions) == 2
        assert len(vl_regions) == 4

    def test_single_provider_in_multi_provider_setup(self, three_providers):
        specs = [ProviderSpec("hetzner", vm_count=5, region_count=2)]
        result = resolve_regions(specs, providers=three_providers)
        assert result == [("hetzner", "fsn1"), ("hetzner", "nbg1")]

    def test_zero_vm_count_skips_provider(self, three_providers):
        specs = [
            ProviderSpec("digitalocean", vm_count=10, region_count=2),
            ProviderSpec("hetzner", vm_count=0, region_count=3),
            ProviderSpec("vultr", vm_count=5, region_count=1),
        ]
        result = resolve_regions(specs, providers=three_providers)
        providers_used = {p for p, _ in result}
        assert "hetzner" not in providers_used
        assert len(result) == 3  # 2 DO + 1 Vultr


class TestResolveRegionsEdgeCases:
    def test_empty_specs_returns_empty(self, single_provider):
        result = resolve_regions([], providers=single_provider)
        assert result == []

    def test_negative_vm_count_skips_provider(self, single_provider):
        specs = [ProviderSpec("digitalocean", vm_count=-1, region_count=3)]
        result = resolve_regions(specs, providers=single_provider)
        assert result == []


class TestResolveRegionsOverride:
    def test_override_bypasses_default_ordering(self, single_provider):
        specs = [
            ProviderSpec(
                "digitalocean",
                vm_count=5,
                region_count=2,
                regions_override=["ams3", "lon1"],
            )
        ]
        result = resolve_regions(specs, providers=single_provider)
        assert result == [("digitalocean", "ams3"), ("digitalocean", "lon1")]

    def test_unknown_region_in_override_raises(self, single_provider):
        specs = [
            ProviderSpec(
                "digitalocean",
                vm_count=5,
                region_count=2,
                regions_override=["lon1", "bogus"],
            )
        ]
        with pytest.raises(ValueError, match="Unknown digitalocean region"):
            resolve_regions(specs, providers=single_provider)


class TestDigitalOceanAllowlistCoversSameMetroReplacements:
    """V2-1440 / V2-1449: DigitalOcean withdrew s-8vcpu-16gb from nyc1; the
    same-metro replacements must be known to the tool or `nodes infra` rejects
    a manifest the provider would accept."""

    def test_nyc2_nyc3_sfo2_are_known(self):
        from saorsa_deploy.providers import PROVIDERS

        for region in ("nyc2", "nyc3", "sfo2"):
            assert region in PROVIDERS["digitalocean"].regions

    def test_default_ordering_unchanged_for_first_nine(self):
        from saorsa_deploy.providers import PROVIDERS

        assert PROVIDERS["digitalocean"].regions[:9] == [
            "lon1",
            "nyc1",
            "ams3",
            "sfo3",
            "sgp1",
            "blr1",
            "fra1",
            "tor1",
            "syd1",
        ]
