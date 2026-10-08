"""Provisioning and teardown of WASM client VMs (V2-1169).

The theme throughout: a deployment with no WASM clients must be untouched, and a
deployment with them must fail EARLY and loudly rather than stand up VMs that
can never reach the network.
"""

from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.client_roles import ROLE_UPLOAD, ROLE_WASM_DOWNLOAD, ROLE_WASM_UPLOAD
from saorsa_deploy.cmd.destroy_clients import VALID_ROLES
from saorsa_deploy.provisioning.client import AntClientProvisioner

BOOTSTRAP_ADDR = (
    "/ip4/1.2.3.4/udp/42768/webrtc-direct/certhash/"
    "uEiAHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBw/p2p/" + "ab" * 32
)


def _provisioner(**overrides):
    kwargs = dict(
        clients=[],
        bootstrap_peers=["1.2.3.4:10000"],
        aws_access_key_id="k",
        aws_secret_access_key="s",
        addresses_s3_bucket="bucket",
        addresses_s3_prefix="prefix",
        console=MagicMock(),
    )
    kwargs.update(overrides)
    return AntClientProvisioner(**kwargs)


def _wasm_env(role=ROLE_WASM_UPLOAD):
    return {
        "1.2.3.4": {
            "role": role,
            "file_size_kb": 10240,
            "region": "lon1",
            "client_index": 1,
            "payment_type": "",
            "secret_key": "0xabc",
            "heaptrack": "0",
            "bootstrap_cache": "true",
            "forward_logs": "false",
            "rust_log": "",
            "upload_presigned_url": "https://example/put",
        }
    }


class TestNoWasmClientsIsANoOp:
    def test_defaults_are_empty(self):
        p = _provisioner()
        assert p.wasm_bundle_url is None
        assert p.bootstrap_webrtc_multiaddrs == []
        assert p.wasm_clients == []

    def test_add_wasm_ops_returns_immediately_for_native_hosts(self):
        # Not even the state object is touched, so a native-only deployment
        # cannot acquire a Node install or a bundle it has no use for.
        p = _provisioner()
        state = MagicMock()
        native_env = {"1.2.3.4": {"role": ROLE_UPLOAD}}
        with patch("saorsa_deploy.provisioning.client.add_op") as add_op:
            p._add_wasm_ops(state, native_env)
        add_op.assert_not_called()


class TestFailsEarlyRatherThanLate:
    def test_missing_bundle_url_raises(self):
        p = _provisioner(bootstrap_webrtc_multiaddrs=[BOOTSTRAP_ADDR])
        with pytest.raises(RuntimeError, match="build ant-wasm"):
            p._add_wasm_ops(MagicMock(), _wasm_env())

    def test_missing_bootstrap_addresses_raises(self):
        # Without a bootstrap address a WASM client cannot reach the network at all, and the
        # failure would otherwise surface as an unexplained connection error
        # minutes after the VMs are up and billing.
        p = _provisioner(wasm_bundle_url="https://example/bundle.tar.gz")
        with pytest.raises(RuntimeError, match="deployments webrtc-addresses"):
            p._add_wasm_ops(MagicMock(), _wasm_env())


class TestWasmOps:
    def _run(self, role=ROLE_WASM_UPLOAD):
        p = _provisioner(
            wasm_bundle_url="https://example/bundle.tar.gz",
            bootstrap_webrtc_multiaddrs=[
                BOOTSTRAP_ADDR,
                BOOTSTRAP_ADDR.replace("1.2.3.4", "5.6.7.8"),
            ],
            wasm_rpc_url="https://rpc.example",
        )
        host = MagicMock()
        host.name = "1.2.3.4"
        state = MagicMock()
        state.inventory.get_active_hosts.return_value = [host]
        with patch("saorsa_deploy.provisioning.client.add_op") as add_op:
            p._add_wasm_ops(state, _wasm_env(role))
        return [c.kwargs for c in add_op.call_args_list]

    def test_installs_node_and_the_bundle(self):
        names = [k["name"] for k in self._run()]
        assert any("Install Node.js" in n for n in names)
        assert any("Install WASM client bundle" in n for n in names)

    def test_bundle_install_is_not_presence_gated(self):
        # The bundle is namespaced by deployment name and the builder always
        # overwrites, so a re-provision after a rebuild must pick up the new one.
        cmds = " ".join(
            " ".join(k.get("commands", [])) for k in self._run() if "bundle" in k["name"]
        )
        assert "rm -rf /opt/ant-wasm-client" in cmds
        assert "test -f" not in cmds.split("wget")[0]

    def test_env_file_carries_every_bootstrap_address(self):
        # A failed endpoint gets a 30-minute cooldown inside ant-core, so a
        # single dead bootstrap would strand the client for that long.
        cmds = " ".join(
            " ".join(k.get("commands", [])) for k in self._run() if "env file" in k["name"]
        )
        assert "BOOTSTRAP_MULTIADDRS=" in cmds
        assert cmds.count("/webrtc-direct/") == 2

    def test_env_file_is_not_world_readable(self):
        # It carries the uploader's wallet key.
        cmds = " ".join(
            " ".join(k.get("commands", [])) for k in self._run() if "env file" in k["name"]
        )
        assert "chmod 600 /etc/ant-wasm-client.env" in cmds

    def test_writes_the_matching_unit_and_removes_the_other(self):
        cmds = " ".join(
            " ".join(k.get("commands", []))
            for k in self._run(ROLE_WASM_UPLOAD)
            if "env file" in k["name"]
        )
        assert "/etc/systemd/system/ant-wasm-uploader.service" in cmds
        assert "rm -f /etc/systemd/system/ant-wasm-downloader.service" in cmds

    def test_downloader_role_writes_the_downloader_unit(self):
        cmds = " ".join(
            " ".join(k.get("commands", []))
            for k in self._run(ROLE_WASM_DOWNLOAD)
            if "env file" in k["name"]
        )
        assert "/etc/systemd/system/ant-wasm-downloader.service" in cmds
        assert "rm -f /etc/systemd/system/ant-wasm-uploader.service" in cmds


class TestTeardownRoleParsing:
    """Orphan-tfstate recovery must recognise the hyphenated WASM roles.

    The role used to be read as the first "-"-separated token of the state key
    suffix, which reads `wasm-upload` as `wasm`, fails the known-role check and
    silently skips the key — leaving a real VM running and billing.
    """

    @staticmethod
    def _parse(suffix: str):
        role = next(
            (r for r in sorted(VALID_ROLES, key=len, reverse=True) if suffix.startswith(f"{r}-")),
            None,
        )
        parts = suffix.split("-")
        if role is None or not parts[-1].isdigit() or len(parts) < 3:
            return None
        return role, suffix[len(role) + 1 : -(len(parts[-1]) + 1)], int(parts[-1])

    def test_wasm_roles_are_recognised(self):
        assert self._parse("wasm-upload-lon1-2") == ("wasm-upload", "lon1", 2)
        assert self._parse("wasm-download-nyc1-1") == ("wasm-download", "nyc1", 1)

    def test_native_roles_still_parse(self):
        assert self._parse("upload-lon1-1") == ("upload", "lon1", 1)
        assert self._parse("download-ams3-3") == ("download", "ams3", 3)

    def test_hyphenated_regions_survive(self):
        assert self._parse("download-eu-west-par-3") == ("download", "eu-west-par", 3)
        assert self._parse("wasm-upload-eu-west-par-1") == (
            "wasm-upload",
            "eu-west-par",
            1,
        )

    def test_longest_role_wins(self):
        # "upload" is a suffix of nothing here, but the sort order is what stops
        # a shorter role shadowing a longer one that shares its tail.
        role, _, _ = self._parse("wasm-upload-lon1-1")
        assert role == "wasm-upload"

    def test_unparseable_key_is_rejected(self):
        assert self._parse("banana-lon1-1") is None
        assert self._parse("upload-lon1-notanumber") is None


class TestIpToEnvPreservesTheRealRole:
    """Regression: `ip_to_env` used to normalise every role to upload/download.

    Found on the first real deployment (DEV-01-wasm, 2026-09-07). The entry for
    a non-`upload` client hardcoded `"role": "download"`, so both WASM roles
    arrived as native downloaders. `_add_wasm_ops` then saw no WASM hosts and
    installed nothing, the native unit writer put an `ant-downloader.service` on
    those VMs, and `clients start` — which maps the client's REAL role to its
    unit — failed with "Unit file ant-wasm-downloader.service does not exist".

    The earlier tests all fed `_add_wasm_ops` a hand-built dict, so none of them
    exercised the construction that was actually wrong.
    """

    @staticmethod
    def _clients():
        return [
            {
                "ip": "10.0.0.1",
                "role": "upload",
                "region": "lon1",
                "client_index": 1,
                "file_size_mb": 10,
            },
            {"ip": "10.0.0.2", "role": "download", "region": "lon1", "client_index": 1},
            {
                "ip": "10.0.0.3",
                "role": "wasm-upload",
                "region": "lon1",
                "client_index": 1,
                "file_size_mb": 10,
            },
            {
                "ip": "10.0.0.4",
                "role": "wasm-download",
                "region": "lon1",
                "client_index": 1,
            },
        ]

    def _env(self):
        p = _provisioner(clients=self._clients())
        with patch.object(AntClientProvisioner, "_presigned_put_url", return_value="https://x"):
            return p._build_ip_to_env("0xdead")

    def test_every_role_survives_verbatim(self):
        env = self._env()
        assert env["10.0.0.1"]["role"] == "upload"
        assert env["10.0.0.2"]["role"] == "download"
        assert env["10.0.0.3"]["role"] == ROLE_WASM_UPLOAD
        assert env["10.0.0.4"]["role"] == ROLE_WASM_DOWNLOAD

    def test_wasm_hosts_are_visible_to_the_wasm_provisioning(self):
        # The predicate `_add_wasm_ops` gates on. This was False for every host.
        from saorsa_deploy.client_roles import is_wasm as _is_wasm

        env = self._env()
        assert [ip for ip, e in env.items() if _is_wasm(e["role"])] == [
            "10.0.0.3",
            "10.0.0.4",
        ]

    def test_a_wasm_uploader_keeps_its_upload_side_settings(self):
        # It fell into the download branch, which zeroes the file size and drops
        # the wallet key and the presigned URL — so even with a unit written it
        # could not have uploaded anything.
        e = self._env()["10.0.0.3"]
        assert e["file_size_kb"] == 10 * 1024
        assert e["secret_key"] == "0xdead"
        assert e["upload_presigned_url"] == "https://x"

    def test_a_wasm_downloader_still_has_no_upload_settings(self):
        e = self._env()["10.0.0.4"]
        assert e["file_size_kb"] == 0
        assert e["upload_presigned_url"] == ""

    def test_native_entries_are_unchanged(self):
        env = self._env()
        assert env["10.0.0.1"]["file_size_kb"] == 10 * 1024
        assert env["10.0.0.2"]["file_size_kb"] == 0
        assert env["10.0.0.2"]["upload_presigned_url"] == ""


class TestAddressKeyNamespacing:
    """Each uploader must own a distinct S3 object (V2-1169, found on DEV-01-wasm).

    `client_index` restarts at 1 per role, so a native and a WASM uploader in the
    same region both resolved to `upload-<region>-1.txt`. Every flush is a
    whole-object PUT, so the two would have repeatedly erased each other's
    address lists.
    """

    @staticmethod
    def _url(role):
        p = _provisioner(
            aws_access_key_id="AKIAEXAMPLE",
            aws_secret_access_key="x" * 40,
            addresses_s3_bucket="bucket",
            addresses_s3_prefix="saorsa-deploy/addresses/DEV-01",
        )
        return p._presigned_put_url(role, "lon1", 1)

    def test_native_key_is_unchanged(self):
        # Existing deployments must keep writing exactly where they always did.
        assert "/saorsa-deploy/addresses/DEV-01/upload-lon1-1.txt" in self._url(ROLE_UPLOAD)

    def test_wasm_key_is_distinct(self):
        assert "/saorsa-deploy/addresses/DEV-01/wasm-upload-lon1-1.txt" in self._url(
            ROLE_WASM_UPLOAD
        )

    def test_the_two_cannot_collide(self):
        native = self._url(ROLE_UPLOAD).split("?")[0]
        wasm = self._url(ROLE_WASM_UPLOAD).split("?")[0]
        assert native != wasm
