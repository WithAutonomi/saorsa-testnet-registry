"""Adapters that enumerate public collections into pool candidates.

A single canary run consumes roughly 350 distinct real files, permanently, and
canaries run once per release train. Adding URLs a dozen at a time cannot feed
that — the pool would be empty by the second train. These adapters exist so a
few hundred genuine files can be catalogued in one command.

The Internet Archive adapter earns its place twice over. Its item metadata API
publishes **name, size, MD5 and SHA1 for every file in an item**, so entries
arrive pre-sized and pre-hashed without a byte being downloaded. That is what
makes an IA URL and a local copy of the same file recognisable as one piece of
content — the case that would otherwise let the pool hand out both.
"""

from dataclasses import dataclass, field
from urllib.parse import quote, unquote

import requests

METADATA_TIMEOUT_SECONDS = 60

#: IA's ``source`` field is NOT a usable content filter, which is only obvious
#: against a real item. On media items the ingested media is marked
#: ``derivative`` and the only ``original`` entries are the metadata XML plus a
#: .torrent — so filtering to originals discards the actual files. Measured on
#: ``BigBuckBunny_124``: 5 "originals" (all metadata/torrent) against three real
#: encodings of 332 MB, 62 MB and 47 MB, every one of them "derivative".
#:
#: ``format`` is the reliable signal, so artefacts are excluded by format and by
#: name instead. ``--originals-only`` is available for items where the
#: distinction genuinely is what the operator wants.
_ORIGINAL_SOURCE = "original"

#: Formats that are artefacts of the archive rather than content anyone would
#: upload to production: thumbnails, checksums, torrents, waveform images.
_ARTEFACT_FORMATS = frozenset(
    {
        "metadata",
        "item tile",
        "item image",
        "thumbnail",
        "json",
        "bittorrent",
        "archive bittorrent",
        "columbia peaks",
        "spectrogram",
        "png thumb",
        "web archive gz",
        "cue",
        "log",
        # A listing of what a torrent contains — an artefact of the archive's
        # own distribution, not content anyone uploaded.
        "bittorrentcontents",
    }
)

#: Bookkeeping files IA adds to every item. Excluded by name because their
#: ``source`` is inconsistently reported across items.
_METADATA_SUFFIXES = (
    "_meta.xml",
    "_files.xml",
    "_meta.sqlite",
    "_reviews.xml",
    "_archive.torrent",
    "__ia_thumb.jpg",
    "_thumb.jpg",
)


@dataclass
class PoolCandidate:
    """One file a source is offering, with whatever it could tell us for free."""

    url: str
    name: str
    size_bytes: int | None = None
    digests: dict[str, str] = field(default_factory=dict)
    provenance: str = ""
    #: The source-internal path this came from, kept so a repeated basename can
    #: be disambiguated. Display and matching still use ``name``.
    path: str = ""


class SourceError(Exception):
    """The source could not be read. Distinct from "it returned nothing"."""


def parse_source_spec(spec: str) -> tuple[str, str]:
    """Split ``kind:identifier``, e.g. ``internet-archive:some-item-id``."""
    if ":" not in spec:
        raise ValueError(
            f"Invalid source '{spec}'. Expected kind:identifier, e.g. "
            "internet-archive:nasa-apollo-audio"
        )
    kind, identifier = spec.split(":", 1)
    kind = kind.strip().lower()
    identifier = identifier.strip()
    if not identifier:
        raise ValueError(f"Invalid source '{spec}': identifier cannot be empty.")
    if kind not in SOURCES:
        valid = ", ".join(sorted(SOURCES))
        raise ValueError(f"Unknown source kind '{kind}'. Valid kinds: {valid}")
    return kind, identifier


def fetch_candidates(
    spec: str, originals_only: bool = False, path_prefix: str | None = None
) -> list[PoolCandidate]:
    kind, identifier = parse_source_spec(spec)
    return SOURCES[kind](
        identifier, originals_only=originals_only, path_prefix=path_prefix
    )


def fetch_internet_archive_item(
    identifier: str,
    originals_only: bool = False,
    path_prefix: str | None = None,
) -> list[PoolCandidate]:
    """Enumerate the content files of one Internet Archive item.

    Reads ``https://archive.org/metadata/<identifier>``, which returns every
    file's name, size and checksums. Nothing is downloaded.

    Archive artefacts (thumbnails, metadata XML, torrents, waveform images) are
    excluded by format and by name. ``originals_only`` additionally restricts to
    IA's ``source == "original"``, which on many media items excludes the real
    files — see the note on ``_ORIGINAL_SOURCE``.

    ``path_prefix`` narrows to one directory within the item. An item's file
    names are its full internal paths, so a large item can be catalogued a
    directory at a time. The prefix is percent-decoded first, so it can be
    pasted straight from a browser URL where spaces appear as ``%20``.
    """
    url = f"https://archive.org/metadata/{identifier}"
    try:
        response = requests.get(url, timeout=METADATA_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise SourceError(f"could not read {url}: {exc}") from exc
    except ValueError as exc:
        raise SourceError(f"{url} did not return JSON: {exc}") from exc

    files = payload.get("files")
    if not files:
        # An unknown identifier returns 200 with an empty body rather than a
        # 404, so this is the only signal that the item does not exist.
        raise SourceError(
            f"Internet Archive item '{identifier}' has no files — check the "
            "identifier (an unknown item returns an empty result, not a 404)."
        )

    prefix = unquote(path_prefix) if path_prefix else None

    candidates: list[PoolCandidate] = []
    for entry in files:
        name = entry.get("name")
        if not name or _is_metadata_file(name):
            continue
        if prefix and not name.startswith(prefix):
            continue
        if str(entry.get("format", "")).strip().lower() in _ARTEFACT_FORMATS:
            continue
        if originals_only and entry.get("source") != _ORIGINAL_SOURCE:
            continue

        size = entry.get("size")
        try:
            size_bytes = int(size) if size is not None else None
        except (TypeError, ValueError):
            size_bytes = None

        digests = {}
        for algo in ("md5", "sha1"):
            value = entry.get(algo)
            if value:
                digests[algo] = str(value).lower()

        candidates.append(
            PoolCandidate(
                url=(
                    f"https://archive.org/download/{identifier}/"
                    # The name is an item-internal path and routinely contains
                    # spaces; the URL has to carry them encoded or the fetch
                    # 404s on the VM.
                    + quote(name)
                ),
                name=name.rsplit("/", 1)[-1],
                size_bytes=size_bytes,
                digests=digests,
                provenance=f"internet-archive:{identifier}",
                path=name,
            )
        )
    return disambiguate_names(candidates)


def disambiguate_names(candidates: list[PoolCandidate]) -> list[PoolCandidate]:
    """Give repeated basenames a distinguishing ancestor directory.

    An item can hold many distinct files sharing one basename — measured on
    ``NIST_9-11_Release_20``, whose eleven DVD rips are all
    ``…/42A00<nn> - FBI#<n>/VIDEO_TS/VTS_01_1.VOB``: eleven different MD5s,
    one name. The pool keys on the locator and the digest, so those catalogue
    correctly and selection can never confuse them, but ``name`` is what
    ``import-history`` matches recorded uploads against and what the legacy
    ``urls=`` duplicate preflight compares — neither can tell eleven identical
    names apart.

    The nearest ancestor directory that makes the name unique is prepended, so
    the eleven become ``42A0061 - FBI#3 - VTS_01_1.VOB`` and so on. Intervening
    directories that discriminate nothing (``VIDEO_TS`` here) are skipped, and
    the full relative path is the last resort.

    **Only colliding names are touched.** Renaming unconditionally would change
    the name of every entry already catalogued from a listing, breaking exactly
    the ``import-history`` matching this exists to protect.
    """
    by_name: dict[str, list[PoolCandidate]] = {}
    for candidate in candidates:
        by_name.setdefault(candidate.name, []).append(candidate)

    for name, group in by_name.items():
        if len(group) < 2:
            continue
        # A rename must not land on a name something else already holds, so
        # every name outside this group counts against it too. Compared by
        # identity: two candidates can be field-equal without being the same
        # entry, and `in` over a list would make this quadratic besides.
        group_ids = {id(c) for c in group}
        taken = {c.name for c in candidates if id(c) not in group_ids}
        # Ancestors nearest-first: the parent, then its parent, and so on.
        depth = max(len(c.path.split("/")) for c in group)
        for level in range(2, depth + 1):
            proposed = [(c, _with_ancestor(c, level)) for c in group]
            names = [new for _, new in proposed]
            if len(set(names)) == len(group) and not (set(names) & taken):
                for candidate, new_name in proposed:
                    candidate.name = new_name
                break
        else:
            # No single ancestor separates them — either they are equally deep
            # under identically-named directories, or the name an ancestor
            # would produce is one another file already holds. The full path
            # always separates them, because a listing cannot offer one path
            # twice.
            proposed = [(c, c.path.replace("/", " - ")) for c in group]
            names = [new for _, new in proposed]
            if len(set(names)) == len(group) and not (set(names) & taken):
                for candidate, new_name in proposed:
                    candidate.name = new_name
    return candidates


def _with_ancestor(candidate: PoolCandidate, level: int) -> str:
    """``name`` prefixed with the ancestor ``level`` segments up, if there is one."""
    parts = candidate.path.split("/")
    if len(parts) < level:
        return candidate.name
    return f"{parts[-level]} - {candidate.name}"


def _is_metadata_file(name: str) -> bool:
    lowered = name.lower()
    if any(lowered.endswith(suffix) for suffix in _METADATA_SUFFIXES):
        return True
    # IA puts generated preview frames under `<item>.thumbs/`, which can be
    # hundreds of small images per item.
    return ".thumbs/" in lowered


SOURCES = {
    "internet-archive": fetch_internet_archive_item,
}
