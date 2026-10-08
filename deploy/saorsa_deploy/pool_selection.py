"""Resolve a manifest's size request into concrete files, once per deployment.

Every consumer that draws from the pool — ``prod uploaders``, ``prod canary``,
``prod local-uploads`` — needs the same three things, and each of them is a
place a naive implementation goes wrong:

**Resume safety.** ``deployments up`` is resumable, so a run that fails
mid-sequence is re-run. Drawing fresh files on the retry would permanently burn
inventory every time — a canary retried twice would consume ~1050 files to
produce one 350-file experiment. The selection is therefore persisted to S3 on
first draw and read back on every subsequent one.

**Failing before infrastructure exists.** A shortfall discovered after
Terraform has stood up VMs against a funded mainnet wallet costs real money and
leaves a half-built deployment. Selection runs in the same pre-infrastructure
slot the duplicate-file preflight already occupies.

**Naming the inventory in the error.** The two kinds are separate pools. "only
1 large available" is not actionable; "only 1 large available in the url pool"
tells the operator that their hundreds of large local files are irrelevant to a
remote uploader.
"""

from dataclasses import dataclass

from saorsa_db.models.prod_file_pool import ClaimSource, ClaimStatus, PoolFileKind
from saorsa_db.repositories import InsufficientPoolInventory
from saorsa_db.size_class import (
    bounded_byte_range,
    classify,
    describe_bounds,
    format_size,
    parse_size,
)
from saorsa_db.size_class import ordered as size_class_ordered
from saorsa_db.size_class import parse as parse_size_class

from saorsa_deploy.state import load_pool_selection, save_pool_selection


class PoolSelectionError(Exception):
    """Selection could not be satisfied. Message is operator-facing."""


@dataclass
class SelectedFile:
    file_id: int
    claim_id: int | None
    name: str
    locator: str
    size_bytes: int | None

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "claim_id": self.claim_id,
            "name": self.name,
            "locator": self.locator,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SelectedFile":
        return cls(
            file_id=data["file_id"],
            claim_id=data.get("claim_id"),
            name=data["name"],
            locator=data["locator"],
            size_bytes=data.get("size_bytes"),
        )


def parse_request(raw: dict) -> dict:
    """Normalise a ``{class_name: count}`` mapping into ``{SizeClass: int}``.

    Accepts the manifest form (``pool: {small: 3, medium: 2}``) and the CLI
    token form once split. Rejects unknown class names and non-positive counts
    loudly — a typo silently selecting nothing would produce an empty upload run
    that looks like it succeeded.
    """
    if not raw:
        raise PoolSelectionError(
            "an empty pool request selects no files; remove the `pool` key or "
            "give it at least one size class"
        )
    request: dict = {}
    for key, value in raw.items():
        size_class = parse_size_class(str(key))
        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            raise PoolSelectionError(
                f"pool request for '{key}' must be a whole number, got {value!r}"
            ) from exc
        if count < 0:
            raise PoolSelectionError(f"pool request for '{key}' cannot be negative ({count})")
        if count:
            request[size_class] = request.get(size_class, 0) + count
    if not request:
        raise PoolSelectionError("the pool request asks for zero files in every size class")
    return request


def parse_request_token(token: str) -> dict:
    """Parse a CLI spec token body such as ``small:3,medium:2``."""
    raw: dict = {}
    for part in token.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise PoolSelectionError(
                f"invalid pool request '{part}'. Expected class:count, e.g. small:3,medium:2"
            )
        name, count = part.split(":", 1)
        raw[name.strip()] = count.strip()
    return parse_request(raw)


def parse_size_bounds(
    min_raw: str | int | None, max_raw: str | int | None
) -> tuple[int | None, int | None]:
    """Parse optional ``min_size``/``max_size`` bounds into bytes.

    Both are operator-supplied strings such as ``5MB``. ``min`` is inclusive and
    ``max`` exclusive, so ``10MB``/``100MB`` is exactly the ``small`` class.
    """
    min_bytes = None
    max_bytes = None
    if min_raw is not None and str(min_raw).strip() != "":
        try:
            min_bytes = parse_size(min_raw)
        except ValueError as exc:
            raise PoolSelectionError(f"invalid min_size: {exc}") from exc
    if max_raw is not None and str(max_raw).strip() != "":
        try:
            max_bytes = parse_size(max_raw)
        except ValueError as exc:
            raise PoolSelectionError(f"invalid max_size: {exc}") from exc
    if max_bytes is not None and max_bytes == 0:
        raise PoolSelectionError("max_size of 0 selects nothing")
    if min_bytes is not None and max_bytes is not None and min_bytes >= max_bytes:
        raise PoolSelectionError(
            f"min_size ({format_size(min_bytes)}) must be below max_size "
            f"({format_size(max_bytes)}); max_size is exclusive"
        )
    return min_bytes, max_bytes


def validate_bounds(request: dict, min_size_bytes: int | None, max_size_bytes: int | None) -> None:
    """Refuse a request whose bounds exclude one of its own size classes.

    ``max_size: 5MB`` alongside ``small: 3`` can never be satisfied, and the
    honest moment to say so is now — not as a shortfall of "0 available", which
    reads as an empty pool and sends the operator off cataloguing files that
    would not have been selected either.
    """
    if min_size_bytes is None and max_size_bytes is None:
        return
    for size_class in size_class_ordered():
        count = request.get(size_class)
        if not count:
            continue
        if bounded_byte_range(size_class, min_size_bytes, max_size_bytes) is None:
            raise PoolSelectionError(
                f"the size bounds ({describe_bounds(min_size_bytes, max_size_bytes)}) "
                f"exclude the '{size_class.value}' class entirely, but the request "
                f"asks for {count} of them"
            )


def describe_request(
    request: dict,
    min_size_bytes: int | None = None,
    max_size_bytes: int | None = None,
) -> str:
    """Render a request in a stable, smallest-first order."""
    rendered = ", ".join(
        f"{size_class.value} {request[size_class]}"
        for size_class in size_class_ordered()
        if request.get(size_class)
    )
    bounds = describe_bounds(min_size_bytes, max_size_bytes)
    return f"{rendered} ({bounds})" if bounds else rendered


def selection_is_live(repo, selection: list["SelectedFile"]) -> bool:
    """True only while a persisted draw is still this run's to use.

    The persisted selection is keyed on the deployment **name**, and every
    prod deployment name is reused each release train — so a brand-new run
    finds the previous run's draw sitting under its own key. Returning it
    unchecked hands the run files that are already on the production network,
    which is exactly what happened to STG-UL-01 on 2026-08-18: it was handed
    the four files the 2026-08-11 run had uploaded, and only the duplicate-file
    gate stopped it re-uploading them with real money.

    A draw is live only while every file still carries the very claim that draw
    recorded, still open. Anything else means it is not ours to reuse:

    * the claim resolved to ``UPLOADED`` — an earlier run finished with it;
    * the claim is gone or was released — the file is back in the pool and may
      since have been drawn by someone else;
    * a *different* claim now blocks the file — another run holds it.

    Deliberately strict: a false "live" silently re-uploads spent files, while a
    false "stale" costs one extra draw from a pool that reports its inventory.
    """
    if not selection:
        return False
    if any(entry.claim_id is None for entry in selection):
        # Predates claim ids, or was hand-written. Ownership is unprovable, and
        # assuming it is ours is the failure this check exists to prevent.
        return False
    blocking = repo.blocking_claims_for_files([e.file_id for e in selection])
    for entry in selection:
        claim = blocking.get(entry.file_id)
        if claim is None or claim.id != entry.claim_id:
            return False
        if claim.status is not ClaimStatus.CLAIMED:
            return False
    return True


def select_for_deployment(
    repo,
    *,
    name: str,
    slot: str,
    kind: PoolFileKind,
    request: dict,
    source: ClaimSource,
    exclude_partial: bool = False,
    min_size_bytes: int | None = None,
    max_size_bytes: int | None = None,
) -> list[SelectedFile]:
    """Claim files for one slot of a deployment, or return the previous draw.

    ``slot`` distinguishes several draws within one deployment — a
    ``prod uploaders`` run has one per uploader VM. It is part of the persisted
    key so a resume returns each VM the same files it had.
    """
    persisted = load_pool_selection(name) or {}
    stored = persisted.get("slots", {}).get(slot)
    if stored is not None:
        previous = [SelectedFile.from_dict(entry) for entry in stored["files"]]
        if selection_is_live(repo, previous):
            return previous
        # The draw belongs to an earlier run that has already finished with
        # these files. Drop it and draw fresh rather than handing this run
        # files that are on the network.
        persisted.get("slots", {}).pop(slot, None)

    validate_bounds(request, min_size_bytes, max_size_bytes)

    try:
        claims = repo.claim_pool_files(
            kind,
            request,
            source,
            # Always for a canary — a loss measurement over content that is
            # already partly stored is not the measurement it reports, and no
            # manifest may opt into that. Optional for everything else: a run
            # testing whether an upload can be *sustained* over a slow link
            # learns nothing from a file that is 85% uploaded already, because
            # ant skips the chunks that are stored and only sends the rest.
            exclude_partial=(exclude_partial or source is ClaimSource.PROD_CANARY),
            min_size_bytes=min_size_bytes,
            max_size_bytes=max_size_bytes,
        )
    except InsufficientPoolInventory as exc:
        raise PoolSelectionError(_shortfall_message(exc)) from exc

    selected: list[SelectedFile] = []
    for claim in claims:
        pool_file = repo.get_pool_file(claim.file_id)
        selected.append(
            SelectedFile(
                file_id=claim.file_id,
                claim_id=claim.id,
                name=pool_file.name if pool_file else "<unknown>",
                # The locator the claim recorded, not the canonical row's:
                # a duplicate group can hold both a local path and a URL, and
                # this run needs the one its kind can actually reach.
                locator=claim.locator_used or (pool_file.locator if pool_file else ""),
                size_bytes=pool_file.size_bytes if pool_file else None,
            )
        )

    persisted.setdefault("slots", {})[slot] = {
        "kind": kind.value,
        "request": {k.value: v for k, v in request.items()},
        # Recorded so the persisted draw explains itself. A resume returns the
        # files already drawn whatever the manifest now says, so these describe
        # the selection that happened, not a filter applied on read.
        "min_size_bytes": min_size_bytes,
        "max_size_bytes": max_size_bytes,
        "files": [entry.to_dict() for entry in selected],
    }
    save_pool_selection(name, persisted)
    return selected


def _shortfall_message(exc: InsufficientPoolInventory) -> str:
    parts = []
    for size_class, detail in sorted(exc.shortfall.items()):
        parts.append(
            f"{size_class} {detail['requested']} requested, {detail['available']} available"
        )
    within = f" within {exc.bounds}" if getattr(exc, "bounds", "") else ""
    return (
        f"the {exc.kind.value} pool cannot satisfy this request{within} "
        f"({'; '.join(parts)}).\n"
        f"Add more with `saorsa-deploy prod pool add --from-listing "
        f"internet-archive:<item>`, or free a stale claim with "
        f"`saorsa-deploy prod pool claims --stale 7d`.\n"
        "No infrastructure was created."
    )


def attach_deployment(repo, selections: list[list[SelectedFile]], deployment_id, name):
    """Backfill the deployment on claims taken before its row existed."""
    if not deployment_id:
        return
    claim_ids = [
        entry.claim_id
        for selection in selections
        for entry in selection
        if entry.claim_id is not None
    ]
    repo.attach_deployment_to_claims(claim_ids, deployment_id, name)


def render_selection(
    console,
    slot_label: str,
    request: dict,
    selected: list,
    min_size_bytes: int | None = None,
    max_size_bytes: int | None = None,
) -> None:
    """Print a selection compactly.

    Summarised per size class rather than listed file by file, because a canary
    draws ~350 files and 350 lines of output buries everything around it. The
    full list is always recoverable from `prod pool claims`.
    """
    console.print(
        f"    {slot_label}:   {describe_request(request, min_size_bytes, max_size_bytes)}"
    )
    by_class: dict[str, list] = {}
    for entry in selected:
        key = classify(entry.size_bytes).value if entry.size_bytes is not None else "unknown"
        by_class.setdefault(key, []).append(entry)

    if len(selected) <= 8:
        for entry in selected:
            console.print(
                f"      #{entry.file_id}  {format_size(entry.size_bytes):>11}  {entry.name}"
            )
        return
    for size_class in size_class_ordered():
        entries = by_class.get(size_class.value)
        if not entries:
            continue
        total = sum(e.size_bytes or 0 for e in entries)
        console.print(
            f"      {size_class.value:<7} {len(entries):>4} file(s), {format_size(total)} total"
        )
    unknown = by_class.get("unknown")
    if unknown:
        console.print(f"      unknown {len(unknown):>4} file(s)")
