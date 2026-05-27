from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.destroy_clients import (
    _recover_clients_from_tfstate,
    cmd_destroy_clients,
)
from saorsa_deploy.terraform import TerraformResult


def _args(**overrides):
    defaults = {"name": "DEV-02", "force": True}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def patches():
    with (
        patch(
            "saorsa_deploy.cmd.destroy_clients.load_client_state"
        ) as p_load_client,
        patch(
            "saorsa_deploy.cmd.destroy_clients.delete_client_state"
        ) as p_delete_client,
        patch(
            "saorsa_deploy.cmd.destroy_clients.delete_terraform_state"
        ) as p_delete_tf,
        patch(
            "saorsa_deploy.cmd.destroy_clients.list_terraform_state_keys"
        ) as p_list_keys,
        patch(
            "saorsa_deploy.cmd.destroy_clients.load_terraform_state"
        ) as p_load_tf,
        patch(
            "saorsa_deploy.cmd.destroy_clients.run_terraform_destroy"
        ) as p_destroy,
    ):
        p_list_keys.return_value = []
        p_destroy.return_value = TerraformResult(
            success=True, provider="digitalocean", region="lon1"
        )
        yield SimpleNamespace(
            load_client=p_load_client,
            delete_client=p_delete_client,
            delete_tf=p_delete_tf,
            list_keys=p_list_keys,
            load_tf=p_load_tf,
            destroy=p_destroy,
        )


class TestFallbackWhenNoClientState:
    def test_scans_tfstate_when_client_state_missing(self, patches):
        patches.load_client.side_effect = RuntimeError("no client state")
        keys = [
            "saorsa-deploy/do-client-DEV-02-upload-lon1-1.tfstate",
            "saorsa-deploy/do-client-DEV-02-download-nyc3-3.tfstate",
        ]
        # Recovery queries each provider's prefix; only the DO prefix matches.
        patches.list_keys.side_effect = lambda prefix: [
            k for k in keys if k.startswith(prefix)
        ]
        patches.load_tf.return_value = {
            "resources": [
                {
                    "type": "digitalocean_droplet",
                    "name": "client_vm",
                    "instances": [
                        {"attributes": {"size": "s-2vcpu-4gb-intel"}}
                    ],
                }
            ]
        }

        cmd_destroy_clients(_args())

        assert patches.destroy.call_count == 2
        destroyed_regions = sorted(
            call.args[0].region for call in patches.destroy.call_args_list
        )
        assert destroyed_regions == ["lon1", "nyc3"]

    def test_no_op_when_no_state_and_no_tfstate(self, patches):
        patches.load_client.side_effect = RuntimeError("no client state")
        patches.list_keys.return_value = []

        # Should return cleanly, not raise SystemExit, so destroy runbooks
        # using `set -e` proceed past partially-deployed testnets.
        cmd_destroy_clients(_args())

        patches.destroy.assert_not_called()


class TestRecoverClientsFromTfstate:
    def test_parses_role_region_and_index_from_key(self):
        console = SimpleNamespace(print=lambda *a, **k: None)
        keys = [
            "saorsa-deploy/do-client-DEV-02-upload-lon1-1.tfstate",
            "saorsa-deploy/do-client-DEV-02-download-ams3-6.tfstate",
        ]
        with (
            patch(
                "saorsa_deploy.cmd.destroy_clients.list_terraform_state_keys",
                side_effect=lambda prefix: [
                    k for k in keys if k.startswith(prefix)
                ],
            ),
            patch(
                "saorsa_deploy.cmd.destroy_clients.load_terraform_state",
                return_value={
                    "resources": [
                        {
                            "type": "digitalocean_droplet",
                            "name": "client_vm",
                            "instances": [{"attributes": {"size": "s-2vcpu-4gb"}}],
                        }
                    ]
                },
            ),
        ):
            recovered = _recover_clients_from_tfstate("DEV-02", console)

        # Sorted by (provider, role, region, client_index).
        assert recovered == [
            {
                "role": "download",
                "provider": "digitalocean",
                "region": "ams3",
                "client_index": 6,
                "vm_size": "s-2vcpu-4gb",
                "ip": "",
            },
            {
                "role": "upload",
                "provider": "digitalocean",
                "region": "lon1",
                "client_index": 1,
                "file_size_mb": 0,
                "vm_size": "s-2vcpu-4gb",
                "ip": "",
            },
        ]

    def test_reads_actual_vm_size_from_tfstate(self):
        """For the sgp1 case specifically — the user had to use the Intel
        variant because the generic slug isn't in sgp1. Destroy must use the
        actual size, not a default that might not match."""
        console = SimpleNamespace(print=lambda *a, **k: None)
        with (
            patch(
                "saorsa_deploy.cmd.destroy_clients.list_terraform_state_keys",
                side_effect=lambda prefix: [
                    k
                    for k in [
                        "saorsa-deploy/do-client-DEV-02-upload-sgp1-7.tfstate"
                    ]
                    if k.startswith(prefix)
                ],
            ),
            patch(
                "saorsa_deploy.cmd.destroy_clients.load_terraform_state",
                return_value={
                    "resources": [
                        {
                            "type": "digitalocean_droplet",
                            "name": "client_vm",
                            "instances": [
                                {"attributes": {"size": "s-2vcpu-4gb-intel"}}
                            ],
                        }
                    ]
                },
            ),
        ):
            recovered = _recover_clients_from_tfstate("DEV-02", console)

        assert recovered[0]["vm_size"] == "s-2vcpu-4gb-intel"

    def test_skips_empty_tfstate(self):
        """The 181-byte init-only tfstate files from failed creates should be
        skipped, not propagated as fake clients."""
        console = SimpleNamespace(print=lambda *a, **k: None)
        with (
            patch(
                "saorsa_deploy.cmd.destroy_clients.list_terraform_state_keys",
                side_effect=lambda prefix: [
                    k
                    for k in [
                        "saorsa-deploy/do-client-DEV-02-upload-sgp1-7.tfstate"
                    ]
                    if k.startswith(prefix)
                ],
            ),
            patch(
                "saorsa_deploy.cmd.destroy_clients.load_terraform_state",
                side_effect=Exception("empty tfstate"),
            ),
        ):
            recovered = _recover_clients_from_tfstate("DEV-02", console)

        assert recovered == []
