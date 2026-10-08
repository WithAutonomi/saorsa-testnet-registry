import json
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.state import (
    S3_BUCKET,
    S3_KEY_PREFIX,
    check_terraform_state_exists,
    delete_deployment_state,
    delete_terraform_state,
    list_terraform_state_keys,
    load_deployment_state,
    load_terraform_state,
    save_deployment_state,
    terraform_state_attributes,
    update_deployment_state,
)


@pytest.fixture
def mock_s3():
    with patch("saorsa_deploy.state.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        yield mock_client


class TestSaveDeploymentState:
    def test_puts_json_to_s3(self, mock_s3):
        save_deployment_state(
            name="DEV-01",
            regions=[("digitalocean", "lon1"), ("digitalocean", "nyc1")],
            terraform_variables={"name": "DEV-01", "vm_count": "2"},
            bootstrap_nodes=[{"ip": "143.198.100.50", "region": "lon1", "bootstrap_index": 1}],
            vm_ips={"digitalocean/lon1": ["10.0.0.1"], "digitalocean/nyc1": ["10.0.0.2"]},
        )

        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == S3_BUCKET
        assert call_kwargs["Key"] == f"{S3_KEY_PREFIX}/DEV-01.json"
        assert call_kwargs["ContentType"] == "application/json"

        body = json.loads(call_kwargs["Body"])
        assert body["name"] == "DEV-01"
        assert body["regions"] == [["digitalocean", "lon1"], ["digitalocean", "nyc1"]]
        assert body["terraform_variables"]["vm_count"] == "2"
        assert body["bootstrap_ip"] == "143.198.100.50"

    def test_regions_stored_as_lists(self, mock_s3):
        save_deployment_state(
            name="TEST",
            regions=[("digitalocean", "ams3")],
            terraform_variables={},
            bootstrap_nodes=[{"ip": "10.0.0.1", "region": "ams3", "bootstrap_index": 1}],
            vm_ips={"digitalocean/ams3": ["10.0.0.2"]},
        )

        body = json.loads(mock_s3.put_object.call_args.kwargs["Body"])
        assert body["regions"] == [["digitalocean", "ams3"]]

    def test_stores_bootstrap_ip(self, mock_s3):
        save_deployment_state(
            name="DEV-01",
            regions=[("digitalocean", "lon1")],
            terraform_variables={"name": "DEV-01"},
            bootstrap_nodes=[{"ip": "143.198.100.50", "region": "lon1", "bootstrap_index": 1}],
            vm_ips={"digitalocean/lon1": ["10.0.0.1"]},
        )

        body = json.loads(mock_s3.put_object.call_args.kwargs["Body"])
        assert body["bootstrap_ip"] == "143.198.100.50"

    def test_stores_vm_ips(self, mock_s3):
        vm_ips = {
            "digitalocean/lon1": ["10.0.0.1", "10.0.0.2"],
            "digitalocean/ams3": ["10.0.0.3"],
        }
        save_deployment_state(
            name="DEV-01",
            regions=[("digitalocean", "lon1"), ("digitalocean", "ams3")],
            terraform_variables={"name": "DEV-01"},
            bootstrap_nodes=[{"ip": "143.198.100.50", "region": "lon1", "bootstrap_index": 1}],
            vm_ips=vm_ips,
        )

        body = json.loads(mock_s3.put_object.call_args.kwargs["Body"])
        assert body["vm_ips"] == vm_ips


class TestLoadDeploymentState:
    def test_loads_json_from_s3(self, mock_s3):
        state = {
            "name": "DEV-01",
            "regions": [["digitalocean", "lon1"]],
            "terraform_variables": {"name": "DEV-01", "vm_count": "2"},
        }
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(state).encode()
        mock_s3.get_object.return_value = {"Body": mock_body}

        result = load_deployment_state("DEV-01")

        mock_s3.get_object.assert_called_once_with(
            Bucket=S3_BUCKET,
            Key=f"{S3_KEY_PREFIX}/DEV-01.json",
        )
        assert result["name"] == "DEV-01"
        assert result["regions"] == [["digitalocean", "lon1"]]

    def test_raises_on_not_found(self, mock_s3):
        error_class = type("NoSuchKey", (Exception,), {})
        mock_s3.exceptions.NoSuchKey = error_class
        mock_s3.get_object.side_effect = error_class("not found")

        with pytest.raises(RuntimeError, match="No deployment state found"):
            load_deployment_state("NONEXISTENT")


class TestUpdateDeploymentState:
    def test_merges_updates_into_existing_state(self, mock_s3):
        existing_state = {
            "name": "DEV-01",
            "regions": [["digitalocean", "lon1"]],
            "bootstrap_ip": "10.0.0.1",
        }
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(existing_state).encode()
        mock_s3.get_object.return_value = {"Body": mock_body}

        update_deployment_state("DEV-01", {"bootstrap_port": 5000})

        mock_s3.put_object.assert_called_once()
        body = json.loads(mock_s3.put_object.call_args.kwargs["Body"])
        assert body["name"] == "DEV-01"
        assert body["bootstrap_ip"] == "10.0.0.1"
        assert body["bootstrap_port"] == 5000

    def test_overwrites_existing_keys(self, mock_s3):
        existing_state = {
            "name": "DEV-01",
            "node_count": 3,
        }
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(existing_state).encode()
        mock_s3.get_object.return_value = {"Body": mock_body}

        update_deployment_state("DEV-01", {"node_count": 5})

        body = json.loads(mock_s3.put_object.call_args.kwargs["Body"])
        assert body["node_count"] == 5


class TestDeleteDeploymentState:
    def test_deletes_from_s3(self, mock_s3):
        delete_deployment_state("DEV-01")

        mock_s3.delete_object.assert_called_once_with(
            Bucket=S3_BUCKET,
            Key=f"{S3_KEY_PREFIX}/DEV-01.json",
        )


class TestDeleteTerraformState:
    def test_deletes_state_key_from_s3(self, mock_s3):
        delete_terraform_state("saorsa-deploy/do-DEV-01-lon1.tfstate")

        mock_s3.delete_object.assert_called_once_with(
            Bucket=S3_BUCKET,
            Key="saorsa-deploy/do-DEV-01-lon1.tfstate",
        )


class TestCheckTerraformStateExists:
    def test_returns_true_when_exists(self, mock_s3):
        assert check_terraform_state_exists("saorsa-deploy/do-DEV-01-lon1.tfstate") is True
        mock_s3.head_object.assert_called_once_with(
            Bucket=S3_BUCKET,
            Key="saorsa-deploy/do-DEV-01-lon1.tfstate",
        )

    def test_returns_false_when_not_found(self, mock_s3):
        error_class = type("ClientError", (Exception,), {})
        mock_s3.exceptions.ClientError = error_class
        mock_s3.head_object.side_effect = error_class("not found")

        assert check_terraform_state_exists("saorsa-deploy/do-DEV-01-lon1.tfstate") is False


class TestListTerraformStateKeys:
    def test_returns_keys_matching_prefix(self, mock_s3):
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "saorsa-deploy/do-bootstrap-DEV-02-lon1.tfstate"},
                    {"Key": "saorsa-deploy/do-bootstrap-DEV-02-nyc1.tfstate"},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = paginator

        keys = list_terraform_state_keys("saorsa-deploy/do-bootstrap-DEV-02-")

        mock_s3.get_paginator.assert_called_once_with("list_objects_v2")
        paginator.paginate.assert_called_once_with(
            Bucket=S3_BUCKET,
            Prefix="saorsa-deploy/do-bootstrap-DEV-02-",
        )
        assert keys == [
            "saorsa-deploy/do-bootstrap-DEV-02-lon1.tfstate",
            "saorsa-deploy/do-bootstrap-DEV-02-nyc1.tfstate",
        ]

    def test_empty_when_no_matches(self, mock_s3):
        paginator = MagicMock()
        paginator.paginate.return_value = [{}]
        mock_s3.get_paginator.return_value = paginator

        assert list_terraform_state_keys("saorsa-deploy/do-bootstrap-NONE-") == []


class TestLoadTerraformState:
    def test_parses_json_from_s3(self, mock_s3):
        payload = {"resources": [{"type": "x", "name": "y", "instances": []}]}
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(payload).encode()
        mock_s3.get_object.return_value = {"Body": mock_body}

        result = load_terraform_state("saorsa-deploy/do-DEV-02-lon1.tfstate")

        assert result == payload
        mock_s3.get_object.assert_called_once_with(
            Bucket=S3_BUCKET,
            Key="saorsa-deploy/do-DEV-02-lon1.tfstate",
        )

    def test_raises_when_missing(self, mock_s3):
        err = type("NoSuchKey", (Exception,), {})
        mock_s3.exceptions.NoSuchKey = err
        mock_s3.get_object.side_effect = err("not found")

        with pytest.raises(RuntimeError, match="No Terraform state"):
            load_terraform_state("saorsa-deploy/missing.tfstate")


class TestTerraformStateAttributes:
    TFSTATE = {
        "resources": [
            {
                "type": "digitalocean_volume",
                "name": "monitoring_storage",
                "instances": [
                    {"attributes": {"name": "x", "size": 100}},
                ],
            },
            {
                "type": "digitalocean_droplet",
                "name": "monitoring",
                "instances": [],
            },
        ]
    }

    def test_returns_attributes_for_matching_resource(self):
        attrs = terraform_state_attributes(
            self.TFSTATE, "digitalocean_volume", "monitoring_storage"
        )
        assert attrs == {"name": "x", "size": 100}

    def test_returns_none_when_resource_missing(self):
        assert terraform_state_attributes(self.TFSTATE, "digitalocean_volume", "other") is None

    def test_returns_none_when_instances_empty(self):
        assert (
            terraform_state_attributes(self.TFSTATE, "digitalocean_droplet", "monitoring") is None
        )


class TestKeepOnDestroyState:
    """`keep_on_destroy` is the only record of the keep intent once the manifest
    is out of the picture, so it has to survive a save and a merge-update."""

    def test_elastic_save_records_the_flag(self, mock_s3):
        from saorsa_deploy.state import S3_ELASTIC_KEY_PREFIX, save_elastic_state

        save_elastic_state(
            name="DEV-01",
            region="lon1",
            vm_ip="10.0.0.1",
            keep_on_destroy=True,
        )
        kwargs = mock_s3.put_object.call_args.kwargs
        assert kwargs["Key"] == f"{S3_ELASTIC_KEY_PREFIX}/DEV-01.json"
        assert json.loads(kwargs["Body"])["keep_on_destroy"] is True

    def test_elastic_save_defaults_to_destroy(self, mock_s3):
        from saorsa_deploy.state import save_elastic_state

        save_elastic_state(name="DEV-01", region="lon1", vm_ip="10.0.0.1")
        assert json.loads(mock_s3.put_object.call_args.kwargs["Body"])["keep_on_destroy"] is False

    def test_monitoring_save_records_the_flag(self, mock_s3):
        from saorsa_deploy.state import S3_MONITORING_KEY_PREFIX, save_monitoring_state

        save_monitoring_state(
            name="DEV-01",
            region="lon1",
            vm_ip="10.0.0.1",
            keep_on_destroy=True,
        )
        kwargs = mock_s3.put_object.call_args.kwargs
        assert kwargs["Key"] == f"{S3_MONITORING_KEY_PREFIX}/DEV-01.json"
        assert json.loads(kwargs["Body"])["keep_on_destroy"] is True

    def test_monitoring_save_defaults_to_destroy(self, mock_s3):
        from saorsa_deploy.state import save_monitoring_state

        save_monitoring_state(name="DEV-01", region="lon1", vm_ip="10.0.0.1")
        assert json.loads(mock_s3.put_object.call_args.kwargs["Body"])["keep_on_destroy"] is False

    def test_update_merges_the_flag_into_existing_state(self, mock_s3):
        # The idempotent-reuse path in `elastic infra` sets the flag this way, on
        # a state doc that predates the option and has no such key.
        from saorsa_deploy.state import update_elastic_state

        mock_s3.get_object.return_value = {
            "Body": MagicMock(
                read=MagicMock(
                    return_value=json.dumps(
                        {"name": "DEV-01", "vm_ip": "10.0.0.1", "region": "lon1"}
                    ).encode()
                )
            )
        }
        update_elastic_state("DEV-01", {"keep_on_destroy": True})
        body = json.loads(mock_s3.put_object.call_args.kwargs["Body"])
        assert body["keep_on_destroy"] is True
        # the pre-existing fields survive the merge
        assert body["vm_ip"] == "10.0.0.1"
        assert body["region"] == "lon1"
