"""The pointer role as it reaches every site that dispatches on a role.

A role is only real if it survives the whole path an operator uses: a manifest
key, through validation, through the argv `deployments up` emits, into the spec
parser, and out the other side into the VM's systemd unit, its Terraform
variable, its wallet, its log forwarder and its teardown.

The WASM tier is the reason this file is written the way it is. It missed
several of these sites, and every one of them fails SILENTLY — a role the code
does not recognise is skipped, not rejected, so the VM is created, billed and
then does nothing. Each test below is one of those hops.
"""

import argparse
from pathlib import Path

import pytest
import yaml

from saorsa_deploy.client_roles import (
    ALL_CLIENT_ROLES,
    ROLE_POINTER,
    is_pointer,
    is_upload,
    is_wasm,
    unit_for_role,
)
from saorsa_deploy.cmd.infra_clients import (
    DEFAULT_POINTER_CHAIN_FRACTION,
    DEFAULT_POINTER_CREATE_INTERVAL_SECS,
    DEFAULT_POINTER_MAX,
    DEFAULT_POINTER_SWEEP_INTERVAL_SECS,
    DEFAULT_POINTER_TRANSFER_INTERVAL_SECS,
    DEFAULT_POINTER_UPDATE_INTERVAL_SECS,
    parse_pointer_spec,
)
from saorsa_deploy.config import profiles_dir
from saorsa_deploy.config.models import (
    POINTER_CHAIN_FRACTION,
    POINTER_CREATE_INTERVAL_SECS,
    POINTER_SWEEP_INTERVAL_SECS,
    POINTER_TRANSFER_INTERVAL_SECS,
    POINTER_UPDATE_INTERVAL_SECS,
    Manifest,
)
from saorsa_deploy.orchestrator import build_deploy_steps
from saorsa_deploy.provisioning.client_clean import ALL_CLIENT_UNITS

RESOURCES = Path(__file__).parent.parent / "saorsa_deploy" / "resources"

_MANIFEST = {
    "name": "PTR-TEST",
    "kind": "testnet",
    "purpose": "pointer plumbing",
    "ssh_key_path": "~/.ssh/ant_deploy",
    "testnet": {
        "mode": True,
        "binaries": {"ant_node": {"version": "0.20.0"}, "ant": {"version": "0.3.8"}},
        "bootstraps": {"count": 3, "port": 10000},
        "nodes": {"per_vm": 5, "providers": {"digital_ocean": {"vm_count": 3}}},
        "clients": {
            "pointers": [
                {"provider": "digital_ocean", "region": "lon1", "eth": "0.5"},
                {
                    "provider": "digital_ocean",
                    "region": "syd1",
                    "eth": "0.5",
                    "create_interval_secs": 120,
                    "update_interval_secs": 90,
                    "sweep_interval_secs": 300,
                    "chain_fraction": 0.25,
                    "transfer_interval_secs": 300,
                    "pointer_max": 400,
                },
            ]
        },
    },
}


def _manifest(**client_overrides) -> Manifest:
    data = yaml.safe_load(yaml.safe_dump(_MANIFEST))
    data["testnet"]["clients"].update(client_overrides)
    return Manifest.model_validate(data)


def _clients_infra_argv(manifest) -> list[str]:
    for step in build_deploy_steps(manifest):
        if step.argv[:2] == ["clients", "infra"]:
            return step.argv
    raise AssertionError("no `clients infra` step was emitted")


# ---------------------------------------------------------------------------
# Manifest -> argv -> spec parser
# ---------------------------------------------------------------------------


def test_a_pointer_entry_reaches_clients_infra_as_a_pointer_spec():
    argv = _clients_infra_argv(_manifest())
    specs = [argv[i + 1] for i, a in enumerate(argv) if a == "--pointer"]
    assert specs == [
        "digital_ocean:lon1:eth=0.5",
        (
            "digital_ocean:syd1:eth=0.5:create_interval_secs=120:"
            "update_interval_secs=90:sweep_interval_secs=300:"
            "chain_fraction=0.25:transfer_interval_secs=300:pointer_max=400"
        ),
    ]


def test_every_emitted_pointer_spec_parses_back():
    """The argv the orchestrator writes must be argv the CLI accepts.

    Guarded because the two are written in different modules and only ever meet
    at deploy time: an option the manifest can express and the parser rejects is
    a run that dies after the nodes are up and billing.
    """
    argv = _clients_infra_argv(_manifest())
    specs = [argv[i + 1] for i, a in enumerate(argv) if a == "--pointer"]
    parsed = [parse_pointer_spec(spec) for spec in specs]
    assert [p["role"] for p in parsed] == [ROLE_POINTER, ROLE_POINTER]
    assert parsed[1]["create_interval_secs"] == 120
    assert parsed[1]["update_interval_secs"] == 90
    assert parsed[1]["sweep_interval_secs"] == 300
    assert parsed[1]["chain_fraction"] == 0.25
    assert parsed[1]["transfer_interval_secs"] == 300
    assert parsed[1]["pointer_max"] == 400


def test_defaults_are_omitted_from_the_spec_and_restored_by_the_parser():
    """A default-rate entry emits the short spec, and parses back to the defaults.

    The argv is what the step journal compares, so a token that narrows nothing
    is not free: it would make a resumed `up` re-run the step.
    """
    argv = _clients_infra_argv(_manifest())
    first = argv[argv.index("--pointer") + 1]
    assert "create_interval_secs" not in first
    # The handover loop is OFF by default, so its key is absent too -- the point
    # being that an unchanged manifest's argv is byte-identical to what it was
    # before handovers existed, and a resumed `up` skips the step.
    assert "transfer_interval_secs" not in first
    parsed = parse_pointer_spec(first)
    assert parsed["create_interval_secs"] == DEFAULT_POINTER_CREATE_INTERVAL_SECS
    assert parsed["update_interval_secs"] == DEFAULT_POINTER_UPDATE_INTERVAL_SECS
    assert parsed["sweep_interval_secs"] == DEFAULT_POINTER_SWEEP_INTERVAL_SECS
    assert parsed["chain_fraction"] == DEFAULT_POINTER_CHAIN_FRACTION
    assert parsed["transfer_interval_secs"] == DEFAULT_POINTER_TRANSFER_INTERVAL_SECS
    assert parsed["pointer_max"] == DEFAULT_POINTER_MAX


def test_the_cli_and_schema_defaults_agree():
    """Duplicated deliberately (the CLI must not import the schema), so checked.

    A drift here would mean a manifest that took the defaults and a `clients
    infra` invoked by hand produced clients running at different rates — and the
    rates are what the run costs.
    """
    assert DEFAULT_POINTER_CREATE_INTERVAL_SECS == POINTER_CREATE_INTERVAL_SECS
    assert DEFAULT_POINTER_UPDATE_INTERVAL_SECS == POINTER_UPDATE_INTERVAL_SECS
    assert DEFAULT_POINTER_SWEEP_INTERVAL_SECS == POINTER_SWEEP_INTERVAL_SECS
    assert DEFAULT_POINTER_CHAIN_FRACTION == POINTER_CHAIN_FRACTION
    assert DEFAULT_POINTER_TRANSFER_INTERVAL_SECS == POINTER_TRANSFER_INTERVAL_SECS


def test_rust_log_stays_last_in_a_pointer_spec():
    """Its value may contain `::`, which is the spec delimiter doubled."""
    manifest = _manifest(
        pointers=[
            {
                "provider": "digital_ocean",
                "region": "lon1",
                "rust_log": "ant_core::data::client::pointer=debug",
                "pointer_max": 10,
            }
        ]
    )
    argv = _clients_infra_argv(manifest)
    spec = argv[argv.index("--pointer") + 1]
    assert spec.endswith(":rust_log=ant_core::data::client::pointer=debug")
    parsed = parse_pointer_spec(spec)
    assert parsed["rust_log"] == "ant_core::data::client::pointer=debug"
    assert parsed["pointer_max"] == 10


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


def test_a_pointer_client_requires_the_ant_binary():
    """It drives `ant pointer`, so `binaries.ant` is as required as for a native
    transfer client. Without this check the omission surfaces as a client VM
    with no binary on it."""
    data = yaml.safe_load(yaml.safe_dump(_MANIFEST))
    del data["testnet"]["binaries"]["ant"]
    with pytest.raises(ValueError, match="binaries.ant is required"):
        Manifest.model_validate(data)


def test_pointers_are_not_subject_to_the_uploader_downloader_pairing_rule():
    """A single pointer client is legal; an uploader without a downloader is not.

    They are different mechanisms: a downloader consumes what an uploader
    publishes, while a pointer client publishes and consumes pointer addresses
    of its own. Applying the pairing rule to it would refuse the simplest useful
    configuration."""
    manifest = _manifest()
    assert not manifest.testnet.clients.uploaders
    assert manifest.testnet.clients.has_pointers


@pytest.mark.parametrize(
    "override, message",
    [
        ({"create_interval_secs": 0}, "must be > 0"),
        ({"update_interval_secs": -1}, "must be > 0"),
        ({"sweep_interval_secs": 0}, "must be > 0"),
        ({"chain_fraction": 1.5}, "between 0 and 1"),
        ({"pointer_max": -1}, "must be >= 0"),
        # 0 is legal here and only here -- it means "no handover loop" -- but a
        # negative is still nonsense.
        ({"transfer_interval_secs": -1}, "must be >= 0"),
    ],
)
def test_an_unrunnable_workload_is_refused_at_validation(override, message):
    """Before any VM exists. A zero interval is a busy loop against an operation
    that costs money on every iteration."""
    with pytest.raises(ValueError, match=message):
        _manifest(pointers=[{"provider": "digital_ocean", "region": "lon1", **override}])


# ---------------------------------------------------------------------------
# The sites that dispatch on a role. Each of these is one the WASM tier missed.
# ---------------------------------------------------------------------------


def test_the_role_has_its_own_unit():
    assert unit_for_role(ROLE_POINTER) == "ant-pointer"
    assert ROLE_POINTER in ALL_CLIENT_ROLES


def test_the_role_is_an_upload_type_role_but_not_wasm():
    """It holds a funded wallet (every write is paid), so it must inherit the
    per-VM wallet mint, the top-up and the drain on destroy. It is not WASM, so
    it must not attract the Node install or the WASM unit."""
    assert is_upload(ROLE_POINTER)
    assert is_pointer(ROLE_POINTER)
    assert not is_wasm(ROLE_POINTER)


def test_clients_clean_removes_the_pointer_unit():
    """`clients clean` is handed IPs, never roles, so it must stop and remove
    EVERY role's unit. It carried a two-entry hard-coded list until the WASM
    roles were added, which left their units running on a 'cleaned' VM."""
    assert unit_for_role(ROLE_POINTER) in ALL_CLIENT_UNITS
    assert set(ALL_CLIENT_UNITS) == {unit_for_role(r) for r in ALL_CLIENT_ROLES}


def test_the_destroy_time_service_stop_uses_each_vm_s_own_unit():
    """It hard-coded `ant-uploader`, so a pointer client was drained while it was
    still spending — the nonce race the stop exists to prevent."""
    from saorsa_deploy.cmd.destroy_clients import _stop_uploader_services

    source = _stop_uploader_services.__code__.co_names
    assert "unit_for_role" in source


def test_every_client_terraform_module_accepts_the_role():
    """The `role` variable is validated in each module, so a role missing from
    one provider's list fails the apply for that provider alone — which reads as
    a provider outage rather than a missing branch."""
    for provider in ("digitalocean", "hetzner", "ovh", "vultr"):
        variables = (RESOURCES / f"{provider}-client" / "variables.tf").read_text()
        assert f'"{ROLE_POINTER}"' in variables, provider


def test_the_forward_log_files_cover_the_pointer_role():
    """`_FORWARD_ROLE_FILES` is a per-role map and the loop only visits the roles
    in it, so a role missing from it forwards nothing while reporting success."""
    from saorsa_deploy.provisioning.client import AntClientProvisioner

    assert ROLE_POINTER in AntClientProvisioner._FORWARD_ROLE_FILES


def test_the_infra_summary_names_the_role_rather_than_guessing():
    """The final summary picked `Uploader` or `Downloader` from a ternary, so
    every WASM and pointer VM was reported as a downloader in the one place an
    operator checks what was created."""
    import inspect

    from saorsa_deploy.cmd.infra_clients import cmd_infra_clients

    source = inspect.getsource(cmd_infra_clients)
    assert '"Uploader" if spec["role"]' not in source


def test_the_wallet_mint_names_the_role_it_is_minting_for():
    """`client_index` restarts at 1 per role, so a native uploader and a pointer
    client in the same region both print "#1". With a literal "Uploader" prefix
    that is two identical lines against two different wallet addresses, in the
    one place an operator reads to see which wallet belongs to which VM."""
    import inspect

    from saorsa_deploy.cmd.provision_clients import (
        _ensure_uploader_wallets,
        _wallet_label,
    )

    assert _wallet_label({"role": ROLE_POINTER, "client_index": 1}) == ("Pointer client #1")
    assert _wallet_label({"role": "upload", "client_index": 1}) == "Uploader #1"
    assert "Uploader #" not in inspect.getsource(_ensure_uploader_wallets)

    # Same for the drain on destroy: that output is the audit trail for money
    # coming back, so it has to name the VM the wallet belonged to.
    from saorsa_deploy.cmd.destroy_clients import _drain_uploader_wallets
    from saorsa_deploy.cmd.destroy_clients import _wallet_label as drain_label

    assert drain_label({"role": ROLE_POINTER, "client_index": 2}) == ("Pointer client #2")
    assert "Uploader #" not in inspect.getsource(_drain_uploader_wallets)


def test_generate_inventory_covers_the_role():
    from saorsa_deploy.cmd.generate_inventory import _build_client_vms_field

    entries = _build_client_vms_field(
        "PTR-TEST",
        [
            {
                "role": ROLE_POINTER,
                "region": "lon1",
                "client_index": 1,
                "ip": "10.0.0.1",
                "provider": "digitalocean",
                "create_interval_secs": 60,
                "update_interval_secs": 30,
                "sweep_interval_secs": 600,
                "chain_fraction": 0.1,
                "transfer_interval_secs": 300,
                "pointer_max": 0,
            }
        ],
        "~/.ssh/ant_deploy",
    )
    assert entries[0]["name"] == "PTR-TEST-ant-client-pointer-lon1-1"
    assert entries[0]["services"] == ["ant-pointer"]
    config = entries[0]["pointer_config"]
    assert config["create_interval_secs"] == 60
    # The paths are the teardown-critical part: nothing collects the owner keys,
    # ownership is immutable, and they die with the VM.
    assert config["transfer_interval_secs"] == 300
    assert config["paths"]["owner_keys"].endswith("/keys")
    assert config["paths"]["ledger"].endswith("ledger.json")
    assert config["paths"]["published_transfers"].endswith("pointer-transfers-lon1-1.txt")


def test_estimate_gas_sizes_a_pointer_client_from_the_manifest_rates():
    """No baseline needed: the rates are deterministic, which is the whole reason
    a pointer client can be sized from the manifest alone."""
    from saorsa_deploy.cmd.estimate_client_gas import _resolve_uploaders
    from saorsa_deploy.gas_model import POINTER

    entries, label = _resolve_uploaders(_manifest())
    assert label == "testnet.clients"
    assert [transport for _, transport in entries] == [POINTER, POINTER]
    # create/60s + update/30s = 60 + 120 writes an hour. No handover loop on the
    # first entry, so it is unchanged by V2-1354.
    assert entries[0][0].writes_per_hour == pytest.approx(180.0)
    # create/120s + update/90s = 30 + 40, plus TWO paid writes per handover
    # cycle at transfer/300s = 24. A handover is a recipient create AND a
    # transfer, and sizing it as one write would under-fund the wallet.
    assert entries[1][0].writes_per_hour == pytest.approx(94.0)


def test_slack_post_renders_a_pointer_client_as_one():
    """It is an upload-type role, so without its own branch it renders with a
    payment mode and a file size that mean nothing here."""
    from saorsa_deploy.cmd.slack_post import _client_line

    line = _client_line(
        {
            "role": ROLE_POINTER,
            "region": "lon1",
            "vm_size": "s-2vcpu-4gb",
            "create_interval_secs": 60,
            "update_interval_secs": 30,
            "sweep_interval_secs": 600,
            "chain_fraction": 0.1,
            "transfer_interval_secs": 300,
        }
    )
    assert line.startswith("• pointer — lon1")
    assert "create/60s" in line
    assert "transfer/300s" in line
    assert "payment" not in line


def test_slack_post_omits_the_handover_rate_when_there_is_no_handover_loop():
    """0 is "no handovers", and `transfer/0s` would read as a rate."""
    from saorsa_deploy.cmd.slack_post import _client_line

    line = _client_line(
        {
            "role": ROLE_POINTER,
            "region": "lon1",
            "vm_size": "s-2vcpu-4gb",
            "create_interval_secs": 60,
            "update_interval_secs": 30,
            "sweep_interval_secs": 600,
            "transfer_interval_secs": 0,
        }
    )
    assert "transfer/" not in line


def test_the_argv_is_stable_across_a_reload():
    """`up` is resumable and the journal compares argv, so a manifest that has
    not changed must emit byte-identical steps twice."""
    assert _clients_infra_argv(_manifest()) == _clients_infra_argv(_manifest())


def test_an_extension_manifest_carries_pointer_clients_too():
    """`extension.clients` is the same shape, and an extension is exactly where a
    second client-version cohort would want its own pointer tier."""
    data = yaml.safe_load(yaml.safe_dump(_MANIFEST))
    data["kind"] = "testnet-extension"
    extension = data.pop("testnet")
    extension["source"] = "PTR-SOURCE"
    for key in ("mode", "bootstraps"):
        extension.pop(key, None)
    data["extension"] = extension
    manifest = Manifest.model_validate(data)
    argv = _clients_infra_argv(manifest)
    assert argv.count("--pointer") == 2


def test_the_cli_parser_accepts_the_pointer_flag():
    """A flag the orchestrator emits and the parser has never heard of fails at
    deploy time, not at plan time."""
    from saorsa_deploy.main import main

    parser_error = {}
    try:
        main(["clients", "infra", "--name", "X", "--pointer"])
    except SystemExit as exc:
        parser_error["code"] = exc.code
    # `--pointer` with no value is an argparse error (2), NOT "unrecognised
    # argument" — which is what an unwired flag would give.
    assert parser_error["code"] == 2


def test_an_unknown_pointer_spec_key_is_rejected_by_name():
    with pytest.raises(ValueError, match="unknown key 'concurrency'"):
        parse_pointer_spec("digital_ocean:lon1:concurrency=4")


def test_argparse_namespace_without_pointer_is_still_accepted():
    """`cmd_infra_clients` reads the flag with getattr, so a namespace built
    before this existed (an old runbook, a test) must not crash."""
    from saorsa_deploy.cmd.infra_clients import cmd_infra_clients

    args = argparse.Namespace(name="X", uploader=[], downloader=[])
    with pytest.raises(SystemExit):
        cmd_infra_clients(args)


# ---------------------------------------------------------------------------
# The spec -> /etc/ant-client.env hop
# ---------------------------------------------------------------------------


def _client_provisioner(**overrides):
    from unittest.mock import MagicMock

    from saorsa_deploy.provisioning.client import AntClientProvisioner

    kwargs = dict(
        clients=[],
        bootstrap_peers=["1.2.3.4:10000"],
        aws_access_key_id="k",
        aws_secret_access_key="s",
        addresses_s3_bucket="bucket",
        addresses_s3_prefix="saorsa-deploy/addresses/PTR-TEST",
        console=MagicMock(),
    )
    kwargs.update(overrides)
    return AntClientProvisioner(**kwargs)


def test_every_pointer_setting_reaches_the_env_file():
    """The one hop that was unguarded: spec -> case block -> `/etc/ant-client.env`.

    `pointer_client.py` reads its whole configuration from the environment and
    from nowhere else, so a variable the case block sets but the heredoc never
    exports is a setting the service silently runs without — at the script's own
    default, which for `TRANSFER_INTERVAL_SECS` means no handover loop at all on
    a run that asked for one.
    """
    from unittest.mock import patch

    from saorsa_deploy.provisioning.client import AntClientProvisioner

    with (
        patch.object(
            AntClientProvisioner, "_presigned_put_url", return_value="https://put/addresses"
        ),
        patch.object(
            AntClientProvisioner,
            "_presigned_transfers_put_url",
            return_value="https://put/transfers",
        ),
    ):
        provisioner = _client_provisioner(
            clients=[
                {
                    "ip": "10.0.0.1",
                    "role": ROLE_POINTER,
                    "region": "lon1",
                    "client_index": 1,
                    "file_size_mb": 0,
                    "create_interval_secs": 120,
                    "update_interval_secs": 90,
                    "sweep_interval_secs": 300,
                    "chain_fraction": 0.25,
                    "transfer_interval_secs": 300,
                    "pointer_max": 400,
                }
            ]
        )
        ip_to_env = provisioner._build_ip_to_env("0xdead")
        env = ip_to_env["10.0.0.1"]
        case_block = provisioner._case_entry("10.0.0.1", env)
        script = provisioner._env_file_script(case_block, "0xdead")

    # Every key `_pointer_env` produces must be exported under its upper-cased
    # name. Asserted over the keys rather than over a hard-coded list, so a
    # future setting cannot be added to one half of the hop alone.
    for key in provisioner._pointer_env(provisioner.clients[0]):
        assert f"\n{key.upper()}=$" in script, key

    # And the case block must actually carry this client's values, not defaults.
    assert "CREATE_INTERVAL_SECS=120" in case_block
    assert "TRANSFER_INTERVAL_SECS=300" in case_block
    assert 'TRANSFERS_PRESIGNED_URL="https://put/transfers"' in case_block
    assert "POINTER_MAX=400" in case_block


def test_the_handover_list_is_a_distinct_s3_object():
    """`pointer-transfers-*` beside `pointer-*`, never inside it.

    The address list is read by every client as a set of readable pointers and
    its reader takes the first field of each line, so a `<old> <recipient>` pair
    written there would publish the old address with the recipient silently
    dropped — leaving a reader with an address whose expected state it does not
    know.
    """
    provisioner = _client_provisioner(
        aws_access_key_id="AKIAEXAMPLE", aws_secret_access_key="x" * 40
    )
    addresses = provisioner._presigned_put_url(ROLE_POINTER, "lon1", 1)
    transfers = provisioner._presigned_transfers_put_url("lon1", 1)
    assert "/saorsa-deploy/addresses/PTR-TEST/pointer-lon1-1.txt" in addresses
    assert "/saorsa-deploy/addresses/PTR-TEST/pointer-transfers-lon1-1.txt" in transfers


def test_a_non_pointer_client_gets_no_handover_variables_set():
    """An uploader's case entry must be byte-identical to what it was before.

    The pointer values are emitted for every role (the case block is one shape),
    so what matters is that they are all the inert zero/empty: a run with no
    pointer clients renders exactly as it did before handovers existed.
    """
    from unittest.mock import patch

    from saorsa_deploy.provisioning.client import AntClientProvisioner

    with patch.object(AntClientProvisioner, "_presigned_put_url", return_value=""):
        provisioner = _client_provisioner(
            clients=[
                {
                    "ip": "10.0.0.2",
                    "role": "download",
                    "region": "lon1",
                    "client_index": 1,
                }
            ]
        )
        env = provisioner._build_ip_to_env("0xdead")["10.0.0.2"]
    entry = provisioner._case_entry("10.0.0.2", env)
    assert "TRANSFER_INTERVAL_SECS=0" in entry
    assert 'TRANSFERS_PRESIGNED_URL=""' in entry


# ---------------------------------------------------------------------------
# The shipped profiles
# ---------------------------------------------------------------------------

# The profiles are package data now, so ask the package where they are rather
# than walking the checkout — that is also what the loader does.
_PROFILES = profiles_dir()


def test_the_pointers_profile_validates_and_carries_the_handover_loop():
    manifest = Manifest.model_validate(yaml.safe_load((_PROFILES / "pointers.yaml").read_text()))
    entries = manifest.testnet.clients.pointers
    assert len(entries) == 2
    assert [p.transfer_interval_secs for p in entries] == [300, 300]


def test_the_staging_profile_carries_the_handover_loop_on_both_entries():
    """Validated through a run manifest that fills in the RC binaries, which is
    how the release train uses it: `staging.yaml` is a BASE profile and has no
    `binaries:` of its own, so it does not validate standalone.
    """
    data = yaml.safe_load((_PROFILES / "staging.yaml").read_text())
    assert "binaries" not in data["testnet"]
    # The run manifest supplies the name, the purpose and the RC binaries.
    data["name"] = "STG-01"
    data["purpose"] = "staging run"
    data["testnet"]["binaries"] = {
        "ant_node": {"version": "0.21.0"},
        "ant": {"version": "0.3.9"},
        # The profile carries a WASM tier too, so the bundle ref is required.
        "ant_wasm": {"branch": "main", "repo_owner": "WithAutonomi"},
    }
    manifest = Manifest.model_validate(data)
    entries = manifest.testnet.clients.pointers
    assert len(entries) == 2
    assert [p.transfer_interval_secs for p in entries] == [300, 300]
    # Two paid writes a cycle on top of the default 180/h, which is what
    # `clients estimate-gas` has to size the wallet for.
    assert entries[0].writes_per_hour == pytest.approx(204.0)


def test_an_absent_handover_key_matches_a_request_for_the_default():
    """Client state recorded before V2-1354 carries no `transfer_interval_secs`,
    and "absent" means exactly what `0` means: no handover loop. Without this a
    resumed `up` would call those VMs a spec MISMATCH and refuse to reuse VMs
    running precisely what was asked for.
    """
    from saorsa_deploy.cmd.infra_clients import _spec_signature

    recorded = {"role": ROLE_POINTER, "region": "lon1", "client_index": 1}
    requested = dict(recorded, transfer_interval_secs=0)
    assert _spec_signature(recorded) == _spec_signature(requested)
    # A request for the loop still mismatches: the rates are what a run costs.
    assert _spec_signature(recorded) != _spec_signature(dict(recorded, transfer_interval_secs=300))
