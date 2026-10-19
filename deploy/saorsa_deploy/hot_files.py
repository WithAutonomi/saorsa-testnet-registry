"""Hot-file selection for the browser-downloader cohort.

The browser role's whole measurement is "many real browsers reading the SAME
file at the SAME moment", so which file that is has to be a fact the cohort
agrees on. Two things make it one:

* It is **chosen by an explicit operator command**, not as a side effect of
  provisioning. A VM provisioned inside ``deployments up`` exists before any
  upload does, so there is nothing to choose from at that point.
* It is written **once, to one object**, which every instance reads at every
  round start. VMs selecting independently from a pool that grows under them
  would each pick a different file, and "which file was hot" would have no
  answer.

Candidates come from the deployment's own native address pool (the
``upload-*.txt`` keys that ``uploader.sh`` flushes), filtered to one exact size
and ordered by ``(key name, line number)`` — the lines are appended in upload
order, so that ordering is upload order, and the first N are the oldest files of
that size.

A pool line is ``<address> <bytes> [<sha256>]``. Two producers write into the
one shared pool and they write different numbers of fields: ``uploader.sh``
publishes all three (the hash is what ``downloader.sh`` checks the bytes it got
back against), the WASM uploaders publish the first two. So every reader of the
pool — this one included — takes the first two fields and ignores the rest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3

from saorsa_deploy.client_roles import HOT_FILES_OBJECT_NAME
from saorsa_deploy.state import (
    S3_ADDRESSES_BUCKET,
    S3_CLIENT_ADDRESSES_PREFIX,
    S3_REGION,
)

#: ``size_mb`` is binary, matching every other file size in this tool
#: (``file_size_mb`` on an uploader is MiB, and the uploader generates
#: ``file_size_mb * 1024 * 1024`` bytes), so a request for 50 means an exact
#: 52,428,800-byte file and nothing else.
BYTES_PER_MB = 1024 * 1024

#: Only the NATIVE uploaders' keys. A WASM uploader publishes under
#: `wasm-upload-*` and its files are capped differently; the point of the
#: candidate set is files of one exact size published by one kind of uploader,
#: so the hot file is the same sort of object whichever round picks it.
POOL_KEY_PREFIX = "upload-"


class HotFileError(RuntimeError):
    """Selection could not be completed. Carries a message for the operator."""


@dataclass(frozen=True)
class Candidate:
    """One pool line that matches the requested size."""

    address: str
    bytes: int
    source_key: str
    line: int


def addresses_prefix(deployment: str) -> str:
    """S3 prefix holding one deployment's published address lists."""
    return f"{S3_CLIENT_ADDRESSES_PREFIX}/{deployment}"


def hot_files_key(deployment: str) -> str:
    return f"{addresses_prefix(deployment)}/{HOT_FILES_OBJECT_NAME}"


def addresses_base_url(deployment: str) -> str:
    """Unsigned HTTPS base of a deployment's address prefix.

    The browser VMs read the hot-file manifest from here with a plain GET. The
    bucket is public-read and the VMs sign nothing, exactly as the native
    downloader fetches address lists — a clock-skewed VM's own SigV4 requests
    would be rejected.

    The **regional** endpoint, matching `downloader.sh`'s
    ``https://${ADDRESSES_S3_BUCKET}.s3.${AWS_DEFAULT_REGION:-eu-west-2}.amazonaws.com``:
    the global `s3.amazonaws.com` form reaches a bucket outside us-east-1 only
    through S3's redirect, and two readers of one pool must not differ in how
    they get to it.
    """
    return (
        f"https://{S3_ADDRESSES_BUCKET}.s3.{S3_REGION}.amazonaws.com/{addresses_prefix(deployment)}"
    )


def _s3():
    return boto3.client("s3")


def parse_pool_line(line: str) -> tuple[str, int] | None:
    """``<address> <bytes> [<sha256>]`` → ``(address, bytes)``, or None.

    Two or more fields with a numeric second field, and **anything after the
    second is ignored** rather than counted: the native ``uploader.sh`` writes
    ``<address> <bytes> <sha256>`` and the WASM uploaders write
    ``<address> <bytes>`` into the same pool, so a field count is not a property
    of a pool line. Requiring exactly two discarded every native line published
    since 2026-09-29 — the whole pool of a run — and reported it as a pool with
    no files of the requested size (V2-1629).

    A bare address line (no size) returns None rather than a zero size: the
    candidate set is "files of exactly this size", and an unknown size is not a
    match for any size. Treating it as 0 would silently never match, which is
    the same outcome by accident rather than by rule.
    """
    fields = line.strip().split()
    if len(fields) < 2:
        return None
    address, size = fields[0], fields[1]
    if not size.isdigit():
        return None
    return address.lower(), int(size)


def collect_candidates(deployment: str, size_bytes: int, *, s3=None) -> list[Candidate]:
    """Every pool line of exactly ``size_bytes``, in (key, line number) order.

    Ordering is the selection rule, so it is sorted explicitly here rather than
    relying on the S3 listing: ``list_objects_v2`` happens to return keys
    lexicographically, but the selection has to be reproducible from the data
    and not from a service's ordering guarantee.
    """
    client = s3 or _s3()
    prefix = f"{addresses_prefix(deployment)}/"
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=S3_ADDRESSES_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"][len(prefix) :]
            if "/" in name or not name.startswith(POOL_KEY_PREFIX) or not name.endswith(".txt"):
                continue
            keys.append(name)

    out: list[Candidate] = []
    for name in sorted(keys):
        body = client.get_object(Bucket=S3_ADDRESSES_BUCKET, Key=f"{prefix}{name}")["Body"].read()
        text = body.decode("utf-8", errors="replace")
        for lineno, raw in enumerate(text.splitlines(), start=1):
            parsed = parse_pool_line(raw)
            if parsed is None:
                continue
            address, size = parsed
            if size != size_bytes:
                continue
            out.append(Candidate(address=address, bytes=size, source_key=name, line=lineno))
    return out


def build_document(
    deployment: str, size_bytes: int, selected: list[Candidate], *, now=None
) -> dict:
    """The ``browser-hot-files.json`` body for a selection."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "deployment": deployment,
        "selected_at": stamp,
        "size_bytes": size_bytes,
        "files": [
            {
                "index": index,
                "address": c.address,
                "bytes": c.bytes,
                "source_key": c.source_key,
                "line": c.line,
            }
            for index, c in enumerate(selected)
        ],
    }


def load_hot_files(deployment: str, *, s3=None) -> dict | None:
    """The deployment's hot-file manifest, or None when it has not been written."""
    client = s3 or _s3()
    try:
        resp = client.get_object(Bucket=S3_ADDRESSES_BUCKET, Key=hot_files_key(deployment))
    except client.exceptions.NoSuchKey:
        return None
    return json.loads(resp["Body"].read())


def save_hot_files(deployment: str, document: dict, *, s3=None) -> None:
    client = s3 or _s3()
    client.put_object(
        Bucket=S3_ADDRESSES_BUCKET,
        Key=hot_files_key(deployment),
        Body=json.dumps(document, indent=2),
        ContentType="application/json",
    )


def select_hot_files(
    deployment: str,
    *,
    count: int,
    size_mb: int,
    s3=None,
    now=None,
) -> dict:
    """Choose ``count`` files of ``size_mb`` MiB and return the document to write.

    Raises :class:`HotFileError` when the pool holds fewer than ``count``
    candidates — naming how many it found, because the fix is either to wait for
    more uploads of that size or to ask for a size the run actually produced.
    Writes nothing: the caller decides, so a shortfall cannot half-replace an
    existing selection.
    """
    size_bytes = size_mb * BYTES_PER_MB
    candidates = collect_candidates(deployment, size_bytes, s3=s3)
    if len(candidates) < count:
        raise HotFileError(
            f"found {len(candidates)} file(s) of exactly {size_bytes:,} bytes "
            f"({size_mb} MiB) in the address pool for '{deployment}', but "
            f"{count} were requested. Nothing has been written. Either wait for "
            f"more uploads of that size, or pass a --size-mb the run's uploaders "
            f"actually produce."
        )
    return build_document(deployment, size_bytes, candidates[:count], now=now)
