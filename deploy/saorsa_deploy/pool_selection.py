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

from saorsa_db.models.prod_file_pool import ClaimSource, PoolFileKind
from saorsa_db.repositories import InsufficientPoolInventory
from saorsa_db.size_class import classify, format_size
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
            raise PoolSelectionError(
                f"pool request for '{key}' cannot be negative ({count})"
            )
        if count:
            request[size_class] = request.get(size_class, 0) + count
    if not request:
        raise PoolSelectionError(
            "the pool request asks for zero files in every size class"
        )
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
                f"invalid pool request '{part}'. Expected class:count, "
                "e.g. small:3,medium:2"
            )
        name, count = part.split(":", 1)
        raw[name.strip()] = count.strip()
    return parse_request(raw)


def describe_request(request: dict) -> str:
    """Render a request in a stable, smallest-first order."""
    return ", ".join(
        f"{size_class.value} {request[size_class]}"
        for size_class in size_class_ordered()
        if request.get(size_class)
    )


def select_for_deployment(
    repo,
    *,
    name: str,
    slot: str,
    kind: PoolFileKind,
    request: dict,
    source: ClaimSource,
    exclude_partial: bool = False,
) -> list[SelectedFile]:
    """Claim files for one slot of a deployment, or return the previous draw.

    ``slot`` distinguishes several draws within one deployment — a
    ``prod uploaders`` run has one per uploader VM. It is part of the persisted
    key so a resume returns each VM the same files it had.
    """
    persisted = load_pool_selection(name) or {}
    if slot in persisted.get("slots", {}):
        return [
            SelectedFile.from_dict(entry)
            for entry in persisted["slots"][slot]["files"]
        ]

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
            exclude_partial=(
                exclude_partial or source is ClaimSource.PROD_CANARY
            ),
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
                locator=claim.locator_used
                or (pool_file.locator if pool_file else ""),
                size_bytes=pool_file.size_bytes if pool_file else None,
            )
        )

    persisted.setdefault("slots", {})[slot] = {
        "kind": kind.value,
        "request": {k.value: v for k, v in request.items()},
        "files": [entry.to_dict() for entry in selected],
    }
    save_pool_selection(name, persisted)
    return selected


def _shortfall_message(exc: InsufficientPoolInventory) -> str:
    parts = []
    for size_class, detail in sorted(exc.shortfall.items()):
        parts.append(
            f"{size_class} {detail['requested']} requested, "
            f"{detail['available']} available"
        )
    return (
        f"the {exc.kind.value} pool cannot satisfy this request "
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


def render_selection(console, slot_label: str, request: dict, selected: list) -> None:
    """Print a selection compactly.

    Summarised per size class rather than listed file by file, because a canary
    draws ~350 files and 350 lines of output buries everything around it. The
    full list is always recoverable from `prod pool claims`.
    """
    console.print(f"    {slot_label}:   {describe_request(request)}")
    by_class: dict[str, list] = {}
    for entry in selected:
        key = (
            classify(entry.size_bytes).value
            if entry.size_bytes is not None
            else "unknown"
        )
        by_class.setdefault(key, []).append(entry)

    if len(selected) <= 8:
        for entry in selected:
            console.print(
                f"      #{entry.file_id}  {format_size(entry.size_bytes):>11}  "
                f"{entry.name}"
            )
        return
    for size_class in size_class_ordered():
        entries = by_class.get(size_class.value)
        if not entries:
            continue
        total = sum(e.size_bytes or 0 for e in entries)
        console.print(
            f"      {size_class.value:<7} {len(entries):>4} file(s), "
            f"{format_size(total)} total"
        )
    unknown = by_class.get("unknown")
    if unknown:
        console.print(f"      unknown {len(unknown):>4} file(s)")
