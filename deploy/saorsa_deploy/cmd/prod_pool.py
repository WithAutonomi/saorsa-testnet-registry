"""Manage the pool of real files available for production Autonomi uploads.

Production uploads must use real files, and a file must never be uploaded
twice — a canary measuring chunk loss on content that has been on the network
for months is measuring nothing. This module is the operator's interface to the
catalogue and to the ledger of what has been handed out.

Two kinds of entry live in one catalogue, discriminated by ``kind``:

* ``url``   — a public URL. What a remote uploader VM can fetch.
* ``local`` — an absolute path on this machine. What ``prod local-uploads``
  and a desktop application can read.

Which kind a consumer draws from is decided by the consumer, never by a flag,
so no manifest can ask an uploader VM for a file on the operator's laptop.
"""

import hashlib
import sys
import urllib.parse
from pathlib import Path

import requests
from rich.console import Console
from saorsa_db.models.prod_file_pool import PoolFileKind
from saorsa_db.size_class import classify, format_size
from saorsa_db.size_class import ordered as size_class_ordered

from saorsa_deploy.registry import get_file_pool_repository

#: Seconds to wait for a size probe. Generous because Internet Archive
#: redirects to a node server that can be slow to answer a cold request.
PROBE_TIMEOUT_SECONDS = 30

#: Read size for local hashing. Large enough that hashing a multi-gigabyte file
#: is bound by the disk rather than by Python loop overhead.
HASH_CHUNK_BYTES = 1 << 20

DOWNLOAD_TIMEOUT_SECONDS = 600


def _require_repo(console: Console):
    repo = get_file_pool_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] No DATABASE_URL configured. The file "
            "pool lives in the registry database, so there is nothing to read "
            "or write without it."
        )
        sys.exit(1)
    return repo


def _url_name(url: str, fallback_index: int = 0) -> str:
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name if parsed.path else ""
    if not name or name in (".", ".."):
        return f"download-{fallback_index}.bin"
    return name


def probe_url_size(url: str) -> int | None:
    """Return the size a URL advertises, or None if it will not say.

    Tries HEAD first, then a streamed GET whose body is never read — some hosts
    (including Internet Archive node servers) answer HEAD with a redirect or a
    405 while answering GET perfectly well.

    Deliberately does NOT infer a digest from ``ETag``. An ETag is often an MD5
    for simple objects and something else entirely for multipart uploads, and a
    wrong digest is worse than no digest: it would link two unrelated files as
    duplicates and silently retire one of them. Real checksums come from the
    source's own metadata — see ``pool_sources``.
    """
    for method in ("head", "get"):
        try:
            response = requests.request(
                method,
                url,
                allow_redirects=True,
                timeout=PROBE_TIMEOUT_SECONDS,
                stream=(method == "get"),
            )
            try:
                if response.status_code >= 400:
                    continue
                length = response.headers.get("Content-Length")
                if length and length.isdigit():
                    return int(length)
            finally:
                response.close()
        except requests.RequestException:
            continue
    return None


def hash_local_file(path: Path) -> dict[str, str]:
    """SHA1 and SHA256 of a local file, computed in a single read.

    SHA1 is not there for its own sake — it is the algorithm the Internet
    Archive publishes, and two entries can only be recognised as the same
    content if they share one. SHA256 is carried alongside as the stronger
    identity for everything else.
    """
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            sha1.update(chunk)
            sha256.update(chunk)
    return {"sha1": sha1.hexdigest(), "sha256": sha256.hexdigest()}


def _download(url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        response.raise_for_status()
        with open(dest, "wb") as handle:
            for chunk in response.iter_content(chunk_size=HASH_CHUNK_BYTES):
                if chunk:
                    handle.write(chunk)
                    size += len(chunk)
    return size


class _AddReport:
    """Tally of one add run, rendered once at the end.

    Counted rather than printed per entry because a bulk add can cover
    thousands of files, and a wall of output buries the two lines that matter:
    what could not be sized, and what was linked as a duplicate.
    """

    def __init__(self):
        self.added: list = []
        self.skipped_existing = 0
        self.linked_duplicates: list[tuple[str, int]] = []
        self.unknown_size: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def render(self, console: Console) -> None:
        by_class: dict[str, int] = {}
        for pool_file in self.added:
            if pool_file.size_bytes is None:
                continue
            key = classify(pool_file.size_bytes).value
            by_class[key] = by_class.get(key, 0) + 1
        spread = ", ".join(
            f"{cls} {by_class[cls]}"
            for cls in (c.value for c in size_class_ordered())
            if by_class.get(cls)
        )
        console.print()
        console.print(f"  added    {len(self.added):>4}" + (f"   {spread}" if spread else ""))
        if self.skipped_existing:
            console.print(
                f"  skipped  {self.skipped_existing:>4}   already in the pool"
            )
        if self.linked_duplicates:
            console.print(
                f"  linked   {len(self.linked_duplicates):>4}   same content as an "
                "existing entry; claiming either spends both"
            )
            for name, other_id in self.linked_duplicates[:10]:
                console.print(f"[dim]             {name} → #{other_id}[/dim]")
            if len(self.linked_duplicates) > 10:
                console.print(
                    f"[dim]             … and {len(self.linked_duplicates) - 10} more[/dim]"
                )
        if self.unknown_size:
            # Loud, not incidental: these entries are invisible to every
            # size-class request, so a pool that looks full can still fail to
            # satisfy a canary.
            console.print(
                f"[yellow]  no size {len(self.unknown_size):>4}   added without a "
                "size — INVISIBLE to size-class selection until `pool verify` "
                "resolves it[/yellow]"
            )
            for url in self.unknown_size[:10]:
                console.print(f"[dim]             {url}[/dim]")
        if self.failed:
            console.print(f"[red]  failed   {len(self.failed):>4}[/red]")
            for locator, reason in self.failed[:10]:
                console.print(f"[red]             {locator}: {reason}[/red]")


def _add_url(
    repo, console: Console, report: _AddReport, url: str, *, tags, provenance,
    allow_unknown_size: bool, digests: dict[str, str] | None = None,
) -> None:
    existing = repo.get_pool_file_by_locator(url)
    if existing is not None:
        report.skipped_existing += 1
        return

    size = probe_url_size(url)
    if size is None and not allow_unknown_size:
        report.failed.append(
            (url, "no Content-Length; re-run with --allow-unknown-size to add anyway")
        )
        return

    pool_file, created = repo.add_pool_file(
        PoolFileKind.URL,
        url,
        _url_name(url),
        size_bytes=size,
        digests=digests,
        tags=tags,
        provenance=provenance,
    )
    if not created:
        report.skipped_existing += 1
        return
    report.added.append(pool_file)
    if size is None:
        report.unknown_size.append(url)
    if pool_file.duplicate_of:
        report.linked_duplicates.append((pool_file.name, pool_file.duplicate_of))


def _add_local(
    repo, console: Console, report: _AddReport, path: Path, *, tags, provenance,
    do_hash: bool,
) -> None:
    locator = str(path.resolve())
    if repo.get_pool_file_by_locator(locator) is not None:
        report.skipped_existing += 1
        return
    try:
        size = path.stat().st_size
    except OSError as exc:
        report.failed.append((locator, str(exc)))
        return

    digests = None
    if do_hash:
        try:
            digests = hash_local_file(path)
        except OSError as exc:
            report.failed.append((locator, f"could not hash: {exc}"))
            return

    pool_file, created = repo.add_pool_file(
        PoolFileKind.LOCAL,
        locator,
        path.name,
        size_bytes=size,
        digests=digests,
        tags=tags,
        provenance=provenance,
    )
    if not created:
        report.skipped_existing += 1
        return
    report.added.append(pool_file)
    if pool_file.duplicate_of:
        report.linked_duplicates.append((pool_file.name, pool_file.duplicate_of))


def _read_url_list(path: Path) -> list[str]:
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def cmd_prod_pool_add(args) -> None:
    console = Console()
    repo = _require_repo(console)

    tags = list(args.tag) if getattr(args, "tag", None) else None
    provenance = getattr(args, "provenance", None)
    report = _AddReport()

    sources = [
        bool(getattr(args, "url", None)),
        bool(getattr(args, "urls_file", None)),
        bool(getattr(args, "dir", None)),
        bool(getattr(args, "path", None)),
        bool(getattr(args, "from_listing", None)),
    ]
    if sum(1 for s in sources if s) != 1:
        console.print(
            "[bold red]Error:[/bold red] pass exactly one of --url, "
            "--urls-file, --dir, --path or --from-listing."
        )
        sys.exit(1)

    if getattr(args, "from_listing", None):
        _add_from_listing(repo, console, report, args, tags, provenance)
    elif args.url or args.urls_file:
        urls = list(args.url) if args.url else _read_url_list(Path(args.urls_file))
        if not urls:
            console.print("[yellow]No URLs to add.[/yellow]")
            return
        if args.materialise:
            _materialise_urls(repo, console, report, urls, args, tags, provenance)
        else:
            console.print(f"Resolving {len(urls)} URL(s)…")
            for url in urls:
                _add_url(
                    repo,
                    console,
                    report,
                    url,
                    tags=tags,
                    provenance=provenance,
                    allow_unknown_size=args.allow_unknown_size,
                )
    elif args.dir:
        directory = Path(args.dir).resolve()
        if not directory.is_dir():
            console.print(f"[bold red]Error:[/bold red] {directory} is not a directory.")
            sys.exit(1)
        files = sorted(p for p in directory.rglob("*") if p.is_file())
        console.print(f"Scanning {directory} — {len(files)} file(s)…")
        if not args.hash:
            console.print(
                "[yellow]--no-hash:[/yellow] entries will carry no digest, so "
                "they cannot be recognised as the same content as a URL entry."
            )
        for path in files:
            _add_local(
                repo, console, report, path,
                tags=tags, provenance=provenance, do_hash=args.hash,
            )
    else:
        path = Path(args.path).resolve()
        if not path.is_file():
            console.print(f"[bold red]Error:[/bold red] {path} is not a file.")
            sys.exit(1)
        _add_local(
            repo, console, report, path,
            tags=tags, provenance=provenance, do_hash=args.hash,
        )

    report.render(console)


def _materialise_urls(repo, console, report, urls, args, tags, provenance) -> None:
    """Download each URL and catalogue the local copy instead of the URL.

    Sidesteps the duplicate problem entirely rather than defending against it:
    one piece of content becomes one row with a hash we computed ourselves, so
    there is no second entry that could be handed out separately.
    """
    target = Path(args.materialise).resolve()
    console.print(f"Materialising {len(urls)} URL(s) into {target}…")
    for index, url in enumerate(urls, start=1):
        dest = target / _url_name(url, index)
        if dest.exists():
            console.print(f"[dim]  exists, not re-fetching: {dest.name}[/dim]")
        else:
            try:
                _download(url, dest)
            except Exception as exc:
                report.failed.append((url, f"download failed: {exc}"))
                continue
        _add_local(
            repo, console, report, dest,
            tags=tags,
            # Keep the origin: a materialised file is only auditable if we can
            # still say where its bytes came from.
            provenance=provenance or url,
            do_hash=True,
        )


def format_pool_file_line(pool_file) -> str:
    size = format_size(pool_file.size_bytes)
    size_class = (
        classify(pool_file.size_bytes).value
        if pool_file.size_bytes is not None
        else "unknown"
    )
    return (
        f"#{pool_file.id}  {pool_file.kind.value:<6} {size_class:<7} "
        f"{size:>11}  {pool_file.name}"
    )


# =============================================================================
# list / show / stats
# =============================================================================


def _parse_since(value: str | None):
    """Parse a --since value: an ISO date/datetime, or a bare Nd/Nh duration."""
    if not value:
        return None
    from datetime import datetime, timedelta, timezone

    text = value.strip().lower()
    if text.endswith(("d", "h")) and text[:-1].isdigit():
        amount = int(text[:-1])
        delta = timedelta(days=amount) if text.endswith("d") else timedelta(hours=amount)
        return datetime.now(timezone.utc) - delta
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _claim_owner(claim) -> str:
    """How a claim identifies itself in a listing.

    A deployment-backed claim names its run — the id matters because deployment
    names are reused, so the name alone cannot identify which run took the file.
    A manual claim shows only its free-text label.
    """
    if claim is None:
        return ""
    if claim.deployment_name:
        suffix = f" (id={claim.deployment_id})" if claim.deployment_id else ""
        return f"{claim.source.value} · {claim.deployment_name}{suffix}"
    return claim.label or claim.source.value


def _state_of(claim) -> str:
    if claim is None:
        return "available"
    return claim.status.value


def cmd_prod_pool_list(args) -> None:
    from rich.table import Table
    from saorsa_db.size_class import parse as parse_size_class

    console = Console()
    repo = _require_repo(console)

    kind = PoolFileKind(args.kind) if getattr(args, "kind", None) else None

    try:
        # Inside the guard: an unknown size class or status must report itself,
        # not raise a traceback at the operator.
        size_class = (
            parse_size_class(args.size) if getattr(args, "size", None) else None
        )
        rows = repo.list_pool_files(
            kind=kind,
            size_class=size_class,
            status=getattr(args, "status", None),
            tags=list(args.tag) if getattr(args, "tag", None) else None,
            active=None if getattr(args, "all", False) else True,
            limit=getattr(args, "limit", None),
            added_since=_parse_since(getattr(args, "since", None)),
        )
    except ValueError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    if not rows:
        console.print("[yellow]No pool files match.[/yellow]")
        return

    claims = repo.blocking_claims_for_files([r.id for r in rows])
    # A partial claim does not block, so it is invisible to the lookup above.
    # Without this a file with chunks already stored renders identically to one
    # nobody has ever touched.
    partial_ids = repo.files_with_partial_claims([r.id for r in rows])

    table = Table(box=None, pad_edge=False)
    table.add_column("FILE", style="bold")
    table.add_column("KIND")
    table.add_column("CLASS")
    table.add_column("SIZE", justify="right")
    table.add_column("NAME")
    table.add_column("STATE")
    table.add_column("BY")
    show_address = getattr(args, "status", None) == "uploaded"
    if show_address:
        table.add_column("ADDRESS")

    unknown_size = 0
    partial_shown = 0
    for row in rows:
        claim = claims.get(row.id)
        if row.size_bytes is None:
            unknown_size += 1
        state = _state_of(claim)
        # Marked once fully uploaded the partial history is superseded — the
        # file IS on the network, so flagging it would only confuse.
        if row.id in partial_ids and state != "uploaded":
            state = f"{state}*"
            partial_shown += 1
        cells = [
            f"#{row.id}",
            row.kind.value,
            classify(row.size_bytes).value if row.size_bytes is not None else "?",
            format_size(row.size_bytes),
            row.name,
            state,
            _claim_owner(claim),
        ]
        if show_address:
            cells.append((claim.address if claim else None) or "—")
        table.add_row(*cells)

    console.print(table)
    console.print()
    console.print(f"  {len(rows)} shown")
    if partial_shown:
        console.print(
            "[dim]  * partially uploaded — some chunks are already stored. "
            "Still drawable by uploaders (completing it is useful work), but "
            "excluded from canary selection.[/dim]"
        )
    if unknown_size:
        # Never let these pass as ordinary rows: they cannot satisfy any
        # size-class request, so a pool that looks stocked may not be.
        console.print(
            f"[yellow]  {unknown_size} of them have no resolved size and are "
            "INVISIBLE to size-class selection — run `prod pool verify`.[/yellow]"
        )


def cmd_prod_pool_show(args) -> None:
    console = Console()
    repo = _require_repo(console)

    if getattr(args, "address", None):
        pool_file = repo.get_pool_file_by_address(args.address)
        if pool_file is None:
            console.print(
                f"[yellow]No pooled file recorded against address "
                f"{args.address}.[/yellow]"
            )
            return
    else:
        pool_file = repo.get_pool_file(args.file_id)
        if pool_file is None:
            console.print(f"[bold red]Error:[/bold red] no pool file #{args.file_id}.")
            sys.exit(1)

    claim = repo.open_claim_for_file(pool_file.id)
    # A partial claim does not block, so open_claim_for_file returns None for
    # it. Falling back here is what stops a file with chunks already stored
    # rendering as though nothing had ever happened to it.
    partial_claim = (
        repo.latest_partial_claim_for_file(pool_file.id) if claim is None else None
    )
    size_class = (
        classify(pool_file.size_bytes).value
        if pool_file.size_bytes is not None
        else "unknown"
    )

    console.print()
    console.print(f"  [bold]#{pool_file.id}  {pool_file.name}[/bold]")
    console.print(
        f"        kind      {pool_file.kind.value:<8} {size_class:<8} "
        f"{format_size(pool_file.size_bytes)}"
    )
    console.print(f"        locator   {pool_file.locator}")
    for digest in sorted(pool_file.digests, key=lambda d: d.algo):
        console.print(f"        {digest.algo:<9} {digest.value}")
    if pool_file.tags:
        console.print(f"        tags      {', '.join(pool_file.tags)}")
    if pool_file.provenance:
        console.print(f"        from      {pool_file.provenance}")
    if pool_file.duplicate_of:
        console.print(
            f"        duplicate of #{pool_file.duplicate_of} — same content, so "
            "claiming either spends both"
        )
    if not pool_file.active:
        console.print("        [yellow]retired (inactive)[/yellow]")

    if partial_claim is not None:
        console.print("        status    available (partially uploaded before)")
        stored = partial_claim.chunks_stored
        total = partial_claim.total_chunks
        if total:
            console.print(
                f"        progress  {stored or 0} of {total} chunk(s) stored on "
                "an earlier attempt"
            )
        else:
            console.print(
                "        progress  an earlier upload stored some chunks and "
                "stopped"
            )
        when = _aware(partial_claim.resolved_at or partial_claim.claimed_at)
        console.print(
            f"        attempt   {_claim_owner(partial_claim)}"
            f"        {when:%Y-%m-%d %H:%M}"
        )
        if partial_claim.address:
            console.print(f"        address   {partial_claim.address}")
        console.print(
            "[dim]        Drawable by uploaders — completing it is useful work "
            "— but excluded from canary selection.[/dim]"
        )
        console.print()
        return

    console.print(f"        status    {_state_of(claim)}")
    if claim is not None:
        owner = _claim_owner(claim)
        when = claim.resolved_at or claim.claimed_at
        console.print(f"        {'run' if claim.deployment_name else 'by':<9} {owner}"
                      f"        {when:%Y-%m-%d %H:%M}")
        if claim.locator_used and claim.locator_used != pool_file.locator:
            console.print(f"        fetched   {claim.locator_used}")
        if claim.address:
            console.print(f"        address   {claim.address}")
        if claim.total_chunks is not None:
            console.print(
                f"        chunks    {claim.chunks_stored} stored / "
                f"{claim.total_chunks} total, {claim.chunks_failed or 0} failed"
            )
        if claim.storage_cost_atto is not None:
            console.print(
                f"        storage   {_atto_to_ant(claim.storage_cost_atto)} ANT   "
                f"({_raw_units(claim.storage_cost_atto)} atto)"
            )
        if claim.gas_cost_wei is not None:
            console.print(
                f"        gas       {_atto_to_ant(claim.gas_cost_wei)} ETH   "
                f"({_raw_units(claim.gas_cost_wei)} wei)"
            )
        if claim.note:
            console.print(f"        note      {claim.note}")
    console.print()


def _raw_units(value) -> str:
    """Render a stored atto/wei amount as a bare integer.

    These are integer quantities, but the column is NUMERIC and can come back
    carrying a fractional tail (SQLite, which has no NUMERIC type, is the usual
    source). Printing that tail would make the stored truth look imprecise
    exactly where precision is the point.
    """
    from decimal import Decimal

    return f"{Decimal(value):.0f}"


def _atto_to_ant(value) -> str:
    """Render an atto/wei integer in whole units.

    Both ANT and ETH use 18 decimal places. The raw integer is printed
    alongside everywhere this is used, because it is the stored truth and this
    rendering is lossy by design.
    """
    from decimal import Decimal

    quantity = Decimal(value) / Decimal(10**18)
    return f"{quantity.normalize():f}"


def cmd_prod_pool_stats(args) -> None:
    from rich.table import Table

    console = Console()
    repo = _require_repo(console)
    stats = repo.pool_stats()

    table = Table(box=None, pad_edge=False)
    table.add_column("SIZE CLASS")
    for kind in PoolFileKind:
        table.add_column(f"{kind.value.upper()} avail/total", justify="right")

    for size_class in size_class_ordered():
        cells = [size_class.value]
        for kind in PoolFileKind:
            entry = stats[kind.value]["by_class"][size_class.value]
            cells.append(f"{entry['available']} / {entry['total']}")
        table.add_row(*cells)

    console.print()
    console.print(table)

    unknown = {k: stats[k]["unknown_size"] for k in stats}
    if any(unknown.values()):
        console.print()
        for kind_value, count in unknown.items():
            if count:
                console.print(
                    f"[yellow]  {count} {kind_value} entr"
                    f"{'y' if count == 1 else 'ies'} have no resolved size and "
                    "cannot satisfy any size request.[/yellow]"
                )
    console.print()
    # The two kinds are separate inventories and are never summed: a remote
    # uploader VM cannot use a local path, and a local upload cannot use a URL.
    console.print(
        "[dim]  URL entries serve prod uploaders and canaries; LOCAL entries "
        "serve local-uploads and the desktop application.[/dim]"
    )


# =============================================================================
# checkout / claims / history / release / mark-uploaded
#
# Everything here is addressed by FILE id, never claim id. A file carries at
# most one blocking claim, so a claim id is an implementation detail — and
# showing an operator two different ids for one thing is how they end up
# resolving the wrong one.
# =============================================================================


def _aware(value):
    """Treat a naive timestamp as UTC.

    SQLite has no timezone type, so a DateTime(timezone=True) column reads back
    naive there while Postgres preserves the offset. Age arithmetic has to work
    on both.
    """
    from datetime import timezone as _tz

    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=_tz.utc)


def _format_age(claimed_at, now) -> str:
    delta = now - _aware(claimed_at)
    seconds = int(delta.total_seconds())
    if seconds < 3600:
        return f"{max(seconds // 60, 0)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _print_claims_table(console, repo, claims, *, now, title=None) -> None:
    from rich.table import Table

    if title:
        console.print(title)
    table = Table(box=None, pad_edge=False)
    table.add_column("FILE", style="bold")
    table.add_column("CLASS")
    table.add_column("SIZE", justify="right")
    table.add_column("NAME")
    table.add_column("OWNER")
    table.add_column("STATE")
    table.add_column("AGE", justify="right")

    for claim in claims:
        pool_file = repo.get_pool_file(claim.file_id)
        size_bytes = pool_file.size_bytes if pool_file else None
        table.add_row(
            f"#{claim.file_id}",
            classify(size_bytes).value if size_bytes is not None else "?",
            format_size(size_bytes),
            pool_file.name if pool_file else "<deleted>",
            _claim_owner(claim),
            claim.status.value,
            _format_age(claim.claimed_at, now),
        )
    console.print(table)


def cmd_prod_pool_checkout(args) -> None:
    from datetime import datetime, timezone

    from saorsa_db.models.prod_file_pool import ClaimSource
    from saorsa_db.repositories import InsufficientPoolInventory
    from saorsa_db.size_class import parse as parse_size_class

    console = Console()
    repo = _require_repo(console)
    now = datetime.now(timezone.utc)

    kind = PoolFileKind(args.kind)
    label = args.consumer or "manual"

    try:
        size_class = parse_size_class(args.size)
    except ValueError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    existing = [
        claim
        for claim in repo.list_pool_claims(label=label)
        if claim.is_open
    ]

    if args.reuse_open:
        if not existing:
            console.print(
                f"[yellow]'{label}' has no open claims to reuse.[/yellow] "
                "Drop --reuse-open to claim a new file."
            )
            return
        _print_claims_table(
            console, repo, existing, now=now,
            title=f"\nReusing the open claim(s) already held by '{label}':\n",
        )
        _print_resolution_hint(console, existing)
        return

    if existing:
        # Never block — checking out several deliberately is normal — but make
        # the pile visible, so an operator cannot drift into a dozen forgotten
        # claims without noticing.
        console.print(
            f"\n[yellow]Note:[/yellow] '{label}' has {len(existing)} unresolved "
            f"claim(s): "
            + ", ".join(
                f"#{c.file_id} ({_format_age(c.claimed_at, now)})" for c in existing[:5]
            )
        )
        console.print(
            f"[dim]      Resolve them with: prod pool claims --consumer {label}[/dim]"
        )

    try:
        claims = repo.claim_pool_files(
            kind,
            {size_class: args.count},
            ClaimSource.MANUAL,
            label=label,
            when=now,
        )
    except InsufficientPoolInventory as exc:
        console.print(f"\n[bold red]Error:[/bold red] {exc}")
        console.print(
            "[dim]Add more with `prod pool add`, or free a stale claim "
            "(`prod pool claims --stale 7d`).[/dim]"
        )
        sys.exit(1)

    console.print(f"\nClaimed {len(claims)} file(s) for '{label}':\n")
    for claim in claims:
        pool_file = repo.get_pool_file(claim.file_id)
        console.print(f"  {format_pool_file_line(pool_file)}")
        console.print(f"        {claim.locator_used or pool_file.locator}")
        for digest in sorted(pool_file.digests, key=lambda d: d.algo):
            console.print(f"[dim]        {digest.algo} {digest.value}[/dim]")
    console.print(
        "\nThese file(s) are now unavailable to every other consumer."
    )
    _print_resolution_hint(console, claims)


def _print_resolution_hint(console, claims) -> None:
    ids = " ".join(str(c.file_id) for c in claims)
    console.print("When the upload finishes:")
    console.print(
        f"  saorsa-deploy prod pool mark-uploaded {claims[0].file_id} "
        "--address <datamap-address>"
    )
    console.print("If it did not happen:")
    console.print(f"  saorsa-deploy prod pool release {ids}")


def cmd_prod_pool_claims(args) -> None:
    from datetime import datetime, timezone

    from saorsa_db.models.prod_file_pool import (
        SELF_RESOLVING_SOURCES,
        ClaimSource,
        ClaimStatus,
    )

    console = Console()
    repo = _require_repo(console)
    now = datetime.now(timezone.utc)

    stale_before = None
    if getattr(args, "stale", None):
        parsed = _parse_since(args.stale)
        stale_before = parsed

    status = None if getattr(args, "all", False) else ClaimStatus.CLAIMED
    if getattr(args, "status", None):
        status = ClaimStatus(args.status)

    claims = repo.list_pool_claims(
        status=status,
        source=ClaimSource(args.source) if getattr(args, "source", None) else None,
        label=getattr(args, "consumer", None),
        stale_before=stale_before,
        unresolvable=getattr(args, "unresolvable", False),
    )

    if stale_before is not None and not getattr(args, "include_deployments", False):
        # A deployment-backed claim awaiting `record-results` is not forgotten,
        # it is merely pending. Listing those beside genuinely stale manual
        # checkouts trains the operator to ignore the list that also carries
        # the real ones.
        claims = [c for c in claims if c.source not in SELF_RESOLVING_SOURCES]

    if not claims:
        console.print("[green]No matching claims.[/green]")
        return

    _print_claims_table(console, repo, claims, now=now, title="")

    pending = [c for c in claims if c.is_open and c.source in SELF_RESOLVING_SOURCES]
    manual = [c for c in claims if c.is_open and c.source not in SELF_RESOLVING_SOURCES]
    console.print()
    if pending:
        console.print(
            f"[dim]  {len(pending)} will resolve on the next "
            "`deployments record-results` for their deployment.[/dim]"
        )
    if manual:
        console.print(
            f"  {len(manual)} need you:  prod pool mark-uploaded <file> "
            "--address <datamap>"
        )
        console.print("                     prod pool release <file>")
    if getattr(args, "unresolvable", False) and claims:
        # Their InfluxDB went with the deployment, so no recording run can ever
        # close them. Saying so is the difference between a wedged claim and a
        # known one.
        console.print(
            "[yellow]  These cannot resolve themselves — their deployment is "
            "gone, and its results with it.[/yellow]"
        )


def cmd_prod_pool_history(args) -> None:
    from datetime import datetime, timezone

    console = Console()
    repo = _require_repo(console)
    now = datetime.now(timezone.utc)

    pool_file = repo.get_pool_file(args.file_id)
    if pool_file is None:
        console.print(f"[bold red]Error:[/bold red] no pool file #{args.file_id}.")
        sys.exit(1)

    canonical = repo.canonical_pool_file_id(pool_file.id)
    claims = repo.list_pool_claims(file_id=canonical)
    console.print()
    console.print(f"  [bold]#{pool_file.id}  {pool_file.name}[/bold]")
    if canonical != pool_file.id:
        console.print(
            f"[dim]  history is kept against #{canonical}, the canonical entry "
            "for this content[/dim]"
        )
    if not claims:
        console.print("  never handed out")
        console.print()
        return
    for claim in sorted(claims, key=lambda c: c.claimed_at):
        when = _aware(claim.claimed_at)
        console.print(
            f"  {when:%Y-%m-%d %H:%M}  claimed by {_claim_owner(claim)} "
            f"({_format_age(claim.claimed_at, now)} ago)"
        )
        if claim.resolved_at:
            resolved = _aware(claim.resolved_at)
            detail = f" address {claim.address}" if claim.address else ""
            console.print(
                f"  {resolved:%Y-%m-%d %H:%M}  → {claim.status.value}{detail}"
            )
        if claim.note:
            console.print(f"[dim]                      note: {claim.note}[/dim]")
    console.print()


def _open_claim_or_exit(repo, console, file_id):
    pool_file = repo.get_pool_file(file_id)
    if pool_file is None:
        console.print(f"[bold red]Error:[/bold red] no pool file #{file_id}.")
        sys.exit(1)
    claim = repo.open_claim_for_file(file_id)
    if claim is None:
        console.print(
            f"[bold red]Error:[/bold red] #{file_id} ({pool_file.name}) has no "
            "open claim — it was never handed out, or was already resolved."
        )
        sys.exit(1)
    return pool_file, claim


def cmd_prod_pool_release(args) -> None:
    console = Console()
    repo = _require_repo(console)
    for file_id in args.file_ids:
        pool_file, claim = _open_claim_or_exit(repo, console, file_id)
        try:
            repo.release_pool_claim(claim.id, note=getattr(args, "note", None))
        except ValueError as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
            sys.exit(1)
        console.print(
            f"Released #{file_id} ({pool_file.name}) — available again."
        )


def cmd_prod_pool_mark_uploaded(args) -> None:
    from decimal import Decimal, InvalidOperation

    console = Console()
    repo = _require_repo(console)
    pool_file, claim = _open_claim_or_exit(repo, console, args.file_id)

    def _decimal(value):
        if value is None:
            return None
        try:
            return Decimal(value)
        except (InvalidOperation, ValueError):
            console.print(
                f"[bold red]Error:[/bold red] '{value}' is not a whole-number "
                "amount. Costs are given in atto/wei, not ANT/ETH."
            )
            sys.exit(1)

    repo.resolve_pool_claim(
        claim.id,
        address=args.address,
        chunks_stored=args.chunks_stored,
        chunks_failed=args.chunks_failed,
        total_chunks=args.total_chunks,
        storage_cost_atto=_decimal(getattr(args, "storage_cost_atto", None)),
        gas_cost_wei=_decimal(getattr(args, "gas_cost_wei", None)),
        note=getattr(args, "note", None),
    )
    console.print(
        f"Recorded upload of #{args.file_id} ({pool_file.name})"
        + (f" → {args.address}" if args.address else "")
        + "."
    )
    duplicates = repo.list_pool_files(active=None)
    also_spent = [
        f.id
        for f in duplicates
        if f.duplicate_of == claim.file_id and f.id != pool_file.id
    ]
    if also_spent:
        console.print(
            "[dim]  Also spent (same content): "
            + ", ".join(f"#{i}" for i in also_spent)
            + "[/dim]"
        )


# =============================================================================
# verify
# =============================================================================


def cmd_prod_pool_verify(args) -> None:
    """Re-check catalogued entries against reality.

    Public URLs rot and local files move. Discovering either mid-deploy — on a
    funded mainnet wallet, after infrastructure is up — is the expensive way to
    find out.
    """
    console = Console()
    repo = _require_repo(console)

    kind = PoolFileKind(args.kind) if getattr(args, "kind", None) else None
    rows = repo.list_pool_files(
        kind=kind, active=True, limit=getattr(args, "limit", None)
    )
    if not rows:
        console.print("[yellow]Nothing to verify.[/yellow]")
        return

    console.print(f"Checking {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}…")

    ok = 0
    resized: list[tuple] = []
    gone: list[tuple] = []
    undigested: list = []

    for row in rows:
        if not row.digests:
            undigested.append(row)

        if row.kind is PoolFileKind.URL:
            size = probe_url_size(row.locator)
            if size is None:
                # Cannot distinguish "gone" from "publishes no Content-Length"
                # by size alone, so only retire it if it also refuses to answer.
                if not _url_reachable(row.locator):
                    gone.append((row, "unreachable"))
                    repo.set_pool_file_active(row.id, False)
                else:
                    ok += 1
                continue
        else:
            path = Path(row.locator)
            if not path.is_file():
                gone.append((row, "missing"))
                repo.set_pool_file_active(row.id, False)
                continue
            size = path.stat().st_size
            if size != row.size_bytes and args.rehash:
                repo.add_pool_file_digests(row.id, hash_local_file(path))

        if size != row.size_bytes:
            resized.append((row, row.size_bytes, size))
            repo.update_pool_file_size(row.id, size)
        else:
            ok += 1

    console.print()
    console.print(f"  ok        {ok:>4}")
    if resized:
        console.print(f"  changed   {len(resized):>4}   size updated")
        for row, before, after in resized[:10]:
            # A size change can move an entry between classes, which changes
            # which requests can ever draw it.
            before_class = (
                classify(before).value if before is not None else "unknown"
            )
            after_class = classify(after).value
            note = (
                f"  [yellow](now {after_class}, was {before_class})[/yellow]"
                if after_class != before_class
                else ""
            )
            console.print(
                f"[dim]            #{row.id} {format_size(before)} → "
                f"{format_size(after)}[/dim]{note}"
            )
    if gone:
        console.print(f"[red]  gone      {len(gone):>4}   retired (set inactive)[/red]")
        for row, reason in gone[:10]:
            console.print(f"[red]            #{row.id} {row.name}: {reason}[/red]")
    if undigested:
        # The honest residual. These entries cannot be recognised as the same
        # content as anything else, so a URL and a local copy of one file could
        # both be handed out. Saying nothing would imply a coverage the tool
        # does not have.
        console.print(
            f"[yellow]  no digest {len(undigested):>4}   cannot be deduplicated "
            "against another entry before upload[/yellow]"
        )
        console.print(
            "[dim]            The datamap address catches these after the fact; "
            "`pool add --materialise` avoids the situation entirely.[/dim]"
        )


def _url_reachable(url: str) -> bool:
    for method in ("head", "get"):
        try:
            response = requests.request(
                method,
                url,
                allow_redirects=True,
                timeout=PROBE_TIMEOUT_SECONDS,
                stream=(method == "get"),
            )
            try:
                if response.status_code < 400:
                    return True
            finally:
                response.close()
        except requests.RequestException:
            continue
    return False


# =============================================================================
# import-history
# =============================================================================


def _history_rows(deployment_repo):
    """Every file already uploaded to production, from BOTH result tables.

    Canary uploads live in ``prod_canary_uploads``, NOT
    ``deployment_prod_upload_results`` — and they are the larger share (1006
    against 538 as at 2026-07-30). Reading only the latter, as the incumbent
    duplicate preflight does, would leave two thirds of production's contents
    invisible and let the pool re-issue files that are already stored.

    Yields ``(file_name, size_bytes, address, chunks, storage_atto, gas_wei,
    when, source_label)``.
    """
    from saorsa_db.models.prod_upload_result import ProdUploadStatus

    # Successes AND partials. A partial upload put real chunks in production,
    # so presenting that file as untouched would misreport what is stored — but
    # it is not spent either, since the file is not there in full.
    #
    # Two passes, because `list_all_prod_upload_results` partitions on success:
    # failed_only=False returns ONLY successes, so partials are reachable only
    # through the other branch, mixed in with genuine failures.
    rows = list(deployment_repo.list_all_prod_upload_results(failed_only=False))
    rows += [
        row
        for row in deployment_repo.list_all_prod_upload_results(failed_only=True)
        if row.status is ProdUploadStatus.PARTIAL_UPLOAD
    ]
    for row in rows:
        if row.status is ProdUploadStatus.SUCCESS:
            complete = True
        elif row.status is ProdUploadStatus.PARTIAL_UPLOAD:
            complete = False
        else:
            continue
        yield (
            row.file_name,
            row.file_size_bytes,
            row.address,
            (row.chunks_stored, row.chunks_failed, row.total_chunks),
            row.storage_cost_atto,
            row.gas_cost_wei,
            row.finished_at,
            "prod-uploaders",
            complete,
        )

    for row in deployment_repo.list_all_prod_canary_uploads():
        yield (
            row.file_name,
            row.file_size_bytes,
            row.top_address,
            (None, None, row.num_chunks),
            row.storage_cost_atto,
            row.gas_cost_wei,
            row.uploaded_at,
            "prod-canary",
            True,
        )


def cmd_prod_pool_import_history(args) -> None:
    """Mark pooled files that are already on the network as spent.

    Matching is by **file name**, because that is all the recorded history
    holds — there are no digests on those rows and no way to add them now. That
    is the same strength as the incumbent duplicate preflight, no more.

    The direction of the residual error is deliberate. A false match retires a
    usable file, which `pool release` undoes. A missed match re-uploads content
    already on the network, which for a canary silently ruins the measurement.
    So a name match is taken at face value, and anything suspicious about it is
    reported rather than used to skip.

    Idempotent and re-runnable: uploads made outside the pool keep happening,
    so this is not a one-shot migration.
    """
    from saorsa_db.models.prod_file_pool import ClaimSource, ClaimStatus

    from saorsa_deploy.registry import get_repository

    console = Console()
    repo = _require_repo(console)
    deployment_repo = get_repository()
    if deployment_repo is None:
        console.print(
            "[bold red]Error:[/bold red] No DATABASE_URL configured — there is "
            "no upload history to read."
        )
        sys.exit(1)

    history = list(_history_rows(deployment_repo))
    if not history:
        console.print("[yellow]No recorded production uploads to import.[/yellow]")
        return

    by_name: dict[str, list] = {}
    for pool_file in repo.list_pool_files(active=None):
        by_name.setdefault(pool_file.name, []).append(pool_file)

    imported = 0
    partial = 0
    already = 0
    unmatched = 0
    size_mismatch: list[tuple[str, int, int]] = []

    console.print(
        f"Checking {len(history)} recorded upload(s) against the pool"
        + (" [dim](dry run)[/dim]" if args.dry_run else "")
        + "…"
    )

    for (name, size, address, chunks, storage, gas, when, source, complete) in history:
        candidates = by_name.get(name)
        if not candidates:
            unmatched += 1
            continue
        target = candidates[0]
        if (
            size is not None
            and target.size_bytes is not None
            and size != target.size_bytes
        ):
            size_mismatch.append((name, target.size_bytes, size))

        if repo.open_claim_for_file(target.id) is not None:
            already += 1
            continue

        if args.dry_run:
            if complete:
                imported += 1
            else:
                partial += 1
            continue

        # Not `claim_pool_files`, which resolves a *size request* against
        # available inventory. Here the file is already known and already
        # uploaded, so the claim is seeded directly in its resolved state.
        canonical = repo.canonical_pool_file_id(target.id)
        claim = repo.seed_uploaded_claim(
            canonical,
            source=(
                ClaimSource.PROD_CANARY
                if source == "prod-canary"
                else ClaimSource.PROD_UPLOADERS
            ),
            status=ClaimStatus.UPLOADED if complete else ClaimStatus.PARTIAL,
            address=address,
            chunks_stored=chunks[0],
            chunks_failed=chunks[1],
            total_chunks=chunks[2],
            storage_cost_atto=storage,
            gas_cost_wei=gas,
            when=when,
            note=(
                "imported from recorded upload history"
                if complete
                else "imported from recorded history — upload was partial"
            ),
        )
        if claim is not None:
            if complete:
                imported += 1
            else:
                partial += 1

    console.print()
    console.print(f"  imported   {imported:>5}   marked as already uploaded")
    if partial:
        # Still available: the file is not on the network in full, and
        # completing it is useful work. Canary selection skips them.
        console.print(
            f"  partial    {partial:>5}   part-uploaded — still available to "
            "uploaders, excluded from canaries"
        )
    console.print(f"  already    {already:>5}   pool entry was spent or claimed")
    console.print(
        f"  unmatched  {unmatched:>5}   uploaded historically, not in the pool"
    )
    if unmatched:
        # Expected, not a fault: the history long predates the catalogue. Said
        # plainly so the numbers are not read as a failure to cover something.
        console.print(
            "[dim]             (expected — most recorded uploads predate the "
            "pool and were never catalogued)[/dim]"
        )
    if size_mismatch:
        console.print(
            f"[yellow]  size differs {len(size_mismatch):>3}   matched on name, but "
            "the recorded size does not match the catalogued one[/yellow]"
        )
        for name, catalogued, recorded in size_mismatch[:10]:
            console.print(
                f"[dim]             {name}: pool {format_size(catalogued)}, "
                f"history {format_size(recorded)}[/dim]"
            )
        console.print(
            "[dim]             These were still marked spent. Re-uploading "
            "content that is already stored is worse than losing a pool entry, "
            "and `pool release` undoes a wrong call.[/dim]"
        )
    if args.dry_run:
        console.print("\n[dim]Dry run — nothing was written.[/dim]")


def _add_from_listing(repo, console, report, args, tags, provenance) -> None:
    """Catalogue a whole public collection in one pass.

    Candidates arrive already sized and hashed from the source's own metadata,
    so nothing is probed or downloaded here. That is the difference between
    cataloguing a few hundred files in seconds and doing a HEAD request per
    file — and, because the digests are real, it is also what lets these
    entries be recognised as the same content as a local copy.
    """
    from saorsa_db.size_class import classify as _classify
    from saorsa_db.size_class import parse as parse_size_class

    from saorsa_deploy.pool_sources import SourceError, fetch_candidates

    try:
        candidates = fetch_candidates(
            args.from_listing, originals_only=getattr(args, "originals_only", False)
        )
    except (ValueError, SourceError) as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    console.print(
        f"{args.from_listing}: {len(candidates)} file(s) offered"
    )

    wanted_class = None
    if getattr(args, "size", None):
        try:
            wanted_class = parse_size_class(args.size)
        except ValueError as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
            sys.exit(1)

    selected = []
    skipped_class = 0
    for candidate in candidates:
        if wanted_class is not None:
            if candidate.size_bytes is None:
                skipped_class += 1
                continue
            if _classify(candidate.size_bytes) is not wanted_class:
                skipped_class += 1
                continue
        selected.append(candidate)
        if args.limit and len(selected) >= args.limit:
            break

    if skipped_class:
        console.print(
            f"[dim]  {skipped_class} did not match --size {args.size}[/dim]"
        )

    if args.dry_run:
        console.print("\n[dim]Dry run — nothing was written.[/dim]")
        for candidate in selected[:20]:
            console.print(
                f"[dim]  {format_size(candidate.size_bytes):>11}  "
                f"{candidate.name}[/dim]"
            )
        if len(selected) > 20:
            console.print(f"[dim]  … and {len(selected) - 20} more[/dim]")
        return

    for candidate in selected:
        existing = repo.get_pool_file_by_locator(candidate.url)
        if existing is not None:
            report.skipped_existing += 1
            continue
        pool_file, created = repo.add_pool_file(
            PoolFileKind.URL,
            candidate.url,
            candidate.name,
            size_bytes=candidate.size_bytes,
            digests=candidate.digests or None,
            tags=tags,
            provenance=provenance or candidate.provenance,
        )
        if not created:
            report.skipped_existing += 1
            continue
        report.added.append(pool_file)
        if candidate.size_bytes is None:
            report.unknown_size.append(candidate.url)
        if pool_file.duplicate_of:
            report.linked_duplicates.append((pool_file.name, pool_file.duplicate_of))
