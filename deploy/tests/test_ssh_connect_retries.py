"""The package raises pyinfra's default SSH connect-retry count so transient
banner / reset errors during provisioning are retried automatically."""

from pyinfra.connectors.ssh import connector_data_meta

from saorsa_deploy.ssh import (
    SSH_CONNECT_RETRIES,
    SSH_CONNECT_RETRY_MAX_DELAY,
    SSH_CONNECT_RETRY_MIN_DELAY,
    configure_ssh_connect_retries,
)


def _defaults() -> tuple:
    return (
        connector_data_meta["ssh_connect_retries"].default,
        connector_data_meta["ssh_connect_retry_min_delay"].default,
        connector_data_meta["ssh_connect_retry_max_delay"].default,
    )


def test_import_applied_retry_defaults():
    # Importing saorsa_deploy (done transitively above) runs
    # configure_ssh_connect_retries(), so the connector default is no longer 0.
    import saorsa_deploy  # noqa: F401

    assert connector_data_meta["ssh_connect_retries"].default == SSH_CONNECT_RETRIES
    assert SSH_CONNECT_RETRIES > 0


def test_configure_sets_all_three_and_reports_applied():
    original = _defaults()
    try:
        assert configure_ssh_connect_retries(7, 1.5, 4.5) is True
        assert _defaults() == (7, 1.5, 4.5)
    finally:
        # Restore the package defaults so global state doesn't leak to other
        # tests that may assert on the connector defaults.
        configure_ssh_connect_retries(
            SSH_CONNECT_RETRIES,
            SSH_CONNECT_RETRY_MIN_DELAY,
            SSH_CONNECT_RETRY_MAX_DELAY,
        )
        assert _defaults() == original


def test_connector_reads_default_but_per_host_data_wins():
    from pyinfra.api import Config, Inventory, State
    from pyinfra.connectors.ssh import SSHConnector

    configure_ssh_connect_retries(SSH_CONNECT_RETRIES)

    inv = Inventory(([("1.2.3.4", {"ssh_user": "root", "ssh_key": "k"})], {}))
    state = State(inventory=inv, config=Config())
    host = list(inv)[0]
    host.init(state)
    assert SSHConnector(state, host).data["ssh_connect_retries"] == SSH_CONNECT_RETRIES

    # An explicit per-host override still takes precedence over the default.
    inv2 = Inventory(
        ([("1.2.3.4", {"ssh_user": "root", "ssh_key": "k", "ssh_connect_retries": 0})], {})
    )
    state2 = State(inventory=inv2, config=Config())
    host2 = list(inv2)[0]
    host2.init(state2)
    assert SSHConnector(state2, host2).data["ssh_connect_retries"] == 0
