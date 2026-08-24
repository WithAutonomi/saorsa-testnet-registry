"""Shared pcap access for the offline analysers.

`flowstat.py` carries its own reader because it has to run standalone on a node
VM; the other analysers import that same reader from here so there is exactly
one parser of pcap records in the package.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from saorsa_deploy.analysis.flowstat import _histogram, _percentile, read_packets

__all__ = ["read_packets", "_histogram", "_percentile", "load_jsonl", "parse_ts"]


def load_jsonl(path):
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rows.append(json.loads(line))
    return rows


def parse_ts(value) -> float:
    """Epoch seconds from an epoch number/string or an ISO-8601 timestamp (UTC if naive)."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
