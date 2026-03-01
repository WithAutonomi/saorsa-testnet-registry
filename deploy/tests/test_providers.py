import pytest

from saorsa_deploy.providers import ProviderConfig, resolve_regions


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
def two_providers():
    return {
        "aws": ProviderConfig(
            name="aws",
            regions=["us-east-1", "eu-west-1", "ap-southeast-1"],
            default_region="us-east-1",
            tf_dir="aws",
            state_key_prefix="saorsa-deploy/aws",
        ),
        "digitalocean": ProviderConfig(
            name="digitalocean",
            regions=["lon1", "nyc1", "ams3", "sfo3", "sgp1"],
            default_region="lon1",
            tf_dir="digitalocean",
            state_key_prefix="saorsa-deploy/do",
        ),
    }


class TestResolveRegionsTestnet:
    def test_testnet_returns_default_provider_and_region(self, single_provider):
        result = resolve_regions("3", testnet=True, providers=single_provider)
        assert result == [("digitalocean", "lon1")]

    def test_testnet_ignores_region_counts(self, single_provider):
        result = resolve_regions("5", testnet=True, providers=single_provider)
        assert result == [("digitalocean", "lon1")]


class TestResolveRegionsSingleProvider:
    def test_selects_requested_number_of_regions(self, single_provider):
        result = resolve_regions("3", testnet=False, providers=single_provider)
        assert result == [
            ("digitalocean", "lon1"),
            ("digitalocean", "nyc1"),
            ("digitalocean", "ams3"),
        ]

    def test_single_region(self, single_provider):
        result = resolve_regions("1", testnet=False, providers=single_provider)
        assert result == [("digitalocean", "lon1")]

    def test_all_regions(self, single_provider):
        result = resolve_regions("5", testnet=False, providers=single_provider)
        assert len(result) == 5

    def test_too_many_regions_raises(self, single_provider):
        with pytest.raises(ValueError, match="only 5 are available"):
            resolve_regions("10", testnet=False, providers=single_provider)


class TestResolveRegionsMultiProvider:
    def test_single_count_applies_to_all_providers(self, two_providers):
        result = resolve_regions("2", testnet=False, providers=two_providers)
        assert ("aws", "us-east-1") in result
        assert ("aws", "eu-west-1") in result
        assert ("digitalocean", "lon1") in result
        assert ("digitalocean", "nyc1") in result
        assert len(result) == 4

    def test_per_provider_counts(self, two_providers):
        result = resolve_regions("1,3", testnet=False, providers=two_providers)
        aws_regions = [(p, r) for p, r in result if p == "aws"]
        do_regions = [(p, r) for p, r in result if p == "digitalocean"]
        assert len(aws_regions) == 1
        assert len(do_regions) == 3

    def test_mismatched_counts_raises(self, two_providers):
        with pytest.raises(ValueError, match="Expected 2 region counts"):
            resolve_regions("1,2,3", testnet=False, providers=two_providers)


class TestResolveRegionsEdgeCases:
    def test_whitespace_in_counts(self, single_provider):
        result = resolve_regions(" 2 ", testnet=False, providers=single_provider)
        assert len(result) == 2

    def test_whitespace_in_comma_separated(self, two_providers):
        result = resolve_regions("1, 2", testnet=False, providers=two_providers)
        assert len(result) == 3
