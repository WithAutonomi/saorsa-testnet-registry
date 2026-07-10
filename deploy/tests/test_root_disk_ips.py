from saorsa_deploy.cmd.provision_nodes import _resolve_root_disk_ips

# vm_ips is keyed "provider/region" — the format that broke the first attempt.
VM_IPS = {
    "digitalocean/blr1": ["1.1.1.1", "1.1.1.2"],
    "vultr/ord": ["2.2.2.1"],
    "ovh/RBX-A": ["3.3.3.1"],
    "ovh_3az/EU-WEST-PAR": ["4.4.4.1", "4.4.4.2"],
    "ovh_3az/EU-SOUTH-MIL": ["5.5.5.1"],
}
ALL = [ip for ips in VM_IPS.values() for ip in ips]


def test_flags_only_ovh_3az_hosts():
    assert _resolve_root_disk_ips(VM_IPS, ALL) == {"4.4.4.1", "4.4.4.2", "5.5.5.1"}


def test_volume_backed_providers_excluded():
    rd = _resolve_root_disk_ips(VM_IPS, ALL)
    for ip in ["1.1.1.1", "2.2.2.1", "3.3.3.1"]:  # do / vultr / ovh-1az
        assert ip not in rd


def test_single_region_intersection():
    # Provisioning just one 3-AZ region: only its IPs are flagged.
    only = ["4.4.4.1", "4.4.4.2"]
    assert _resolve_root_disk_ips(VM_IPS, only) == {"4.4.4.1", "4.4.4.2"}


def test_no_ovh_3az_returns_empty():
    vm = {"digitalocean/blr1": ["1.1.1.1"], "ovh/RBX-A": ["3.3.3.1"]}
    assert _resolve_root_disk_ips(vm, ["1.1.1.1", "3.3.3.1"]) == set()
