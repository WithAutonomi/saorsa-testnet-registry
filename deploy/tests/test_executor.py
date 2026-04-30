"""Regression tests for ``execute_terraform_runs``.

The pool runs configs in parallel and futures complete out of order. Several
callers (notably ``cmd_infra_clients``) ``zip(configs, results)`` to attach
per-VM outputs back to the originating spec. If the result order didn't match
submission order, same-region clients would get each other's IPs — exactly
the symptom that motivated this regression test.
"""
from pathlib import Path
from unittest.mock import patch

from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.terraform import TerraformResult, TerraformRunConfig


def _config(region: str, idx: int) -> TerraformRunConfig:
    return TerraformRunConfig(
        provider="digitalocean",
        region=region,
        tf_source_dir=Path("/dev/null"),
        workspace_dir=Path(f"/tmp/ws-{region}-{idx}"),
        state_key=f"key-{region}-{idx}",
        variables={"client_index": str(idx)},
    )


def test_results_preserve_submission_order_when_futures_complete_out_of_order():
    configs = [_config("lon1", i) for i in range(1, 5)]

    # Map each config to a tagged result so we can verify which result lands
    # at which output index. Bake an artificial completion delay into the run
    # function — earlier-submitted configs finish *later* — to guarantee the
    # naive append-on-completion implementation would shuffle them.
    import time

    delays = {1: 0.20, 2: 0.05, 3: 0.15, 4: 0.0}

    def fake_run(config: TerraformRunConfig) -> TerraformResult:
        idx = int(config.variables["client_index"])
        time.sleep(delays[idx])
        return TerraformResult(
            success=True,
            provider=config.provider,
            region=config.region,
            outputs={"vm_ip": f"10.0.0.{idx}"},
        )

    with patch("saorsa_deploy.executor.run_terraform", side_effect=fake_run):
        results = execute_terraform_runs(configs)

    assert [r.outputs["vm_ip"] for r in results] == [
        "10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4",
    ]
