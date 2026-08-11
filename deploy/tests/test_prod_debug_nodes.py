"""Tests for the prod-debug-nodes kind and the Contabo provider wiring.

These deliberately exercise the pure functions (unit-file builders, provider
tables, orchestrator argv, schema) and avoid the pyinfra ``execute()`` path.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from saorsa_deploy.cmd.infra_nodes import (
    NO_VOLUME_PROVIDERS,
    _build_provider_specs,
    _build_provider_state,
)
from saorsa_deploy.cmd.obs_provider import (
    OBS_PROVIDERS,
    default_elastic_size,
    default_influxdb_size,
    default_region,
    state_key_abbrev,
)
from saorsa_deploy.cmd.prod_debug_nodes import (
    COREDUMP_PRUNE_UNIT,
    DEBUG_LOG_PRUNE_UNIT,
    build_coredump_prune_units,
    build_debug_logprune_units,
    build_prune_install_commands,
)
from saorsa_deploy.config.models import Manifest
from saorsa_deploy.contabo import CONTABO_REGIONS, missing_contabo_env
from saorsa_deploy.orchestrator import build_deploy_steps, build_destroy_steps
from saorsa_deploy.providers import PROVIDERS
from saorsa_deploy.provisioning.node import (
    COREDUMP_DIR,
    INSTALL_DEBUG_TOOLS_COMMAND,
    LOG_DIR,
    _build_node_unit_file,
    build_coredump_setup_command,
)
from saorsa_deploy.provisioning.telegraf import TELEGRAF_SERVICE_NAME

# --------------------------------------------------------------------------- #
# node unit file: debug knobs are additive and default-off
# --------------------------------------------------------------------------- #
_EXEC = "/usr/local/bin/ant-node --root-dir /d --port 10000"


class TestDebugUnitFile:
    def test_defaults_have_no_debug_lines(self):
        unit = _build_node_unit_file("ant-node-1", _EXEC)
        assert "Environment=" not in unit
        assert "COLORBT_SHOW_HIDDEN" not in unit
        assert "LimitCORE" not in unit

    def test_prod_style_unit_unchanged_by_new_params(self):
        # The migration / prod-nodes unit (always/10/65536) must be byte-identical
        # whether or not the new debug params exist.
        a = _build_node_unit_file(
            "ant-node-1", _EXEC, restart_policy="always", restart_sec=10,
            limit_nofile=65536,
        )
        b = _build_node_unit_file(
            "ant-node-1", _EXEC, restart_policy="always", restart_sec=10,
            limit_nofile=65536, rust_backtrace=None, coredumps=False,
            colorbt_show_hidden=False,
        )
        assert a == b
        assert "Environment=" not in a

    def test_rust_backtrace_adds_environment_line(self):
        unit = _build_node_unit_file("ant-node-1", _EXEC, rust_backtrace="1")
        assert "Environment=RUST_BACKTRACE=1\n" in unit

    def test_rust_backtrace_full(self):
        unit = _build_node_unit_file("ant-node-1", _EXEC, rust_backtrace="full")
        assert "Environment=RUST_BACKTRACE=full\n" in unit

    def test_coredumps_adds_limitcore(self):
        unit = _build_node_unit_file("ant-node-1", _EXEC, coredumps=True)
        assert "LimitCORE=infinity\n" in unit

    def test_colorbt_show_hidden_adds_environment_line(self):
        # ant-node's color-backtrace filters every frame (<empty backtrace>)
        # unless COLORBT_SHOW_HIDDEN=1 is set on the service env.
        unit = _build_node_unit_file(
            "ant-node-1", _EXEC, colorbt_show_hidden=True
        )
        assert "Environment=COLORBT_SHOW_HIDDEN=1\n" in unit

    def test_debug_env_present_on_nat_variant(self):
        # The RUST_BACKTRACE / COLORBT_SHOW_HIDDEN envs must be systemd
        # Environment= lines (so they survive the ip netns exec wrapper), not
        # inlined on ExecStart.
        unit = _build_node_unit_file(
            "ant-node-1", _EXEC, is_nat=True, rust_backtrace="full",
            coredumps=True, colorbt_show_hidden=True,
        )
        assert "Environment=RUST_BACKTRACE=full\n" in unit
        assert "Environment=COLORBT_SHOW_HIDDEN=1\n" in unit
        assert "ip netns exec" in unit
        assert "LimitCORE=infinity\n" in unit


def test_coredump_setup_command_writes_absolute_pattern():
    cmd = build_coredump_setup_command()
    assert COREDUMP_DIR == "/mnt/ant-node-storage/coredumps"
    assert f"kernel.core_pattern={COREDUMP_DIR}/core.%e.%p.%t" in cmd
    assert f"mkdir -p {COREDUMP_DIR}" in cmd
    assert "/etc/sysctl.d/99-ant-coredump.conf" in cmd


def test_install_debug_tools_command_is_idempotent():
    # gdb + eu-stack (elfutils) to read the file cores in place; idempotent via a
    # command -v gate, mirroring INSTALL_HEAPTRACK_COMMAND. systemd-coredump is
    # deliberately NOT installed.
    cmd = INSTALL_DEBUG_TOOLS_COMMAND
    assert "command -v gdb" in cmd
    assert "command -v eu-stack" in cmd
    assert "apt-get install -y -qq gdb elfutils" in cmd
    assert "DEBUG_TOOLS:SKIP" in cmd and "DEBUG_TOOLS:INSTALLED" in cmd
    assert "systemd-coredump" not in cmd


# --------------------------------------------------------------------------- #
# Contabo provider wiring
# --------------------------------------------------------------------------- #
class TestContaboProvider:
    def test_contabo_registered_as_node_provider(self):
        assert "contabo" in PROVIDERS
        cfg = PROVIDERS["contabo"]
        assert cfg.tf_dir == "contabo"
        assert cfg.state_key_prefix == "saorsa-deploy/contabo"
        assert "EU" in cfg.regions

    def test_contabo_is_volumeless(self):
        assert "contabo" in NO_VOLUME_PROVIDERS
        assert "ovh_3az" in NO_VOLUME_PROVIDERS

    def test_contabo_regions_match_helper(self):
        assert set(PROVIDERS["contabo"].regions) == set(CONTABO_REGIONS)

    def test_contabo_in_obs_providers(self):
        assert "contabo" in OBS_PROVIDERS
        assert state_key_abbrev("contabo") == "contabo"
        assert default_region("contabo") == "EU"
        # product ids, not slugs
        assert default_influxdb_size("contabo").startswith("V")
        assert default_elastic_size("contabo").startswith("V")

    def test_missing_contabo_env_reports_all_when_unset(self, monkeypatch):
        for v in (
            "CNTB_OAUTH2_CLIENT_ID", "CNTB_OAUTH2_CLIENT_SECRET",
            "CNTB_OAUTH2_USER", "CNTB_OAUTH2_PASS",
        ):
            monkeypatch.delenv(v, raising=False)
        assert set(missing_contabo_env()) == {
            "CNTB_OAUTH2_CLIENT_ID", "CNTB_OAUTH2_CLIENT_SECRET",
            "CNTB_OAUTH2_USER", "CNTB_OAUTH2_PASS",
        }

    def test_missing_contabo_env_empty_when_all_set(self, monkeypatch):
        for v in (
            "CNTB_OAUTH2_CLIENT_ID", "CNTB_OAUTH2_CLIENT_SECRET",
            "CNTB_OAUTH2_USER", "CNTB_OAUTH2_PASS",
        ):
            monkeypatch.setenv(v, "x")
        assert missing_contabo_env() == []


def _infra_args(**overrides):
    base = dict(
        do_vm_count=0, hetzner_vm_count=0, vultr_vm_count=0,
        ovh_vm_count=0, ovh_3az_vm_count=0, contabo_vm_count=0,
        do_regions=1, hetzner_regions=1, vultr_regions=1,
        do_regions_list=None, hetzner_regions_list=None, vultr_regions_list=None,
        ovh_regions=1, ovh_regions_list=None,
        ovh_3az_regions=1, ovh_3az_regions_list=None,
        contabo_regions=1, contabo_regions_list=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestContaboNodeInfra:
    def test_build_provider_specs_picks_up_contabo(self):
        specs = _build_provider_specs(
            _infra_args(contabo_vm_count=1, contabo_regions_list="EU")
        )
        assert [s.name for s in specs] == ["contabo"]
        assert specs[0].regions_override == ["EU"]

    def test_build_provider_state_records_product_id(self):
        specs = _build_provider_specs(_infra_args(contabo_vm_count=2))
        state = _build_provider_state(
            specs, "s-2vcpu-4gb", "cpx22", "vc2-2c-4gb", "b3-16",
            contabo_product_id="V45",
        )
        assert state["contabo"] == {
            "vm_count": 2, "regions": 1, "product_id": "V45"
        }


# --------------------------------------------------------------------------- #
# orchestrator: prod-debug-nodes manifest -> argv
# --------------------------------------------------------------------------- #
def _debug_manifest(**spec):
    body = {
        "binary": {"branch": "main", "repo_owner": "WithAutonomi", "debug": True},
        "region": "EU",
    }
    body.update(spec)
    return Manifest.model_validate(
        {
            "name": "DEBUG-01",
            "kind": "prod-debug-nodes",
            "purpose": "crash debug",
            "ssh_key_path": "/ssh/key",
            "prod_debug_nodes": body,
        }
    )


def _argv(steps):
    return [s.argv for s in steps]


class TestDebugNodesOrchestrator:
    def test_minimal_manifest_builds_ant_node_then_deploy(self):
        steps = build_deploy_steps(_debug_manifest())
        assert len(steps) == 2
        assert steps[0].argv[:3] == ["build", "ant-node", "--name"]
        assert steps[0].argv[:2] == ["build", "ant-node"]  # NOT `build ant`
        deploy = steps[1].argv
        assert deploy[:3] == ["prod", "debug-nodes", "deploy"]
        assert "--name" in deploy and "DEBUG-01" in deploy
        assert "--region" in deploy and "EU" in deploy
        # informational debug flag from the debug binary
        assert "--debug" in deploy
        assert deploy[-2:] == ["--ssh-key-path", "/ssh/key"]

    def test_optional_fields_omitted_when_unset(self):
        deploy = build_deploy_steps(_debug_manifest())[1].argv
        for flag in (
            "--provider", "--vm-size", "--coredumps", "--monitoring",
            "--forward-logs", "--contabo-image-id", "--log-retention-days",
        ):
            assert flag not in deploy

    def test_contabo_and_debug_knobs_emitted(self):
        deploy = build_deploy_steps(
            _debug_manifest(
                provider="contabo",
                vm_size="V10",
                coredumps=True,
                coredump_retention_days=7,
                log_retention_days=1,
                rust_backtrace="full",
                monitoring=True,
                monitoring_provider="contabo",
                forward_logs=True,
                elastic_provider="contabo",
                elastic_retention_days=3,
                contabo_image_id="uuid-123",
                contabo_ssh_public_key_path="/ssh/key.pub",
            )
        )[1].argv
        assert ["--provider", "contabo"] == _pair(deploy, "--provider")
        assert ["--rust-backtrace", "full"] == _pair(deploy, "--rust-backtrace")
        assert "--coredumps" in deploy
        assert ["--coredump-retention-days", "7"] == _pair(
            deploy, "--coredump-retention-days"
        )
        assert ["--log-retention-days", "1"] == _pair(
            deploy, "--log-retention-days"
        )
        assert ["--monitoring-provider", "contabo"] == _pair(
            deploy, "--monitoring-provider"
        )
        assert ["--elastic-provider", "contabo"] == _pair(deploy, "--elastic-provider")
        assert ["--elastic-retention-days", "3"] == _pair(
            deploy, "--elastic-retention-days"
        )
        assert ["--contabo-image-id", "uuid-123"] == _pair(deploy, "--contabo-image-id")

    def test_bool_false_emits_no_flag(self):
        deploy = build_deploy_steps(
            _debug_manifest(coredumps=False, monitoring=False, forward_logs=False)
        )[1].argv
        assert "--no-coredumps" in deploy
        assert "--no-monitoring" in deploy
        assert "--no-forward-logs" in deploy

    def test_destroy_steps_and_keep_flags(self):
        steps = build_destroy_steps(
            _debug_manifest(keep_monitoring=True, keep_elastic=True)
        )
        assert len(steps) == 1
        argv = steps[0].argv
        assert argv[:3] == ["prod", "debug-nodes", "destroy"]
        assert "--keep-monitoring" in argv
        assert "--keep-elastic" in argv


def _pair(argv, flag):
    i = argv.index(flag)
    return [argv[i], argv[i + 1]]


# --------------------------------------------------------------------------- #
# schema: kind/block consistency
# --------------------------------------------------------------------------- #
class TestDebugNodesSchema:
    def test_requires_matching_block(self):
        with pytest.raises(ValidationError):
            Manifest.model_validate(
                {
                    "name": "X",
                    "kind": "prod-debug-nodes",
                    "purpose": "p",
                    "prod_nodes": {  # wrong block for this kind
                        "binary": {"version": "0.11.6"},
                        "region": "EU",
                    },
                }
            )

    def test_valid_manifest_round_trips(self):
        m = _debug_manifest(log_level="debug")
        assert m.kind == "prod-debug-nodes"
        assert m.prod_debug_nodes.region == "EU"
        assert m.spec is m.prod_debug_nodes

    def test_log_level_enum_enforced(self):
        with pytest.raises(ValidationError):
            _debug_manifest(log_level="verbose")


# --------------------------------------------------------------------------- #
# prune timers: the debug-log prune MUST restart the ES log forwarder
# --------------------------------------------------------------------------- #
class TestDebugLogPruneUnits:
    """A plain ``find -delete`` reclaims nothing while telegraf holds the files.

    Telegraf's ``tail`` input keeps an open descriptor for every file it
    matches and has no idle-close, so unlinking only removes the directory
    entry: the file leaves ``du`` while ext4 keeps the blocks. Measured on
    ant-prod-debug-01 (2026-08-02): 694 GiB across 70 descriptors on a 968 GB
    root disk reporting 0 bytes free with 271 GB visible. Restarting the
    forwarder released all of it.
    """

    def test_prune_deletes_then_restarts_the_forwarder(self):
        service, _ = build_debug_logprune_units(2)
        lines = [ln for ln in service.splitlines() if ln.startswith("ExecStart=")]
        assert len(lines) == 2
        assert lines[0] == (
            f"ExecStart=/usr/bin/find {LOG_DIR} -type f -mtime +2 -delete"
        )
        assert lines[1] == (
            "ExecStart=-/usr/bin/systemctl try-restart "
            f"{TELEGRAF_SERVICE_NAME}.service"
        )

    def test_restart_is_ordered_after_the_delete(self):
        # The unlink has to land first; the restart is what frees the blocks.
        service, _ = build_debug_logprune_units(1)
        assert service.index("/usr/bin/find") < service.index("try-restart")

    def test_restart_failure_cannot_fail_the_prune(self):
        # The `-` prefix: by the time the restart runs the prune has already
        # done its job, so a forwarder that won't come back must not mark the
        # unit failed (which would also stop `Persistent=true` catch-up runs).
        service, _ = build_debug_logprune_units(1)
        assert "ExecStart=-/usr/bin/systemctl" in service

    def test_uses_try_restart_so_it_is_a_no_op_when_absent(self):
        # The kind supports --no-forward-logs, in which case there is no
        # forwarder installed at all. `try-restart` does nothing rather than
        # starting one.
        service, _ = build_debug_logprune_units(1)
        assert "try-restart" in service
        assert "systemctl restart" not in service
        assert "systemctl start" not in service

    def test_retention_days_is_interpolated(self):
        for days in (1, 2, 7):
            service, _ = build_debug_logprune_units(days)
            assert f"-mtime +{days} -delete" in service

    def test_timer_is_daily_and_persistent(self):
        _, timer = build_debug_logprune_units(1)
        assert "OnCalendar=daily" in timer
        assert "Persistent=true" in timer
        assert "WantedBy=timers.target" in timer


class TestCoredumpPruneUnits:
    def test_coredump_prune_does_not_restart_the_forwarder(self):
        """Core dumps are not tailed, so nothing holds them open.

        Restarting the forwarder here would drop buffered log lines for no
        reclaim at all.
        """
        service, _ = build_coredump_prune_units(7)
        lines = [ln for ln in service.splitlines() if ln.startswith("ExecStart=")]
        assert len(lines) == 1
        assert "systemctl" not in service
        assert TELEGRAF_SERVICE_NAME not in service

    def test_coredump_prune_targets_only_core_files(self):
        service, _ = build_coredump_prune_units(7)
        assert f"/usr/bin/find {COREDUMP_DIR} -type f -name 'core.*'" in service
        assert "-mtime +7 -delete" in service


class TestPruneInstallCommands:
    def test_writes_units_reloads_and_enables(self):
        service, timer = build_debug_logprune_units(2)
        cmds = build_prune_install_commands(
            DEBUG_LOG_PRUNE_UNIT, service, timer, run_now=True
        )
        assert f"/etc/systemd/system/{DEBUG_LOG_PRUNE_UNIT}.service" in cmds[0]
        assert f"/etc/systemd/system/{DEBUG_LOG_PRUNE_UNIT}.timer" in cmds[1]
        assert cmds[2] == "systemctl daemon-reload"
        assert cmds[3] == f"systemctl enable --now {DEBUG_LOG_PRUNE_UNIT}.timer"

    def test_run_now_fires_the_prune_immediately(self):
        # A disk that is already full should get relief without waiting for
        # the next scheduled run.
        service, timer = build_debug_logprune_units(2)
        with_now = build_prune_install_commands(
            DEBUG_LOG_PRUNE_UNIT, service, timer, run_now=True
        )
        without = build_prune_install_commands(
            DEBUG_LOG_PRUNE_UNIT, service, timer
        )
        assert with_now[-1] == f"systemctl start {DEBUG_LOG_PRUNE_UNIT}.service"
        assert without == with_now[:-1]

    def test_coredump_prune_does_not_run_immediately(self):
        service, timer = build_coredump_prune_units(7)
        cmds = build_prune_install_commands(
            COREDUMP_PRUNE_UNIT, service, timer
        )
        assert not any(c.startswith("systemctl start") for c in cmds)
