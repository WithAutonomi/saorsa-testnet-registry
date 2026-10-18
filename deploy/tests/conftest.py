"""Shared fixtures for the `saorsa-deploy` test suite.

Three autouse fixtures, and all three are safety belts rather than conveniences.

`SAORSA_DEPLOY_STATE_DIR` is pointed at a tmp dir for every test, so nothing can
write a Terraform workspace or a lane log into the operator's real
`~/.local/share/saorsa-deploy`. Deployment names are reused between the tests
and the operator's own runs, so the paths collide by construction — exactly as
they do for the local inventory below.

The local inventory root is redirected into a tmp dir for every test. The real
`~/.autonomi/testnets/<name>/inventory.json` files describe the operator's own
deployments, and the destroy commands now RENAME them
(`local_inventory.retire_local_inventory`) — so a test that asserts a teardown
retires a snapshot would retire a real one as a side effect if it reached the
real root. Deployment names are reused, so the test names collide with the real
directories by construction.

The client-extension scan is stubbed to "no extensions" for every test. See
:func:`no_client_extensions` — a test must not make that call for real, and
until it was stubbed the only thing stopping it was an `except Exception` in the
caller, which is not a test guarantee at all.
"""

import pytest

from saorsa_deploy import local_inventory, paths, state


@pytest.fixture(autouse=True)
def state_dir_root(tmp_path, monkeypatch):
    """Point the tool's state dir at a per-test tmp dir. Returns its root."""
    root = tmp_path / "saorsa-deploy-state"
    monkeypatch.setenv(paths.STATE_DIR_ENV_VAR, str(root))
    return root


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


@pytest.fixture(autouse=True)
def no_client_extensions(monkeypatch):
    """Answer the client-extension scan with "none" instead of calling S3.

    `clients destroy`, `nodes destroy`, `bootstraps destroy` and `deployments
    down` all consult `list_client_extensions` before they touch anything: an
    extension standing against the deployment means the teardown must refuse,
    because its VMs run against the bootstraps, InfluxDB and address pool that
    teardown would remove. The check therefore sits at the TOP of each command,
    which puts it in the path of every existing test of those commands — none of
    which has any reason to know about it.

    Left unstubbed it is a real `ListObjectsV2` against the production state
    bucket, keyed on names those tests reuse (`DEV-01`, `DEV-02`). On a machine
    with credentials it passes only because no extension happens to exist under
    those names, and on a runner without them `botocore` raises
    `NoCredentialsError`, which the guard correctly turns into exit 2 — 26 tests
    failed that way on CI, and they were "passing" locally for a reason that had
    nothing to do with what they assert.

    So the default is the deterministic one, and a test that wants to exercise
    the guard sets its own value over this (the test's `monkeypatch` is the same
    instance, so a later `setattr` simply wins and is undone with it).
    """
    monkeypatch.setattr(state, "list_client_extensions", lambda name: [])
