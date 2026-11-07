"""The hot-file reader client role, end to end (INFRA-531).

`downloader.sh` runs one process per VM and draws a random address out of the
whole pool, so it never puts two readers on one file together -- which is the
only condition under which the ADR-0026 peer swarm forms at all. The browser
role does arrange it, but only inside Chromium. This role is the native mirror
of that one, and this file is modelled on `test_browser_downloaders.py`, which
is this codebase's pattern for a new client role: roles, spec parsing, manifest
validation, orchestrator emission, provisioner rendering, the service's own
arithmetic, and the "everything else is unchanged" guards.
"""

import importlib.util
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from saorsa_deploy.client_roles import (
    ALL_CLIENT_ROLES,
    DOWNLOAD_ROLES,
    HOT_FILE_MAX_INSTANCES,
    NATIVE_ROLES,
    ROLE_HOT_FILE_READ,
    is_browser,
    is_hot_file_read,
    is_pointer,
    is_upload,
    is_wasm,
    unit_for_role,
)
from saorsa_deploy.cmd.infra_clients import parse_hot_file_reader_spec
from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator import build_deploy_steps
from saorsa_deploy.orchestrator.clients import (
    hot_file_stagger_notes,
    planned_client_vms,
)
from saorsa_deploy.provisioning.client import AntClientProvisioner
from saorsa_deploy.provisioning.client_clean import ALL_CLIENT_UNITS

SSH = "~/.ssh/ant_deploy"
SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "saorsa_deploy"
    / "resources"
    / "scripts"
    / "hot_file_reader.py"
)


@pytest.fixture(scope="module")
def hfr():
    """Import the reader script by path.

    It is deliberately import-free of `saorsa_deploy` (it is copied verbatim
    onto the client VM), so it is not importable as a package module.
    """
    spec = importlib.util.spec_from_file_location("hot_file_reader", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# roles
# --------------------------------------------------------------------------- #
class TestRole:
    def test_has_its_own_unit(self):
        assert unit_for_role(ROLE_HOT_FILE_READ) == "ant-hot-file-reader"

    def test_is_a_native_download_role(self):
        # Native, not browser or WASM: it drives the `ant` binary, so
        # `_native_only` keeps its hosts and the unit writer needs its own arm.
        assert is_hot_file_read(ROLE_HOT_FILE_READ)
        assert not is_wasm(ROLE_HOT_FILE_READ)
        assert not is_browser(ROLE_HOT_FILE_READ)
        assert ROLE_HOT_FILE_READ in DOWNLOAD_ROLES
        assert ROLE_HOT_FILE_READ in NATIVE_ROLES

    def test_carries_no_wallet(self):
        # It only reads, and what it reads is named by `select-hot-files`.
        assert not is_upload(ROLE_HOT_FILE_READ)
        assert not is_pointer(ROLE_HOT_FILE_READ)

    def test_clean_removes_its_unit(self):
        # `clients clean` is given IPs, never roles, so it stops and removes
        # EVERY role's unit. A role missing from that list leaves its service
        # running on a "cleaned" VM.
        assert ROLE_HOT_FILE_READ in ALL_CLIENT_ROLES
        assert "ant-hot-file-reader" in ALL_CLIENT_UNITS

    def test_an_extension_may_not_be_named_after_it(self):
        # The tfstate key is `<prefix>-<tf name>-<role>-<region>-<index>`, so an
        # extension called `hot-file-read-x` would make the deployment's own
        # orphan-recovery scan adopt the extension's VM.
        from saorsa_deploy.client_extension import ExtensionError, validate_extension_name

        with pytest.raises(ExtensionError, match="starts with the client role"):
            validate_extension_name("hot-file-read-extra")

    def test_destroy_accepts_it(self):
        from saorsa_deploy.cmd.destroy_clients import VALID_ROLES

        assert ROLE_HOT_FILE_READ in VALID_ROLES


# --------------------------------------------------------------------------- #
# spec-token parsing
# --------------------------------------------------------------------------- #
class TestSpecParsing:
    def test_minimal(self):
        spec = parse_hot_file_reader_spec("digital_ocean:lon1")
        assert spec["role"] == ROLE_HOT_FILE_READ
        assert spec["provider"] == "digitalocean"
        assert spec["region"] == "lon1"
        assert spec["vm_size"] == "s-2vcpu-4gb"
        assert spec["instances"] == 4
        assert spec["round_period_secs"] == 600
        assert spec["download_timeout_secs"] == 300
        assert spec["round_parity"] == "all"
        assert spec["start_stagger_secs"] == 0

    def test_all_options(self):
        spec = parse_hot_file_reader_spec(
            "hetzner:fsn1:cpx41:instances=2:round_period_secs=300"
            ":download_timeout_secs=120:round_parity=even:start_stagger_secs=3"
            ":forward_logs=true:rust_log=ant_core=debug"
        )
        assert spec["vm_size"] == "cpx41"
        assert spec["instances"] == 2
        assert spec["round_period_secs"] == 300
        assert spec["download_timeout_secs"] == 120
        assert spec["round_parity"] == "even"
        assert spec["start_stagger_secs"] == 3
        assert spec["forward_logs"] is True
        assert spec["rust_log"] == "ant_core=debug"

    @pytest.mark.parametrize("parity", ["even", "odd", "all"])
    def test_every_parity_is_accepted(self, parity):
        assert (
            parse_hot_file_reader_spec(f"digital_ocean:lon1:round_parity={parity}")["round_parity"]
            == parity
        )

    def test_an_unknown_parity_is_refused_naming_the_three(self):
        with pytest.raises(ValueError, match="round_parity must be one of"):
            parse_hot_file_reader_spec("digital_ocean:lon1:round_parity=both")

    def test_instances_over_the_cap_is_refused_naming_it(self):
        with pytest.raises(ValueError, match=f"between 1 and {HOT_FILE_MAX_INSTANCES}"):
            parse_hot_file_reader_spec(f"digital_ocean:lon1:instances={HOT_FILE_MAX_INSTANCES + 1}")

    def test_zero_instances_is_refused(self):
        with pytest.raises(ValueError, match="between 1 and"):
            parse_hot_file_reader_spec("digital_ocean:lon1:instances=0")

    def test_too_short_a_round_is_refused(self):
        with pytest.raises(ValueError, match="round_period_secs must be >="):
            parse_hot_file_reader_spec("digital_ocean:lon1:round_period_secs=60")

    def test_a_timeout_that_can_outlive_its_round_is_refused(self):
        with pytest.raises(ValueError, match="must be < round_period_secs"):
            parse_hot_file_reader_spec(
                "digital_ocean:lon1:round_period_secs=300:download_timeout_secs=300"
            )

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_nonpositive_timeout_is_refused(self, bad):
        """It PASSES the relationship check, which is the trap.

        `0 < 120`, so `(0, 120)` validated -- and a falsy recorded value then
        took the 300 s default on the VM, giving an effective `(300, 120)` the
        validator would have refused. A download running 300 s of a 120 s round
        reads inside the other cohort's rounds.
        """
        with pytest.raises(ValueError, match="download_timeout_secs must be >= 1"):
            parse_hot_file_reader_spec(
                f"digital_ocean:lon1:round_period_secs=120:download_timeout_secs={bad}"
            )

    def test_a_negative_stagger_is_refused(self):
        with pytest.raises(ValueError, match="start_stagger_secs must be >= 0"):
            parse_hot_file_reader_spec("digital_ocean:lon1:start_stagger_secs=-1")

    @pytest.mark.parametrize("key", ["clock_skew=2h", "network=home-fttc", "bootstrap_cache=true"])
    def test_options_that_would_distort_the_measurement_are_refused(self, key):
        # What is measured is WHEN each reader read and what its swarm did. A
        # shifted clock or a shaped link distorts exactly that.
        with pytest.raises(ValueError, match="unknown key"):
            parse_hot_file_reader_spec(f"digital_ocean:lon1:{key}")

    def test_a_non_integer_knob_is_refused(self):
        with pytest.raises(ValueError, match="must be an integer"):
            parse_hot_file_reader_spec("digital_ocean:lon1:instances=four")

    def test_there_is_no_count_token(self):
        # `count` is manifest-only: the orchestrator expands it to repeated
        # flags, because `clients infra` allocates one client_index per flag and
        # a reader's start offset is derived from it.
        with pytest.raises(ValueError, match="unknown key 'count'"):
            parse_hot_file_reader_spec("digital_ocean:lon1:count=3")

    def test_the_schedule_is_part_of_the_spec_signature(self):
        # Changing the parity or the stagger must be a spec MISMATCH, not a
        # silent reuse of VMs still running the old schedule.
        from saorsa_deploy.cmd.infra_clients import _spec_signature

        base = parse_hot_file_reader_spec("digital_ocean:lon1")
        even = parse_hot_file_reader_spec("digital_ocean:lon1:round_parity=even")
        stagger = parse_hot_file_reader_spec("digital_ocean:lon1:start_stagger_secs=2")
        assert _spec_signature(base) != _spec_signature(even)
        assert _spec_signature(base) != _spec_signature(stagger)

    def test_an_existing_role_s_signature_is_unchanged(self):
        # The schedule fields are None on every other role, so client state
        # recorded before this release still matches a request that takes the
        # defaults. `seed_until_secs` (INFRA-532) is the exception and is `or
        # 0`-ed: 0 IS its default, so an absent key and an explicit 0 have to
        # give the same signature or a resumed run would refuse to reuse VMs
        # running precisely what was asked for.
        from saorsa_deploy.cmd.infra_clients import _spec_signature, parse_downloader_spec

        spec = parse_downloader_spec("digital_ocean:lon1")
        assert _spec_signature(spec)[-3:] == (None, None, 0)


# --------------------------------------------------------------------------- #
# manifest schema
# --------------------------------------------------------------------------- #
NATIVE_BINARIES = {"ant_node": {"version": "1.0"}, "ant": {"version": "0.3.13"}}


def _manifest(clients: dict, binaries: dict | None = None, **extra):
    base = {
        "name": "DEV-01",
        "kind": "testnet",
        "purpose": "p",
        "ssh_key_path": SSH,
        "testnet": {
            "binaries": binaries or NATIVE_BINARIES,
            "bootstraps": {"count": 1},
            "nodes": {
                "per_vm": 5,
                "providers": {"digital_ocean": {"vm_count": 2, "regions": 3}},
            },
            "clients": clients,
            **extra,
        },
    }
    return Manifest.model_validate(base)


READERS = {"hot_file_readers": [{"provider": "digital_ocean", "region": "lon1"}]}


class TestSchema:
    def test_a_reader_tier_alone_is_valid(self):
        m = _manifest(READERS)
        assert len(m.testnet.clients.hot_file_readers) == 1
        # Native: it needs no WASM bundle at all.
        assert not m.testnet.clients.has_wasm

    def test_it_needs_the_native_ant_binary(self):
        with pytest.raises(ValidationError, match="binaries.ant is required"):
            _manifest(READERS, binaries={"ant_node": {"version": "1.0"}})

    def test_there_is_no_pairing_rule(self):
        # Unlike the uploader/downloader pair: the file this tier reads is named
        # by `select-hot-files` out of what is ALREADY in the pool, so the tier
        # is added to a deployment whose uploads have already happened -- which
        # is the only time a hot file exists.
        assert _manifest(READERS).testnet.clients.hot_file_readers

    def test_the_defaults_are_runnable(self):
        entry = _manifest(READERS).testnet.clients.hot_file_readers[0]
        assert entry.instances == 4
        assert entry.round_period_secs == 600
        assert entry.download_timeout_secs == 300
        assert entry.round_parity == "all"
        assert entry.start_stagger_secs == 0
        assert entry.count == 1

    def test_count_below_one_is_refused(self):
        with pytest.raises(ValidationError, match="count must be >= 1"):
            _manifest({"hot_file_readers": [{**READERS["hot_file_readers"][0], "count": 0}]})

    @pytest.mark.parametrize("bad", [0, HOT_FILE_MAX_INSTANCES + 1])
    def test_instances_outside_the_cap_is_refused(self, bad):
        with pytest.raises(ValidationError, match=f"between 1 and {HOT_FILE_MAX_INSTANCES}"):
            _manifest({"hot_file_readers": [{**READERS["hot_file_readers"][0], "instances": bad}]})

    def test_too_short_a_round_is_refused(self):
        with pytest.raises(ValidationError, match="round_period_secs must be >="):
            _manifest(
                {"hot_file_readers": [{**READERS["hot_file_readers"][0], "round_period_secs": 119}]}
            )

    def test_a_timeout_that_can_outlive_its_round_is_refused(self):
        with pytest.raises(ValidationError, match="must be < round_period_secs"):
            _manifest(
                {
                    "hot_file_readers": [
                        {
                            **READERS["hot_file_readers"][0],
                            "round_period_secs": 300,
                            "download_timeout_secs": 300,
                        }
                    ]
                }
            )

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_nonpositive_timeout_is_refused(self, bad):
        # See the spec-parser twin: a non-positive value passes the
        # `< round_period_secs` check, and defaulting then made it 300.
        with pytest.raises(ValidationError, match="download_timeout_secs must be >= 1"):
            _manifest(
                {
                    "hot_file_readers": [
                        {
                            **READERS["hot_file_readers"][0],
                            "round_period_secs": 120,
                            "download_timeout_secs": bad,
                        }
                    ]
                }
            )

    def test_an_unknown_parity_is_refused(self):
        with pytest.raises(ValidationError, match="round_parity must be one of"):
            _manifest(
                {"hot_file_readers": [{**READERS["hot_file_readers"][0], "round_parity": "both"}]}
            )

    def test_a_negative_stagger_is_refused(self):
        with pytest.raises(ValidationError, match="start_stagger_secs must be >= 0"):
            _manifest(
                {"hot_file_readers": [{**READERS["hot_file_readers"][0], "start_stagger_secs": -1}]}
            )

    def test_a_cohort_whose_starts_outrun_its_round_is_NOT_refused(self):
        # The operator sizes it: a deliberately long stagger over a long round
        # is a legitimate shape. `plan` says so out loud instead.
        m = _manifest(
            {
                "hot_file_readers": [
                    {
                        **READERS["hot_file_readers"][0],
                        "count": 20,
                        "start_stagger_secs": 30,
                    }
                ]
            }
        )
        assert m.testnet.clients.hot_file_readers[0].count == 20

    def test_forward_logs_still_requires_elastic(self):
        with pytest.raises(ValidationError, match="forward_logs requires elastic.enabled"):
            _manifest(
                {"hot_file_readers": [{**READERS["hot_file_readers"][0], "forward_logs": True}]}
            )

    @pytest.mark.parametrize("key", ["clock_skew", "network"])
    def test_measurement_distorting_keys_are_not_fields(self, key):
        with pytest.raises(ValidationError):
            _manifest({"hot_file_readers": [{**READERS["hot_file_readers"][0], key: "2h"}]})

    def test_rust_log_IS_a_field(self):
        # Unlike the browser role: the transfers run in a Rust process whose
        # tracing subscriber there is something to configure.
        m = _manifest(
            {"hot_file_readers": [{**READERS["hot_file_readers"][0], "rust_log": "info"}]}
        )
        assert m.testnet.clients.hot_file_readers[0].rust_log == "info"


class TestExtensionSchema:
    def _extension(self, entries, **extra):
        return Manifest.model_validate(
            {
                "name": "readers",
                "kind": "clients",
                "purpose": "p",
                "ssh_key_path": SSH,
                "deployment": "DEV-01",
                "clients": {"hot_file_readers": entries},
                **extra,
            }
        )

    def test_a_reader_tier_alone_satisfies_the_at_least_one_rule(self):
        m = self._extension([{"provider": "digital_ocean", "region": "lon1"}])
        assert len(m.clients.hot_file_readers) == 1

    def test_the_at_least_one_message_names_the_role(self):
        with pytest.raises(ValidationError, match="hot_file_readers must be non-empty"):
            Manifest.model_validate(
                {
                    "name": "readers",
                    "kind": "clients",
                    "purpose": "p",
                    "ssh_key_path": SSH,
                    "deployment": "DEV-01",
                    "clients": {},
                }
            )

    def test_a_released_ant_override_is_accepted(self):
        # The control cohort of the DEV-01 run: a RELEASED client against
        # branch-built nodes, which is the whole point of the parity.
        m = self._extension(
            [{"provider": "digital_ocean", "region": "lon1", "round_parity": "odd"}],
            binaries={"ant": {"version": "0.3.13"}},
        )
        assert m.binaries.ant.version == "0.3.13"
        assert m.clients.hot_file_readers[0].round_parity == "odd"


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def _step(steps, head):
    return next(s for s in steps if s.argv[: len(head)] == head)


def _maybe_step(steps, head):
    return next((s for s in steps if s.argv[: len(head)] == head), None)


class TestOrchestrator:
    def test_infra_emits_the_flag(self):
        argv = _step(build_deploy_steps(_manifest(READERS)), ["clients", "infra"]).argv
        assert argv[argv.index("--hot-file-reader") + 1] == "digital_ocean:lon1"

    def test_count_expands_to_one_flag_per_vm(self):
        # `clients infra` allocates one consecutive client_index per repeated
        # flag, and the reader's own start offset is derived from it.
        m = _manifest(
            {"hot_file_readers": [{"provider": "digital_ocean", "region": "lon1", "count": 3}]}
        )
        argv = _step(build_deploy_steps(m), ["clients", "infra"]).argv
        assert argv.count("--hot-file-reader") == 3

    def test_non_default_knobs_appear_in_the_token(self):
        m = _manifest(
            {
                "hot_file_readers": [
                    {
                        "provider": "digital_ocean",
                        "region": "lon1",
                        "instances": 2,
                        "round_period_secs": 300,
                        "download_timeout_secs": 120,
                        "round_parity": "even",
                        "start_stagger_secs": 2,
                    }
                ]
            }
        )
        argv = _step(build_deploy_steps(m), ["clients", "infra"]).argv
        assert argv[argv.index("--hot-file-reader") + 1] == (
            "digital_ocean:lon1:instances=2:round_period_secs=300"
            ":download_timeout_secs=120:round_parity=even:start_stagger_secs=2"
        )

    def test_a_default_entry_emits_the_short_token(self):
        argv = _step(build_deploy_steps(_manifest(READERS)), ["clients", "infra"]).argv
        token = argv[argv.index("--hot-file-reader") + 1]
        assert token == "digital_ocean:lon1"

    def test_rust_log_is_last_in_the_token(self):
        # Its value may contain ':' (RUST_LOG '::' paths), so the parser takes
        # everything after the marker verbatim.
        m = _manifest(
            {
                "hot_file_readers": [
                    {
                        "provider": "digital_ocean",
                        "region": "lon1",
                        "round_parity": "odd",
                        "rust_log": "ant_core::swarm=trace",
                    }
                ]
            }
        )
        token = _step(build_deploy_steps(m), ["clients", "infra"]).argv[-1]
        assert token.endswith(":rust_log=ant_core::swarm=trace")
        assert parse_hot_file_reader_spec(token)["rust_log"] == "ant_core::swarm=trace"

    def test_the_round_trip_through_the_token_preserves_every_knob(self):
        m = _manifest(
            {
                "hot_file_readers": [
                    {
                        "provider": "hetzner",
                        "region": "fsn1",
                        "vm_size": "cpx41",
                        "instances": 8,
                        "round_period_secs": 900,
                        "download_timeout_secs": 600,
                        "round_parity": "even",
                        "start_stagger_secs": 5,
                    }
                ]
            }
        )
        argv = _step(build_deploy_steps(m), ["clients", "infra"]).argv
        parsed = parse_hot_file_reader_spec(argv[argv.index("--hot-file-reader") + 1])
        entry = m.testnet.clients.hot_file_readers[0]
        for key in (
            "instances",
            "round_period_secs",
            "download_timeout_secs",
            "round_parity",
            "start_stagger_secs",
        ):
            assert parsed[key] == getattr(entry, key), key

    def test_no_wasm_steps_are_added(self):
        # Native: no bundle, and no bootstrap WebRTC address collection.
        steps = build_deploy_steps(_manifest(READERS))
        assert _maybe_step(steps, ["build", "ant-wasm"]) is None
        assert _maybe_step(steps, ["deployments", "webrtc-addresses"]) is None

    def test_a_deployment_without_the_role_is_completely_unaffected(self):
        m = _manifest(
            {
                "uploaders": [{"provider": "digital_ocean", "region": "lon1", "file_size_mb": 10}],
                "downloaders": [{"provider": "digital_ocean", "region": "lon1"}],
            }
        )
        assert "--hot-file-reader" not in _step(build_deploy_steps(m), ["clients", "infra"]).argv


class TestExtensionPlan:
    def _extension(self, entries):
        return Manifest.model_validate(
            {
                "name": "readers",
                "kind": "clients",
                "purpose": "p",
                "ssh_key_path": SSH,
                "deployment": "DEV-01",
                "clients": {"hot_file_readers": entries},
            }
        )

    def test_each_expanded_vm_is_planned(self):
        vms = planned_client_vms(
            self._extension([{"provider": "digital_ocean", "region": "lon1", "count": 3}])
        )
        assert [v["client_index"] for v in vms] == [1, 2, 3]
        assert all(v["role"] == ROLE_HOT_FILE_READ for v in vms)
        assert vms[2]["hostname"] == "DEV-01-readers-ant-client-hot-file-read-lon1-3"

    def test_the_planned_vm_size_is_the_shared_client_default(self):
        # A native reader needs no Chromium and holds no file in WASM memory.
        vms = planned_client_vms(self._extension([{"provider": "digital_ocean", "region": "l"}]))
        assert vms[0]["vm_size"] == "s-2vcpu-4gb"

    def test_indexes_continue_from_the_deployment_s_own(self):
        vms = planned_client_vms(
            self._extension([{"provider": "digital_ocean", "region": "l", "count": 2}]),
            {ROLE_HOT_FILE_READ: 7},
        )
        assert [v["client_index"] for v in vms] == [8, 9]

    def test_the_plan_names_the_last_reader_s_offset(self):
        (note,) = hot_file_stagger_notes(
            self._extension(
                [
                    {
                        "provider": "digital_ocean",
                        "region": "l",
                        "count": 4,
                        "instances": 4,
                        "start_stagger_secs": 2,
                    }
                ]
            )
        )
        # Indexes 1..4, so the last reader is ((4-1) x 4 + 3) x 2 = 30s, and
        # 30 + 300 < 600, so no warning.
        assert "(client_index 1..4)" in note
        assert "the last reader starts 30s after the boundary" in note
        assert "exceeds the round" not in note

    def test_the_offset_is_derived_from_the_ALLOCATED_indexes(self):
        """An extension's indexes CONTINUE from the deployment's own.

        Counting VMs instead printed 150s for the reviewer's case -- four
        existing reader VMs, four more with four readers and a 10 s stagger --
        where the service's own formula puts the last reader at 310s, and
        therefore omitted the warning that 310 + 300 overruns a 600 s round.
        The offset an operator sizes the stagger against has to be the one the
        VMs will actually run.
        """
        (note,) = hot_file_stagger_notes(
            self._extension(
                [
                    {
                        "provider": "digital_ocean",
                        "region": "l",
                        "count": 4,
                        "instances": 4,
                        "start_stagger_secs": 10,
                    }
                ]
            ),
            {ROLE_HOT_FILE_READ: 4},
        )
        assert "(client_index 5..8)" in note
        assert "the last reader starts 310s after the boundary" in note
        # Compared with the DEADLINE, not with `last + deadline` against the
        # period: two of these sixteen readers start at or past 300s.
        assert "2 of the 16 readers start at or past it" in note

    def test_a_later_schedule_group_accounts_for_the_earlier_one_s_vms(self):
        # Indexes are allocated with one running offset over the whole role
        # list, exactly as `planned_client_vms` allocates them, so the odd
        # cohort's indexes continue past the even cohort's.
        even, odd = hot_file_stagger_notes(
            self._extension(
                [
                    {
                        "provider": "digital_ocean",
                        "region": "l",
                        "count": 2,
                        "instances": 4,
                        "round_parity": "even",
                        "start_stagger_secs": 2,
                    },
                    {
                        "provider": "digital_ocean",
                        "region": "f",
                        "count": 2,
                        "instances": 4,
                        "round_parity": "odd",
                        "start_stagger_secs": 2,
                    },
                ]
            )
        )
        assert "(client_index 1..2)" in even
        assert "the last reader starts 14s after the boundary" in even
        assert "(client_index 3..4)" in odd
        assert "the last reader starts 30s after the boundary" in odd

    def test_the_planned_indexes_and_the_note_s_indexes_agree(self):
        # Two derivations of one allocation; a plan whose VM list and whose
        # schedule note disagree describes two different cohorts.
        manifest = self._extension(
            [{"provider": "digital_ocean", "region": "l", "count": 3, "start_stagger_secs": 1}]
        )
        base = {ROLE_HOT_FILE_READ: 5}
        indexes = [v["client_index"] for v in planned_client_vms(manifest, base)]
        (note,) = hot_file_stagger_notes(manifest, base)
        assert f"(client_index {min(indexes)}..{max(indexes)})" in note

    def test_the_plan_warns_when_the_starts_outrun_the_round(self):
        (note,) = hot_file_stagger_notes(
            self._extension(
                [
                    {
                        "provider": "digital_ocean",
                        "region": "l",
                        "count": 10,
                        "instances": 4,
                        "start_stagger_secs": 10,
                    }
                ]
            )
        )
        # Indexes 1..10, so ((10-1) x 4 + 3) x 10 = 390s, past the 300s deadline.
        assert "the last reader starts 390s after the boundary" in note
        assert "start at or past it" in note

    def test_the_warning_is_on_the_deadline_not_on_last_plus_deadline(self):
        """The deadline is an ABSOLUTE offset from the boundary.

        The service refuses `offset >= download_timeout_secs`, so it is not an
        extra download duration added to each start. Summing them said nothing:
        with period 600, deadline 100 and a 150 s last start, `150 + 100 <= 600`
        passed silently while six of the sixteen readers could never run.
        """
        (note,) = hot_file_stagger_notes(
            self._extension(
                [
                    {
                        "provider": "digital_ocean",
                        "region": "l",
                        "count": 4,
                        "instances": 4,
                        "start_stagger_secs": 10,
                        "round_period_secs": 600,
                        "download_timeout_secs": 100,
                    }
                ]
            )
        )
        assert "the last reader starts 150s after the boundary" in note
        # Offsets 100, 110, 120, 130, 140, 150 are all at or past the deadline.
        assert "6 of the 16 readers start at or past it" in note
        assert "can never run" in note

    def test_a_cohort_within_its_deadline_gets_no_warning(self):
        # The last reader at 30s against a 300s deadline: every reader runs.
        (note,) = hot_file_stagger_notes(
            self._extension(
                [
                    {
                        "provider": "digital_ocean",
                        "region": "l",
                        "count": 4,
                        "instances": 4,
                        "start_stagger_secs": 2,
                    }
                ]
            )
        )
        assert "start at or past it" not in note

    def test_the_plan_s_stranded_count_matches_the_service_s_refusal(self):
        """The plan's count and the service's guard must agree exactly.

        Two copies of one rule — the plan counts what the reader refuses — so a
        disagreement means the plan is describing a cohort that behaves
        differently.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location("hot_file_reader", SCRIPT)
        service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(service)

        instances, stagger, deadline = 4, 10, 100
        indexes = [1, 2, 3, 4]
        expected = sum(
            1
            for k in indexes
            for i in range(1, instances + 1)
            if service.reader_offset_secs(k, i, instances, stagger) >= deadline
        )
        (note,) = hot_file_stagger_notes(
            self._extension(
                [
                    {
                        "provider": "digital_ocean",
                        "region": "l",
                        "count": len(indexes),
                        "instances": instances,
                        "start_stagger_secs": stagger,
                        "download_timeout_secs": deadline,
                    }
                ]
            )
        )
        assert f"{expected} of the {len(indexes) * instances} readers" in note

    def test_a_cohort_with_no_stagger_reports_zero(self):
        (note,) = hot_file_stagger_notes(
            self._extension([{"provider": "digital_ocean", "region": "l", "count": 4}])
        )
        assert "the last reader starts 0s after the boundary" in note

    def test_a_testnet_manifest_gets_the_note_too(self):
        (note,) = hot_file_stagger_notes(
            _manifest(
                {
                    "hot_file_readers": [
                        {"provider": "digital_ocean", "region": "l", "start_stagger_secs": 1}
                    ]
                }
            )
        )
        assert "the last reader starts 3s after the boundary" in note

    def test_a_manifest_without_the_role_gets_no_note(self):
        assert hot_file_stagger_notes(_manifest(READERS | {})) or True
        m = _manifest(
            {
                "uploaders": [{"provider": "digital_ocean", "region": "l", "file_size_mb": 1}],
                "downloaders": [{"provider": "digital_ocean", "region": "l"}],
            }
        )
        assert hot_file_stagger_notes(m) == []


# --------------------------------------------------------------------------- #
# Terraform
# --------------------------------------------------------------------------- #
def test_every_provider_module_accepts_the_role():
    # `test_client_role_terraform.py` asserts set equality against
    # ALL_CLIENT_ROLES for all four providers; this is the one-line statement of
    # what that buys, because a role the Python half is complete for but a
    # module's allow-list omits dies at `terraform apply`.
    resources = Path(__file__).resolve().parents[1] / "saorsa_deploy" / "resources"
    for provider in ("digitalocean", "hetzner", "ovh", "vultr"):
        body = (resources / f"{provider}-client" / "variables.tf").read_text()
        assert '"hot-file-read"' in body, provider


# --------------------------------------------------------------------------- #
# provisioner rendering
# --------------------------------------------------------------------------- #
def _client_provisioner(**overrides):
    kwargs = dict(
        clients=[],
        bootstrap_peers=["1.2.3.4:10000"],
        aws_access_key_id="k",
        aws_secret_access_key="s",
        addresses_s3_bucket="bucket",
        addresses_s3_prefix="saorsa-deploy/addresses/DEV-01",
        deployment_name="DEV-01",
        console=MagicMock(),
    )
    kwargs.update(overrides)
    return AntClientProvisioner(**kwargs)


READER_CLIENT = {
    "ip": "10.0.0.1",
    "role": ROLE_HOT_FILE_READ,
    "region": "lon1",
    "client_index": 3,
    "instances": 2,
    "round_period_secs": 900,
    "download_timeout_secs": 420,
    "round_parity": "even",
    "start_stagger_secs": 2,
}


class TestProvisioning:
    def _script(self, client=None):
        with patch.object(AntClientProvisioner, "_presigned_put_url", return_value=""):
            provisioner = _client_provisioner(clients=[client or READER_CLIENT])
            env = provisioner._build_ip_to_env("0xdead")["10.0.0.1"]
            case_block = provisioner._case_entry("10.0.0.1", env)
            return provisioner, env, case_block, provisioner._env_file_script(case_block, "0xdead")

    def test_every_reader_setting_reaches_the_env_file(self):
        """The one hop that matters: spec -> case block -> `/etc/ant-client.env`.

        `hot_file_reader.py` reads its whole configuration from the environment
        and from nowhere else, so a variable the case block sets but the heredoc
        never exports is a setting the service silently runs without -- at the
        script's own default, which for `ROUND_PARITY` means a cohort reading
        every round and contaminating the other cohort's swarm.
        """
        provisioner, _, _, script = self._script()
        for key in provisioner._hot_file_env(READER_CLIENT):
            assert f"\n{key.upper()}=$" in script, key

    def test_the_case_block_carries_this_vm_s_values(self):
        _, _, case_block, _ = self._script()
        assert "INSTANCES=2" in case_block
        assert "ROUND_PERIOD_SECS=900" in case_block
        assert "DOWNLOAD_TIMEOUT_SECS=420" in case_block
        assert 'ROUND_PARITY="even"' in case_block
        assert "START_STAGGER_SECS=2" in case_block

    def test_the_env_file_names_the_hot_file_url_and_the_directories(self):
        _, _, _, script = self._script()
        assert (
            "HOT_FILES_URL=https://saorsa-testnet-addresses.s3.eu-west-2.amazonaws.com"
            "/saorsa-deploy/addresses/DEV-01/browser-hot-files.json"
        ) in script
        assert "HOT_FILE_DOWNLOAD_DIR=/var/lib/ant-client/hot-file" in script
        assert "HOT_FILE_LOG_DIR=/var/log/ant-hot-file-reader" in script

    def test_the_env_file_names_the_testnet_so_the_point_can_be_tagged(self):
        _, _, _, script = self._script()
        assert "TESTNET_NAME=DEV-01" in script

    def test_a_recorded_knob_is_defaulted_only_when_ABSENT(self):
        """`or` let a recorded `0` take a non-zero default.

        So a spec validated as `(timeout 0, period 120)` ran on the VM as
        `(300, 120)` — a pair the validator would have refused. Both input paths
        now reject a non-positive timeout, and this is the second half of the
        same guarantee: defaulting cannot invent a value the spec did not carry.
        """
        env = AntClientProvisioner._hot_file_env(
            {"round_period_secs": 120, "download_timeout_secs": 0, "round_parity": "even"}
        )
        assert env["download_timeout_secs"] == 0
        assert env["round_period_secs"] == 120
        # Absent still takes the default, which is what the key is for.
        assert AntClientProvisioner._hot_file_env({})["download_timeout_secs"] == 300

    def test_a_zero_stagger_is_carried_not_defaulted(self):
        # Why the distinction is not academic: 0 is both the default AND a
        # legitimate explicit setting for this one knob.
        assert (
            AntClientProvisioner._hot_file_env({"start_stagger_secs": 0})["start_stagger_secs"] == 0
        )

    def test_the_recorded_role_config_applies_the_same_rule(self):
        from saorsa_deploy.hot_file_records import hot_file_config_entry

        entry = hot_file_config_entry(
            "DEV-01", "swarm", {"region": "lon1", "client_index": 1, "start_stagger_secs": 0}
        )
        assert entry["start_stagger_secs"] == 0
        assert entry["round_period_secs"] == 600

    def test_a_non_reader_client_gets_the_inert_defaults(self):
        # The case block is one shape, so these are emitted for every role.
        # What matters is that a run with no readers renders them all inert.
        _, _, case_block, _ = self._script(
            client={
                "ip": "10.0.0.1",
                "role": "download",
                "region": "lon1",
                "client_index": 1,
            }
        )
        assert "INSTANCES=0" in case_block
        assert "ROUND_PERIOD_SECS=0" in case_block
        assert 'ROUND_PARITY=""' in case_block
        assert "START_STAGGER_SECS=0" in case_block

    def test_the_unit_runs_the_reader_script(self):
        from saorsa_deploy.provisioning.client import _build_hot_file_reader_unit

        unit = _build_hot_file_reader_unit()
        assert "ExecStart=/usr/bin/python3 /root/ant-client/hot_file_reader.py" in unit
        assert "EnvironmentFile=/etc/ant-client.env" in unit
        assert "Restart=on-failure" in unit

    def test_the_native_unit_writer_owns_its_hosts(self):
        # A role whose unit is written elsewhere must be EXCLUDED from
        # `_native_only`; this one is native, so its host must be kept -- and
        # the writer's case block needs its own arm, or the host falls into the
        # `else` and gets an ant-downloader unit `clients start` cannot find.
        with patch.object(AntClientProvisioner, "_presigned_put_url", return_value=""):
            provisioner = _client_provisioner(clients=[READER_CLIENT])
            ip_to_env = provisioner._build_ip_to_env("0xdead")
        assert "10.0.0.1" in AntClientProvisioner._native_only(ip_to_env)

    def test_its_forward_logs_are_mapped(self):
        # A role missing from this map forwards NOTHING while reporting nothing
        # wrong, which is how both WASM roles silently forwarded nothing for a
        # release.
        globs, label = AntClientProvisioner._FORWARD_ROLE_FILES[ROLE_HOT_FILE_READ]
        assert label == "hot_file_reader"
        assert globs == [
            "/var/log/ant-client-forward/hot-file-reader.stdout.log",
            "/var/log/ant-client-forward/hot-file-reader.stderr.log",
        ]

    def test_the_script_tees_into_the_files_the_forwarder_tails(self, hfr):
        # The two halves of the hop: the provisioner tells telegraf where to
        # look, and the script is what puts anything there.
        globs, _ = AntClientProvisioner._FORWARD_ROLE_FILES[ROLE_HOT_FILE_READ]
        source = SCRIPT.read_text()
        for path in globs:
            assert Path(path).name in source


# --------------------------------------------------------------------------- #
# the reader service: round arithmetic
# --------------------------------------------------------------------------- #
class TestRoundArithmetic:
    def test_the_round_is_derived_from_the_wall_clock(self, hfr):
        # Anchored, not slept: a sleep after each round moves its start by the
        # round's own duration, so two VMs started minutes apart would drift
        # apart indefinitely.
        assert hfr.round_id_at(0, 600) == 0
        assert hfr.round_id_at(599, 600) == 0
        assert hfr.round_id_at(600, 600) == 1
        assert hfr.round_id_at(1_800_000_000, 600) == 3_000_000
        assert hfr.boundary_of(3_000_000, 600) == 1_800_000_000

    @pytest.mark.parametrize(
        ("parity", "round_id", "ours"),
        [
            ("all", 10, True),
            ("all", 11, True),
            ("even", 10, True),
            ("even", 11, False),
            ("odd", 10, False),
            ("odd", 11, True),
        ],
    )
    def test_the_parity_says_which_rounds_are_ours(self, hfr, parity, round_id, ours):
        assert hfr.is_our_round(round_id, parity) is ours

    def test_two_cohorts_never_share_a_round(self, hfr):
        # The whole reason the parity exists: a branch build on even and a
        # released control on odd are never inside each other's swarm.
        for round_id in range(100, 110):
            assert hfr.is_our_round(round_id, "even") != hfr.is_our_round(round_id, "odd")

    def test_the_hot_file_cycles(self, hfr):
        assert hfr.hot_file_index_for(10, 3) == 1
        assert hfr.hot_file_index_for(11, 3) == 2
        assert hfr.hot_file_index_for(12, 3) == 0
        # Every VM derives the round id from the wall clock, so every VM picks
        # the same file without being told.
        assert hfr.hot_file_index_for(12, 1) == 0

    def test_no_files_is_a_recorded_skip_not_a_crash(self, hfr):
        assert hfr.hot_file_index_for(10, 0) is None

    def test_the_offset_is_deterministic_from_the_inventory(self, hfr):
        # reader i on the VM with client_index k starts at
        # boundary + ((k-1) x instances + (i-1)) x stagger.
        assert hfr.reader_offset_secs(1, 1, 4, 2) == 0
        assert hfr.reader_offset_secs(1, 4, 4, 2) == 6
        assert hfr.reader_offset_secs(2, 1, 4, 2) == 8
        assert hfr.reader_offset_secs(4, 4, 4, 2) == 30

    def test_a_cohort_of_four_vms_starts_one_reader_every_stagger(self, hfr):
        offsets = [hfr.reader_offset_secs(k, i, 4, 2) for k in range(1, 5) for i in range(1, 5)]
        assert offsets == list(range(0, 32, 2))

    def test_no_stagger_starts_every_reader_on_the_boundary(self, hfr):
        assert [hfr.reader_offset_secs(k, i, 4, 0) for k in (1, 2) for i in (1, 2)] == [0] * 4

    def test_the_last_offset_matches_what_the_plan_prints(self, hfr):
        """The script's arithmetic and the orchestrator's must agree.

        Two copies by necessity -- the script is copied verbatim onto a VM and
        imports nothing from the package -- so they are pinned together here. A
        plan that describes a schedule the VMs do not run is worse than no plan.
        """
        from saorsa_deploy.orchestrator.clients import last_hot_file_reader_offset_secs

        for index, instances, stagger in [(4, 4, 2), (8, 4, 10), (1, 1, 0), (3, 2, 5)]:
            assert hfr.last_reader_offset_secs(index, instances, stagger) == (
                last_hot_file_reader_offset_secs(index, instances, stagger)
            ), (index, instances, stagger)

    def test_the_last_offset_is_keyed_on_the_index_not_the_vm_count(self, hfr):
        # The reviewer's case: a cohort of four VMs at indexes 5..8 has its last
        # reader at 310s, not at the 150s a VM count gives.
        assert hfr.last_reader_offset_secs(8, 4, 10) == 310
        assert hfr.reader_offset_secs(8, 4, 4, 10) == 310


# --------------------------------------------------------------------------- #
# the reader service: parsing `ant --json`
# --------------------------------------------------------------------------- #
#: The result object ant-client `feat/peer-swarm` prints, with every field the
#: issue names. Derived from the branch's `SwarmStats` field names.
SWARM_RESULT = {
    "file": "hot.bin",
    "size": 52428800,
    "elapsed_secs": 12.5,
    "chunk_peer_check": "ok",
    "swarm": {
        "records_from_peers": 7,
        "bytes_from_peers": 7340032,
        "records_rejected": 1,
        "asks_busy": 2,
        "asks_missing": 3,
        "asks_failed": 4,
        "records_served": 5,
        "bytes_served": 5242880,
        "links": 6,
        "rooms": 1,
        "discovery": {
            "lookups": 11,
            "announces": 12,
            "announces_failed": 13,
            "announces_timed_out": 14,
            "announces_refused": 15,
            "bytes_sent": 16,
            "bytes_received": 17,
            "offers_made": 18,
            "offers_delivered": 19,
            "offers_received": 20,
            "answers_sent": 21,
            "answers_received": 22,
            "channels_opened": 23,
            "links_proven": 24,
            "links_failed": 25,
        },
    },
    "swarm_rooms": [
        {
            "tag": "abc",
            "records_from_peers": 7,
            "bytes_from_peers": 7340032,
            "records_rejected": 1,
            "records_served": 5,
            "bytes_served": 5242880,
            "links": 6,
            "open_links": 2,
            "open_link_peers": ["p1", "p2"],
        }
    ],
}

#: What a RELEASED `ant` (0.3.13) prints: the same object without `swarm` and
#: without `swarm_rooms`.
RELEASED_RESULT = {
    "file": "hot.bin",
    "size": 52428800,
    "elapsed_secs": 30.0,
    "chunk_peer_check": "ok",
}


class TestResultParsing:
    def test_the_last_json_object_on_stdout_is_the_result(self, hfr):
        stdout = f"some log line\n{json.dumps(RELEASED_RESULT)}\n"
        assert hfr.parse_result(stdout)["size"] == 52428800

    def test_a_trailing_log_line_does_not_hide_the_result(self, hfr):
        # `ant` is invoked with `-v` so that `rust_log` has something to
        # configure, so a tracing line can land after the record. Taking the
        # final line outright would turn every success into "no json result".
        stdout = f"{json.dumps(RELEASED_RESULT)}\n2026-11-06 INFO shutting down\n"
        assert hfr.parse_result(stdout)["size"] == 52428800

    def test_no_json_at_all_is_none(self, hfr):
        assert hfr.parse_result("just logs\n") is None
        assert hfr.parse_result("") is None

    def test_a_json_array_is_not_a_result(self, hfr):
        assert hfr.parse_result("[1, 2]\n") is None

    def test_the_swarm_figures_are_carried_verbatim(self, hfr):
        fields = hfr.swarm_fields(SWARM_RESULT)
        assert fields["swarm_present"] == 1
        assert fields["records_from_peers"] == 7
        assert fields["bytes_from_peers"] == 7340032
        assert fields["records_rejected"] == 1
        assert fields["asks_busy"] == 2
        assert fields["rooms"] == 1
        assert fields["discovery_lookups"] == 11
        assert fields["discovery_links_failed"] == 25
        assert json.loads(fields["swarm_rooms"])[0]["tag"] == "abc"

    def test_every_counter_the_issue_names_is_recorded(self, hfr):
        fields = hfr.swarm_fields(SWARM_RESULT)
        for name in hfr.SWARM_FIELDS:
            assert name in fields, name
        for name in hfr.SWARM_DISCOVERY_FIELDS:
            assert f"discovery_{name}" in fields, name
        assert len(hfr.SWARM_FIELDS) + len(hfr.SWARM_DISCOVERY_FIELDS) == 25

    def test_a_released_binary_reports_no_swarm_and_that_is_not_a_failure(self, hfr):
        # The control cohort of a branch-vs-release comparison runs a released
        # binary, so this is the ordinary case for half the run.
        fields = hfr.swarm_fields(RELEASED_RESULT)
        assert fields["swarm_present"] == 0
        assert fields["records_from_peers"] == 0
        assert fields["swarm_rooms"] == "[]"

    def test_swarm_present_is_what_tells_the_two_zeros_apart(self, hfr):
        # A swarm that formed and shared nothing, versus a binary that reports
        # no swarm at all. Without the flag the two are identical.
        formed = dict(SWARM_RESULT)
        formed["swarm"] = {**SWARM_RESULT["swarm"], "records_from_peers": 0}
        assert hfr.swarm_fields(formed)["swarm_present"] == 1
        assert hfr.swarm_fields(formed)["records_from_peers"] == 0
        assert hfr.swarm_fields(RELEASED_RESULT)["swarm_present"] == 0
        assert hfr.swarm_fields(RELEASED_RESULT)["records_from_peers"] == 0

    def test_a_malformed_swarm_block_reads_as_absent(self, hfr):
        assert hfr.swarm_fields({"swarm": "nonsense"})["swarm_present"] == 0
        assert hfr.swarm_fields({"swarm": {"records_from_peers": "x"}})["records_from_peers"] == 0

    def test_a_non_list_swarm_rooms_reads_as_empty(self, hfr):
        assert hfr.swarm_fields({"swarm_rooms": {"a": 1}})["swarm_rooms"] == "[]"


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("stderr", "kind"),
        [
            (
                "Error: Failed to fetch public DataMap: not found: DataMap chunk not found",
                "not_found",
            ),
            ("Error: payment error: RPC Request failed", "payment"),
            ("Error: quorum not reached: 2/5", "quorum"),
            ("Error: failed to connect to peer", "connection"),
            ("Error: dial failed after 3 attempts", "connection"),
            ("Error: channel closed unexpectedly", "connection"),
            ("Error: something nobody has seen before", "other"),
            ("", "other"),
        ],
    )
    def test_the_classifier(self, hfr, stderr, kind):
        assert hfr.classify_failure(stderr) == kind

    def test_order_is_load_bearing(self, hfr):
        # The chunk-exhaustion sentence can carry "connect" inside it; the
        # narrowest claim has to win, as it does in `transfer_errors.py`.
        assert hfr.classify_failure("Error: not found after connect to 5 peers") == "not_found"

    def test_the_whole_block_is_matched_not_its_first_line(self, hfr):
        # `eprintln!("Error: {e:#}")` does not promise one physical line.
        assert hfr.classify_failure("Error: upload failed\nDetails: payment error") == "payment"

    def test_the_verbose_startup_stream_is_not_classified(self, hfr):
        """Only the final `Error:` block, never the whole `-v` stream.

        `ant` is invoked with `-v` so the role's `rust_log` has something to
        configure, so stderr is a tracing stream with the error at its foot.
        Classifying the stream matched the startup banner: a run that logged
        "Connected to autonomi network" and then died of a full disk came out as
        `connection`, which attributes a local disk failure to the network.
        """
        stderr = (
            "2026-11-06T08:00:00Z INFO Connected to autonomi network\n"
            "2026-11-06T08:00:01Z DEBUG dialled 12 peers, channel open\n"
            "Error: I/O error: No space left on device\n"
        )
        assert hfr.classify_failure(stderr) == hfr.KIND_OTHER

    def test_the_block_is_the_LAST_error_to_the_end_of_stderr(self, hfr):
        stderr = (
            "INFO connecting\n"
            "Error: transient: dial failed\n"
            "INFO retrying\n"
            "Error: payment error: RPC Request failed\n"
            "Details: nonce too low\n"
        )
        assert hfr.error_block(stderr) == (
            "Error: payment error: RPC Request failed\nDetails: nonce too low"
        )
        assert hfr.classify_failure(stderr) == hfr.KIND_PAYMENT

    def test_stderr_that_announced_no_error_classifies_as_other(self, hfr):
        # The honest answer for a child that failed without saying why -- and
        # the banner must not stand in for one.
        assert hfr.error_block("INFO connected to peer\n") == ""
        assert hfr.classify_failure("INFO connected to peer\n") == hfr.KIND_OTHER

    def test_the_error_text_still_keeps_the_whole_stderr_tail(self, hfr):
        # The classifier narrowed; `error_text` did not. The issue asks for the
        # last 2,000 characters of stderr and that is still what is recorded,
        # banner and all, because it is what an operator reads.
        stderr = "INFO banner\n" * 10 + "Error: quorum not reached\n"
        kept = hfr.clip_error_text(stderr)
        assert "INFO banner" in kept
        assert kept.endswith("Error: quorum not reached\n")

    def test_the_error_text_keeps_the_tail(self, hfr):
        # `ant`'s last words are the error; the head of a `-v` run is a banner.
        text = "x" * 3000 + "THE REASON"
        clipped = hfr.clip_error_text(text)
        assert len(clipped) == hfr.ERROR_TEXT_MAX
        assert clipped.endswith("THE REASON")


class TestVerification:
    def test_a_matching_hash_verifies(self, hfr, tmp_path):
        path = tmp_path / "f"
        path.write_bytes(b"hello")
        import hashlib

        digest = hashlib.sha256(b"hello").hexdigest()
        assert hfr.verify_download(digest, str(path)) == (hfr.VERIFY_OK, digest)

    def test_a_differing_hash_is_a_mismatch(self, hfr, tmp_path):
        path = tmp_path / "f"
        path.write_bytes(b"hello")
        status, actual = hfr.verify_download("0" * 64, str(path))
        assert status == hfr.VERIFY_MISMATCH
        assert actual != "0" * 64

    def test_no_published_hash_is_unavailable_not_a_mismatch(self, hfr, tmp_path):
        # A pool line with only two fields (every WASM-uploaded file) leaves
        # nothing to check against. The content may be perfectly healthy.
        path = tmp_path / "f"
        path.write_bytes(b"hello")
        assert hfr.verify_download(None, str(path)) == (hfr.VERIFY_UNAVAILABLE, None)

    def test_an_unreadable_file_is_unavailable(self, hfr, tmp_path):
        assert hfr.verify_download("0" * 64, str(tmp_path / "missing")) == (
            hfr.VERIFY_UNAVAILABLE,
            None,
        )


class TestHotFileManifest:
    def test_the_files_array_is_read(self, hfr):
        body = json.dumps({"files": [{"address": "ab", "bytes": 1, "sha256": "cd"}]})
        assert hfr.parse_hot_files(body) == [{"address": "ab", "bytes": 1, "sha256": "cd"}]

    @pytest.mark.parametrize(
        "body", ["", "not json", "[]", '{"files": "x"}', '{"files": [{"bytes": 1}]}']
    )
    def test_anything_unusable_is_no_hot_files(self, hfr, body):
        # Which is a recorded `skipped` round, not a crash loop on a VM that is
        # billing.
        assert hfr.parse_hot_files(body) == []


# --------------------------------------------------------------------------- #
# the reader service: the Influx line
# --------------------------------------------------------------------------- #
class TestInfluxLine:
    def _tags(self, **extra):
        return {
            "host": "DEV-01-ant-client-hot-file-read-lon1-3",
            "testnet_name": "DEV-01",
            "host_role": "hot_file_reader",
            "region": "lon1",
            "round_id": 3000000,
            "instance": 2,
            **extra,
        }

    def test_a_success(self, hfr):
        fields = hfr._base_fields(1_800_000_000_000_000_000)
        fields.update(hfr.swarm_fields(SWARM_RESULT))
        fields["status"] = hfr.STATUS_SUCCESS
        fields["address"] = "deadbeef"
        fields["file_size"] = 52428800
        fields["total_ms"] = 12800
        fields["client_elapsed_ms"] = 12500
        fields["verify_status"] = 0
        line = hfr.build_line(self._tags(hot_file_index=1), fields)
        assert line.startswith("hot_file_download,host=DEV-01-ant-client-hot-file-read-lon1-3,")
        assert "host_role=hot_file_reader" in line
        assert "round_id=3000000,instance=2,hot_file_index=1 " in line
        assert 'status="success"' in line
        assert "file_size=52428800i" in line
        assert "total_ms=12800i" in line
        assert "client_elapsed_ms=12500i" in line
        assert "start_ns=1800000000000000000i" in line
        assert "swarm_present=1i" in line
        assert "records_from_peers=7i" in line
        assert "discovery_links_failed=25i" in line
        assert 'swarm_rooms="[{' in line

    def test_a_failure(self, hfr):
        fields = hfr._base_fields(1_800_000_000_000_000_000)
        fields["status"] = hfr.STATUS_FAILURE
        fields["error_kind"] = hfr.KIND_TIMEOUT
        fields["error_text"] = "deadline"
        line = hfr.build_line(self._tags(), fields)
        assert 'status="failure"' in line
        assert 'error_kind="timeout"' in line
        assert 'error_text="deadline"' in line
        # Absent on a skip/failure that never chose a file.
        assert "hot_file_index=" not in line

    def test_a_skipped_round(self, hfr):
        fields = hfr._base_fields(1_800_000_000_000_000_000)
        fields["error_kind"] = hfr.KIND_NO_HOT_FILES
        line = hfr.build_line(self._tags(), fields)
        assert 'status="skipped"' in line
        assert 'error_kind="no_hot_files"' in line
        # Still a point: a round with none is itself a finding, and it only
        # reads that way if a skip is a point.
        assert "swarm_present=0i" in line

    def test_no_timestamp_is_ever_sent(self, hfr):
        # Influx stamps the point with its own server-receive time, the only
        # correct clock on a testnet whose manifests skew client clocks.
        line = hfr.build_line(self._tags(), hfr._base_fields(1))
        assert not line.rstrip().split(" ")[-1].isdigit()

    def test_start_ns_is_exact(self, hfr):
        # Nanoseconds since the epoch is ~1.8e18. Python ints are exact, but the
        # field must carry the `i` suffix or Influx types it as a float and
        # rounds it.
        line = hfr.build_line(self._tags(), hfr._base_fields(1_800_000_000_123_456_789))
        assert "start_ns=1800000000123456789i" in line

    def test_a_newline_in_the_error_cannot_split_the_point(self, hfr):
        # Influx's string-field escape handles only backslashes and quotes, so a
        # newline would split one failure's point across several physical lines
        # and take the write batch with it.
        fields = hfr._base_fields(1)
        fields["error_text"] = 'line one\nline "two"\\'
        line = hfr.build_line(self._tags(), fields)
        assert "\n" not in line
        assert '\\"two\\"' in line


# --------------------------------------------------------------------------- #
# the reader service: the command and the deadline
# --------------------------------------------------------------------------- #
def _config(hfr, **env):
    base = {
        "TESTNET_NAME": "DEV-01",
        "REGION": "lon1",
        "CLIENT_INDEX": "3",
        "INSTANCES": "2",
        "ROUND_PERIOD_SECS": "900",
        "DOWNLOAD_TIMEOUT_SECS": "420",
        "ROUND_PARITY": "even",
        "START_STAGGER_SECS": "2",
        "HOT_FILES_URL": "https://example/hot.json",
        "ANT": "/root/.local/bin/ant",
    }
    base.update(env)
    with patch.dict(os.environ, base, clear=True):
        return hfr.Config()


class TestCommand:
    def test_json_is_a_global_flag_before_the_subcommand(self, hfr):
        # Where `uploader.sh` puts it, and the only position proven to work.
        argv = _config(hfr).command("deadbeef", "/tmp/out")
        assert argv.index("--json") < argv.index("file")
        assert argv[argv.index("file") + 1] == "download"
        assert argv[argv.index("download") + 1] == "deadbeef"
        assert argv[argv.index("-o") + 1] == "/tmp/out"

    def test_verbose_is_always_passed(self, hfr):
        # `ant` gates its tracing subscriber behind `-v`, so without it the
        # role's `rust_log` option would be a setting nothing applied.
        assert "-v" in _config(hfr).command("a", "/tmp/o")

    def test_the_peer_cache_default_omits_bootstrap(self, hfr):
        argv = _config(hfr, BOOTSTRAP_ARGS="--bootstrap 1.2.3.4:10000").command("a", "/tmp/o")
        assert "--bootstrap" not in argv

    def test_bootstrap_cache_false_restores_the_explicit_peers(self, hfr):
        argv = _config(
            hfr, BOOTSTRAP_CACHE="false", BOOTSTRAP_ARGS="--bootstrap 1.2.3.4:10000"
        ).command("a", "/tmp/o")
        assert argv[argv.index("--bootstrap") + 1] == "1.2.3.4:10000"

    def test_an_empty_rust_log_is_removed_not_passed_empty(self, hfr):
        # An EMPTY RUST_LOG sets ant's EnvFilter to an empty filter, which
        # silences everything and overrides `-v`.
        with patch.dict(os.environ, {"RUST_LOG": ""}, clear=False):
            assert "RUST_LOG" not in _config(hfr).child_env()

    def test_a_set_rust_log_reaches_the_child(self, hfr):
        assert _config(hfr, RUST_LOG="ant_core=debug").child_env()["RUST_LOG"] == "ant_core=debug"

    def test_the_offset_comes_from_this_vm_s_index(self, hfr):
        cfg = _config(hfr)
        # client_index 3, 2 instances, 2s stagger.
        assert cfg.offset_for(1) == 8
        assert cfg.offset_for(2) == 10


class TestDeadline:
    def _entry(self):
        return {"address": "deadbeef", "bytes": 5, "sha256": None}

    def _run(self, hfr, tmp_path, argv, *, deadline_secs=1, size=5):
        """Run a round with `ant` replaced by a shell command.

        The real binary does not exist here, so the command is swapped for one
        whose behaviour the test chooses. Everything around the spawn -- the
        deadline, the classification, the verification, the point -- is what
        this role's correctness consists of.
        """
        import time as _time

        cfg = _config(hfr)
        cfg.download_dir = str(tmp_path / "dl")
        cfg.log_dir = str(tmp_path / "logs")
        cfg.download_timeout_secs = deadline_secs
        cfg.command = lambda address, output_path: argv(output_path)
        # The deadline is measured from the round BOUNDARY, so the round has to
        # be the one that is current now.
        round_id = hfr.round_id_at(_time.time(), cfg.round_period_secs)
        boundary = hfr.boundary_of(round_id, cfg.round_period_secs)
        cfg.round_period_secs = cfg.round_period_secs
        cfg.download_timeout_secs = int(_time.time() - boundary) + deadline_secs
        entry = self._entry()
        entry["bytes"] = size
        return cfg, hfr.run_download(cfg, round_id, 1, entry, boundary * 1_000_000_000)

    def test_a_success_is_recorded_with_its_swarm_figures(self, hfr, tmp_path):
        payload = json.dumps({**SWARM_RESULT, "size": 5})
        _, fields = self._run(
            hfr,
            tmp_path,
            lambda out: ["sh", "-c", f"printf 'hello' > {out}; echo '{payload}'"],
        )
        assert fields["status"] == hfr.STATUS_SUCCESS
        assert fields["error_kind"] == ""
        assert fields["records_from_peers"] == 7
        assert fields["swarm_present"] == 1
        assert fields["client_elapsed_ms"] == 12500
        # No published hash, so unverifiable -- which is not a failure.
        assert fields["verify_status"] == hfr.VERIFY_UNAVAILABLE

    def test_a_successful_download_is_deleted_and_its_logs_discarded(self, hfr, tmp_path):
        payload = json.dumps({**SWARM_RESULT, "size": 5})
        cfg, _ = self._run(
            hfr,
            tmp_path,
            lambda out: ["sh", "-c", f"printf 'hello' > {out}; echo '{payload}'"],
        )
        assert list(Path(cfg.download_dir).iterdir()) == []
        # Only a FAILED download's `ant` output is kept, as `downloader.sh` does.
        assert list(Path(cfg.log_dir).iterdir()) == []

    def test_a_failed_download_keeps_its_logs(self, hfr, tmp_path):
        cfg, fields = self._run(
            hfr, tmp_path, lambda out: ["sh", "-c", "echo 'Error: quorum' >&2; exit 1"]
        )
        assert fields["status"] == hfr.STATUS_FAILURE
        assert fields["error_kind"] == hfr.KIND_QUORUM
        assert "quorum" in fields["error_text"]
        assert {p.suffix for p in Path(cfg.log_dir).iterdir()} == {".stdout", ".stderr"}

    def test_the_deadline_kills_the_process(self, hfr, tmp_path):
        _, fields = self._run(hfr, tmp_path, lambda out: ["sleep", "30"], deadline_secs=1)
        assert fields["status"] == hfr.STATUS_FAILURE
        assert fields["error_kind"] == hfr.KIND_TIMEOUT
        assert fields["error_text"] == "deadline"

    def test_an_expired_round_spawns_nothing_at_all(self, hfr, tmp_path):
        """A reader whose own start lands past the deadline must not run `ant`.

        Spawning anyway (and granting `max(1.0, remaining)`) let a download run
        on into the NEXT round -- which on a parity-split deployment is the
        other cohort's, so its reads would join their swarm and contaminate the
        branch/control comparison. With period 600, deadline 300, two instances
        and a 600 s stagger, reader 2 is exactly that case, and it reported
        `success`.
        """
        import time as _time

        cfg = _config(hfr)
        cfg.download_dir = str(tmp_path / "dl")
        cfg.log_dir = str(tmp_path / "logs")
        # A round whose boundary + deadline is already in the past.
        round_id = hfr.round_id_at(_time.time(), cfg.round_period_secs) - 2
        cfg.command = lambda address, out: ["sh", "-c", f"printf 'x' > {out}"]
        with patch.object(subprocess, "Popen", side_effect=AssertionError("spawned")):
            fields = hfr.run_download(
                cfg,
                round_id,
                1,
                {"address": "deadbeef", "bytes": 1, "sha256": None},
                round_id * cfg.round_period_secs * 1_000_000_000,
            )
        assert fields["status"] == hfr.STATUS_FAILURE
        assert fields["error_kind"] == hfr.KIND_TIMEOUT
        assert fields["error_text"] == "deadline passed before start"

    def test_the_remaining_time_is_never_floored_upwards(self, hfr, tmp_path):
        # `max(1.0, deadline - now)` handed an already-expired download another
        # second, which is how one came back `success` past its own deadline.
        source = SCRIPT.read_text()
        assert "max(1.0, deadline" not in source
        assert "proc.wait(timeout=remaining)" in source

    def test_a_size_mismatch_is_a_failure_naming_what_came_back(self, hfr, tmp_path):
        payload = json.dumps({**RELEASED_RESULT, "size": 4})
        _, fields = self._run(
            hfr,
            tmp_path,
            lambda out: ["sh", "-c", f"printf 'hell' > {out}; echo '{payload}'"],
            size=5,
        )
        assert fields["status"] == hfr.STATUS_FAILURE
        assert fields["error_kind"] == hfr.KIND_OTHER
        assert fields["error_text"] == "size mismatch: got 4"

    def test_exit_zero_with_no_result_is_a_failure(self, hfr, tmp_path):
        _, fields = self._run(hfr, tmp_path, lambda out: ["sh", "-c", f"printf 'x' > {out}"])
        assert fields["status"] == hfr.STATUS_FAILURE
        assert fields["error_kind"] == hfr.KIND_OTHER
        assert fields["error_text"] == "no json result"

    def test_a_content_mismatch_is_its_own_kind(self, hfr, tmp_path):
        # A download that fails is availability; one that returns DIFFERENT
        # bytes is correctness.
        import time as _time

        cfg = _config(hfr)
        cfg.download_dir = str(tmp_path / "dl")
        cfg.log_dir = str(tmp_path / "logs")
        payload = json.dumps({**RELEASED_RESULT, "size": 5})
        cfg.command = lambda address, out: [
            "sh",
            "-c",
            f"printf 'hello' > {out}; echo '{payload}'",
        ]
        round_id = hfr.round_id_at(_time.time(), cfg.round_period_secs)
        boundary = hfr.boundary_of(round_id, cfg.round_period_secs)
        cfg.download_timeout_secs = int(_time.time() - boundary) + 5
        entry = {"address": "deadbeef", "bytes": 5, "sha256": "0" * 64}
        fields = hfr.run_download(cfg, round_id, 1, entry, boundary * 1_000_000_000)
        assert fields["status"] == hfr.STATUS_FAILURE
        assert fields["error_kind"] == hfr.KIND_VERIFY
        assert fields["verify_status"] == hfr.VERIFY_MISMATCH

    def test_the_swarm_figures_of_a_failure_are_still_recorded(self, hfr, tmp_path):
        # A reader fed by a peer and then failing says something different from
        # one that never reached the swarm at all.
        payload = json.dumps(SWARM_RESULT)
        _, fields = self._run(
            hfr,
            tmp_path,
            lambda out: ["sh", "-c", f"echo '{payload}'; echo 'Error: quorum' >&2; exit 1"],
        )
        assert fields["status"] == hfr.STATUS_FAILURE
        assert fields["records_from_peers"] == 7

    def test_an_exception_around_the_download_is_a_recorded_failure(self, hfr, tmp_path):
        # A reader that stopped emitting points would read as one that never ran.
        cfg = _config(hfr)
        cfg.download_dir = str(tmp_path / "dl")
        cfg.log_dir = str(tmp_path / "logs")
        cfg.sink = MagicMock(enabled=False)
        emitted = []
        with (
            patch.object(hfr, "fetch_hot_files", return_value=[{"address": "ab", "bytes": 1}]),
            patch.object(hfr, "run_download", side_effect=OSError("boom")),
            patch.object(hfr, "emit", side_effect=lambda c, r, i, idx, f: emitted.append(f)),
        ):
            hfr.run_reader_round(cfg, 2, 1)
        (fields,) = emitted
        assert fields["status"] == hfr.STATUS_FAILURE
        assert fields["error_kind"] == hfr.KIND_OTHER
        assert "boom" in fields["error_text"]

    def test_a_round_with_no_selection_emits_one_skip_and_spawns_nothing(self, hfr, tmp_path):
        cfg = _config(hfr)
        cfg.sink = MagicMock(enabled=False)
        emitted = []
        with (
            patch.object(hfr, "fetch_hot_files", return_value=[]),
            patch.object(hfr, "emit", side_effect=lambda c, r, i, idx, f: emitted.append(f)),
            patch.object(subprocess, "Popen", side_effect=AssertionError("spawned")),
        ):
            hfr.run_reader_round(cfg, 2, 1)
        (fields,) = emitted
        assert fields["status"] == hfr.STATUS_SKIPPED
        assert fields["error_kind"] == hfr.KIND_NO_HOT_FILES

    def test_an_impossible_start_is_refused_without_sleeping(self, hfr):
        """A stagger that puts a start at or past its own deadline.

        Sleeping to it anyway blocked the whole cohort: `main` joins every
        reader thread, so one reader waiting until boundary + 720 s of a 600 s
        round held the loop past the NEXT boundary and every reader — including
        the ones that finished on time — took a `round_overrun` skip for a round
        they could have run. A long stagger is deliberately allowed, so the
        answer is to record THIS reader's timeout and leave the others alone.
        """
        # `CLIENT_INDEX=1` so the offsets are the reviewer's 0 and 720; the
        # shared `_config` helper is index 3, which only makes them larger.
        cfg = _config(hfr, CLIENT_INDEX="1", INSTANCES="2", START_STAGGER_SECS="720")
        cfg.sink = MagicMock(enabled=False)
        assert cfg.offset_for(2) == 720
        assert cfg.download_timeout_secs == 420

        emitted = []
        slept = []
        with (
            patch.object(hfr, "emit", side_effect=lambda c, r, i, idx, f: emitted.append(f)),
            patch.object(hfr.time, "sleep", side_effect=lambda d: slept.append(d)),
            patch.object(hfr, "fetch_hot_files", side_effect=AssertionError("fetched")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("spawned")),
        ):
            hfr.run_reader_round(cfg, 1000, 2)
        (fields,) = emitted
        assert fields["status"] == hfr.STATUS_FAILURE
        assert fields["error_kind"] == hfr.KIND_TIMEOUT
        assert "start offset 720s is at or past the 420s deadline" in fields["error_text"]
        # The point of the fix: no sleep, so it cannot hold the cohort's loop.
        assert slept == []

    def test_a_possible_start_is_not_refused(self, hfr):
        # The guard is on the OFFSET against the deadline, so an ordinary
        # stagger still runs: 2s offset against a 420s deadline.
        cfg = _config(hfr)
        cfg.sink = MagicMock(enabled=False)
        emitted = []
        with (
            patch.object(hfr, "emit", side_effect=lambda c, r, i, idx, f: emitted.append(f)),
            patch.object(hfr, "fetch_hot_files", return_value=[{"address": "ab", "bytes": 1}]),
            patch.object(
                hfr,
                "run_download",
                side_effect=lambda c, r, i, e, s: {
                    **hfr._base_fields(s),
                    "status": hfr.STATUS_SUCCESS,
                },
            ),
            patch.object(hfr.time, "sleep", return_value=None),
        ):
            hfr.run_reader_round(cfg, 1000, 1)
        assert emitted[0]["status"] == hfr.STATUS_SUCCESS

    def test_an_impossible_start_suppresses_no_other_reader_s_rounds(self, hfr):
        """Driven through the real `main` on a simulated clock.

        The reviewer's reproduction: two instances, period 600, deadline 300,
        stagger 720. Reader 2's start can never run; reader 1 must keep every
        round, and nothing may emit `round_overrun`.
        """

        class _Stop(Exception):
            pass

        clock = {"t": 1.0}
        emitted = []

        def _sleep(seconds):
            clock["t"] += max(0.0, seconds)

        def _sleep_until(when):
            if when > clock["t"]:
                clock["t"] = when
            if clock["t"] > 2400:
                raise _Stop()

        def _download(cfg, round_id, instance, entry, start_ns):
            clock["t"] += 5
            return {**hfr._base_fields(start_ns), "status": hfr.STATUS_SUCCESS}

        env = {
            "TESTNET_NAME": "DEV-03",
            "REGION": "lon1",
            "CLIENT_INDEX": "1",
            "INSTANCES": "2",
            "ROUND_PERIOD_SECS": "600",
            "DOWNLOAD_TIMEOUT_SECS": "300",
            "ROUND_PARITY": "all",
            "START_STAGGER_SECS": "720",
            "ANT": "/bin/true",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(hfr.time, "time", side_effect=lambda: clock["t"]),
            patch.object(hfr.time, "sleep", side_effect=_sleep),
            patch.object(hfr, "sleep_until", side_effect=_sleep_until),
            patch.object(hfr, "fetch_hot_files", return_value=[{"address": "ab", "bytes": 1}]),
            patch.object(hfr, "run_download", side_effect=_download),
            patch.object(
                hfr, "emit", side_effect=lambda c, r, i, idx, f: emitted.append((r, i, f))
            ),
            patch.object(subprocess, "Popen", side_effect=AssertionError("spawned")),
        ):
            with pytest.raises(_Stop):
                hfr.main()

        assert hfr.KIND_ROUND_OVERRUN not in {f["error_kind"] for _, _, f in emitted}
        ran = sorted(r for r, i, f in emitted if i == 1 and f["status"] == hfr.STATUS_SUCCESS)
        # Consecutive: not one of reader 1's rounds was suppressed.
        assert ran == list(range(ran[0], ran[0] + len(ran)))
        assert len(ran) >= 3
        # Reader 2 still reports, every round, so its impossibility is visible.
        assert all(f["error_kind"] == hfr.KIND_TIMEOUT for _, i, f in emitted if i == 2)

    def test_a_truncated_manifest_response_still_records_the_round(self, hfr, tmp_path):
        """A body that stops short of its `Content-Length`.

        `http.client.IncompleteRead` is an `HTTPException`, not an `OSError`, so
        it escaped the fetch's except tuple — and with the fetch outside the
        round's guard the reader thread died emitting nothing, leaving a
        permanent hole in that reader's series. Driven against a real socket
        server, as the reviewer's probe was.
        """
        import socket as _socket
        import threading as _threading

        server = _socket.socket()
        server.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def _serve():
            try:
                conn, _ = server.accept()
                conn.recv(4096)
                # Advertises 1,000 bytes and sends ten, then closes.
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n" + b'{"files":[')
                conn.close()
            except OSError:
                pass

        thread = _threading.Thread(target=_serve, daemon=True)
        thread.start()
        try:
            cfg = _config(hfr, HOT_FILES_URL=f"http://127.0.0.1:{port}/hot.json")
            cfg.sink = MagicMock(enabled=False)
            emitted = []
            with patch.object(hfr, "emit", side_effect=lambda c, r, i, idx, f: emitted.append(f)):
                hfr.run_reader_round(cfg, 3000000, 1)
        finally:
            server.close()
            thread.join(timeout=5)

        # One point, always: a round with none is the one thing that must not
        # happen, because a missing point is read as a finding.
        (fields,) = emitted
        assert fields["status"] == hfr.STATUS_SKIPPED
        assert fields["error_kind"] == hfr.KIND_NO_HOT_FILES

    def test_the_fetch_swallows_an_http_body_read_failure(self, hfr):
        import http.client

        with patch.object(
            hfr.urllib.request,
            "urlopen",
            side_effect=http.client.IncompleteRead(b"{", 990),
        ):
            assert hfr.fetch_hot_files("https://example/hot.json") == []

    def test_anything_at_all_around_a_round_still_records_a_point(self, hfr):
        # The backstop: the inner guard covers the download, this one covers
        # the fetch, the sleep and the emit path, so no exception can turn a
        # round into silence.
        cfg = _config(hfr)
        cfg.sink = MagicMock(enabled=False)
        emitted = []
        with (
            patch.object(hfr, "fetch_hot_files", side_effect=RuntimeError("boom")),
            patch.object(hfr, "emit", side_effect=lambda c, r, i, idx, f: emitted.append(f)),
        ):
            hfr.run_reader_round(cfg, 3000000, 1)
        (fields,) = emitted
        assert fields["status"] == hfr.STATUS_FAILURE
        assert fields["error_kind"] == hfr.KIND_OTHER
        assert "RuntimeError: boom" in fields["error_text"]

    def test_an_overrun_round_emits_one_skip_per_reader(self, hfr):
        cfg = _config(hfr)
        cfg.sink = MagicMock(enabled=False)
        emitted = []
        with patch.object(hfr, "emit", side_effect=lambda c, r, i, idx, f: emitted.append((i, f))):
            hfr.emit_overrun(cfg, 2)
        assert [i for i, _ in emitted] == [1, 2]
        assert all(f["error_kind"] == hfr.KIND_ROUND_OVERRUN for _, f in emitted)
        assert all(f["status"] == hfr.STATUS_SKIPPED for _, f in emitted)


def test_the_script_compiles_under_the_vm_s_python():
    # Ubuntu 24.04 ships 3.12; this runs under whatever the test environment
    # has, which is >= 3.13, so the useful property is that it compiles at all.
    subprocess.run(["python3", "-I", "-m", "py_compile", str(SCRIPT)], check=True)


def test_the_script_imports_nothing_from_saorsa_deploy():
    # It is copied verbatim onto the client VM, which has no checkout. The
    # module docstring says so and so names the package, so the check is on the
    # IMPORT lines rather than on the whole text.
    imports = [
        line
        for line in SCRIPT.read_text().splitlines()
        if line.startswith(("import ", "from ")) or line.lstrip().startswith(("import ", "from "))
    ]
    assert imports
    assert not [line for line in imports if "saorsa_deploy" in line]


# --------------------------------------------------------------------------- #
# Influx -> Postgres
# --------------------------------------------------------------------------- #
def _csv(rows: list[dict]) -> str:
    """A pivoted Influx CSV answer for the fields each row names."""
    from saorsa_deploy.influx import HOT_FILE_BASE_FIELDS, HOT_FILE_SWARM_FIELDS

    columns = [
        "",
        "result",
        "table",
        "_time",
        "_measurement",
        "host",
        "region",
        "round_id",
        "instance",
        "hot_file_index",
        *HOT_FILE_BASE_FIELDS,
        *HOT_FILE_SWARM_FIELDS,
    ]
    lines = [",".join(columns)]
    for row in rows:
        values = ["", "_result", "0"]
        for column in columns[3:]:
            values.append(str(row.get(column, "")))
        lines.append(",".join(values))
    return "\n".join(lines) + "\n"


BASE_POINT = {
    "_time": "2026-11-06T12:00:00Z",
    "_measurement": "hot_file_download",
    "host": "DEV-01-readers-ant-client-hot-file-read-lon1-1",
    "region": "lon1",
    "round_id": 3000000,
    "instance": 1,
    "hot_file_index": 0,
    "status": "success",
    "address": "deadbeef",
    "file_size": 52428800,
    "total_ms": 12800,
    "client_elapsed_ms": 12500,
    "start_ns": 1800000000000000000,
    "started_late_ms": 42,
    "verify_status": 0,
    "swarm_present": 1,
    "swarm_rooms": "[]",
    "records_from_peers": 7,
    "bytes_from_peers": 7340032,
}


class TestInfluxQuery:
    def test_every_field_is_in_the_allowlist(self):
        # A field a client emits that the query does not name reaches no
        # consumer at all -- which is exactly how `reason` was invisible until
        # it was added to `PIVOT_FLUX_TEMPLATE`'s allowlist.
        from saorsa_deploy.influx import (
            HOT_FILE_BASE_FIELDS,
            HOT_FILE_SWARM_FIELDS,
            _hot_file_flux,
        )

        flux = _hot_file_flux("testnet-metrics", "0", "2100-01-01T00:00:00Z")
        for name in (*HOT_FILE_BASE_FIELDS, *HOT_FILE_SWARM_FIELDS):
            assert f'"{name}"' in flux, name

    def test_the_script_and_the_query_agree_on_the_counters(self, hfr):
        # Two lists of twenty-five, one on the VM and one in the tool. A counter
        # in one and not the other is a column that silently reads NULL forever.
        from saorsa_deploy.influx import HOT_FILE_SWARM_FIELDS

        expected = set(hfr.SWARM_FIELDS) | {f"discovery_{n}" for n in hfr.SWARM_DISCOVERY_FIELDS}
        assert set(HOT_FILE_SWARM_FIELDS) == expected

    def test_the_table_and_the_query_agree_on_the_counters(self):
        from saorsa_db.models import SWARM_COUNTER_COLUMNS

        from saorsa_deploy.influx import HOT_FILE_SWARM_FIELDS

        assert set(SWARM_COUNTER_COLUMNS) == set(HOT_FILE_SWARM_FIELDS)

    def test_the_range_carries_an_explicit_stop(self):
        from saorsa_deploy.influx import FLUX_FAR_FUTURE, _hot_file_flux

        # Flux defaults `stop` to `now()`, which silently discards a
        # forward-skewed host's points.
        assert f"stop: {FLUX_FAR_FUTURE}" in _hot_file_flux("b", "0", FLUX_FAR_FUTURE)

    def test_a_pivoted_point_is_parsed(self):
        from saorsa_deploy.influx import parse_hot_file_download_csv

        (point,) = parse_hot_file_download_csv(_csv([BASE_POINT]))
        assert point["host"] == BASE_POINT["host"]
        assert point["round_id"] == 3000000
        assert point["instance"] == 1
        assert point["hot_file_index"] == 0
        assert point["status"] == "success"
        assert point["file_size"] == 52428800
        assert point["client_elapsed_ms"] == 12500
        assert point["start_ns"] == 1800000000000000000
        assert point["verify_status"] == 0
        assert point["swarm_present"] == 1
        assert point["records_from_peers"] == 7

    def test_an_absent_counter_parses_as_none_not_zero(self):
        from saorsa_deploy.influx import parse_hot_file_download_csv

        (point,) = parse_hot_file_download_csv(_csv([BASE_POINT]))
        # `records_rejected` was not emitted by this point at all.
        assert point["records_rejected"] is None

    def test_a_point_with_no_round_or_reader_is_dropped(self):
        from saorsa_deploy.influx import parse_hot_file_download_csv

        assert parse_hot_file_download_csv(_csv([{**BASE_POINT, "round_id": ""}])) == []
        assert parse_hot_file_download_csv(_csv([{**BASE_POINT, "instance": ""}])) == []

    def test_another_measurement_is_ignored(self):
        from saorsa_deploy.influx import parse_hot_file_download_csv

        assert parse_hot_file_download_csv(_csv([{**BASE_POINT, "_measurement": "download"}])) == []


# --------------------------------------------------------------------------- #
# the summary the --hot-file view renders
# --------------------------------------------------------------------------- #
def _summary_point(**extra):
    return {
        "round_id": 10,
        "host": "h1",
        "instance": 1,
        "status": "success",
        "total_ms": 1000,
        "start_ns": 6_000_000_000_000,
        "records_from_peers": 0,
        "bytes_from_peers": 0,
        "records_served": 0,
        **extra,
    }


class TestHotFileRoundSummary:
    def test_one_row_per_round(self):
        from saorsa_deploy.cmd.print_file_ops import summarise_hot_file_rounds

        rounds = summarise_hot_file_rounds(
            [
                _summary_point(round_id=10, instance=1),
                _summary_point(round_id=10, instance=2),
                _summary_point(round_id=11, instance=1),
            ]
        )
        assert [r["round_id"] for r in rounds] == [10, 11]
        assert rounds[0]["readers"] == 2

    def test_rows_are_split_by_extension(self):
        # The whole point of the role is two cohorts on one deployment; a row
        # pooling them would average the two arms of the comparison.
        from saorsa_deploy.cmd.print_file_ops import summarise_hot_file_rounds

        rounds = summarise_hot_file_rounds(
            [
                _summary_point(round_id=10, extension="branch", records_from_peers=5),
                _summary_point(round_id=10, instance=2, extension="control"),
            ]
        )
        assert [(r["extension"], r["records_from_peers"]) for r in rounds] == [
            ("branch", 5),
            ("control", 0),
        ]

    def test_skips_are_counted_but_out_of_the_percentiles(self):
        from saorsa_deploy.cmd.print_file_ops import summarise_hot_file_rounds

        (row,) = summarise_hot_file_rounds(
            [
                _summary_point(total_ms=1000),
                _summary_point(instance=2, status="skipped", total_ms=None),
            ]
        )
        assert row["success"] == 1
        assert row["skipped"] == 1
        assert row["p50_ms"] == 1000

    def test_peer_fed_counts_readers_not_records(self):
        # One reader served by a peer a thousand times is not a thousand readers
        # the swarm reached.
        from saorsa_deploy.cmd.print_file_ops import summarise_hot_file_rounds

        (row,) = summarise_hot_file_rounds(
            [
                _summary_point(instance=1, records_from_peers=1000),
                _summary_point(instance=2, records_from_peers=0),
            ]
        )
        assert row["readers_with_peer_records"] == 1
        assert row["records_from_peers"] == 1000

    def test_failures_are_grouped_by_kind(self):
        from saorsa_deploy.cmd.print_file_ops import summarise_hot_file_rounds

        (row,) = summarise_hot_file_rounds(
            [
                _summary_point(status="failure", error_kind="timeout"),
                _summary_point(instance=2, status="failure", error_kind="timeout"),
                _summary_point(instance=3, status="failure", error_kind=None),
            ]
        )
        assert row["error_kinds"] == {"timeout": 2, "unknown": 1}

    def test_the_boundary_is_round_id_times_the_resolved_period(self):
        from saorsa_deploy.cmd.print_file_ops import summarise_hot_file_rounds

        # round 10 x a 600 s period.
        (row,) = summarise_hot_file_rounds(
            [_summary_point(start_ns=6_000_000_000_000)], lambda extension, secs: 600
        )
        assert row["boundary"] == datetime.fromtimestamp(6000, tz=timezone.utc)

    def test_the_boundary_ignores_the_stagger(self):
        """`start_ns` is a reader's SCHEDULED start, offset included.

        So the minimum over a group is the boundary only when an unstaggered
        reader happens to be in it. A cohort whose indexes begin at 5 with a 2 s
        stagger has its earliest reader at +32 s, and the column read 08:10:32
        for a round that began at 08:10:00. A missing early reader did the same.
        """
        from saorsa_deploy.cmd.print_file_ops import summarise_hot_file_rounds

        period, round_id = 600, 3011502
        boundary = round_id * period

        def _reader(offset, instance):
            return _summary_point(
                round_id=round_id,
                instance=instance,
                extension="control",
                start_ns=(boundary + offset) * 1_000_000_000,
            )

        expected = datetime.fromtimestamp(boundary, tz=timezone.utc)

        def periods(extension, start_secs):
            return period if extension == "control" else None

        # Indexes from 5, four instances, 2 s stagger: offsets 32, 34, 36, 38.
        (row,) = summarise_hot_file_rounds(
            [_reader(32, 1), _reader(34, 2), _reader(36, 3), _reader(38, 4)], periods
        )
        assert row["boundary"] == expected
        # And with only the LAST reader reporting, which is the same defect.
        (row,) = summarise_hot_file_rounds([_reader(38, 4)], periods)
        assert row["boundary"] == expected
        # An unstaggered cohort (the default) is unchanged.
        (row,) = summarise_hot_file_rounds([_reader(0, 1)], periods)
        assert row["boundary"] == expected

    def test_a_daily_round_whose_offsets_exceed_the_round_id(self):
        """Why the period is RESOLVED rather than inferred from the round id.

        `floor(start_secs / round_id)` is the period only while every offset is
        below the round id, and nothing guarantees that. A daily round
        (`round_period_secs=86400`) has round ids in the twenty thousands, so a
        7,200 s stagger puts offsets far above one — and the inference reported
        05:46:03 for a 00:00:00 boundary.
        """
        from saorsa_deploy.cmd.print_file_ops import summarise_hot_file_rounds

        period, round_id = 86400, 20763
        boundary = round_id * period

        def _reader(offset, instance):
            return _summary_point(
                round_id=round_id,
                instance=instance,
                extension="control",
                start_ns=(boundary + offset) * 1_000_000_000,
            )

        # client_index 2, four instances, stagger 7200 -> offsets 28800, 36000.
        (row,) = summarise_hot_file_rounds(
            [_reader(28800, 1), _reader(36000, 2)], lambda extension, secs: period
        )
        assert row["boundary"] == datetime.fromtimestamp(boundary, tz=timezone.utc)

    def test_two_eras_of_one_round_are_two_rows(self):
        """The live grouping is schedule-qualified by the round's boundary.

        A `round_id` is `floor(epoch / period)`, so a cohort recreated under the
        same name with 86401 s instead of 86400 s has two real rounds called
        20833 — 2027-01-15 00:00:00 and 05:47:13. Pooling them averages two
        schedules.
        """
        from saorsa_deploy.cmd.print_file_ops import summarise_hot_file_rounds
        from saorsa_deploy.hot_file_records import merge_hot_file_config, period_resolver

        host = "DEV-03-ctl-ant-client-hot-file-read-lon1-1"
        schedule = {
            "hostname": host,
            "extension": "ctl",
            "client_index": 1,
            "instances": 1,
            "download_timeout_secs": 300,
            "round_parity": "all",
            "start_stagger_secs": 0,
        }
        # The re-provision falls BETWEEN the two starts, which is the realistic
        # shape: rounds before it ran on the old period, rounds after it on the
        # new one.
        resolve = period_resolver(
            merge_hot_file_config(
                [{**schedule, "round_period_secs": 86400, "recorded_at": "2027-01-01T00:00:00Z"}],
                [{**schedule, "round_period_secs": 86401, "recorded_at": "2027-01-15T03:00:00Z"}],
            )
        )

        def _reader(period):
            return _summary_point(
                round_id=20833,
                host=host,
                extension="ctl",
                start_ns=20833 * period * 1_000_000_000,
            )

        rows = summarise_hot_file_rounds([_reader(86400), _reader(86401)], resolve)
        assert [r["boundary"] for r in rows] == [
            datetime(2027, 1, 15, 0, 0, tzinfo=timezone.utc),
            datetime(2027, 1, 15, 5, 47, 13, tzinfo=timezone.utc),
        ]
        # And with no resolver the two collapse into one bucket with no
        # boundary, exactly as before — the accepted display limitation.
        (row,) = summarise_hot_file_rounds([_reader(86400), _reader(86401)])
        assert row["boundary"] is None

    def test_an_unresolved_period_shows_no_boundary(self):
        # A dash is a worse reading than a time but a better one than a wrong
        # time, and the recorded role config exists so this is answerable.
        from saorsa_deploy.cmd.print_file_ops import summarise_hot_file_rounds

        (row,) = summarise_hot_file_rounds([_summary_point()])
        assert row["boundary"] is None
        # A resolver that cannot settle this cohort's period says so.
        (row,) = summarise_hot_file_rounds([_summary_point()], lambda extension, secs: None)
        assert row["boundary"] is None

    def test_the_period_is_resolved_per_cohort_and_disagreement_is_unknown(self):
        from saorsa_deploy.hot_file_records import round_periods_by_extension

        assert round_periods_by_extension(
            [
                {"extension": "branch", "round_period_secs": 600},
                {"extension": "branch", "round_period_secs": 600},
                {"extension": "control", "round_period_secs": 86400},
                {"extension": None, "round_period_secs": 600},
            ]
        ) == {"branch": 600, "control": 86400, None: 600}
        # Two periods inside one cohort is not one cohort; no guess is made.
        assert (
            round_periods_by_extension(
                [
                    {"extension": "split", "round_period_secs": 600},
                    {"extension": "split", "round_period_secs": 900},
                ]
            )
            == {}
        )

    def test_the_cohort_totals_are_never_pooled(self):
        """A branch cohort at 100% and a control at 0% average to a lie.

        `--summary` reported "50% completion, 50% peer-fed" with neither cohort
        named, which deletes the comparison the role exists for — only the
        per-round table above it was split.
        """
        from saorsa_deploy.cmd.print_file_ops import hot_file_cohort_totals

        def _round(extension, ok, fail, peer_fed):
            return {
                "round_id": 1,
                "extension": extension,
                "success": ok,
                "failure": fail,
                "skipped": 0,
                "attempted": ok + fail,
                "readers": ok + fail,
                "readers_with_peer_records": peer_fed,
                "records_from_peers": peer_fed * 10,
                "bytes_from_peers": peer_fed * 100,
                "records_served": 0,
            }

        branch, control = hot_file_cohort_totals(
            [_round("branch", 4, 0, 4), _round("control", 0, 4, 0)]
        )
        assert branch["label"] == "branch"
        assert branch["success"] == 4 and branch["attempted"] == 4
        assert branch["readers_with_peer_records"] == 4
        assert control["label"] == "control"
        assert control["success"] == 0 and control["attempted"] == 4
        assert control["readers_with_peer_records"] == 0

    def test_the_deployment_s_own_tier_is_labelled_in_the_totals(self):
        from saorsa_deploy.cmd.print_file_ops import hot_file_cohort_totals

        (cohort,) = hot_file_cohort_totals(
            [
                {
                    "round_id": 1,
                    "extension": None,
                    "success": 1,
                    "failure": 0,
                    "skipped": 0,
                    "attempted": 1,
                    "readers": 1,
                    "readers_with_peer_records": 1,
                    "records_from_peers": 1,
                    "bytes_from_peers": 1,
                    "records_served": 0,
                }
            ]
        )
        assert cohort["label"] == "deployment"

    def test_the_rendered_summary_names_both_cohorts(self):
        from rich.console import Console

        from saorsa_deploy.cmd.print_file_ops import _print_hot_file_summary

        def _round(extension, ok, fail, peer_fed):
            return {
                "round_id": 1,
                "extension": extension,
                "success": ok,
                "failure": fail,
                "skipped": 0,
                "attempted": ok + fail,
                "readers": ok + fail,
                "readers_with_peer_records": peer_fed,
                "records_from_peers": peer_fed * 10,
                "bytes_from_peers": peer_fed * 100,
                "records_served": 0,
            }

        console = Console(record=True, width=200)
        _print_hot_file_summary(
            [_round("branch", 4, 0, 4), _round("control", 0, 4, 0)], "DEV-03", console
        )
        text = console.export_text()
        assert "branch" in text and "control" in text
        assert "100.00%" in text and "0.00%" in text
        # And never the pooled figure the two used to average to.
        assert "50.00%" not in text

    def test_the_browser_summary_s_boundary_is_untouched(self):
        # The browser harness stamps `start_ns` AS the round boundary (it has no
        # stagger), so its derivation must stay exactly as it was.
        from saorsa_deploy.cmd.print_file_ops import summarise_browser_rounds

        (row,) = summarise_browser_rounds(
            [
                {
                    "round_id": 2987191,
                    "host": "h",
                    "instance": 1,
                    "status": "success",
                    "total_ms": 1,
                    "start_ns": 2987191 * 600 * 1_000_000_000,
                }
            ]
        )
        assert row["boundary"] == datetime.fromtimestamp(2987191 * 600, tz=timezone.utc)

    def test_the_live_handler_labels_each_host_with_its_extension(self):
        """The live view must split by cohort, not pool them.

        The Influx point does not carry `extension` -- it is a property of the
        VM -- so without resolving it every live row read `None` and two
        cohorts reporting the same round came out as ONE row with their figures
        added together. On a parity-split run that is the whole comparison
        averaged away.
        """
        from types import SimpleNamespace

        from rich.console import Console

        from saorsa_deploy.cmd import print_file_ops as pfo

        swarm = "DEV-03-swarm-ant-client-hot-file-read-lon1-1"
        control = "DEV-03-control-ant-client-hot-file-read-fra1-2"

        def _point(host, records):
            return {
                "_time": None,
                "host": host,
                "region": "lon1",
                "round_id": 3000000,
                "instance": 1,
                "hot_file_index": 0,
                "status": "success",
                "error_kind": None,
                "error_text": None,
                "address": "ab",
                "file_size": 1,
                "total_ms": 1000,
                "client_elapsed_ms": 900,
                "start_ns": 3000000 * 600 * 1_000_000_000,
                "started_late_ms": 0,
                "verify_status": 2,
                "swarm_present": 1,
                "swarm_rooms": [],
                "records_from_peers": records,
                "bytes_from_peers": records * 10,
                "records_served": 0,
            }

        tiers = [
            (
                "swarm",
                {"clients": [{"role": "hot-file-read", "region": "lon1", "client_index": 1}]},
            ),
            (
                "control",
                {"clients": [{"role": "hot-file-read", "region": "fra1", "client_index": 2}]},
            ),
        ]
        args = SimpleNamespace(
            name="DEV-03",
            json_out=None,
            window="-1h",
            recent=False,
            summary=False,
            browser=False,
            hot_file=True,
        )
        console = Console(record=True, width=260)
        with (
            patch.object(
                pfo, "_resolve_influx", return_value=("http://i", "t", "ant", "testnet-metrics")
            ),
            patch.object(
                pfo,
                "fetch_hot_file_download_points",
                return_value=[_point(swarm, 7), _point(control, 0)],
            ),
            patch.object(pfo, "_client_tiers", return_value=tiers),
            patch("saorsa_deploy.registry.read_deploy_args", return_value=None),
            patch.object(pfo, "_resolve_retention", return_value=None),
            patch.object(pfo, "_resolve_deployment_start", return_value=None),
        ):
            pfo._run_hot_file_rounds(args, console)
        text = console.export_text()
        assert "Extension" in text
        assert "swarm" in text and "control" in text
        # One row per (round, extension): two rows for the one round, not one.
        assert len([line for line in text.splitlines() if "3000000" in line]) == 2
        # And the title counts ROUNDS, not rows -- counting rows reported a
        # parity-split run's rounds as twice what they are.
        assert "1 round(s), 2 row(s)" in text

    def test_the_live_handler_still_prints_when_the_extension_cannot_be_resolved(self):
        # A read-only diagnostic must not refuse over a registry or S3 failure:
        # it says the rounds are not split and prints them anyway.
        from types import SimpleNamespace

        from rich.console import Console

        from saorsa_deploy.cmd import print_file_ops as pfo

        args = SimpleNamespace(
            name="DEV-03",
            json_out=None,
            window="-1h",
            recent=False,
            summary=False,
            browser=False,
            hot_file=True,
        )
        console = Console(record=True, width=260)
        with (
            patch.object(
                pfo, "_resolve_influx", return_value=("http://i", "t", "ant", "testnet-metrics")
            ),
            patch.object(
                pfo,
                "fetch_hot_file_download_points",
                return_value=[
                    {
                        "host": "h1",
                        "round_id": 10,
                        "instance": 1,
                        "status": "success",
                        "total_ms": 1000,
                        "start_ns": 6_000_000_000_000,
                    }
                ],
            ),
            patch.object(pfo, "_client_tiers", side_effect=RuntimeError("no state")),
            patch.object(pfo, "_resolve_retention", return_value=None),
            patch.object(pfo, "_resolve_deployment_start", return_value=None),
        ):
            pfo._run_hot_file_rounds(args, console)
        text = console.export_text()
        assert "not split by cohort" in text
        assert "hot-file reader rounds" in text

    def test_skips_are_out_of_the_peer_fed_denominator(self):
        """`readers` counted skips, so the rate described reads nobody made.

        Nine `no_hot_files` skips followed by one peer-fed success reported
        "1/10 = 10.0%" when the only read ATTEMPTED was peer-fed. Skips are
        excluded from every denominator here, as they are from the completion
        rate.
        """
        from saorsa_deploy.cmd.print_file_ops import summarise_hot_file_rounds

        rows = [
            _summary_point(instance=i, status="skipped", error_kind="no_hot_files", total_ms=None)
            for i in range(1, 10)
        ]
        rows.append(_summary_point(instance=10, records_from_peers=7, bytes_from_peers=70))
        (row,) = summarise_hot_file_rounds(rows)
        assert row["readers"] == 10
        assert row["skipped"] == 9
        assert row["attempted"] == 1
        assert row["readers_with_peer_records"] == 1

    def test_the_rendered_peer_fed_rate_excludes_skips(self):
        from rich.console import Console

        from saorsa_deploy.cmd.print_file_ops import (
            _print_hot_file_summary,
            summarise_hot_file_rounds,
        )

        rows = [
            _summary_point(instance=i, status="skipped", error_kind="no_hot_files", total_ms=None)
            for i in range(1, 10)
        ]
        rows.append(_summary_point(instance=10, records_from_peers=7, bytes_from_peers=70))
        console = Console(record=True, width=220)
        _print_hot_file_summary(summarise_hot_file_rounds(rows), "DEV-03", console)
        text = console.export_text()
        assert "1/1 = 100.0%" in text
        assert "1/10" not in text

    def test_a_round_with_nothing_attempted_has_no_peer_fed_rate(self):
        # Undefined rather than 0%: a cohort that only skipped has no rate.
        from rich.console import Console

        from saorsa_deploy.cmd.print_file_ops import (
            _print_hot_file_summary,
            summarise_hot_file_rounds,
        )

        rows = [
            _summary_point(instance=i, status="skipped", error_kind="no_hot_files", total_ms=None)
            for i in range(1, 5)
        ]
        rounds = summarise_hot_file_rounds(rows)
        assert rounds[0]["attempted"] == 0
        console = Console(record=True, width=220)
        _print_hot_file_summary(rounds, "DEV-03", console)
        assert "n/a" in console.export_text()

    def test_browser_and_hot_file_together_are_refused(self):
        from types import SimpleNamespace

        from saorsa_deploy.cmd.print_file_ops import _dispatch

        args = SimpleNamespace(name="DEV-01", json_out=None, browser=True, hot_file=True)
        with pytest.raises(SystemExit) as exc:
            _dispatch(args, op_type="download")
        assert exc.value.code == 2


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #
def test_slack_renders_the_cohort_s_schedule():
    from saorsa_deploy.cmd.slack_post import _client_line, _clients_block

    client = {
        "role": ROLE_HOT_FILE_READ,
        "region": "lon1",
        "vm_size": "s-2vcpu-4gb",
        "instances": 4,
        "round_period_secs": 600,
        "download_timeout_secs": 300,
        "round_parity": "even",
        "start_stagger_secs": 2,
    }
    line = _client_line(client)
    assert line.startswith("• hot-file-read — lon1 (s-2vcpu-4gb, 4 readers, ")
    assert "round 600s" in line
    assert "deadline 300s" in line
    assert "parity even" in line
    assert "stagger 2s" in line
    block = json.dumps(_clients_block([client]))
    assert "Hot-file readers (native):" in block


def test_slack_renders_a_zero_stagger_as_zero():
    from saorsa_deploy.cmd.slack_post import _client_line

    line = _client_line(
        {
            "role": ROLE_HOT_FILE_READ,
            "region": "lon1",
            "vm_size": "s",
            "instances": 1,
            "round_period_secs": 600,
            "download_timeout_secs": 300,
            "round_parity": "all",
            "start_stagger_secs": 0,
        }
    )
    assert "stagger 0s" in line


def test_a_deployment_without_readers_renders_exactly_as_before():
    from saorsa_deploy.cmd.slack_post import _clients_block

    clients = [{"role": "download", "region": "lon1", "vm_size": "s-2vcpu-4gb"}]
    block = json.dumps(_clients_block(clients))
    assert "Hot-file readers" not in block
    assert "Native (QUIC)" not in block


# --------------------------------------------------------------------------- #
# the reader service: publishing must not move the schedule
# --------------------------------------------------------------------------- #
class TestPublishingIsOffTheSchedule:
    """Metrics latency must not be able to move a round boundary.

    Every emit used to POST synchronously, so InfluxDB's latency was inside the
    round loop: a 30 s write with a 120 s period and eight readers carried a
    round past the next boundary, the catch-up then wrote eight more points
    sequentially, and the loop fell further behind on every pass — 40 overrun
    points and not one download after the first round.
    """

    def test_a_write_does_not_wait_for_the_post(self, hfr):
        import time as _time

        sink = hfr.InfluxSink("http://influx", "token", "ant", "testnet-metrics")
        posted = []

        def _slow(line):
            _time.sleep(0.4)
            posted.append(line)

        with patch.object(hfr.InfluxSink, "_post", side_effect=_slow):
            started = _time.time()
            for instance in range(8):
                sink.write(f"hot_file_download,host=h instance={instance}i")
            elapsed = _time.time() - started
            # Eight writes against a 0.4 s POST: synchronously that is 3.2 s.
            assert elapsed < 0.2, elapsed
            assert sink.drain(timeout=30)
        assert len(posted) == 8

    def test_a_backlog_drops_rather_than_blocks(self, hfr):
        """The publisher is BLOCKED with a releasable event, never a sleep.

        A `sleep(60)` left a daemon publisher holding a full queue after the
        mock came off, so the leaked worker reached real HTTP and the next
        test's `_post` mock: `test_the_publisher_survives_a_failing_post` saw
        350 calls instead of 2. The blocker is released and the queue drained
        INSIDE the patch, in a `finally` so an assertion failure cleans up too.
        """
        import threading as _threading
        import time as _time

        sink = hfr.InfluxSink("http://influx", "token", "ant", "b")
        release = _threading.Event()

        def _blocked(line):
            # Bounded, so a wedged test cannot hang the suite either.
            release.wait(timeout=30)

        with patch.object(hfr.InfluxSink, "_post", side_effect=_blocked):
            try:
                started = _time.time()
                for _ in range(hfr.INFLUX_QUEUE_SIZE + 50):
                    sink.write("x")
                # A reader that cannot publish drops the point rather than
                # waiting: a missing point is a smaller lie than a round that
                # never ran.
                assert _time.time() - started < 5
                assert sink._dropped >= 40
            finally:
                release.set()
                # Drained while `_post` is still this test's own, so not one
                # queued line can reach the real endpoint or another test.
                assert sink.drain(timeout=30)

    def test_a_disabled_sink_starts_no_thread(self, hfr):
        sink = hfr.InfluxSink("", "", "ant", "b")
        sink.write("x")
        assert sink._worker is None

    def test_the_publisher_survives_a_failing_post(self, hfr):
        calls = []

        def _boom(line):
            calls.append(line)
            raise RuntimeError("influx is down")

        sink = hfr.InfluxSink("http://influx", "token", "ant", "b")
        with patch.object(hfr.InfluxSink, "_post", side_effect=_boom):
            sink.write("a")
            sink.write("b")
            assert sink.drain(timeout=30)
        # Both were attempted: a failed write must not kill the publisher and
        # strand every later point behind it.
        assert len(calls) == 2


class TestCatchUpIsBounded:
    def test_the_catch_up_emits_at_most_the_cap(self, hfr):
        # Emitting a point for every missed round is work proportional to the
        # lateness, which is what let a slow InfluxDB push the loop further
        # behind on every pass.
        # `ROUND_PARITY="all"` so the missed rounds are consecutive; the
        # parity's own effect on the catch-up has a test of its own below.
        cfg = _config(hfr, INSTANCES="2", ROUND_PERIOD_SECS="120", ROUND_PARITY="all")
        cfg.sink = MagicMock(enabled=False)
        emitted = []
        now = 1_000_000 * 120 + 5
        with (
            patch.object(hfr.time, "time", return_value=now),
            patch.object(hfr, "emit", side_effect=lambda c, r, i, idx, f: emitted.append((r, i))),
        ):
            # Twenty rounds behind.
            resumed = hfr._resynchronise(cfg, 999_980)
        rounds = sorted({r for r, _ in emitted})
        assert len(rounds) == hfr.OVERRUN_EMIT_LIMIT
        # The most recent missed rounds are the informative ones.
        assert rounds == [999_999, 1_000_000]
        # Two readers each.
        assert len(emitted) == hfr.OVERRUN_EMIT_LIMIT * 2
        # And the loop returns to the present rather than catching up.
        assert resumed == 1_000_001

    def test_no_missed_rounds_emits_nothing(self, hfr):
        cfg = _config(hfr, ROUND_PERIOD_SECS="120")
        cfg.sink = MagicMock(enabled=False)
        with (
            patch.object(hfr.time, "time", return_value=1_000_000 * 120 - 10),
            patch.object(hfr, "emit", side_effect=AssertionError("emitted")),
        ):
            assert hfr._resynchronise(cfg, 1_000_000) == 1_000_000

    def test_the_parity_still_decides_which_missed_rounds_count(self, hfr):
        cfg = _config(hfr, INSTANCES="1", ROUND_PERIOD_SECS="120", ROUND_PARITY="even")
        cfg.sink = MagicMock(enabled=False)
        emitted = []
        with (
            patch.object(hfr.time, "time", return_value=1_000_000 * 120 + 5),
            patch.object(hfr, "emit", side_effect=lambda c, r, i, idx, f: emitted.append(r)),
        ):
            hfr._resynchronise(cfg, 999_990)
        assert all(r % 2 == 0 for r in emitted), emitted

    def test_downloads_continue_while_the_publisher_is_slow(self, hfr):
        """Driven through the real `main` on a simulated clock.

        The reviewer's shape: period 120, deadline 119, eight readers. Before,
        the loop recorded downloads for round 1 and then only overruns.
        """

        class _Stop(Exception):
            pass

        clock = {"t": 1.0}
        events = []

        def _sleep_until(when):
            if when > clock["t"]:
                clock["t"] = when
            if clock["t"] > 1500:
                raise _Stop()

        def _download(cfg, round_id, instance, entry, start_ns):
            clock["t"] += 1
            return {**hfr._base_fields(start_ns), "status": hfr.STATUS_SUCCESS}

        env = {
            "TESTNET_NAME": "DEV-03",
            "REGION": "lon1",
            "CLIENT_INDEX": "1",
            "INSTANCES": "8",
            "ROUND_PERIOD_SECS": "120",
            "DOWNLOAD_TIMEOUT_SECS": "119",
            "ROUND_PARITY": "all",
            "START_STAGGER_SECS": "0",
            "ANT": "/bin/true",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(hfr.time, "time", side_effect=lambda: clock["t"]),
            patch.object(
                hfr.time, "sleep", side_effect=lambda d: clock.update(t=clock["t"] + max(0, d))
            ),
            patch.object(hfr, "sleep_until", side_effect=_sleep_until),
            patch.object(hfr, "fetch_hot_files", return_value=[{"address": "ab", "bytes": 1}]),
            patch.object(hfr, "run_download", side_effect=_download),
            patch.object(
                hfr, "emit", side_effect=lambda c, r, i, idx, f: events.append((r, f["status"]))
            ),
        ):
            with pytest.raises(_Stop):
                hfr.main()

        ran = sorted({r for r, status in events if status == hfr.STATUS_SUCCESS})
        assert len(ran) >= 10
        assert ran == list(range(ran[0], ran[0] + len(ran)))


# --------------------------------------------------------------------------- #
# the forwarded log: one line per download
# --------------------------------------------------------------------------- #
def test_a_log_record_is_written_whole(hfr, tmp_path):
    """`print()` writes the message and the newline separately.

    So another reader thread could land between them: eight threads over 800
    records produced 106 merged lines and 121 blank ones, including a line
    carrying seven download records. One line per download is a contract both
    the forwarded log and `grep` depend on.
    """
    import io
    import re
    import threading as _threading

    path = tmp_path / "hot-file-reader.stdout.log"
    path.write_text("")
    real = io.StringIO()
    original = hfr.sys.stdout
    hfr.sys.stdout = hfr._ForwardingStream(real, str(path))
    try:

        def _work(reader):
            for i in range(100):
                hfr._log(
                    f"round=3000000 instance={reader} status=success kind=- "
                    f"total_ms=1 records_from_peers=0 bytes_from_peers=0 "
                    f"address=deadbeef{reader}{i:03d}"
                )

        threads = [_threading.Thread(target=_work, args=(n,)) for n in range(1, 9)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        hfr.sys.stdout = original

    lines = path.read_text().splitlines()
    assert len(lines) == 800
    assert not [line for line in lines if not line.strip()]
    assert not [line for line in lines if len(re.findall(r"round=3000000", line)) > 1]
    # Every record recoverable, which is what the contract is for.
    assert len(set(re.findall(r"address=(\S+)", "\n".join(lines)))) == 800


def test_nothing_in_the_script_uses_print(hfr):
    # `_log` and `_warn` assemble the whole record and write it once; a stray
    # `print()` would reintroduce the split write.
    source = SCRIPT.read_text()
    code = [
        line
        for line in source.splitlines()
        if not line.lstrip().startswith("#") and not line.lstrip().startswith("#:")
    ]
    assert not [line for line in code if re.search(r"(?<![\w.])print\(", line)], [
        line for line in code if re.search(r"(?<![\w.])print\(", line)
    ]
