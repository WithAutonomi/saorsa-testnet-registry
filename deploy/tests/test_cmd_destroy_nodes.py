from types import SimpleNamespace
from unittest.mock import patch

import pytest
from saorsa_db.models import DeploymentStatus

from saorsa_deploy.cmd.destroy_nodes import cmd_destroy_nodes
from saorsa_deploy.terraform import TerraformResult


def _args(**overrides):
    defaults = {"name": "DEV-02", "force": True}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def patches():
    with (
        patch("saorsa_deploy.cmd.destroy_nodes.record_event") as p_record_event,
        patch("saorsa_deploy.cmd.destroy_nodes.record_status") as p_record_status,
        patch("saorsa_deploy.cmd.destroy_nodes.load_deployment_state") as p_load_state,
        patch("saorsa_deploy.cmd.destroy_nodes.update_deployment_state") as p_update_state,
        patch("saorsa_deploy.cmd.destroy_nodes.delete_deployment_state") as p_delete_state,
        patch("saorsa_deploy.cmd.destroy_nodes.delete_terraform_state") as p_delete_tf,
        patch("saorsa_deploy.cmd.destroy_nodes.list_terraform_state_keys") as p_list_keys,
        patch("saorsa_deploy.cmd.destroy_nodes.load_terraform_state") as p_load_tf,
        patch("saorsa_deploy.cmd.destroy_nodes.execute_terraform_runs") as p_execute,
    ):
        p_list_keys.return_value = []
        p_load_tf.side_effect = Exception("no tfstate")
        p_execute.return_value = [
            TerraformResult(success=True, provider="digitalocean", region="lon1"),
        ]
        yield SimpleNamespace(
            record_event=p_record_event,
            record_status=p_record_status,
            load_state=p_load_state,
            update_state=p_update_state,
            delete_state=p_delete_state,
            delete_tf=p_delete_tf,
            list_keys=p_list_keys,
            load_tf=p_load_tf,
            execute=p_execute,
        )


class TestHappyPath:
    def test_destroys_nodes_and_keeps_bootstraps_in_state(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [["digitalocean", "lon1"]],
            "terraform_variables": {
                "name": "DEV-02",
                "attached_volume_size": "50",
                "providers": {
                    "digitalocean": {
                        "vm_count": 7,
                        "regions": 1,
                        "droplet_size": "s-8vcpu-16gb",
                    }
                },
            },
            "vm_ips": {"digitalocean/lon1": ["10.0.0.1"]},
        }

        cmd_destroy_nodes(_args())

        patches.execute.assert_called_once()
        patches.update_state.assert_called_once()
        updates = patches.update_state.call_args.args[1]
        assert updates["regions"] == []
        assert updates["vm_ips"] == {}
        assert updates["vm_ids"] == {}
        patches.delete_state.assert_not_called()

    def test_removes_state_file_when_no_bootstraps_left(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [],
            "regions": [["digitalocean", "lon1"]],
            "terraform_variables": {"name": "DEV-02", "providers": {}},
        }

        cmd_destroy_nodes(_args())

        patches.delete_state.assert_called_once()
        patches.update_state.assert_not_called()


class TestStatusFlip:
    """The row must reach DESTROYED when the whole deployment goes, and must not
    when only the node tier does — otherwise a successful teardown is
    indistinguishable from one that stalled with resources still billing."""

    @staticmethod
    def _statuses(patches):
        return [c.args[1] for c in patches.record_status.call_args_list]

    def test_records_destroyed_when_whole_state_file_removed(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [],
            "regions": [["digitalocean", "lon1"]],
            "terraform_variables": {"name": "DEV-02", "providers": {}},
        }

        cmd_destroy_nodes(_args())

        assert self._statuses(patches) == [
            DeploymentStatus.DESTROYING,
            DeploymentStatus.DESTROYED,
        ]

    def test_stays_destroying_when_bootstraps_survive(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [["digitalocean", "lon1"]],
            "terraform_variables": {"name": "DEV-02", "providers": {}},
        }

        cmd_destroy_nodes(_args())

        assert self._statuses(patches) == [DeploymentStatus.DESTROYING]

    def test_does_not_record_destroyed_when_state_deletion_fails(self, patches):
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [],
            "regions": [["digitalocean", "lon1"]],
            "terraform_variables": {"name": "DEV-02", "providers": {}},
        }
        patches.delete_state.side_effect = Exception("s3 unavailable")

        cmd_destroy_nodes(_args())

        assert DeploymentStatus.DESTROYED not in self._statuses(patches)


class TestMissingStateFallback:
    def test_scans_s3_for_orphan_tfstate(self, patches):
        patches.load_state.side_effect = RuntimeError("No deployment state found")
        patches.list_keys.side_effect = lambda prefix: (
            ["saorsa-deploy/do-DEV-02-lon1.tfstate"]
            if prefix.startswith("saorsa-deploy/do-DEV-02-")
            else []
        )

        cmd_destroy_nodes(_args())

        patches.execute.assert_called_once()
        args, kwargs = patches.execute.call_args
        configs = args[0]
        assert len(configs) == 1
        assert configs[0].region == "lon1"

    def test_no_op_when_no_state_and_no_orphans(self, patches):
        patches.load_state.side_effect = RuntimeError("No deployment state found")
        patches.list_keys.return_value = []

        cmd_destroy_nodes(_args())

        patches.execute.assert_not_called()


class TestBeltAndBraces:
    def test_destroys_orphan_tfstate_not_in_state_regions(self, patches):
        """If state.regions lists lon1 but an extra orphan tfstate exists for
        nyc1 (e.g. partial save somewhere), `nodes destroy` should still tear it
        down."""
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [["digitalocean", "lon1"]],
            "terraform_variables": {
                "name": "DEV-02",
                "attached_volume_size": "50",
                "providers": {
                    "digitalocean": {
                        "vm_count": 1,
                        "regions": 1,
                        "droplet_size": "s-2vcpu-4gb",
                    }
                },
            },
        }
        patches.list_keys.side_effect = lambda prefix: (
            [
                "saorsa-deploy/do-DEV-02-lon1.tfstate",
                "saorsa-deploy/do-DEV-02-nyc1.tfstate",
            ]
            if prefix.startswith("saorsa-deploy/do-DEV-02-")
            else []
        )

        cmd_destroy_nodes(_args())

        patches.execute.assert_called_once()
        configs = patches.execute.call_args.args[0]
        regions = sorted(c.region for c in configs)
        assert regions == ["lon1", "nyc1"]


class TestOvhDestroy:
    OVH_ENV = {
        "OVH_APPLICATION_KEY": "ak",
        "OVH_APPLICATION_SECRET": "as",
        "OVH_CONSUMER_KEY": "ck",
        "OVH_CLOUD_PROJECT_SERVICE": "project-id",
    }

    def _ovh_state(self):
        return {
            "name": "DEV-02",
            "bootstrap_nodes": [
                {"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1},
            ],
            "regions": [["ovh", "GRA9"]],
            "terraform_variables": {
                "name": "DEV-02",
                "attached_volume_size": "50",
                "providers": {
                    "ovh": {
                        "vm_count": 2,
                        "regions": 1,
                        "flavor_name": "b3-32",
                    }
                },
            },
            "vm_ips": {"ovh/GRA9": ["10.0.0.9"]},
        }

    def test_missing_env_vars_error_out_before_terraform(self, patches, monkeypatch):
        patches.load_state.return_value = self._ovh_state()
        for var in self.OVH_ENV:
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(SystemExit):
            cmd_destroy_nodes(_args())

        patches.execute.assert_not_called()

    def test_destroy_variables_include_flavor_and_service_name(self, patches, monkeypatch):
        patches.load_state.return_value = self._ovh_state()
        for var, value in self.OVH_ENV.items():
            monkeypatch.setenv(var, value)
        patches.execute.return_value = [
            TerraformResult(success=True, provider="ovh", region="GRA9"),
        ]

        cmd_destroy_nodes(_args())

        configs = patches.execute.call_args.args[0]
        assert len(configs) == 1
        cfg = configs[0]
        assert cfg.state_key == "saorsa-deploy/ovh-DEV-02-GRA9.tfstate"
        assert cfg.variables["flavor_name"] == "b3-32"
        assert cfg.variables["service_name"] == "project-id"
        assert cfg.variables["name_template"] == "ant-node"


class TestRetiresTheLocalSnapshot:
    """The last tier going means the local inventory now names released IPs.

    V2-1530's `DEV-02` snapshot outlived its deployment by ten days and a
    phases script took its 14 addresses as a live fleet.
    """

    @staticmethod
    def _snapshot(root, name="DEV-02"):
        path = root / name / "inventory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'{{"testnet_id": "{name}", "created_at": "2026-10-03T21:13:45Z"}}')
        return path

    @staticmethod
    def _retired(path):
        return [p.name for p in path.parent.iterdir() if p.name.startswith("inventory.json.")]

    def test_retires_it_when_the_state_file_goes(self, patches, local_inventory_root):
        path = self._snapshot(local_inventory_root)
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [],
            "regions": [["digitalocean", "lon1"]],
            "terraform_variables": {"name": "DEV-02", "providers": {}},
        }

        cmd_destroy_nodes(_args())

        assert not path.exists()
        assert len(self._retired(path)) == 1

    def test_keeps_it_while_the_bootstraps_survive(self, patches, local_inventory_root):
        """A partial teardown updates the state rather than deleting it, and the
        surviving tier's addresses are still ours."""
        path = self._snapshot(local_inventory_root)
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [{"ip": "1.1.1.1", "region": "lon1", "bootstrap_index": 1}],
            "regions": [["digitalocean", "lon1"]],
            "terraform_variables": {"name": "DEV-02", "providers": {}},
            "vm_ips": {"digitalocean/lon1": ["10.0.0.1"]},
        }

        cmd_destroy_nodes(_args())

        assert path.exists()

    def test_keeps_it_when_the_state_deletion_fails(self, patches, local_inventory_root):
        path = self._snapshot(local_inventory_root)
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [],
            "regions": [["digitalocean", "lon1"]],
            "terraform_variables": {"name": "DEV-02", "providers": {}},
        }
        patches.delete_state.side_effect = Exception("s3 unavailable")

        cmd_destroy_nodes(_args())

        assert path.exists()

    def test_leaves_another_deployments_snapshot_alone(self, patches, local_inventory_root):
        other = self._snapshot(local_inventory_root, "DEV-01")
        patches.load_state.return_value = {
            "name": "DEV-02",
            "bootstrap_nodes": [],
            "regions": [["digitalocean", "lon1"]],
            "terraform_variables": {"name": "DEV-02", "providers": {}},
        }

        cmd_destroy_nodes(_args())

        assert other.exists()


class TestClientExtensionGuard:
    """`nodes destroy` refuses while a client extension stands.

    A client extension's VMs run their workload against this node fleet, so the
    fleet cannot go first. The scan answers "none" for every test via the
    `no_client_extensions` autouse fixture in conftest; these set their own.
    """

    def test_refuses_while_a_client_extension_stands(self, patches, monkeypatch):
        from saorsa_deploy import state as state_mod

        monkeypatch.setattr(state_mod, "list_client_extensions", lambda name: ["browsers"])

        with pytest.raises(SystemExit) as exc:
            cmd_destroy_nodes(_args(name="DEV-02"))

        assert exc.value.code == 2
        # Refused before the state load, so the orphan-tfstate path is covered.
        patches.load_state.assert_not_called()
        patches.execute.assert_not_called()
        patches.delete_state.assert_not_called()

    def test_refuses_when_the_scan_cannot_complete(self, patches, monkeypatch):
        # "Could not look" is not "nothing there". This is the case that was
        # reaching real S3 and failing CI.
        from saorsa_deploy import state as state_mod

        def boom(name):
            raise OSError("Unable to locate credentials")

        monkeypatch.setattr(state_mod, "list_client_extensions", boom)

        with pytest.raises(SystemExit) as exc:
            cmd_destroy_nodes(_args(name="DEV-02"))

        assert exc.value.code == 2
        patches.execute.assert_not_called()
