"""`config validate` must reject region names the deploy tool does not know.

V2-1449: a run manifest naming DigitalOcean `nyc2` passed `config validate`
and `plan`, then `nodes infra` refused it after the build and half the
infrastructure existed. The manifest model now applies the same allowlist
check `nodes infra` and `bootstraps infra` apply.
"""

import pytest
from pydantic import ValidationError

from saorsa_deploy.config.models import Bootstraps, Nodes


def _nodes(regions_list):
    return {
        "per_vm": 1,
        "providers": {"digital_ocean": {"vm_count": 1, "regions_list": regions_list}},
    }


class TestNodesRegionsList:
    def test_known_regions_pass(self):
        Nodes.model_validate(_nodes(["lon1", "nyc2", "sfo2", "nyc3"]))

    def test_unknown_region_is_rejected_at_validation(self):
        with pytest.raises(ValidationError, match="unknown digitalocean region.*not-a-region"):
            Nodes.model_validate(_nodes(["lon1", "not-a-region"]))

    def test_error_names_the_manifest_path(self):
        with pytest.raises(ValidationError, match="nodes.providers.digital_ocean.regions_list"):
            Nodes.model_validate(_nodes(["bogus"]))

    def test_vultr_list_checked_against_vultr_allowlist(self):
        with pytest.raises(ValidationError, match="unknown vultr region.*nyc1"):
            Nodes.model_validate(
                {
                    "per_vm": 1,
                    "providers": {"vultr": {"vm_count": 1, "regions_list": ["ams", "nyc1"]}},
                }
            )

    def test_regions_count_form_is_not_affected(self):
        Nodes.model_validate(
            {"per_vm": 1, "providers": {"digital_ocean": {"vm_count": 1, "regions": 3}}}
        )


class TestBootstrapsRegionsList:
    def test_known_regions_pass(self):
        Bootstraps.model_validate({"count": 2, "regions_list": ["lon1", "nyc2"]})

    def test_unknown_region_is_rejected(self):
        with pytest.raises(
            ValidationError, match="bootstraps.regions_list: unknown digitalocean region"
        ):
            Bootstraps.model_validate({"count": 1, "regions_list": ["mars1"]})
