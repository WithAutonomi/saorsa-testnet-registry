"""Tests for client extensions — the ``kind: clients`` manifest (V2-1616).

Five things are worth a test here, and they are the five that would be
expensive to find in production:

* the manifest rules, each of which refuses a run that would otherwise create
  infrastructure before failing;
* ``client_index`` continuation, because a reused index silently erases
  another uploader's S3 address list on every flush;
* the teardown guard, because destroying a deployment under a standing
  extension leaves VMs running, billing and failing every operation;
* that ``clients down`` leaves the deployment's address pool alone, because
  wiping it would empty a running deployment's download workload;
* that ``record-results`` resolves an extension VM's transport, because a
  missed one relabels a WASM failure as native and pushes it into the native
  hard floors.
"""

import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy import client_extension
from saorsa_deploy.config import ManifestError, load_manifest
from saorsa_deploy.orchestrator import build_deploy_steps, build_destroy_steps
from saorsa_deploy.orchestrator.clients import planned_client_vms

EXTENSION = """\
    kind: clients
    name: extra-downloaders
    deployment: DEV-01
    purpose: "V2-1616: two more downloaders"
    ssh_key_path: ~/.ssh/ant_deploy
    clients:
      downloaders:
        - { provider: digital_ocean, region: lon1 }
        - { provider: digital_ocean, region: lon1 }
"""


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(textwrap.dedent(body))
    return p


def _manifest(tmp_path, body=EXTENSION, name="ext.yaml"):
    return load_manifest(_write(tmp_path, name, body))


# --------------------------------------------------------------------------- #
# the manifest kind
# --------------------------------------------------------------------------- #
class TestManifest:
    def test_loads(self, tmp_path):
        m = _manifest(tmp_path)
        assert m.kind == "clients"
        assert m.name == "extra-downloaders"
        assert m.deployment == "DEV-01"
        assert len(m.clients.downloaders) == 2
        assert m.clients.downloaders[0].region == "lon1"
        # The kind-specific block resolves through the same property every
        # other kind uses.
        assert m.spec is m.clients

    def test_deployment_is_required(self, tmp_path):
        body = EXTENSION.replace("    deployment: DEV-01\n", "")
        with pytest.raises(ManifestError, match="requires a top-level `deployment:`"):
            _manifest(tmp_path, body)

    def test_name_must_be_hostname_safe(self, tmp_path):
        body = EXTENSION.replace("name: extra-downloaders", 'name: "bad name"')
        with pytest.raises(ManifestError, match="invalid client-extension name"):
            _manifest(tmp_path, body)

    @pytest.mark.parametrize("bad", ["upload", "upload-extra", "wasm-download-2", "pointer-x"])
    def test_name_may_not_start_with_a_client_role(self, tmp_path, bad):
        # `DEV-01-upload-extra-download-lon1-1.tfstate` parses as role `upload`
        # of DEV-01, so the deployment's orphan-recovery scan would adopt the
        # extension's VM and destroy against a key that does not exist.
        body = EXTENSION.replace("name: extra-downloaders", f"name: {bad}")
        with pytest.raises(ManifestError, match="starts with the client role"):
            _manifest(tmp_path, body)

    def test_deployment_is_refused_on_other_kinds(self, tmp_path):
        body = """\
            name: T
            kind: testnet
            purpose: p
            deployment: DEV-01
            testnet:
              binaries: { ant_node: { version: "0.1.0" } }
              nodes: { per_vm: 1, providers: { digital_ocean: { vm_count: 1 } } }
        """
        with pytest.raises(ManifestError, match="only valid on kind 'clients'"):
            _manifest(tmp_path, body)

    def test_at_least_one_role_list_must_be_populated(self, tmp_path):
        body = """\
            kind: clients
            name: empty
            deployment: DEV-01
            purpose: p
            clients:
              downloaders: []
        """
        with pytest.raises(ManifestError, match="at least one of uploaders"):
            _manifest(tmp_path, body)

    def test_unknown_role_key_is_a_validation_error(self, tmp_path):
        # A role list this schema does not know must be a loud error, not a
        # silently-ignored list -- the manifest is the sole description of a
        # real cloud deployment. (This was `browser_downloaders` until V2-1617
        # added the role; `browser_uploaders` does not exist and is the same
        # shape of mistake.)
        body = EXTENSION + "      browser_uploaders: []\n"
        with pytest.raises(ManifestError, match="browser_uploaders"):
            _manifest(tmp_path, body)

    def test_browser_downloaders_are_a_known_role_list(self, tmp_path):
        # V2-1617's role, and the reason an extension exists for it: a hot-read
        # cohort is added to a deployment whose uploads have already happened,
        # which is the only time a hot file exists to select.
        body = """\
            kind: clients
            name: browsers
            deployment: DEV-01
            purpose: p
            clients:
              browser_downloaders:
                - { provider: digital_ocean, region: lon1, count: 2 }
        """
        manifest = _manifest(tmp_path, body)
        assert len(manifest.clients.browser_downloaders) == 1
        assert manifest.clients.has_browser

    def test_extends_is_refused(self, tmp_path):
        _write(tmp_path, "base.yaml", EXTENSION)
        body = """\
            kind: clients
            extends: base.yaml
            name: child
            deployment: DEV-01
            purpose: p
            clients:
              downloaders:
                - { provider: digital_ocean, region: lon1 }
        """
        with pytest.raises(ManifestError, match="`extends` is not supported"):
            _manifest(tmp_path, body, name="child.yaml")

    def test_downloaders_without_uploaders_are_allowed(self, tmp_path):
        # The whole point: an extension reads the deployment's EXISTING address
        # pool, so the pairing rule a deployment manifest applies must not.
        m = _manifest(tmp_path)
        assert m.clients.uploaders == []
        assert len(m.clients.downloaders) == 2

    def test_no_binaries_block(self, tmp_path):
        body = EXTENSION + "    binaries:\n      ant: { version: '0.3.1' }\n"
        with pytest.raises(ManifestError, match="binaries"):
            _manifest(tmp_path, body)

    def test_slack_post_is_refused(self, tmp_path):
        # `deployments slack-post` reports on a deployment; an extension has no
        # registry row, so the step would look up a name that does not exist.
        body = EXTENSION.replace("    purpose:", "    slack_post: true\n    purpose:")
        with pytest.raises(ManifestError, match="slack_post is not supported"):
            _manifest(tmp_path, body)

    def test_wasm_file_cap_still_applies(self, tmp_path):
        body = """\
            kind: clients
            name: wasm-ext
            deployment: DEV-01
            purpose: p
            clients:
              wasm_uploaders:
                - { provider: digital_ocean, region: lon1, file_size_mb: 1000 }
        """
        with pytest.raises(ManifestError, match="browser limit"):
            _manifest(tmp_path, body)


# --------------------------------------------------------------------------- #
# step builders / plan
# --------------------------------------------------------------------------- #
class TestSteps:
    def test_deploy_sequence(self, tmp_path):
        steps = build_deploy_steps(_manifest(tmp_path))
        assert [s.argv for s in steps] == [
            [
                "clients",
                "infra",
                "--name",
                "DEV-01",
                "--extension",
                "extra-downloaders",
                "--downloader",
                "digital_ocean:lon1",
                "--downloader",
                "digital_ocean:lon1",
            ],
            [
                "clients",
                "provision",
                "--name",
                "DEV-01",
                "--extension",
                "extra-downloaders",
                "--ssh-key-path",
                "~/.ssh/ant_deploy",
            ],
            [
                "clients",
                "start",
                "--name",
                "DEV-01",
                "--extension",
                "extra-downloaders",
                "--ssh-key-path",
                "~/.ssh/ant_deploy",
            ],
        ]

    def test_deploy_sequence_has_no_build_or_webrtc_step(self, tmp_path):
        # Both would reach into the deployment: a build would produce a second
        # client binary, and webrtc-addresses would SSH its bootstrap VMs.
        body = """\
            kind: clients
            name: wasm-ext
            deployment: DEV-01
            purpose: p
            clients:
              wasm_downloaders:
                - { provider: digital_ocean, region: lon1 }
        """
        argv0 = [s.argv[0:2] for s in build_deploy_steps(_manifest(tmp_path, body))]
        assert ["build", "ant-wasm"] not in argv0
        assert ["deployments", "webrtc-addresses"] not in argv0

    def test_destroy_sequence(self, tmp_path):
        steps = build_destroy_steps(_manifest(tmp_path))
        assert [s.argv for s in steps] == [
            [
                "clients",
                "stop",
                "--name",
                "DEV-01",
                "--extension",
                "extra-downloaders",
                "--ssh-key-path",
                "~/.ssh/ant_deploy",
            ],
            [
                "clients",
                "destroy",
                "--name",
                "DEV-01",
                "--extension",
                "extra-downloaders",
                "--force",
                "--ssh-key-path",
                "~/.ssh/ant_deploy",
            ],
        ]
        # The stop must not fail a teardown whose VMs are already gone.
        assert steps[0].best_effort is True

    def test_planned_vm_names_are_namespaced_by_extension(self, tmp_path):
        vms = planned_client_vms(_manifest(tmp_path))
        assert [vm["hostname"] for vm in vms] == [
            "DEV-01-extra-downloaders-ant-client-download-lon1-1",
            "DEV-01-extra-downloaders-ant-client-download-lon1-2",
        ]
        assert [vm["client_index"] for vm in vms] == [1, 2]

    def test_planned_vm_indexes_continue_from_the_base(self, tmp_path):
        vms = planned_client_vms(_manifest(tmp_path), {"download": 4})
        assert [vm["client_index"] for vm in vms] == [5, 6]
        assert vms[0]["hostname"].endswith("-download-lon1-5")


# --------------------------------------------------------------------------- #
# state keys and client_index continuation
# --------------------------------------------------------------------------- #
class TestStateKeys:
    def test_own_tier_key_is_unchanged(self):
        from saorsa_deploy.state import client_state_key

        assert client_state_key("DEV-01") == "saorsa-deploy/clients/DEV-01.json"

    def test_extension_key_is_under_its_deployment(self):
        from saorsa_deploy.state import client_state_key

        assert (
            client_state_key("DEV-01", "browsers")
            == "saorsa-deploy/client-extensions/DEV-01/browsers.json"
        )

    def test_terraform_name(self):
        assert client_extension.terraform_name("DEV-01", None) == "DEV-01"
        assert client_extension.terraform_name("DEV-01", "browsers") == "DEV-01-browsers"

    def test_journal_name_cannot_collide_with_a_deployment_name(self):
        assert client_extension.journal_name("DEV-01", "browsers") == "DEV-01--browsers"

    def test_client_vm_name_carries_the_extension_segment(self):
        """The name is the Terraform VM name, so an extension's VMs are
        distinguishable from the deployment's own in every consumer."""
        assert (
            client_extension.client_vm_name(
                "DEV-03", "DEV-03-browsers", "browser-download", "lon1", 1
            )
            == "DEV-03-DEV-03-browsers-ant-client-browser-download-lon1-1"
        )
        assert (
            client_extension.client_vm_name("DEV-03", None, "upload", "lon1", 1)
            == "DEV-03-ant-client-upload-lon1-1"
        )

    def test_client_hostname_is_the_same_construction(self):
        """`browser_records.client_hostname` and the provisioner must agree, or
        a browser row can never be joined to its trace."""
        from saorsa_deploy.browser_records import client_hostname

        own = {"role": "upload", "region": "lon1", "client_index": 1}
        ext = {"role": "browser-download", "region": "sgp1", "client_index": 3}
        assert client_hostname("DEV-03", None, own) == client_extension.client_vm_name(
            "DEV-03", None, "upload", "lon1", 1
        )
        assert client_hostname("DEV-03", "DEV-03-browsers", ext) == client_extension.client_vm_name(
            "DEV-03", "DEV-03-browsers", "browser-download", "sgp1", 3
        )


def _tiers(monkeypatch, own, extensions):
    """Patch the client-state loaders to present ``own`` plus ``extensions``.

    ``own``/each extension value is a list of client dicts, or None for absent.
    """
    import saorsa_deploy.state as state

    def load(name, extension=None):
        doc = own if extension is None else extensions.get(extension)
        if doc is None:
            raise RuntimeError(f"no client state for {name}/{extension}")
        return {"name": name, "clients": doc}

    monkeypatch.setattr(state, "load_client_state", load)
    monkeypatch.setattr(state, "list_client_extensions", lambda name: sorted(extensions))


class TestClientIndexContinuation:
    def test_continues_across_own_clients_and_another_extension(self, monkeypatch):
        _tiers(
            monkeypatch,
            own=[
                {"role": "upload", "client_index": 1},
                {"role": "upload", "client_index": 2},
                {"role": "download", "client_index": 1},
            ],
            extensions={
                "first": [
                    {"role": "upload", "client_index": 3},
                    {"role": "download", "client_index": 2},
                ]
            },
        )
        assert client_extension.highest_client_indexes("DEV-01") == {"upload": 3, "download": 2}

    def test_excludes_the_extension_asking(self, monkeypatch):
        # A resume must allocate the SAME indexes it allocated last time, or it
        # reads as a spec mismatch against VMs running exactly what was asked.
        _tiers(
            monkeypatch,
            own=[{"role": "download", "client_index": 1}],
            extensions={"mine": [{"role": "download", "client_index": 2}]},
        )
        assert client_extension.highest_client_indexes("DEV-01", exclude="mine") == {"download": 1}

    def test_absent_deployment_clients_are_not_an_error(self, monkeypatch):
        _tiers(monkeypatch, own=None, extensions={})
        assert client_extension.highest_client_indexes("DEV-01") == {}

    def test_a_failed_extension_scan_raises_rather_than_allocating_blind(self, monkeypatch):
        # The collision this function exists to prevent: with the scan swallowed,
        # the deployment's own index 1 is the only floor, and a second extension
        # gets index 2 — which the FIRST extension's uploader already owns, and
        # whose address object every flush then overwrites whole.
        import saorsa_deploy.state as state

        monkeypatch.setattr(
            state, "load_client_state", lambda name, extension=None: {"clients": []}
        )

        def boom(name):
            raise OSError("s3 unreachable")

        monkeypatch.setattr(state, "list_client_extensions", boom)
        with pytest.raises(OSError, match="s3 unreachable"):
            client_extension.highest_client_indexes("DEV-01")

    def test_an_unreadable_extension_state_raises(self, monkeypatch):
        import saorsa_deploy.state as state

        def load(name, extension=None):
            if extension is None:
                return {"clients": []}
            raise OSError("s3 unreachable")

        monkeypatch.setattr(state, "load_client_state", load)
        monkeypatch.setattr(state, "list_client_extensions", lambda name: ["first"])
        with pytest.raises(OSError, match="s3 unreachable"):
            client_extension.highest_client_indexes("DEV-01")

    def test_an_extension_torn_down_mid_scan_holds_no_index(self, monkeypatch):
        # RuntimeError is the missing-key case only: the key went away between
        # the list and the get, so that extension is gone and owns nothing.
        import saorsa_deploy.state as state

        def load(name, extension=None):
            if extension is None:
                return {"clients": [{"role": "download", "client_index": 1}]}
            raise RuntimeError("No client state found")

        monkeypatch.setattr(state, "load_client_state", load)
        monkeypatch.setattr(state, "list_client_extensions", lambda name: ["gone"])
        assert client_extension.highest_client_indexes("DEV-01") == {"download": 1}

    def test_a_non_runtime_failure_on_the_own_tier_raises(self, monkeypatch):
        import saorsa_deploy.state as state

        def load(name, extension=None):
            raise OSError("credentials")

        monkeypatch.setattr(state, "load_client_state", load)
        monkeypatch.setattr(state, "list_client_extensions", lambda name: [])
        with pytest.raises(OSError, match="credentials"):
            client_extension.highest_client_indexes("DEV-01")

    def test_infra_exits_2_rather_than_allocate_from_a_failed_scan(self, monkeypatch):
        from saorsa_deploy.cmd import infra_clients

        def boom(name, exclude=None):
            raise OSError("s3 unreachable")

        monkeypatch.setattr(infra_clients, "highest_client_indexes", boom)
        monkeypatch.setattr(
            infra_clients,
            "save_client_state",
            lambda *a, **k: pytest.fail("saved state after a failed scan"),
        )
        monkeypatch.setattr(
            infra_clients,
            "execute_terraform_runs",
            lambda configs: pytest.fail("created VMs after a failed scan"),
        )
        with pytest.raises(SystemExit) as exc:
            infra_clients.cmd_infra_clients(
                SimpleNamespace(
                    name="DEV-01",
                    extension="browsers",
                    uploader=[],
                    downloader=["digital_ocean:lon1"],
                    wasm_uploader=[],
                    wasm_downloader=[],
                    pointer=[],
                    force=False,
                )
            )
        assert exc.value.code == 2

    def test_reporting_tiers_stay_tolerant_of_a_failed_scan(self, monkeypatch):
        # The opposite choice to the index scan, and deliberately so: a tier
        # missing from a report costs an annotation, where one missing from the
        # index scan costs a live uploader its address list.
        import saorsa_deploy.state as state

        monkeypatch.setattr(
            state,
            "list_client_extensions",
            lambda name: (_ for _ in ()).throw(OSError("s3 unreachable")),
        )
        assert client_extension.client_extension_tiers("DEV-01") == []

    def test_infra_allocates_continued_indexes(self, monkeypatch):
        from saorsa_deploy.cmd import infra_clients

        monkeypatch.setattr(
            infra_clients, "highest_client_indexes", lambda name, exclude=None: {"download": 7}
        )
        monkeypatch.setattr(
            infra_clients, "load_client_state", lambda *a, **k: (_ for _ in ()).throw(RuntimeError)
        )
        saved: dict = {}
        monkeypatch.setattr(
            infra_clients,
            "save_client_state",
            lambda name, clients, extension=None: saved.update(
                name=name, clients=clients, extension=extension
            ),
        )
        monkeypatch.setattr(infra_clients, "update_client_state", lambda *a, **k: None)
        monkeypatch.setattr(infra_clients, "check_terraform_state_exists", lambda key: False)
        monkeypatch.setattr(
            infra_clients,
            "execute_terraform_runs",
            lambda configs: [
                SimpleNamespace(success=True, outputs={"vm_ip": f"10.0.0.{i}"})
                for i, _ in enumerate(configs, start=1)
            ],
        )
        infra_clients.cmd_infra_clients(
            SimpleNamespace(
                name="DEV-01",
                extension="browsers",
                uploader=[],
                downloader=["digital_ocean:lon1", "digital_ocean:lon1"],
                wasm_uploader=[],
                wasm_downloader=[],
                pointer=[],
                force=False,
            )
        )
        assert [c["client_index"] for c in saved["clients"]] == [8, 9]
        assert {c["extension"] for c in saved["clients"]} == {"browsers"}
        assert saved["extension"] == "browsers"


# --------------------------------------------------------------------------- #
# the teardown guard
# --------------------------------------------------------------------------- #
def _patch_extension_names(monkeypatch, names):
    monkeypatch.setattr(client_extension, "client_extension_names", lambda name: list(names))


class TestTeardownGuard:
    def test_refusal_names_each_extension_and_its_command(self, monkeypatch, capsys):
        _patch_extension_names(monkeypatch, ["browsers", "extra"])
        from rich.console import Console

        with pytest.raises(SystemExit) as exc:
            client_extension.refuse_while_client_extensions_exist(
                "DEV-01", Console(width=200), "tear down"
            )
        assert exc.value.code == 2
        out = capsys.readouterr().out
        assert "browsers" in out and "extra" in out
        assert "clients down --name DEV-01 --extension browsers" in out

    def test_a_failed_scan_also_refuses(self, monkeypatch, capsys):
        # "Could not look" is not "nothing there", and what the guard protects
        # (an extension's VMs, and the address pool its downloaders read) is
        # not recoverable once the teardown has run.
        from rich.console import Console

        import saorsa_deploy.state as state

        monkeypatch.setattr(
            state,
            "list_client_extensions",
            lambda name: (_ for _ in ()).throw(OSError("s3 unreachable")),
        )
        with pytest.raises(SystemExit) as exc:
            client_extension.refuse_while_client_extensions_exist(
                "DEV-01", Console(width=200), "tear down"
            )
        assert exc.value.code == 2
        out = capsys.readouterr().out
        assert "could not determine" in out
        assert "clients ls --name DEV-01" in out

    def test_an_empty_prefix_is_not_a_failure(self, monkeypatch):
        from rich.console import Console

        import saorsa_deploy.state as state

        monkeypatch.setattr(state, "list_client_extensions", lambda name: [])
        client_extension.refuse_while_client_extensions_exist(
            "DEV-01", Console(), "tear down"
        )  # does not raise

    def test_no_extensions_is_a_no_op(self, monkeypatch):
        _patch_extension_names(monkeypatch, [])
        from rich.console import Console

        client_extension.refuse_while_client_extensions_exist(
            "DEV-01", Console(), "tear down"
        )  # does not raise

    def test_bootstraps_destroy_refuses_then_succeeds(self, monkeypatch):
        from saorsa_deploy.cmd import destroy_bootstraps as mod

        calls: list[str] = []
        monkeypatch.setattr(
            mod,
            "refuse_while_client_extensions_exist",
            lambda name, console, what: calls.append(name),
        )
        monkeypatch.setattr(
            mod, "load_deployment_state", lambda name: (_ for _ in ()).throw(RuntimeError)
        )
        monkeypatch.setattr(
            mod, "_destroy_bootstraps_without_state", lambda *a, **k: calls.append("destroyed")
        )
        mod.cmd_destroy_bootstraps(SimpleNamespace(name="DEV-01", force=True))
        # The guard runs BEFORE the state load, so it also covers the
        # orphan-tfstate path.
        assert calls == ["DEV-01", "destroyed"]

    def test_nodes_destroy_consults_the_guard_first(self, monkeypatch):
        from saorsa_deploy.cmd import destroy_nodes as mod

        calls: list[str] = []
        monkeypatch.setattr(
            mod,
            "refuse_while_client_extensions_exist",
            lambda name, console, what: calls.append(name),
        )
        monkeypatch.setattr(
            mod, "load_deployment_state", lambda name: (_ for _ in ()).throw(RuntimeError)
        )
        monkeypatch.setattr(
            mod, "_destroy_nodes_without_state", lambda *a, **k: calls.append("destroyed")
        )
        mod.cmd_destroy_nodes(SimpleNamespace(name="DEV-01", force=True))
        assert calls == ["DEV-01", "destroyed"]

    @pytest.mark.parametrize("by_name", [True, False])
    def test_deployments_down_refuses_on_both_forms(self, monkeypatch, tmp_path, by_name):
        from saorsa_deploy.cmd import deploy_cmds

        def refuse(name, console, what):
            raise SystemExit(2)

        monkeypatch.setattr(deploy_cmds, "refuse_while_client_extensions_exist", refuse)
        monkeypatch.setattr(deploy_cmds, "_execute", lambda *a, **k: pytest.fail("ran a step"))
        if by_name:
            args = SimpleNamespace(config=None, name="DEV-01", dry_run=False)
        else:
            testnet = _write(
                tmp_path,
                "t.yaml",
                """\
                name: DEV-01
                kind: testnet
                purpose: p
                testnet:
                  binaries: { ant_node: { version: "0.1.0" } }
                  nodes: { per_vm: 1, providers: { digital_ocean: { vm_count: 1 } } }
                """,
            )
            args = SimpleNamespace(config=str(testnet), set=[], name=None, dry_run=False)
        with pytest.raises(SystemExit) as exc:
            deploy_cmds.cmd_down(args)
        assert exc.value.code == 2

    def test_deployments_down_refuses_a_clients_manifest(self, monkeypatch, tmp_path):
        from saorsa_deploy.cmd import deploy_cmds

        monkeypatch.setattr(
            deploy_cmds, "refuse_while_client_extensions_exist", lambda *a, **k: None
        )
        monkeypatch.setattr(deploy_cmds, "_execute", lambda *a, **k: pytest.fail("ran a step"))
        path = _write(tmp_path, "ext.yaml", EXTENSION)
        args = SimpleNamespace(config=str(path), set=[], name=None, dry_run=False)
        with pytest.raises(SystemExit) as exc:
            deploy_cmds.cmd_down(args)
        assert exc.value.code == 2


# --------------------------------------------------------------------------- #
# teardown must not touch the deployment's address pool
# --------------------------------------------------------------------------- #
class TestDestroyLeavesTheAddressPool:
    def _run_destroy(self, extension):
        with (
            patch("saorsa_deploy.cmd.destroy_clients.load_client_state") as p_load,
            patch("saorsa_deploy.cmd.destroy_clients.delete_client_state") as p_delete_state,
            patch("saorsa_deploy.cmd.destroy_clients.update_client_state"),
            patch("saorsa_deploy.cmd.destroy_clients.delete_terraform_state"),
            patch(
                "saorsa_deploy.cmd.destroy_clients.delete_client_addresses_prefix"
            ) as p_delete_addrs,
            patch("saorsa_deploy.cmd.destroy_clients.run_terraform_destroy") as p_destroy,
            patch(
                "saorsa_deploy.cmd.destroy_clients.refuse_while_client_extensions_exist"
            ) as p_guard,
        ):
            p_load.return_value = {
                "name": "DEV-01",
                "clients": [
                    {
                        "role": "download",
                        "region": "lon1",
                        "vm_size": "s-2vcpu-4gb",
                        "client_index": 3,
                        "ip": "10.0.0.1",
                        "provider": "digitalocean",
                    }
                ],
            }
            p_destroy.return_value = MagicMock(success=True, stderr="")
            from saorsa_deploy.cmd.destroy_clients import cmd_destroy_clients

            cmd_destroy_clients(
                SimpleNamespace(
                    name="DEV-01",
                    extension=extension,
                    force=True,
                    wallet_secret_key=None,
                    skip_drain_failures=False,
                    ssh_key_path="/k",
                    skip_stop_services=True,
                )
            )
            return SimpleNamespace(
                destroy=p_destroy,
                delete_addrs=p_delete_addrs,
                delete_state=p_delete_state,
                guard=p_guard,
            )

    def test_extension_destroy_never_deletes_the_address_prefix(self):
        result = self._run_destroy("browsers")
        result.delete_addrs.assert_not_called()

    def test_extension_destroy_targets_its_own_tfstate_key(self):
        result = self._run_destroy("browsers")
        config = result.destroy.call_args.args[0]
        assert config.variables["name"] == "DEV-01-browsers"
        assert "DEV-01-browsers-download-lon1-3" in config.state_key

    def test_extension_destroy_deletes_only_its_own_state(self):
        result = self._run_destroy("browsers")
        result.delete_state.assert_called_once_with("DEV-01", "browsers")

    def test_the_deployments_own_destroy_still_wipes_the_prefix(self):
        result = self._run_destroy(None)
        result.delete_addrs.assert_called_once_with("DEV-01")
        assert result.destroy.call_args.args[0].variables["name"] == "DEV-01"

    def test_the_deployments_own_destroy_consults_the_guard(self):
        # It is the path that wipes the pool an extension's downloaders read,
        # and the one an operator is most likely to run by hand mid-run.
        result = self._run_destroy(None)
        assert result.guard.call_count == 1
        assert result.guard.call_args.args[0] == "DEV-01"

    def test_an_extensions_own_destroy_is_never_guarded(self):
        # It is the way out of the guard; guarding it would be a deadlock.
        result = self._run_destroy("browsers")
        result.guard.assert_not_called()

    def test_the_deployments_own_destroy_stops_when_an_extension_stands(self, monkeypatch):
        from saorsa_deploy.cmd import destroy_clients as mod

        def refuse(name, console, what):
            raise SystemExit(2)

        monkeypatch.setattr(mod, "refuse_while_client_extensions_exist", refuse)
        monkeypatch.setattr(
            mod,
            "delete_client_addresses_prefix",
            lambda name: pytest.fail("wiped the pool under a live extension"),
        )
        with pytest.raises(SystemExit) as exc:
            mod.cmd_destroy_clients(
                SimpleNamespace(
                    name="DEV-01",
                    extension=None,
                    force=True,
                    wallet_secret_key=None,
                    skip_drain_failures=False,
                    ssh_key_path="/k",
                    skip_stop_services=True,
                )
            )
        assert exc.value.code == 2


# --------------------------------------------------------------------------- #
# record-results
# --------------------------------------------------------------------------- #
class TestRecordResultsResolvesExtensionTransport:
    def _lookup(self, monkeypatch, own, extensions):
        from saorsa_deploy.cmd import record_results as mod

        def load(name, extension=None):
            doc = own if extension is None else extensions.get(extension)
            if doc is None:
                raise RuntimeError("absent")
            return {"name": name, "clients": doc}

        monkeypatch.setattr(mod, "load_client_state", load)
        monkeypatch.setattr(
            mod,
            "client_extension_tiers",
            lambda name: [(k, {"clients": v}) for k, v in sorted(extensions.items())],
        )
        return mod._build_client_kind_lookup(
            SimpleNamespace(name="DEV-01", deploy_args={"kind": "testnet"})
        )

    def test_wasm_extension_vm_resolves_as_wasm(self, monkeypatch):
        lookup = self._lookup(
            monkeypatch,
            own=[{"role": "download", "region": "lon1", "client_index": 1}],
            extensions={
                "browsers": [{"role": "wasm-download", "region": "lon1", "client_index": 1}]
            },
        )
        assert lookup("DEV-01-browsers-ant-client-wasm-download-lon1-1") == "wasm"
        # The deployment's own native VM is still native, under its own prefix.
        assert lookup("DEV-01-ant-client-download-lon1-1") == "native"

    def test_a_wasm_tier_in_an_extension_alone_still_enables_the_lookup(self, monkeypatch):
        # Without the extension in scope this returns a constant None ("no WASM
        # clients"), which would record every browser-path op as native.
        lookup = self._lookup(
            monkeypatch,
            own=None,
            extensions={
                "browsers": [{"role": "wasm-download", "region": "lon1", "client_index": 1}]
            },
        )
        assert lookup("DEV-01-browsers-ant-client-wasm-download-lon1-1") == "wasm"

    def test_native_only_deployment_is_unannotated(self, monkeypatch):
        lookup = self._lookup(
            monkeypatch,
            own=[{"role": "download", "region": "lon1", "client_index": 1}],
            extensions={"extra": [{"role": "download", "region": "lon1", "client_index": 2}]},
        )
        assert lookup("DEV-01-ant-client-download-lon1-1") is None

    def test_network_profile_map_covers_extension_vms(self, monkeypatch):
        from saorsa_deploy.cmd import record_results as mod

        monkeypatch.setattr(
            mod,
            "load_client_state",
            lambda name, extension=None: (_ for _ in ()).throw(RuntimeError),
        )
        monkeypatch.setattr(
            mod,
            "client_extension_tiers",
            lambda name: [
                (
                    "shaped",
                    {
                        "clients": [
                            {
                                "role": "download",
                                "region": "lon1",
                                "client_index": 2,
                                "network": "home-fttc",
                            }
                        ]
                    },
                )
            ],
        )
        assert mod._network_map_from_client_state("DEV-01") == {
            "DEV-01-shaped-ant-client-download-lon1-2": "home-fttc"
        }


# --------------------------------------------------------------------------- #
# generate-inventory
# --------------------------------------------------------------------------- #
def test_inventory_entry_names_its_extension():
    from saorsa_deploy.cmd.generate_inventory import _build_client_vms_field

    entries = _build_client_vms_field(
        "DEV-01",
        [
            {
                "role": "download",
                "region": "lon1",
                "client_index": 1,
                "ip": "10.0.0.1",
                "vm_size": "s-2vcpu-4gb",
            },
            {
                "role": "download",
                "region": "lon1",
                "client_index": 2,
                "ip": "10.0.0.2",
                "vm_size": "s-2vcpu-4gb",
                "extension": "browsers",
            },
        ],
        "/k",
    )
    assert entries[0]["name"] == "DEV-01-ant-client-download-lon1-1"
    # Absent, not None, on the deployment's own VMs: the entry shape for every
    # existing deployment is byte-identical, so the inventory schema version
    # does not move.
    assert "extension" not in entries[0]
    assert entries[1]["name"] == "DEV-01-browsers-ant-client-download-lon1-2"
    assert entries[1]["extension"] == "browsers"


# --------------------------------------------------------------------------- #
# the extension reuses the deployment's recorded client binary
# --------------------------------------------------------------------------- #
class TestBinaryReuse:
    def _args(self, **overrides):
        defaults = {
            "name": "DEV-01",
            "branch_name": None,
            "repo_owner": None,
            "ant_version": None,
            "wasm_branch_name": None,
            "wasm_repo_owner": None,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def _apply(self, monkeypatch, record, **overrides):
        from rich.console import Console

        import saorsa_deploy.state as state
        from saorsa_deploy.cmd import provision_clients as mod

        monkeypatch.setattr(
            state,
            "load_deployment_state",
            lambda name: {"name": name, **({"client_binaries": record} if record else {})},
        )
        args = self._args(**overrides)
        mod._apply_recorded_client_binaries(args, Console())
        return args

    def test_branch_build_is_reused(self, monkeypatch):
        args = self._apply(
            monkeypatch,
            {
                "ant": {"branch": "feature-x", "repo_owner": "myorg", "version": None},
                "ant_wasm": {"branch": "rc-tag", "repo_owner": "WithAutonomi"},
            },
        )
        assert (args.branch_name, args.repo_owner) == ("feature-x", "myorg")
        assert (args.wasm_branch_name, args.wasm_repo_owner) == ("rc-tag", "WithAutonomi")

    def test_release_version_is_reused(self, monkeypatch):
        args = self._apply(
            monkeypatch,
            {"ant": {"branch": None, "repo_owner": None, "version": "0.3.11"}, "ant_wasm": {}},
        )
        assert args.ant_version == "0.3.11"
        assert args.branch_name is None

    def test_an_explicit_flag_wins(self, monkeypatch):
        args = self._apply(
            monkeypatch,
            {"ant": {"branch": "recorded", "repo_owner": "them", "version": None}, "ant_wasm": {}},
            branch_name="mine",
            repo_owner="me",
        )
        assert (args.branch_name, args.repo_owner) == ("mine", "me")

    def test_no_record_and_no_flags_exits_2(self, monkeypatch):
        # Reusing "latest release" silently could install a different build from
        # the one the deployment is running, which is the single thing this kind
        # exists to prevent.
        with pytest.raises(SystemExit) as exc:
            self._apply(monkeypatch, None)
        assert exc.value.code == 2

    def test_a_missing_deployment_exits_2(self, monkeypatch):
        from rich.console import Console

        import saorsa_deploy.state as state
        from saorsa_deploy.cmd import provision_clients as mod

        monkeypatch.setattr(
            state,
            "load_deployment_state",
            lambda name: (_ for _ in ()).throw(RuntimeError("no state")),
        )
        with pytest.raises(SystemExit) as exc:
            mod._apply_recorded_client_binaries(self._args(), Console())
        assert exc.value.code == 2

    def test_the_deployments_own_provision_records_them(self, monkeypatch):
        from rich.console import Console

        import saorsa_deploy.state as state
        from saorsa_deploy.cmd import provision_clients as mod

        written: dict = {}
        monkeypatch.setattr(
            state,
            "update_deployment_state",
            lambda name, updates: written.update(name=name, **updates),
        )
        mod._record_client_binaries(
            self._args(branch_name="feature-x", repo_owner="myorg"), Console()
        )
        assert written["name"] == "DEV-01"
        assert written["client_binaries"]["ant"]["branch"] == "feature-x"
        # The WASM owner/branch fall back to the native pair, exactly as
        # `_resolve_wasm_bundle` does when only --branch-name is given.
        assert written["client_binaries"]["ant_wasm"]["repo_owner"] == "myorg"


# --------------------------------------------------------------------------- #
# `clients down`: --name means the deployment, and only on the state path
# --------------------------------------------------------------------------- #
class TestDownFlagCombinations:
    def _down(self, monkeypatch, **args):
        from saorsa_deploy.cmd import clients_cmds as mod

        monkeypatch.setattr(mod, "_execute", lambda *a, **k: pytest.fail("ran a step"))
        defaults = {
            "config": None,
            "set": [],
            "name": None,
            "extension": None,
            "ssh_key_path": None,
            "dry_run": True,
            "skip_drain_failures": False,
        }
        defaults.update(args)
        return mod.cmd_clients_down(SimpleNamespace(**defaults))

    @pytest.mark.parametrize(
        ("extra", "expected"),
        [
            ({"name": "DEV-01"}, "--name"),
            ({"extension": "browsers"}, "--extension"),
            ({"name": "DEV-01", "extension": "browsers"}, "--name and --extension"),
        ],
    )
    def test_name_and_extension_are_refused_with_config(
        self, monkeypatch, tmp_path, capsys, extra, expected
    ):
        # `--name` is the DEPLOYMENT on the state path but overrides the
        # manifest's `name:` — the EXTENSION — with --config. Typed out of
        # habit it would aim the teardown at an extension called "DEV-01",
        # report "Nothing to destroy", and leave the real cohort billing.
        path = _write(tmp_path, "ext.yaml", EXTENSION)
        with pytest.raises(SystemExit) as exc:
            self._down(monkeypatch, config=str(path), **extra)
        assert exc.value.code == 2
        out = capsys.readouterr().out
        assert expected in out
        assert "cannot be combined with --config" in out

    def test_config_alone_is_accepted(self, monkeypatch, tmp_path, capsys):
        path = _write(tmp_path, "ext.yaml", EXTENSION)
        self._down(monkeypatch, config=str(path))
        assert "extra-downloaders (extension of DEV-01)" in capsys.readouterr().out

    def test_name_and_extension_without_config_destroy_from_state(self, monkeypatch, capsys):
        from saorsa_deploy.cmd import clients_cmds as mod

        monkeypatch.setattr(
            mod, "load_client_state", lambda name, ext: {"clients": [], "ssh_key_path": "/k"}
        )
        self._down(monkeypatch, name="DEV-01", extension="browsers")
        out = capsys.readouterr().out
        assert "browsers (extension of DEV-01)" in out

    def test_name_alone_is_refused(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            self._down(monkeypatch, name="DEV-01")
        assert exc.value.code == 2
        assert "--name <deployment> --extension <name>" in capsys.readouterr().out


def test_clients_manifest_name_help_describes_the_extension():
    # `--name` on a `kind: clients` manifest command overrides the EXTENSION
    # name; the shared wording ("deployment name") describes the wrong thing.
    from saorsa_deploy.main import _CLIENTS_NAME_HELP

    assert "EXTENSION" in _CLIENTS_NAME_HELP
    assert "deployment" in _CLIENTS_NAME_HELP


class TestPlanIsReadOnlyAndNeverRaises:
    """`clients plan` is advertised as working without the deployment being up.

    So neither a missing deployment nor an unreachable bucket may raise at the
    operator: each is a note on the plan, and the indexes shown are flagged as
    probably wrong. `clients up` makes the opposite choice on the same failures.
    """

    def _note(self, monkeypatch, loader):
        from saorsa_deploy.cmd import clients_cmds as mod

        monkeypatch.setattr(mod, "load_deployment_state", loader)
        manifest = SimpleNamespace(deployment="DEV-01", name="browsers")
        return mod._base_indexes_or_note(manifest)

    def test_missing_deployment_is_a_note(self, monkeypatch):
        base, note = self._note(
            monkeypatch, lambda name: (_ for _ in ()).throw(RuntimeError("no state"))
        )
        assert base is None
        assert "no state for deployment 'DEV-01'" in note

    def test_unreachable_state_is_a_note(self, monkeypatch):
        base, note = self._note(
            monkeypatch, lambda name: (_ for _ in ()).throw(OSError("credentials"))
        )
        assert base is None
        assert "could not be read" in note
        assert "probably wrong" in note

    def test_a_failed_index_scan_is_a_note(self, monkeypatch):
        from saorsa_deploy.cmd import clients_cmds as mod

        monkeypatch.setattr(mod, "load_deployment_state", lambda name: {"name": name})
        monkeypatch.setattr(
            mod,
            "highest_client_indexes",
            lambda name, exclude=None: (_ for _ in ()).throw(OSError("s3 unreachable")),
        )
        base, note = mod._base_indexes_or_note(SimpleNamespace(deployment="DEV-01", name="b"))
        assert base is None
        assert "refuses rather than allocate" in note

    def test_up_refuses_where_plan_only_notes(self, monkeypatch, capsys):
        from rich.console import Console

        from saorsa_deploy.cmd import clients_cmds as mod

        monkeypatch.setattr(
            mod,
            "highest_client_indexes",
            lambda name, exclude=None: (_ for _ in ()).throw(OSError("s3 unreachable")),
        )
        with pytest.raises(SystemExit) as exc:
            mod._base_indexes_or_exit(
                SimpleNamespace(deployment="DEV-01", name="b"), Console(width=200)
            )
        assert exc.value.code == 2
        assert "would erase its S3 address list" in capsys.readouterr().out
