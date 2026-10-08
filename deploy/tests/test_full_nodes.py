from saorsa_deploy.provisioning.full_nodes import (
    DEFAULT_FULL_HEADROOM_MB,
    FULL_FILLER_PATH,
    build_fill_storage_command,
    select_full_ips,
)


class TestSelectFullIps:
    def test_zero_percent_returns_empty(self):
        vm_ips = {"digitalocean/lon1": ["1.1.1.1", "1.1.1.2"]}
        assert select_full_ips(vm_ips, 0) == []

    def test_negative_percent_returns_empty(self):
        vm_ips = {"digitalocean/lon1": ["1.1.1.1"]}
        assert select_full_ips(vm_ips, -5) == []

    def test_empty_input_returns_empty(self):
        assert select_full_ips({}, 50) == []

    def test_empty_lanes_return_empty(self):
        assert select_full_ips({"digitalocean/lon1": []}, 50) == []

    def test_hundred_percent_returns_all(self):
        vm_ips = {
            "digitalocean/lon1": ["1.1.1.1", "1.1.1.2"],
            "vultr/nrt": ["2.2.2.1"],
        }
        result = select_full_ips(vm_ips, 100)
        assert sorted(result) == sorted(["1.1.1.1", "1.1.1.2", "2.2.2.1"])

    def test_over_hundred_clamps_to_total(self):
        vm_ips = {"digitalocean/lon1": ["1.1.1.1", "1.1.1.2"]}
        assert len(select_full_ips(vm_ips, 500)) == 2

    def test_rounds_up(self):
        # 10 total * 15% = 1.5 -> ceil = 2
        vm_ips = {"digitalocean/lon1": [f"1.1.1.{i}" for i in range(1, 11)]}
        assert len(select_full_ips(vm_ips, 15)) == 2

    def test_one_percent_still_picks_one(self):
        vm_ips = {"digitalocean/lon1": [f"1.1.1.{i}" for i in range(1, 11)]}
        assert len(select_full_ips(vm_ips, 1)) == 1

    def test_same_seed_same_output(self):
        vm_ips = {
            "digitalocean/lon1": ["1.1.1.2", "1.1.1.1"],
            "vultr/nrt": ["2.2.2.3", "2.2.2.1"],
        }
        assert select_full_ips(vm_ips, 50, seed="DEV-01") == select_full_ips(
            vm_ips, 50, seed="DEV-01"
        )

    def test_seed_independent_of_input_dict_ordering(self):
        a = {"r1": ["1.1.1.1", "1.1.1.2"], "r2": ["2.2.2.1"]}
        b = {"r2": ["2.2.2.1"], "r1": ["1.1.1.2", "1.1.1.1"]}
        assert select_full_ips(a, 67, seed="X") == select_full_ips(b, 67, seed="X")

    def test_different_seeds_can_differ(self):
        # 100 VMs, pick 50 — overwhelmingly likely two seeds disagree on the set.
        vm_ips = {"p/r": [f"10.0.0.{i}" for i in range(100)]}
        s1 = set(select_full_ips(vm_ips, 50, seed="seed-a"))
        s2 = set(select_full_ips(vm_ips, 50, seed="seed-b"))
        assert s1 != s2

    def test_not_region_clustered(self):
        # 4 regions × 25 VMs, pick 4. A region-spread strategy would force one
        # per region; a random draw must NOT be guaranteed to do so. With this
        # fixed seed the draw lands >1 in some region (i.e. not 1-per-region).
        vm_ips = {f"prov/reg{i}": [f"{i}.0.0.{j}" for j in range(1, 26)] for i in range(4)}
        result = select_full_ips(vm_ips, 4, seed="DEV-01")  # ceil(100*4/100)=4
        assert len(result) == 4
        region_counts = {}
        for ip in result:
            region_counts[ip.split(".")[0]] = region_counts.get(ip.split(".")[0], 0) + 1
        assert max(region_counts.values()) > 1

    def test_unseeded_is_still_valid_selection(self):
        vm_ips = {"p/r": [f"10.0.0.{i}" for i in range(10)]}
        result = select_full_ips(vm_ips, 50)
        assert len(result) == 5
        assert len(set(result)) == 5
        assert all(ip in vm_ips["p/r"] for ip in result)


class TestSelectFullIpsDisjointFromNat:
    def test_excludes_nat_ips_from_pool(self):
        vm_ips = {"p/r": ["1", "2", "3", "4"]}
        # NAT holds "1" and "2"; full must come only from {"3","4"}.
        result = select_full_ips(vm_ips, 100, exclude={"1", "2"})
        assert sorted(result) == ["3", "4"]

    def test_percent_is_of_non_excluded_pool(self):
        # 10 VMs, 5 are NAT. 50% of the remaining 5 -> ceil(2.5) = 3.
        vm_ips = {"p/r": [str(i) for i in range(10)]}
        exclude = {"0", "1", "2", "3", "4"}
        result = select_full_ips(vm_ips, 50, exclude=exclude)
        assert len(result) == 3
        assert all(ip not in exclude for ip in result)

    def test_result_never_overlaps_exclude(self):
        vm_ips = {
            "a/1": ["a1", "a2", "a3"],
            "b/1": ["b1", "b2", "b3"],
        }
        exclude = {"a1", "b2"}
        result = set(select_full_ips(vm_ips, 100, exclude=exclude))
        assert result.isdisjoint(exclude)
        assert result == {"a2", "a3", "b1", "b3"}

    def test_all_excluded_returns_empty(self):
        vm_ips = {"p/r": ["1", "2"]}
        assert select_full_ips(vm_ips, 100, exclude={"1", "2"}) == []


class TestBuildFillStorageCommand:
    def test_idempotent_guard_emits_already(self):
        cmd = build_fill_storage_command()
        assert "ANT_FULL:ALREADY" in cmd
        assert FULL_FILLER_PATH in cmd

    def test_emits_configured_on_success(self):
        cmd = build_fill_storage_command()
        assert "ANT_FULL:CONFIGURED" in cmd

    def test_emits_no_space_when_below_headroom(self):
        cmd = build_fill_storage_command()
        assert "ANT_FULL:NO_SPACE" in cmd

    def test_default_headroom_is_baked_in(self):
        cmd = build_fill_storage_command()
        assert f"{DEFAULT_FULL_HEADROOM_MB} * 1024" in cmd

    def test_custom_headroom_is_used(self):
        cmd = build_fill_storage_command(512)
        assert "512 * 1024" in cmd

    def test_uses_portable_df_in_kib(self):
        cmd = build_fill_storage_command()
        assert 'df -Pk "$MOUNT"' in cmd

    def test_prefers_fallocate_with_dd_fallback(self):
        cmd = build_fill_storage_command()
        assert "fallocate -l" in cmd
        assert "dd if=/dev/zero" in cmd

    def test_no_bashisms(self):
        cmd = build_fill_storage_command()
        assert "set -eu" in cmd
        assert "pipefail" not in cmd
