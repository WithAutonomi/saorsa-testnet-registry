"""The shipped browser harness itself (V2-1617).

Mirrors `test_wasm_client_source.py`: run the harness's OWN unit suite rather
than re-implementing its arithmetic in Python. One implementation of the round
maths, the error classifier and the derived fields, exercised in the place it
runs — a second Python copy would be the thing that drifts, and the numbers it
agreed with would be its own rather than the harness's.

The expected values those tests assert against are pinned independently in
`test_browser_downloaders.py::TestTraceFixture`, so the pair cannot quietly
move together.

Skipped when Node is unavailable, so the suite still runs on a machine without
it. The harness needs no `npm install` for these: everything under `lib/` is
dependency-free, and Playwright is only imported by `harness.mjs`.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS_DIR = Path(__file__).resolve().parents[1] / "saorsa_deploy" / "resources" / "browser-client"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def test_the_harness_unit_tests_pass():
    result = subprocess.run(
        # A directory argument would make node treat it as a module path; the
        # glob is what selects the test files.
        ["node", "--test", "test/*.test.mjs"],
        cwd=HARNESS_DIR,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_harness_itself_parses():
    # `harness.mjs` imports Playwright, so it cannot be executed here — but a
    # syntax error in it would otherwise only surface on a VM, after the
    # infrastructure is up and billing.
    result = subprocess.run(
        ["node", "--check", "harness.mjs"],
        cwd=HARNESS_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("module", sorted(p.name for p in (HARNESS_DIR / "lib").glob("*.mjs")))
def test_every_library_module_loads_without_a_dependency(module):
    # The provisioner copies these files one by one, so a module that needs
    # something from node_modules would fail on the VM and nowhere else.
    result = subprocess.run(
        ["node", "-e", f"import('./lib/{module}').then(()=>{{}})"],
        cwd=HARNESS_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
