from saorsa_deploy.provisioning.authorized_keys import (
    build_authorized_keys_command,
    load_operator_authorized_keys,
)

OPERATOR_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFeNb683ohEnVRNO2mDNadW3n7KVhV9/6qbGXkWtRRf6 "
    "studio1@studio1.local"
)


def test_operator_key_is_bundled():
    keys = load_operator_authorized_keys()
    assert OPERATOR_KEY in keys


def test_command_prepares_ssh_dir_and_appends_each_key():
    cmd = build_authorized_keys_command(["key-a", "key-b"])
    assert "install -d -m 700 /root/.ssh" in cmd
    assert "chmod 600 /root/.ssh/authorized_keys" in cmd
    # Each key is appended only when absent (idempotent re-runs).
    assert cmd.count("grep -qxF") == 2
    assert "'key-a'" in cmd
    assert "'key-b'" in cmd


def test_command_guards_against_duplicate_entries():
    cmd = build_authorized_keys_command([OPERATOR_KEY])
    assert (
        f"grep -qxF '{OPERATOR_KEY}' /root/.ssh/authorized_keys "
        f"|| echo '{OPERATOR_KEY}' >> /root/.ssh/authorized_keys"
    ) in cmd
