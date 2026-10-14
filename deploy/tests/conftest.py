"""Shared fixtures for the `saorsa-deploy` test suite.

One autouse fixture, and it is a safety belt rather than a convenience: the
local inventory root is redirected into a tmp dir for every test. The real
`~/.autonomi/testnets/<name>/inventory.json` files describe the operator's own
deployments, and the destroy commands now RENAME them
(`local_inventory.retire_local_inventory`) — so a test that asserts a teardown
retires a snapshot would retire a real one as a side effect if it reached the
real root. Deployment names are reused, so the test names collide with the real
directories by construction.
"""

import pytest

from saorsa_deploy import local_inventory


@pytest.fixture(autouse=True)
def local_inventory_root(tmp_path, monkeypatch):
    """Point `~/.autonomi/testnets` at a per-test tmp dir. Returns its root."""
    root = tmp_path / "autonomi-testnets"
    monkeypatch.setattr(
        local_inventory,
        "DEFAULT_INVENTORY_TEMPLATE",
        str(root / "{name}" / local_inventory.INVENTORY_FILENAME),
    )
    return root
