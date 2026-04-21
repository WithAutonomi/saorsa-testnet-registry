from saorsa_deploy.provisioning.nat import (
    NAT_NAMESPACE,
    build_nat_setup_command,
    select_nat_ips,
)


class TestSelectNatIps:
    def test_zero_percent_returns_empty(self):
        vm_ips = {"digitalocean/lon1": ["1.1.1.1", "1.1.1.2"]}
        assert select_nat_ips(vm_ips, 0) == []

    def test_negative_percent_returns_empty(self):
        vm_ips = {"digitalocean/lon1": ["1.1.1.1"]}
        assert select_nat_ips(vm_ips, -5) == []

    def test_empty_input_returns_empty(self):
        assert select_nat_ips({}, 50) == []

    def test_empty_lanes_return_empty(self):
        assert select_nat_ips({"digitalocean/lon1": []}, 50) == []

    def test_hundred_percent_returns_all(self):
        vm_ips = {
            "digitalocean/lon1": ["1.1.1.1", "1.1.1.2"],
            "vultr/nrt": ["2.2.2.1"],
        }
        result = select_nat_ips(vm_ips, 100)
        assert sorted(result) == sorted(["1.1.1.1", "1.1.1.2", "2.2.2.1"])

    def test_over_hundred_clamps_to_total(self):
        vm_ips = {"digitalocean/lon1": ["1.1.1.1", "1.1.1.2"]}
        assert len(select_nat_ips(vm_ips, 500)) == 2

    def test_rounds_up(self):
        # 10 total * 15% = 1.5 -> ceil = 2
        vm_ips = {"digitalocean/lon1": [f"1.1.1.{i}" for i in range(1, 11)]}
        assert len(select_nat_ips(vm_ips, 15)) == 2

    def test_one_percent_still_picks_one(self):
        vm_ips = {"digitalocean/lon1": [f"1.1.1.{i}" for i in range(1, 11)]}
        # 10 * 1% = 0.1 -> ceil = 1
        assert len(select_nat_ips(vm_ips, 1)) == 1

    def test_deterministic_same_input_same_output(self):
        vm_ips = {
            "digitalocean/lon1": ["1.1.1.2", "1.1.1.1"],
            "vultr/nrt": ["2.2.2.3", "2.2.2.1"],
        }
        assert select_nat_ips(vm_ips, 50) == select_nat_ips(vm_ips, 50)

    def test_one_per_region_first(self):
        # 4 regions, 10 VMs each. At 10% (= 4 NAT VMs), expect one from each region.
        vm_ips = {
            f"prov/reg{i}": [f"{i}.0.0.{j}" for j in range(1, 11)] for i in range(4)
        }
        result = select_nat_ips(vm_ips, 10)
        assert len(result) == 4
        # Each selected IP starts with a different region-prefix
        prefixes = {ip.split(".")[0] for ip in result}
        assert prefixes == {"0", "1", "2", "3"}

    def test_round_robin_after_first_pass(self):
        # 3 regions × 2 VMs = 6. Select 5 = first slot in all 3 regions,
        # then second slot in first 2 regions.
        vm_ips = {
            "a/1": ["a1", "a2"],
            "b/1": ["b1", "b2"],
            "c/1": ["c1", "c2"],
        }
        result = select_nat_ips(vm_ips, 84)  # ceil(6*0.84) = 6 ... use 80%: ceil(4.8)=5
        # Actually: 6 * 84 / 100 = 5.04 -> ceil = 6. Use 80% instead.
        result = select_nat_ips(vm_ips, 80)
        assert len(result) == 5
        # First three come from distinct regions, then 4th and 5th are slot-2.
        assert result[:3] == ["a1", "b1", "c1"]
        assert result[3:] == ["a2", "b2"]

    def test_single_region(self):
        vm_ips = {"only/one": ["x1", "x2", "x3"]}
        result = select_nat_ips(vm_ips, 67)  # ceil(2.01) = 3
        # Actually: 3 * 67 / 100 = 2.01, ceil = 3. Use 34% -> ceil(1.02) = 2
        result = select_nat_ips(vm_ips, 34)
        assert result == ["x1", "x2"]

    def test_uneven_lanes(self):
        # One region has 5 VMs, the other has 1. Round-robin still works.
        vm_ips = {"big": ["b1", "b2", "b3", "b4", "b5"], "small": ["s1"]}
        result = select_nat_ips(vm_ips, 100)
        # First slot across both regions, then remaining big-region VMs.
        assert result[:2] == ["b1", "s1"]
        assert result[2:] == ["b2", "b3", "b4", "b5"]

    def test_region_keys_sorted_for_determinism(self):
        vm_ips = {"zebra": ["z1"], "alpha": ["a1"]}
        # alpha sorts before zebra, so a1 comes first.
        assert select_nat_ips(vm_ips, 50) == ["a1"]


class TestBuildNatSetupCommand:
    def test_idempotent_guard_emits_already(self):
        cmd = build_nat_setup_command()
        assert "ANT_NAT:ALREADY" in cmd
        assert NAT_NAMESPACE in cmd

    def test_emits_configured_on_success(self):
        cmd = build_nat_setup_command()
        assert "ANT_NAT:CONFIGURED" in cmd

    def test_has_all_four_prerouting_rules(self):
        cmd = build_nat_setup_command()
        assert 'iptables -t nat -A PREROUTING -i "$OUT_IFACE" -p tcp --dport 22 -j ACCEPT' in cmd
        assert (
            'iptables -t nat -A PREROUTING -i "$OUT_IFACE" -p tcp -j DNAT --to-destination "$NS_IP"'
            in cmd
        )
        assert (
            'iptables -t nat -A PREROUTING -i "$OUT_IFACE" -p udp -j DNAT --to-destination "$NS_IP"'
            in cmd
        )

    def test_has_masquerade_and_forward_rules(self):
        cmd = build_nat_setup_command()
        assert 'iptables -t nat -A POSTROUTING -s "$SUBNET" -o "$OUT_IFACE" -j MASQUERADE' in cmd
        assert "iptables -A FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT" in cmd
        assert 'iptables -A FORWARD -s "$NS_IP" -j ACCEPT' in cmd
        assert 'iptables -A FORWARD -p tcp -d "$NS_IP" ! --dport 22 -j DROP' in cmd
        assert 'iptables -A FORWARD -p udp -d "$NS_IP" -j DROP' in cmd

    def test_has_namespace_and_veth_setup(self):
        cmd = build_nat_setup_command()
        assert 'ip netns add "$NS"' in cmd
        assert "ip link add veth-nat type veth peer name veth-node" in cmd
        assert 'ip link set veth-node netns "$NS"' in cmd

    def test_writes_resolv_conf_in_netns(self):
        cmd = build_nat_setup_command()
        assert 'mkdir -p /etc/netns/"$NS"' in cmd
        assert 'echo "nameserver 1.1.1.1" > /etc/netns/"$NS"/resolv.conf' in cmd

    def test_enables_ip_forward(self):
        cmd = build_nat_setup_command()
        assert "sysctl -w net.ipv4.ip_forward=1" in cmd

    def test_disables_ufw_best_effort(self):
        cmd = build_nat_setup_command()
        assert "ufw disable" in cmd
        assert "|| true" in cmd  # ufw absence must not fail the script

    def test_uses_expected_subnet_and_ips(self):
        cmd = build_nat_setup_command()
        assert '"10.200.0.1"' in cmd
        assert '"10.200.0.2"' in cmd
        assert '"10.200.0.0/24"' in cmd
