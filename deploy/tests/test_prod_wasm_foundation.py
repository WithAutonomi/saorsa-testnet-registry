"""The shared production WASM foundation (V2-1271 part A).

Bootstrap multiaddr resolution and its preflight ICE probe, VM sizing, the
build-provenance sidecar, and the extraction of the WASM provisioning builders
out of the testnet client provisioner — which has to stay byte-identical.
"""

import json
from types import SimpleNamespace

import pytest

from saorsa_deploy.prod_wasm import (
    DEFAULT_WASM_VM_SIZE,
    BootstrapProbeResult,
    WasmBootstrapError,
    bootstrap_deploy_args,
    check_wasm_vm_size,
    default_wasm_vm_size,
    digitalocean_slug_ram_mb,
    get_prod_bootstrap_webrtc_path,
    parse_multiaddr_endpoint,
    probe_bootstrap_multiaddrs,
    read_bootstrap_multiaddrs,
    resolve_bootstrap_multiaddrs,
    wasm_vm_size_warning,
)

MA1 = (
    "/ip4/1.2.3.4/udp/10001/webrtc-direct/certhash/uEiAaaa/p2p/"
    "57a89f84e1652d22fd6b791c29d83f187fbed3ba8e08e265fe3031b3a6f725da"
)
MA2 = (
    "/ip4/5.6.7.8/udp/10001/webrtc-direct/certhash/uEiBbbb/p2p/"
    "bb72725f8f4383eb475ac9cb681505d9147e8b2c5a4c0197d4efdb328c54601f"
)


class TestTheCommittedList:
    def test_the_default_source_is_the_committed_file(self):
        # The default is not a preference: nothing publishes these addresses at
        # a URL, which is why there is no URL flag to override it with.
        path = get_prod_bootstrap_webrtc_path()
        assert path.endswith("prod_bootstrap_webrtc_multiaddrs.txt")
        assert len(read_bootstrap_multiaddrs(path)) == 7

    def test_every_committed_entry_names_an_ip_and_a_udp_port(self):
        # A malformed line would be probed against nothing and read as dead.
        for multiaddr in read_bootstrap_multiaddrs(get_prod_bootstrap_webrtc_path()):
            endpoint = parse_multiaddr_endpoint(multiaddr)
            assert endpoint is not None, multiaddr
            assert endpoint[1] == 10001

    def test_blanks_and_comments_are_skipped(self, tmp_path):
        path = tmp_path / "ma.txt"
        path.write_text(f"# a note\n\n{MA1}\n  \n{MA2}\n")
        assert read_bootstrap_multiaddrs(str(path)) == [MA1, MA2]


class TestMultiaddrParsing:
    def test_a_line_with_no_udp_port_is_unparseable(self):
        assert parse_multiaddr_endpoint("/ip4/1.2.3.4/tcp/443") is None

    def test_a_non_numeric_port_is_unparseable(self):
        assert parse_multiaddr_endpoint("/ip4/1.2.3.4/udp/notaport") is None

    def test_a_line_with_no_ip_is_unparseable(self):
        assert parse_multiaddr_endpoint("/dns4/example.com/udp/10001") is None


class TestThePreflightProbe:
    def test_all_answering_proceeds(self, tmp_path):
        path = tmp_path / "ma.txt"
        path.write_text(f"{MA1}\n{MA2}\n")
        addrs, result, source = resolve_bootstrap_multiaddrs(str(path), probe=lambda ip, port: True)
        assert addrs == [MA1, MA2]
        assert result.all_answered
        assert result.dead == ()
        assert source == str(path)

    def test_a_partial_answer_warns_and_names_the_dead(self, tmp_path):
        # ant-core cools a failed endpoint down for 30 minutes and moves on, so
        # a live subset is a working deployment — refusing would block a viable
        # run, and saying nothing would hide why its results look odd.
        path = tmp_path / "ma.txt"
        path.write_text(f"{MA1}\n{MA2}\n")
        _, result, _ = resolve_bootstrap_multiaddrs(
            str(path), probe=lambda ip, port: ip == "1.2.3.4"
        )
        assert result.answered == 1
        assert result.dead == (MA2,)
        assert not result.all_answered

    def test_nothing_answering_is_refused(self, tmp_path):
        # A VM would bill from creation and fail every single operation.
        path = tmp_path / "ma.txt"
        path.write_text(f"{MA1}\n{MA2}\n")
        with pytest.raises(WasmBootstrapError, match="none of the 2"):
            resolve_bootstrap_multiaddrs(str(path), probe=lambda ip, port: False)

    def test_an_empty_list_is_refused(self, tmp_path):
        path = tmp_path / "ma.txt"
        path.write_text("# nothing here\n")
        with pytest.raises(WasmBootstrapError, match="is empty"):
            resolve_bootstrap_multiaddrs(str(path), probe=lambda ip, port: True)

    def test_skipping_the_probe_does_not_probe(self, tmp_path):
        # ant-node 0.20.0 opens the listener 2-15 minutes after a restart, so a
        # just-upgraded tier is legitimately silent and must not be an
        # unoverridable block.
        path = tmp_path / "ma.txt"
        path.write_text(f"{MA1}\n{MA2}\n")
        calls = []

        def _probe(ip, port):
            calls.append(ip)
            return False

        addrs, result, _ = resolve_bootstrap_multiaddrs(str(path), skip_probe=True, probe=_probe)
        assert calls == []
        assert result.skipped is True
        assert addrs == [MA1, MA2]

    def test_an_unparseable_entry_counts_as_dead(self):
        result = probe_bootstrap_multiaddrs(
            [MA1, "/ip4/1.2.3.4/tcp/443"], probe=lambda ip, port: True
        )
        assert result.answered == 1
        assert result.dead == ("/ip4/1.2.3.4/tcp/443",)

    def test_the_deploy_args_record_carries_the_source_and_the_counts(self):
        result = BootstrapProbeResult(total=7, answered=6, dead=(MA2,))
        record = bootstrap_deploy_args(result, "/some/list.txt")
        assert record == {
            "source": "/some/list.txt",
            "total": 7,
            "answered": 6,
            "probe_skipped": False,
            "dead": [MA2],
        }


class TestVmSizing:
    @pytest.mark.parametrize(
        "slug,expected",
        [
            ("s-4vcpu-8gb", 8192),
            ("s-2vcpu-4gb", 4096),
            ("g-2vcpu-8gb", 8192),
            ("m3-2vcpu-16gb", 16384),
            ("so1_5-2vcpu-16gb", 16384),
            ("cx43", None),
            ("b3-16", None),
            ("", None),
        ],
    )
    def test_digitalocean_slugs_encode_their_memory(self, slug, expected):
        assert digitalocean_slug_ram_mb(slug) == expected

    def test_a_do_slug_below_the_floor_is_refused(self):
        # A browser-path download holds ~4x the file in WASM memory (900 MB
        # measured at 3.9 GB RSS), so an under-sized box makes every transfer an
        # OOM kill — which reads as a network fault.
        problem = check_wasm_vm_size("s-2vcpu-4gb", "digitalocean")
        assert problem is not None and "4 GB" in problem

    def test_a_do_slug_at_or_above_the_floor_passes(self):
        assert check_wasm_vm_size("s-4vcpu-8gb", "digitalocean") is None
        assert check_wasm_vm_size("m3-2vcpu-16gb", "digitalocean") is None

    def test_another_providers_size_is_not_refused_but_is_flagged(self):
        # Hetzner/Vultr/OVH names cannot be interpreted here, and a hard error on
        # an unreadable name would block a perfectly adequate box.
        assert check_wasm_vm_size("cx43", "hetzner") is None
        assert wasm_vm_size_warning("cx43", "hetzner") is not None

    def test_a_digitalocean_run_gets_no_advisory(self):
        assert wasm_vm_size_warning("s-4vcpu-8gb", "digitalocean") is None

    def test_only_digitalocean_has_a_default_to_apply(self):
        assert default_wasm_vm_size("digitalocean") == DEFAULT_WASM_VM_SIZE
        assert default_wasm_vm_size(None) == DEFAULT_WASM_VM_SIZE
        assert default_wasm_vm_size("hetzner") is None


class TestTheBuildInfoSidecar:
    def test_the_url_sits_beside_the_bundle(self):
        from saorsa_deploy.binary_source import (
            get_ant_wasm_build_info_url,
            get_ant_wasm_build_url,
        )

        bundle = get_ant_wasm_build_url("X", "WithAutonomi", "ant-cli-v0.3.8")
        info = get_ant_wasm_build_info_url("X", "WithAutonomi", "ant-cli-v0.3.8")
        assert info.rsplit("/", 1)[0] == bundle.rsplit("/", 1)[0]
        assert info.endswith("ant-wasm-client.build-info.json")

    def test_a_missing_sidecar_reads_as_None(self, monkeypatch):
        # Every bundle built before provenance existed has none, and a deploy
        # must not fail over provenance.
        import saorsa_deploy.binary_source as mod

        monkeypatch.setattr("requests.get", lambda *a, **k: SimpleNamespace(status_code=404))
        assert mod.fetch_ant_wasm_build_info("X", "o", "b") is None

    def test_a_transport_failure_reads_as_None(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("no network")

        monkeypatch.setattr("requests.get", _boom)
        import saorsa_deploy.binary_source as mod

        assert mod.fetch_ant_wasm_build_info("X", "o", "b") is None

    def test_a_present_sidecar_is_returned(self, monkeypatch):
        payload = {"commit": "deadbeef" * 5, "ref": "ant-cli-v0.3.8"}
        monkeypatch.setattr(
            "requests.get",
            lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: payload),
        )
        import saorsa_deploy.binary_source as mod

        assert mod.fetch_ant_wasm_build_info("X", "o", "b") == payload


class TestTheBuildRecordsItsProvenance:
    def test_the_build_installs_from_the_lockfile(self):
        # `npm install` is free to resolve a newer minor of anything declared
        # with a caret, so two builds of the same ant-client ref could ship
        # different node-datachannel — the WebRTC implementation the whole
        # browser path runs on — with nothing recording which.
        from saorsa_deploy.wasm_build import NPM_INSTALL_ARGV

        assert NPM_INSTALL_ARGV[:2] == ("npm", "ci")

    def test_the_lockfile_reaches_the_build_vm(self):
        # `npm ci` refuses to run without it, so a build that did not upload it
        # would fail at the install step.
        from pathlib import Path

        src = Path("deploy/saorsa_deploy/provisioning/build_ant_wasm.py").read_text()
        assert "package-lock.json" in src
        assert (Path("deploy/saorsa_deploy/resources/wasm-client/package-lock.json")).exists()

    def test_the_provenance_command_reads_the_commit_and_the_dependency(self):
        from saorsa_deploy.wasm_build import build_info_command

        cmd = build_info_command(
            checkout_dir="/root/ant-client",
            client_src_dir="/root/wasm-client",
            dest_path="/root/build-info.json",
            ref="ant-cli-v0.3.8-rc.1",
            repo_owner="WithAutonomi",
        )
        # The commit is what identifies the code: a branch name does not.
        assert "git -C /root/ant-client rev-parse HEAD" in cmd
        assert "node_modules/node-datachannel/package.json" in cmd
        assert '"ref": "ant-cli-v0.3.8-rc.1"' in cmd
        assert '"repo_owner": "WithAutonomi"' in cmd
        assert '"wasm_pack_version": "0.15.0"' in cmd

    def test_the_rendered_body_is_valid_json_once_expanded(self):
        from saorsa_deploy.wasm_build import build_info_command

        cmd = build_info_command(
            checkout_dir="/c",
            client_src_dir="/s",
            dest_path="/d.json",
            ref="r",
            repo_owner="o",
        )
        body = cmd.split("BUILD_INFO_EOF\n", 1)[1].rsplit("BUILD_INFO_EOF", 1)[0]
        # The shell expands $COMMIT/$ND/$BUILT_AT; substitute placeholders so the
        # structure itself can be checked here.
        expanded = (
            body.replace("$COMMIT", "abc")
            .replace("$ND", "0.33.2")
            .replace("$BUILT_AT", "2026-09-25T00:00:00Z")
        )
        parsed = json.loads(expanded)
        assert parsed["commit"] == "abc"
        assert parsed["node_datachannel_version"] == "0.33.2"


class TestTheTestnetTierIsUnchangedByTheExtraction:
    """The builders moved out of `client.py`; its output must not have.

    A change here would alter an existing testnet's env file or units, which the
    issue explicitly rules out.
    """

    def test_both_units_render_exactly_as_before(self):
        from saorsa_deploy.provisioning.client import _build_wasm_unit

        expected_upload = (
            "[Unit]\n"
            "Description=Ant WASM Client Uploader\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            "EnvironmentFile=/etc/ant-wasm-client.env\n"
            "Environment=NODE_ENV=production\n"
            "WorkingDirectory=/opt/ant-wasm-client\n"
            "ExecStart=/usr/bin/node /opt/ant-wasm-client/dist/main.js\n"
            "Restart=on-failure\n"
            "RestartSec=10\n"
            "StandardOutput=journal\n"
            "StandardError=journal\n"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )
        assert _build_wasm_unit("wasm-upload") == expected_upload
        assert _build_wasm_unit("wasm-download") == expected_upload.replace(
            "Uploader", "Downloader"
        )

    def test_the_env_file_renders_exactly_as_before(self):
        from saorsa_deploy.provisioning.client import WASM_ENV_QUOTED_KEYS
        from saorsa_deploy.provisioning.wasm_client import render_env_file

        body = render_env_file(
            {
                "ROLE": "wasm-download",
                "REGION": "lon1",
                "CLIENT_INDEX": 1,
                "BOOTSTRAP_MULTIADDRS": MA1,
                "FILE_SIZE_KB": 0,
                "MAX_DOWNLOAD_BYTES": 1_000_000_000,
            },
            quoted=WASM_ENV_QUOTED_KEYS,
        )
        assert body == (
            "ROLE=wasm-download\n"
            "REGION=lon1\n"
            "CLIENT_INDEX=1\n"
            f'BOOTSTRAP_MULTIADDRS="{MA1}"\n'
            "FILE_SIZE_KB=0\n"
            "MAX_DOWNLOAD_BYTES=1000000000\n"
        )

    def test_the_testnet_bundle_install_does_not_require_the_cli(self):
        # A testnet built against a pre-CLI bundle must keep provisioning: only
        # the prod kinds spawn `ant-wasm`.
        from saorsa_deploy.provisioning.wasm_client import (
            build_bundle_install_command,
        )

        testnet = build_bundle_install_command("https://x/b.tar.gz")
        assert "dist/cli.js" not in testnet
        prod = build_bundle_install_command("https://x/b.tar.gz", require_cli=True)
        assert "test -f /opt/ant-wasm-client/dist/cli.js" in prod


class TestTheAntWasmCliWrapper:
    def test_it_execs_node_so_a_signal_reaches_the_child(self):
        # An OOM kill must reach the node process and surface to the calling
        # service as a signal exit; a wrapper that forked would absorb it.
        from saorsa_deploy.provisioning.wasm_client import build_ant_wasm_cli_command

        cmd = build_ant_wasm_cli_command()
        assert 'exec /usr/bin/node /opt/ant-wasm-client/dist/cli.js "$@"' in cmd
        assert "chmod +x /usr/local/bin/ant-wasm" in cmd


class TestTheNodeInstallIsVersionGated:
    """Presence is not enough — it has to be the right MAJOR.

    Found by the live verification. Two DigitalOcean VMs created minutes apart
    from the same size and region: one had no node and got 22.23.3, the other
    carried Ubuntu 24.04's preinstalled 18.19.1 and was skipped by a
    `command -v node` gate. The bundles were byte-identical; only the second
    failed, panicking inside ant-core's constructor with
    "could not initialize thread_rng: Node.js ES modules are not directly
    supported" and failing every transfer.
    """

    def _run(self, node_version: str | None) -> str:
        """Run the real generated shell against a fake `node` of that version."""
        import os
        import subprocess
        import tempfile

        from saorsa_deploy.provisioning.wasm_client import (
            build_node_install_command,
        )

        # The install branch is replaced by a marker: the point is which branch
        # is taken, and nothing should touch the network or apt.
        command = build_node_install_command()
        head, sep, _ = command.partition("curl -fsSL")
        assert sep, "the install branch must start with the NodeSource fetch"
        probe = f"{head}echo INSTALL_BRANCH_TAKEN; fi"

        with tempfile.TemporaryDirectory() as tmp:
            if node_version is not None:
                fake = os.path.join(tmp, "node")
                with open(fake, "w") as handle:
                    handle.write(f"#!/bin/sh\necho {node_version}\n")
                os.chmod(fake, 0o755)
            result = subprocess.run(
                ["sh", "-c", probe],
                env={"PATH": f"{tmp}:/usr/bin:/bin"},
                capture_output=True,
                text=True,
                timeout=30,
            )
        return result.stdout.strip().splitlines()[-1]

    def test_the_required_major_is_skipped(self):
        assert self._run("v22.23.3") == "NODE:SKIP"

    def test_a_patch_or_minor_difference_still_skips(self):
        # Only the major matters; re-installing on every provision would be
        # pointless churn.
        assert self._run("v22.0.0") == "NODE:SKIP"

    def test_no_node_installs(self):
        assert self._run(None) == "INSTALL_BRANCH_TAKEN"

    def test_ubuntus_preinstalled_node_18_installs(self):
        # The exact version that broke the live run.
        assert self._run("v18.19.1") == "INSTALL_BRANCH_TAKEN"

    def test_another_wrong_major_installs(self):
        assert self._run("v20.11.0") == "INSTALL_BRANCH_TAKEN"

    def test_unparseable_output_installs(self):
        # Erring toward installing: a node whose version cannot be read is not
        # evidence that the right one is present.
        assert self._run("weird-build") == "INSTALL_BRANCH_TAKEN"
