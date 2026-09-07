"""Parsing and repairing the bootstrap tier's WebRTC-Direct seed addresses (V2-1169)."""

import pytest

from saorsa_deploy.webrtc_addresses import (
    address_host,
    bootstrap_root_dir,
    multiaddr_path,
    parse_multiaddr,
    peer_id,
    rewrite_host,
    spread_sample,
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


class TestSpreadSample:
    """Seeds must cover the peer-id keyspace, not just be the first N.

    A node publishes its browser endpoint only to the peers CLOSEST TO ITSELF,
    so a node knows endpoints for its own neighbourhood and nobody else's. A
    client seeded only with the bootstrap tier can reach the bootstraps and
    nothing else: asked for the peers closest to some chunk, a bootstrap returns
    20 nodes of which ~1 has a reachable endpoint, and the walk stalls.

    Measured on DEV-01-wasm (100 nodes), same 10 MB target: 3 bootstrap seeds
    failed with "no closest WebRtcDirect node returned chunk"; 30 spread seeds
    succeeded in 31s.
    """

    @staticmethod
    def _fleet(n: int) -> list[str]:
        # Peer ids spread across the space, deliberately supplied out of order.
        return [_addr(f"{i:02x}" * 32) for i in reversed(range(n))]

    def test_returns_everything_when_the_fleet_is_small(self):
        fleet = self._fleet(5)
        assert sorted(spread_sample(fleet, 32)) == sorted(fleet)

    def test_returns_the_requested_count(self):
        assert len(spread_sample(self._fleet(100), 32)) == 32

    def test_samples_are_distinct(self):
        out = spread_sample(self._fleet(100), 32)
        assert len(set(out)) == 32

    def test_covers_the_whole_keyspace_not_just_the_start(self):
        # Truncating a sorted list would take only the low end of the keyspace,
        # leaving every chunk in the upper half unreachable. The fleet's ids run
        # 0x00..0x63, so an even sample must reach most of the way to 0x63 —
        # a truncation would stop at 0x09.
        out = spread_sample(self._fleet(100), 10)
        positions = sorted(int(peer_id(a)[:2], 16) for a in out)
        assert positions[0] == 0x00
        assert positions[-1] >= 0x50, positions

    def test_spacing_is_even(self):
        out = spread_sample(self._fleet(100), 10)
        positions = sorted(int(peer_id(a)[:2], 16) for a in out)
        gaps = [b - a for a, b in zip(positions, positions[1:])]
        assert max(gaps) - min(gaps) <= 1, gaps

    def test_ordering_of_the_input_does_not_matter(self):
        fleet = self._fleet(50)
        assert spread_sample(fleet, 8) == spread_sample(list(reversed(fleet)), 8)

    def test_zero_or_negative_count_returns_everything(self):
        fleet = self._fleet(10)
        assert spread_sample(fleet, 0) == fleet


class TestPeerId:
    def test_extracts_the_trailing_hex_id(self):
        assert peer_id(_addr("ab" * 32)) == "ab" * 32
