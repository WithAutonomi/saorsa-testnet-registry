"""Parsing and repairing the WebRTC-Direct bootstrap addresses (V2-1169)."""

import pytest

from saorsa_deploy.webrtc_addresses import (
    address_host,
    bootstrap_root_dir,
    multiaddr_path,
    parse_multiaddr,
    rewrite_host,
)

CERTHASH = "uEiAHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBw"
PEER = "ab" * 32
VALID = f"/ip4/1.2.3.4/udp/42768/webrtc-direct/certhash/{CERTHASH}/p2p/{PEER}"


class TestPaths:
    def test_root_dir_matches_the_provisioner(self):
        assert bootstrap_root_dir(3) == "/mnt/ant-node-storage/data/ant-bootstrap-3"

    def test_multiaddr_path(self):
        assert multiaddr_path(1) == (
            "/mnt/ant-node-storage/data/ant-bootstrap-1/webrtc-direct.multiaddr"
        )


class TestParse:
    def test_accepts_a_valid_address(self):
        assert parse_multiaddr(VALID) == VALID

    def test_strips_trailing_newline(self):
        # ant-node writes the address with a trailing "\n".
        assert parse_multiaddr(VALID + "\n") == VALID

    def test_accepts_ipv6(self):
        addr = f"/ip6/::1/udp/42768/webrtc-direct/certhash/{CERTHASH}/p2p/{PEER}"
        assert parse_multiaddr(addr) == addr

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   \n",
            "not a multiaddr",
            # A QUIC address -- the shape the rest of the tool passes around.
            "/ip4/1.2.3.4/udp/10000/quic",
            # Missing the certhash: the address would look plausible but no
            # browser could pin the DTLS certificate with it.
            f"/ip4/1.2.3.4/udp/42768/webrtc-direct/p2p/{PEER}",
            # Peer id in some other encoding (base58 rather than 64 hex chars).
            f"/ip4/1.2.3.4/udp/42768/webrtc-direct/certhash/{CERTHASH}/p2p/12D3KooWabc",
        ],
    )
    def test_rejects_malformed(self, raw):
        with pytest.raises(ValueError):
            parse_multiaddr(raw)

    def test_rejects_multiple_addresses(self):
        with pytest.raises(ValueError, match="single address"):
            parse_multiaddr(f"{VALID}\n{VALID}\n")


class TestRewriteHost:
    def test_replaces_the_host_and_keeps_everything_else(self):
        out = rewrite_host(VALID, "9.9.9.9")
        assert out == f"/ip4/9.9.9.9/udp/42768/webrtc-direct/certhash/{CERTHASH}/p2p/{PEER}"

    def test_repairs_a_loopback_advertisement(self):
        # ant-node falls back to a routed local IP and finally loopback when its
        # transport has not yet learned an external address. Such an address is
        # well-formed and completely useless to a client on another machine, so
        # the deployment's own knowledge of the VM's public IP has to win.
        local = f"/ip4/127.0.0.1/udp/42768/webrtc-direct/certhash/{CERTHASH}/p2p/{PEER}"
        assert address_host(local) == "127.0.0.1"
        assert address_host(rewrite_host(local, "1.2.3.4")) == "1.2.3.4"

    def test_preserves_the_certhash(self):
        # The certhash is the one part of the address the deployment cannot
        # derive -- it comes from the node's persisted DTLS certificate.
        assert CERTHASH in rewrite_host(VALID, "9.9.9.9")


def _addr(peer_hex: str, ip: str = "1.2.3.4") -> str:
    return f"/ip4/{ip}/udp/42768/webrtc-direct/certhash/{CERTHASH}/p2p/{peer_hex}"


class TestClientBootstrapsAreTheBootstrapTierOnly:
    """A WASM client is given the bootstrap tier and nothing more.

    It differs from a native client only in address FORMAT: the native tier
    takes bare ``ip:port`` peers, a browser client needs the full multiaddr
    (UDP port + DTLS certhash + peer id). It must then find the rest of the
    network by walking the DHT, exactly as the native client does.

    An earlier revision sampled the node tier and handed a client ~32 ordinary
    node addresses. That is not a shape production can supply — a browser gets
    a small bootstrap list — so it masked any failure to discover peers rather
    than exposing it. If transfers do not work from the bootstraps alone, the
    defect is node-side.
    """

    def test_state_carries_only_the_bootstrap_tier_key(self):
        import inspect

        from saorsa_deploy.cmd import webrtc_addresses as cmd

        src = inspect.getsource(cmd.cmd_webrtc_addresses)
        assert '"bootstrap_webrtc_multiaddrs"' in src
        # The node-tier sample must not come back under any name.
        assert "webrtc_client_bootstrap_multiaddrs" not in src
        assert "webrtc_seed_multiaddrs" not in src

    def test_the_node_tier_is_not_collected(self):
        import inspect

        from saorsa_deploy.cmd import webrtc_addresses as cmd

        # `state["vm_ips"]` is the node-tier VM list; reading it would only be
        # to build a sample. (The bootstrap helper is named `_vm_ips`, so match
        # the state lookup itself rather than the bare word.)
        assert 'state.get("vm_ips")' not in inspect.getsource(cmd)

    def test_sampling_helpers_are_gone(self):
        import saorsa_deploy.webrtc_addresses as mod

        assert not hasattr(mod, "spread_sample")

    def test_provisioning_does_not_fall_back_to_a_node_tier_sample(self):
        # A stale pre-change deployment state still holds the old sample keys.
        # Reading one would silently reintroduce the pre-loading.
        import inspect

        from saorsa_deploy.cmd import provision_clients

        src = inspect.getsource(provision_clients)
        assert 'deploy_state.get("bootstrap_webrtc_multiaddrs")' in src
        assert 'deploy_state.get("webrtc_seed_multiaddrs")' not in src
        assert 'deploy_state.get("webrtc_client_bootstrap_multiaddrs")' not in src
