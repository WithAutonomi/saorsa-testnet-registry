"""Synthetic pcaps with known ground truth for the `analysis` pcap analysers.

Each generator writes an Ethernet/IPv4/UDP pcap to the given path and returns
the constants a test needs. The ground truth is documented on each function.
"""
from __future__ import annotations

import struct

LOCAL = "10.200.0.2"


def eth_ipv4_udp(src, sport, dst, dport, payload_len):
    eth = b"\x02" * 6 + b"\x03" * 6 + struct.pack(">H", 0x0800)
    udp_len = 8 + payload_len
    ip_len = 20 + udp_len
    ip = struct.pack(
        ">BBHHHBBH4s4s",
        0x45, 0, ip_len, 0, 0, 64, 17, 0,
        bytes(int(o) for o in src.split(".")),
        bytes(int(o) for o in dst.split(".")),
    )
    udp = struct.pack(">HHHH", sport, dport, udp_len, 0)
    return eth + ip + udp + b"\x00" * payload_len


def write_pcap(path, packets):
    packets = sorted(packets, key=lambda p: p[0])
    with open(path, "wb") as fh:
        fh.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, 1))
        for ts, data in packets:
            sec = int(ts)
            usec = int(round((ts - sec) * 1e6))
            fh.write(struct.pack("<IIII", sec, usec, len(data), len(data)))
            fh.write(data)
    return len(packets)


def pinged_flow(packets, pinger, pport, acker, aport, t_start, t_end, skip=lambda t: False,
                period=10.0):
    t = t_start
    while t < t_end:
        if not skip(t):
            packets.append((t, eth_ipv4_udp(pinger, pport, acker, aport, 32)))
            packets.append((t + 0.04, eth_ipv4_udp(acker, aport, pinger, pport, 32)))
        t += period


def write_flowstat_pcap(path):
    """Ground truth (flowstat):
      * 3 long-lived flows, both directions, keep-alives every 2.0 s, 60-byte wire.
        Open before the window and still open at the end -> censored, not counted
        in the observed lifetime histogram.
      * 1 flow born at +100 s and last seen at +136 s -> a 36 s lifetime, which
        must land in the explicit 30-45 bucket.
      * 1 bulk flow with 1200-byte packets every 0.01 s -> must be excluded from
        the cadence stats by the quiescent filter.
    """
    t0 = 1_755_000_000.0
    packets = []
    for i in range(3):
        peer = f"10.0.0.{10 + i}"
        t = t0
        while t < t0 + 300:
            packets.append((t, eth_ipv4_udp(LOCAL, 10001 + i, peer, 20001, 32)))
            packets.append((t + 0.4, eth_ipv4_udp(peer, 20001, LOCAL, 10001 + i, 32)))
            t += 2.0
    t = t0 + 100
    while t <= t0 + 136:
        packets.append((t, eth_ipv4_udp(LOCAL, 10050, "10.0.0.99", 20099, 32)))
        t += 2.0
    t = t0 + 50
    while t < t0 + 60:
        packets.append((t, eth_ipv4_udp(LOCAL, 10060, "10.0.0.50", 20050, 1200)))
        t += 0.01
    write_pcap(path, packets)
    return {"t0": t0}


def write_backstop_pcap(path):
    """Ground truth (backstop + probe-score), local host 10.200.0.2:

      A. 3 flows where the LOCAL host is the ACCEPTOR: peer pings every 10 s, we
         ACK 40 ms later. Healthy -> 0 unsolicited acceptor packets each.
      B. 2 flows where the LOCAL host is the DIALLER: we ping every 10 s, peer
         ACKs 40 ms later. Healthy -> 0 unsolicited; the acceptor is the PEER.
      C. 1 flow, local = acceptor, whose peer goes quiet for 27 s twice; each
         time we emit a backstop PING at +25 s of silence and the peer ACKs it.
         -> exactly 2 unsolicited acceptor packets, gaps 25 s.
      D. 1 bulk flow (1200-byte packets at 10 ms) -> excluded (not quiescent).

      Stall-probe ground truth, all with local = acceptor, peer pings every 10 s:
      E. peer 10.0.1.1 blocked [T0+150, T0+171): SURVIVES, traffic resumes 2 s
         after lift.
      F. peer 10.0.1.2 blocked [T0+150, T0+181): DIES: nothing after the block,
         and a NEW flow to the same peer on a new port appears at T0+200.
      G. peer 10.0.1.3 blocked [T0+150, T0+171): DIES silently -> scored dead.

    Expected: backstop flows=11, flows_quiescent=10, flows_role_resolved=10,
    acceptor_unsolicited=2 (flow C only), both gaps in the 24-27 bin, median
    25.0. probe-score: 21 s -> 10.0.1.1 SURVIVED (resume +2.0 s), 10.0.1.3 DIED
    (replaced=False); 31 s -> 10.0.1.2 DIED (replaced=True).
    """
    t0 = 1_756_000_000.0
    window = 300.0
    p = []
    for i in range(3):
        pinged_flow(p, f"10.0.0.{10 + i}", 20001, LOCAL, 10001 + i, t0, t0 + window)
    for i in range(2):
        pinged_flow(p, LOCAL, 10101 + i, f"10.0.0.{20 + i}", 20002, t0, t0 + window)
    peer = "10.0.0.30"

    def quiet(t):
        return (t0 + 100 <= t < t0 + 127) or (t0 + 200 <= t < t0 + 227)

    pinged_flow(p, peer, 20003, LOCAL, 10201, t0, t0 + window, skip=quiet)
    for base in (t0 + 90, t0 + 190):
        p.append((base + 25.0, eth_ipv4_udp(LOCAL, 10201, peer, 20003, 32)))
        p.append((base + 25.04, eth_ipv4_udp(peer, 20003, LOCAL, 10201, 32)))
    t = t0 + 50
    while t < t0 + 60:
        p.append((t, eth_ipv4_udp(LOCAL, 10060, "10.0.0.50", 20050, 1200)))
        t += 0.01
    pinged_flow(p, "10.0.1.1", 20004, LOCAL, 10301, t0, t0 + window,
                skip=lambda t: t0 + 150 <= t < t0 + 171)
    p.append((t0 + 173.0, eth_ipv4_udp("10.0.1.1", 20004, LOCAL, 10301, 32)))
    p.append((t0 + 173.04, eth_ipv4_udp(LOCAL, 10301, "10.0.1.1", 20004, 32)))
    pinged_flow(p, "10.0.1.2", 20005, LOCAL, 10302, t0, t0 + 150)
    pinged_flow(p, "10.0.1.2", 20105, LOCAL, 10402, t0 + 200, t0 + window)
    pinged_flow(p, "10.0.1.3", 20006, LOCAL, 10303, t0, t0 + 150)
    write_pcap(path, p)
    probes = [
        {"peer": "10.0.1.1", "block_start": t0 + 150, "block_end": t0 + 171, "seconds": 21},
        {"peer": "10.0.1.3", "block_start": t0 + 150, "block_end": t0 + 171, "seconds": 21},
        {"peer": "10.0.1.2", "block_start": t0 + 150, "block_end": t0 + 181, "seconds": 31},
    ]
    return {"t0": t0, "probes": probes}


def write_close_delivery_pcap(path):
    """Ground truth (close-delivery), local 10.200.0.2 dialling relay addresses,
    keep-alive every 10 s, teardown at T = t0 + 200:

      * 203.0.113.10:41000  TOLD   — remote sends the close at T+0.2, nothing after.
      * 203.0.113.10:41001  TOLD   — close at T+0.2, then one trailing outbound.
      * 203.0.113.11:41002  UNTOLD — last inbound at T-1, then local pings at
                                     T+10, T+20, T+30 into the void.
      * 203.0.113.12:41003  unscored — flow ended at T-300, outside the window.
      * 203.0.113.13:41004  a teardown with no flow in the pcap at all.
      * 10.0.0.77:20001     unrelated healthy flow, must not be scored.

    Expected: scored=3, told=2, untold=1, unscored_outside_window=1,
    teardowns=5, teardowns_without_flows=1.
    """
    t0 = 1_757_000_000.0
    teardown = t0 + 200
    p = []
    # TOLD, clean
    pinged_flow(p, LOCAL, 10001, "203.0.113.10", 41000, t0, teardown - 5)
    p.append((teardown + 0.2, eth_ipv4_udp("203.0.113.10", 41000, LOCAL, 10001, 40)))
    # TOLD, one trailing outbound (an ACK of the close)
    pinged_flow(p, LOCAL, 10002, "203.0.113.10", 41001, t0, teardown - 5)
    p.append((teardown + 0.2, eth_ipv4_udp("203.0.113.10", 41001, LOCAL, 10002, 40)))
    p.append((teardown + 0.25, eth_ipv4_udp(LOCAL, 10002, "203.0.113.10", 41001, 32)))
    # UNTOLD: last inbound at T-1, then pings into the void
    pinged_flow(p, LOCAL, 10003, "203.0.113.11", 41002, t0, teardown - 5)
    p.append((teardown - 1.0, eth_ipv4_udp("203.0.113.11", 41002, LOCAL, 10003, 32)))
    for dt in (10, 20, 30):
        p.append((teardown + dt, eth_ipv4_udp(LOCAL, 10003, "203.0.113.11", 41002, 32)))
    # Ended long before the teardown: unscored
    pinged_flow(p, LOCAL, 10004, "203.0.113.12", 41003, t0 - 400, teardown - 300)
    # Unrelated
    pinged_flow(p, LOCAL, 10005, "10.0.0.77", 20001, t0, teardown + 100)
    write_pcap(path, p)
    teardowns = [
        {"ts": teardown, "relay_addr": "203.0.113.10:41000", "allocation_id": 1, "host": "nat-1"},
        {"ts": teardown, "relay_addr": "203.0.113.10:41001", "allocation_id": 2, "host": "nat-1"},
        {"ts": teardown, "relay_addr": "203.0.113.11:41002", "allocation_id": 3, "host": "nat-2"},
        {"ts": teardown, "relay_addr": "203.0.113.12:41003", "allocation_id": 4, "host": "nat-2"},
        {"ts": teardown, "relay_addr": "203.0.113.13:41004", "allocation_id": 5, "host": "nat-3"},
    ]
    return {"t0": t0, "teardown": teardown, "teardowns": teardowns}
