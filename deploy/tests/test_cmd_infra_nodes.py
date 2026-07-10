from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.infra_nodes import cmd_infra_nodes
from saorsa_deploy.terraform import TerraformResult


def _args(**overrides):
    defaults = {
        "name": "DEV-02",
        "attached_volume_size": 20,
        "do_vm_count": 1,
        "do_regions": 1,
        "do_regions_list": None,
        "do_droplet_size": "s-2vcpu-4gb",
        "hetzner_vm_count": 0,
        "hetzner_regions": 0,
        "hetzner_regions_list": None,
        "hetzner_server_type": "cpx22",
        "vultr_vm_count": 0,
        "vultr_regions": 0,
        "vultr_regions_list": None,
        "vultr_plan": "vc2-2c-4gb",
        "ovh_vm_count": 0,
        "ovh_regions": 0,
        "ovh_regions_list": None,
        "ovh_flavor": "b3-16",
        "ovh_3az_vm_count": 0,
        "ovh_3az_regions": 1,
        "ovh_3az_regions_list": None,
        "ovh_3az_flavor": "b3-16",
        "do_attached_volume_size": None,
        "hetzner_attached_volume_size": None,
        "vultr_attached_volume_size": None,
        "ovh_attached_volume_size": None,
        "force": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _state_with_bootstraps():
    return {
        "name": "DEV-02",
        "bootstrap_nodes": [
            {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            {"ip": "2.2.2.2", "region": "nyc1", "bootstrap_index": 2},
            {"ip": "3.3.3.3", "region": "ams3", "bootstrap_index": 3},
        ],
        "regions": [],
        "vm_ips": {},
        "vm_ids": {},
    }


@pytest.fixture
def patches():
    with (
        patch("saorsa_deploy.cmd.infra_nodes.record_event") as p_record_event,
        patch("saorsa_deploy.cmd.infra_nodes.record_status") as p_record_status,
        patch("saorsa_deploy.cmd.infra_nodes.load_deployment_state") as p_load_state,
        patch(
            "saorsa_deploy.cmd.infra_nodes.update_deployment_state"
        ) as p_update_state,
        patch(
            "saorsa_deploy.cmd.infra_nodes.check_terraform_state_exists"
        ) as p_check_state,
        patch(
            "saorsa_deploy.cmd.infra_nodes.delete_terraform_state"
        ) as p_delete_state,
        patch(
            "saorsa_deploy.cmd.infra_nodes.execute_terraform_runs"
        ) as p_execute,
    ):
        p_check_state.return_value = False
        p_execute.return_value = [
            TerraformResult(
                success=True,
                provider="digitalocean",
                region="lon1",
                outputs={"vm_ips": ["10.0.0.1"], "vm_ids": [1234]},
            ),
        ]
        yield SimpleNamespace(
            record_event=p_record_event,
            record_status=p_record_status,
            load_state=p_load_state,
            update_state=p_update_state,
            check_state=p_check_state,
            delete_state=p_delete_state,
            execute=p_execute,
        )


class TestRefusesWithoutBootstraps:
    def test_errors_when_no_deployment_state(self, patches):
        patches.load_state.side_effect = RuntimeError("No deployment state found")

        with pytest.raises(SystemExit):
            cmd_infra_nodes(_args())

        patches.execute.assert_not_called()

    def test_errors_when_state_has_no_bootstrap_nodes(self, patches):
        patches.load_state.return_value = {"bootstrap_nodes": []}

        with pytest.raises(SystemExit):
            cmd_infra_nodes(_args())

        patches.execute.assert_not_called()


class TestPartialStateBugFix:
    """The bug this whole split exists to fix: destroy couldn't clean up
    main-VM orphans because infra used to save regions=[] upfront. The new
    `nodes infra` must write regions BEFORE terraform apply."""

    def test_writes_regions_before_running_terraform(self, patches):
        patches.load_state.return_value = _state_with_bootstraps()

        execute_seen_updates = []

        def _track(*_a, **_kw):
            execute_seen_updates.append(patches.update_state.call_count)
            return patches.execute.return_value

        patches.execute.side_effect = _track

        cmd_infra_nodes(_args())

        # update_deployment_state must have been called at least once before
        # terraform started, and that call must include populated regions.
        assert execute_seen_updates == [1]
        first_update = patches.update_state.call_args_list[0]
        updates = first_update.args[1]
        assert "regions" in updates
        assert updates["regions"] == [["digitalocean", "lon1"]]
        assert "terraform_variables" in updates

    def test_updates_vm_ips_after_terraform(self, patches):
        patches.load_state.return_value = _state_with_bootstraps()

        cmd_infra_nodes(_args())

        # Two update calls: first with regions, second with vm_ips/vm_ids.
        assert patches.update_state.call_count == 2
        second_updates = patches.update_state.call_args_list[1].args[1]
        assert "vm_ips" in second_updates
        assert second_updates["vm_ips"] == {"digitalocean/lon1": ["10.0.0.1"]}


class TestStaleMainVmCheck:
    def test_errors_when_stale_state_without_force(self, patches):
        patches.load_state.return_value = _state_with_bootstraps()
        patches.check_state.return_value = True

        with pytest.raises(SystemExit):
            cmd_infra_nodes(_args(force=False))

        patches.execute.assert_not_called()

    def test_force_purges_stale_state_then_applies(self, patches):
        patches.load_state.return_value = _state_with_bootstraps()
        patches.check_state.return_value = True

        execute_calls = []

        def _track(configs, action="apply"):
            execute_calls.append(action)
            return patches.execute.return_value

        patches.execute.side_effect = _track

        cmd_infra_nodes(_args(force=True))

        assert execute_calls == ["destroy", "apply"]

    def test_skips_stale_check_on_rerun_with_existing_regions(self, patches):
        state = _state_with_bootstraps()
        state["regions"] = [["digitalocean", "lon1"]]
        patches.load_state.return_value = state

        cmd_infra_nodes(_args())

        patches.check_state.assert_not_called()


class TestRegionsOverride:
    def test_do_regions_list_pins_exact_regions(self, patches):
        patches.load_state.return_value = _state_with_bootstraps()
        patches.execute.return_value = [
            TerraformResult(
                success=True,
                provider="digitalocean",
                region="lon1",
                outputs={"vm_ips": ["10.0.0.1"], "vm_ids": [1]},
            ),
            TerraformResult(
                success=True,
                provider="digitalocean",
                region="ams3",
                outputs={"vm_ips": ["10.0.0.2"], "vm_ids": [2]},
            ),
        ]

        cmd_infra_nodes(
            _args(
                do_vm_count=5,
                do_regions=1,
                do_regions_list="lon1,ams3",
            )
        )

        # First state save: regions came from the override, not the default
        # top-of-list ordering.
        first_update = patches.update_state.call_args_list[0].args[1]
        assert first_update["regions"] == [
            ["digitalocean", "lon1"],
            ["digitalocean", "ams3"],
        ]

    def test_vultr_regions_list_feeds_preflight(self, patches, monkeypatch):
        patches.load_state.return_value = _state_with_bootstraps()
        patches.execute.return_value = [
            TerraformResult(
                success=True,
                provider="vultr",
                region="ewr",
                outputs={"vm_ips": ["10.0.0.1"], "vm_ids": ["a"]},
            ),
        ]
        monkeypatch.setenv("VULTR_API_KEY", "t")

        with patch(
            "saorsa_deploy.cmd.infra_nodes.resolve_vultr_regions"
        ) as mock_resolve:
            mock_resolve.return_value = (["ewr", "ord", "lax"], [])
            cmd_infra_nodes(
                _args(
                    do_vm_count=0, do_regions=0,
                    vultr_vm_count=5,
                    vultr_regions=3,
                    vultr_regions_list="ewr,ord,lax",
                    vultr_plan="vc2-8c-32gb",
                )
            )
            kwargs = mock_resolve.call_args.kwargs
            assert kwargs["preferred"] == ["ewr", "ord", "lax"]
            # Fallback pool is the same list — no silent substitution.
            assert kwargs["fallback_pool"] == ["ewr", "ord", "lax"]
            assert kwargs["count"] == 3

    def test_unknown_region_in_override_errors_out(self, patches):
        patches.load_state.return_value = _state_with_bootstraps()

        with pytest.raises(SystemExit):
            cmd_infra_nodes(
                _args(
                    do_vm_count=5,
                    do_regions_list="lon1,bogus-region",
                )
            )

        patches.execute.assert_not_called()


OVH_ENV = {
    "OVH_APPLICATION_KEY": "ak",
    "OVH_APPLICATION_SECRET": "as",
    "OVH_CONSUMER_KEY": "ck",
    "OVH_CLOUD_PROJECT_SERVICE": "project-id",
}


class TestOvhProvider:
    def test_missing_env_vars_error_out_before_terraform(self, patches, monkeypatch):
        patches.load_state.return_value = _state_with_bootstraps()
        for var in OVH_ENV:
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(SystemExit):
            cmd_infra_nodes(
                _args(do_vm_count=0, ovh_vm_count=2, ovh_regions_list="GRA9,UK1")
            )

        patches.execute.assert_not_called()

    def test_variables_include_flavor_service_name_and_name_template(
        self, patches, monkeypatch
    ):
        patches.load_state.return_value = _state_with_bootstraps()
        for var, value in OVH_ENV.items():
            monkeypatch.setenv(var, value)
        patches.execute.return_value = [
            TerraformResult(
                success=True,
                provider="ovh",
                region="GRA9",
                outputs={"vm_ips": ["10.0.0.9"], "vm_ids": ["uuid-1"]},
            ),
        ]

        cmd_infra_nodes(
            _args(
                do_vm_count=0,
                ovh_vm_count=2,
                ovh_regions_list="GRA9",
                ovh_flavor="b3-32",
            )
        )

        configs = patches.execute.call_args.args[0]
        assert len(configs) == 1
        cfg = configs[0]
        assert cfg.provider == "ovh"
        assert cfg.state_key == "saorsa-deploy/ovh-DEV-02-GRA9.tfstate"
        assert cfg.variables["flavor_name"] == "b3-32"
        assert cfg.variables["service_name"] == "project-id"
        # Testnet node VMs follow the NAME-ant-node-REGION-N convention, not
        # the module's migration-oriented "ovh" default.
        assert cfg.variables["name_template"] == "ant-node"

    def test_ovh_3az_routes_to_ovh_3az_module_with_zones(self, patches, monkeypatch):
        # ovh_3az must resolve to the ovh-3az module (state_key prefix), pass the
        # flavor + service_name, and inject the region's AZ list for round-robin.
        patches.load_state.return_value = _state_with_bootstraps()
        for var, value in OVH_ENV.items():
            monkeypatch.setenv(var, value)
        patches.execute.return_value = [
            TerraformResult(
                success=True,
                provider="ovh_3az",
                region="EU-WEST-PAR",
                outputs={"vm_ips": ["10.0.0.9"], "vm_ids": ["uuid-1"]},
            ),
        ]

        cmd_infra_nodes(
            _args(
                do_vm_count=0,
                ovh_3az_vm_count=2,
                ovh_3az_regions_list="EU-WEST-PAR",
                ovh_3az_flavor="b3-16",
            )
        )

        cfg = patches.execute.call_args.args[0][0]
        assert cfg.provider == "ovh_3az"
        assert cfg.state_key == "saorsa-deploy/ovh-3az-DEV-02-EU-WEST-PAR.tfstate"
        assert cfg.variables["flavor_name"] == "b3-16"
        assert cfg.variables["service_name"] == "project-id"
        assert cfg.variables["name_template"] == "ant-node"
        assert (
            cfg.variables["availability_zones"]
            == '["eu-west-par-a","eu-west-par-b","eu-west-par-c"]'
        )
        # ovh_3az has no block storage — the module has no attached_volume_size
        # variable, so it must never be passed.
        assert "attached_volume_size" not in cfg.variables

    def test_do_attached_volume_size_override_beats_global(self, patches, monkeypatch):
        # A per-provider --do-attached-volume-size overrides the global default.
        patches.load_state.return_value = _state_with_bootstraps()
        patches.execute.return_value = [
            TerraformResult(
                success=True, provider="digitalocean", region="nyc1",
                outputs={"vm_ips": ["10.0.0.9"], "vm_ids": ["uuid-1"]},
            ),
        ]
        cmd_infra_nodes(
            _args(do_vm_count=1, do_regions_list="nyc1",
                  attached_volume_size=20, do_attached_volume_size=250)
        )
        cfg = patches.execute.call_args.args[0][0]
        assert cfg.variables["attached_volume_size"] == "250"

    def test_state_records_flavor_name(self, patches, monkeypatch):
        patches.load_state.return_value = _state_with_bootstraps()
        for var, value in OVH_ENV.items():
            monkeypatch.setenv(var, value)
        patches.execute.return_value = [
            TerraformResult(
                success=True,
                provider="ovh",
                region="GRA9",
                outputs={"vm_ips": ["10.0.0.9"], "vm_ids": ["uuid-1"]},
            ),
        ]

        cmd_infra_nodes(
            _args(do_vm_count=0, ovh_vm_count=1, ovh_regions_list="GRA9")
        )

        first_update = patches.update_state.call_args_list[0].args[1]
        assert first_update["regions"] == [["ovh", "GRA9"]]
        providers_state = first_update["terraform_variables"]["providers"]
        assert providers_state["ovh"] == {
            "vm_count": 1,
            "regions": 1,
            "flavor_name": "b3-16",
        }


class TestPartialFailureRecordsVmIps:
    def test_vm_ips_for_successful_regions_are_saved_even_on_failure(self, patches):
        patches.load_state.return_value = _state_with_bootstraps()
        patches.execute.return_value = [
            TerraformResult(
                success=True,
                provider="digitalocean",
                region="lon1",
                outputs={"vm_ips": ["10.0.0.1"], "vm_ids": [1234]},
            ),
            TerraformResult(
                success=False,
                provider="hetzner",
                region="fsn1",
                stderr="boom",
            ),
        ]

        with pytest.raises(SystemExit):
            cmd_infra_nodes(_args(hetzner_vm_count=1, hetzner_regions=1))

        # regions were saved before apply; vm_ips for the one success were
        # recorded before the failure caused the exit.
        assert patches.update_state.call_count == 2
        second = patches.update_state.call_args_list[1].args[1]
        assert second["vm_ips"] == {"digitalocean/lon1": ["10.0.0.1"]}
