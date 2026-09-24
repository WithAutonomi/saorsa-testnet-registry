"""An ICE connectivity check against a WebRTC-Direct listener.

Proving that a node's browser-facing UDP port is actually reachable needs more
than a STUN Binding Request: ``saorsa-transport``'s admission filter answers an
*unauthenticated* binding with silence. Every failure path in
``webrtc/admission.rs`` returns ``Decision::Ignore``, so a plain STUN probe
cannot tell a firewalled port from a healthy one -- both look like a timeout.

The probe here is therefore a real ICE connectivity check, carrying the
credentials the V2 scheme in ``saorsa-transport/src/webrtc/direct.rs`` derives:
the listener accepts any client whose ``USERNAME`` names a server fragment of
``saorsa+webrtc+v2/<client-pwd>``, which lets a dialler that has never
exchanged an SDP offer still be admitted. It answers with a connectivity check
of its own -- a Binding *Request* carrying ``ICE-CONTROLLED`` -- and that reply
is the whole signal. The probe stops there: it does not go on to complete DTLS,
because the question being asked is "do packets reach the listener and come
back", not "can a browser fetch a chunk".

Standard library only, deliberately: the rollout-watch scripts bundle imports
this module, and those run wherever the operator happens to be, without this
package's dependency set.
"""

import hashlib
import hmac
import os
import socket
import struct
import time
import zlib
from dataclasses import dataclass

#: ``saorsa-transport``'s V2 credential prefix. The server fragment is this
#: followed by the client's own password, which is what lets an unsolicited
#: dialler address a listener it has never negotiated with.
SERVER_UFRAG_PREFIX = "saorsa+webrtc+v2/"

#: RFC 5245 ``ice-char``: the alphabet an ufrag/pwd may be drawn from.
_ICE_CHARS = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ" "abcdefghijklmnopqrstuvwxyz" "0123456789+/"
)
_ICE_STRING_LENGTH = 32

MAGIC_COOKIE = 0x2112A442
BINDING_REQUEST = 0x0001
ATTR_USERNAME = 0x0006
ATTR_MESSAGE_INTEGRITY = 0x0008
ATTR_FINGERPRINT = 0x8028
#: RFC 5389 §15.5: the CRC-32 is XORed with this before being sent, so a
#: fingerprinted message cannot be confused with another protocol's framing.
FINGERPRINT_XOR = 0x5354554E

_HEADER_LEN = 20
#: A MESSAGE-INTEGRITY attribute is a 20-byte SHA-1 digest plus its 4-byte TLV
#: header; FINGERPRINT is 4 bytes plus the same header.
_MESSAGE_INTEGRITY_LEN = 24
_FINGERPRINT_LEN = 8

DEFAULT_TIMEOUT_SECS = 5.0


@dataclass(frozen=True)
class StunMessage:
    """A parsed STUN message."""

    message_type: int
    transaction_id: bytes
    attributes: tuple[tuple[int, bytes], ...]


def random_ice_string(length: int = _ICE_STRING_LENGTH) -> str:
    """A fresh ICE ufrag or password."""
    return "".join(_ICE_CHARS[b % len(_ICE_CHARS)] for b in os.urandom(length))


def server_ufrag(client_pwd: str) -> str:
    """The server fragment a V2 listener will accept for ``client_pwd``."""
    return f"{SERVER_UFRAG_PREFIX}{client_pwd}"


def _attribute(attr_type: int, value: bytes) -> bytes:
    """One STUN TLV, padded to the 4-byte boundary the header length counts in."""
    padding = b"\x00" * (-len(value) % 4)
    return struct.pack("!HH", attr_type, len(value)) + value + padding


def _header(message_type: int, body_length: int, transaction_id: bytes) -> bytes:
    return (
        struct.pack("!HHI", message_type, body_length, MAGIC_COOKIE) + transaction_id
    )


def build_binding_request(
    username: str, key: str, transaction_id: bytes
) -> bytes:
    """A short-term-credential Binding Request, integrity-protected and
    fingerprinted.

    Both trailing attributes are computed over a header whose declared length
    *already counts the attribute being computed* -- the rule that makes a STUN
    message verifiable in one pass, and the detail that silently produces an
    ignored probe when it is got wrong.
    """
    if len(transaction_id) != 12:
        raise ValueError("transaction id must be 12 bytes")

    body = _attribute(ATTR_USERNAME, username.encode())

    integrity_header = _header(
        BINDING_REQUEST, len(body) + _MESSAGE_INTEGRITY_LEN, transaction_id
    )
    digest = hmac.new(key.encode(), integrity_header + body, hashlib.sha1).digest()
    body += _attribute(ATTR_MESSAGE_INTEGRITY, digest)

    fingerprint_header = _header(
        BINDING_REQUEST, len(body) + _FINGERPRINT_LEN, transaction_id
    )
    crc = (zlib.crc32(fingerprint_header + body) & 0xFFFFFFFF) ^ FINGERPRINT_XOR
    body += _attribute(ATTR_FINGERPRINT, struct.pack("!I", crc))

    return _header(BINDING_REQUEST, len(body), transaction_id) + body


def parse_stun_message(data: bytes) -> StunMessage:
    """Validate one datagram as STUN and return it decomposed.

    Raises ``ValueError`` rather than returning a partial result: the probe
    treats "a well-formed STUN message came back" as its pass condition, so
    anything it cannot fully account for must not count.
    """
    if len(data) < _HEADER_LEN:
        raise ValueError(f"too short for a STUN header: {len(data)} bytes")
    # RFC 5389 §6: the two most significant bits of a STUN message are zero.
    if data[0] & 0xC0:
        raise ValueError("leading bits are not zero")

    message_type, body_length, cookie = struct.unpack("!HHI", data[:8])
    if cookie != MAGIC_COOKIE:
        raise ValueError(f"bad magic cookie: {cookie:#010x}")
    if body_length % 4:
        raise ValueError(f"body length {body_length} is not a multiple of 4")
    if _HEADER_LEN + body_length != len(data):
        raise ValueError(
            f"declared length {body_length} does not match {len(data) - _HEADER_LEN}"
        )

    transaction_id = data[8:_HEADER_LEN]
    attributes: list[tuple[int, bytes]] = []
    offset = _HEADER_LEN
    end = _HEADER_LEN + body_length
    while offset < end:
        if offset + 4 > end:
            raise ValueError("truncated attribute header")
        attr_type, attr_len = struct.unpack("!HH", data[offset : offset + 4])
        offset += 4
        if offset + attr_len > end:
            raise ValueError(f"truncated attribute {attr_type:#06x}")
        attributes.append((attr_type, data[offset : offset + attr_len]))
        offset += attr_len + (-attr_len % 4)

    return StunMessage(
        message_type=message_type,
        transaction_id=transaction_id,
        attributes=tuple(attributes),
    )


def probe(
    ip: str, port: int, timeout: float = DEFAULT_TIMEOUT_SECS, attempts: int = 1
) -> bool:
    """True if ``ip:port`` answers an ICE connectivity check within ``timeout``.

    Each attempt gets its own credentials and transaction id, so a reply to an
    earlier attempt arriving late cannot pass a later one.
    """
    for _ in range(max(1, attempts)):
        if _probe_once(ip, port, timeout):
            return True
    return False


def _probe_once(ip: str, port: int, timeout: float) -> bool:
    client_ufrag = random_ice_string()
    client_pwd = random_ice_string()
    remote_ufrag = server_ufrag(client_pwd)
    request = build_binding_request(
        f"{remote_ufrag}:{client_ufrag}", remote_ufrag, os.urandom(12)
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        try:
            sock.sendto(request, (ip, port))
        except OSError:
            return False

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(2048)
            except OSError:
                # A timeout, or an ICMP port-unreachable surfacing as ECONNREFUSED.
                return False
            if addr[0] != ip:
                continue
            try:
                parse_stun_message(data)
            except ValueError:
                continue
            return True
    finally:
        sock.close()
