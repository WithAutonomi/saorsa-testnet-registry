"""The transfer-failure classifier, and the one copy of it that is importable.

``ant`` emits no classification of its own, so ``error_kind`` is derived here
from the text it does emit. The vocabulary is defined once in
:mod:`saorsa_db.models.transfer_error_kind`; this module is the canonical copy
of the CLASSIFIER, and the block below is mirrored by hand into every service
script -- each of those is pushed to its VM as one self-contained file run by
``/usr/bin/python3`` with no virtualenv, so it can import nothing of ours.

Only one caller imports it: ``cmd/prod_local_downloads.py``, which runs ``ant``
on the operator's own machine. Its real job is to be the copy a test compares
every other copy against.
"""

# --- transfer error classification: keep byte-identical in every copy ---
#
# `ant` has no `error_kind` of its own: the kind is entirely our invention, and
# the only material it can be derived from is the single line `ant-cli` prints
# to stderr on a failure (`ant-cli/src/main.rs`: `eprintln!("Error: {e:#}")`).
# Exit codes are 0 or 1 and so discriminate nothing, and a failed DOWNLOAD
# writes nothing at all to stdout -- `--json` included, because the only
# `println!` of the result document sits after the `?` that aborts. Verified
# against the 0.3.12 release binary: stdout empty, one `Error: ...` line on
# stderr, exit 1.
#
# So this maps ant-core's own `Display` text -- the `#[error(...)]` literals in
# `ant-core/src/data/error.rs`, stable within a release -- onto the vocabulary.
# Matched as SUBSTRINGS, not prefixes: by the time a variant reaches stderr it
# is wrapped in context. A real capture reads
# `Error: Failed to fetch public DataMap: not found: DataMap chunk not found at
# <64 hex>`.
#
# ORDER IS LOAD-BEARING, as it is in `pointer_client.classify_failure`. One
# string can satisfy several markers and the first match wins:
#
#   * the chunk-exhaustion sentence contains the words "not found" AND is
#     wrapped in `encryption error:`, so it is tested before either;
#   * `partial upload: N/M stored, K failed: <inner reason>` carries the real
#     reason INSIDE it, so every specific marker is tested before the wrapper.
#
# The copies are byte-identical by TEST, not by convention: this block lives in
# `saorsa_deploy/transfer_errors.py` and is mirrored by hand into
# `prod_downloader.py`, `prod_continuous_uploader.py`, `prod_uploader.py` and
# the embedded Python block in `uploader.sh`, because each of those is
# delivered to its VM as one self-contained file and can import nothing of
# ours.

#: Every value a transfer's `error_kind` may take. Mirrors
#: `saorsa_db.models.transfer_error_kind.TransferErrorKind`, which is the
#: canonical definition and names the column these are stored in.
TRANSFER_ERROR_KINDS = (
    "payment",
    "quorum",
    "timeout",
    "connection",
    "not_found",
    "over_cap",
    "crash",
    "unreachable",
    "other",
)

#: `(marker, kind)` pairs in match order -- see ORDER IS LOAD-BEARING above.
TRANSFER_ERROR_MARKERS = (
    # ant-core refuses a browser-path file above its cap once the data map
    # resolves. That is a property of the transport and not of the network, and
    # every consumer records it as a SKIP, so it is tested first and on its
    # own -- exactly as the WASM client's own classifier does.
    ("invalid public file size", "over_cap"),
    # The collapsed native-DOWNLOAD kind. `chunk_get` returns `Ok(None)` when
    # the peer sweep is exhausted, and its retry predicate swallows
    # `Timeout | Network | Protocol` into that exhaustion -- so not-found, a
    # quorum shortfall, a timeout and a connection failure all render as this
    # one sentence. Calling it `not_found` would assert something ant-core does
    # not know, so the kind is named for what IS known: the chunk could not be
    # reached. It can only arise on a read; an `unreachable` recorded against an
    # upload means the markers have moved and the classifier fell through.
    ("Chunk not found after", "unreachable"),
    # A missing DataMap chunk, by contrast, genuinely IS an answer: the close
    # group agreed there is nothing stored at that address.
    ("DataMap chunk not found", "not_found"),
    ("not found:", "not_found"),
    ("payment error:", "payment"),
    # `insufficient peers:` is ant-core's `InsufficientPeers` -- too few peers
    # quoted or stored -- so it is a quorum shortfall even where the text talks
    # about quotes. The WASM client's classifier reads those same words as
    # `payment`; it matches loose words on a JavaScript exception, while this
    # matches the variant ant-core actually raised.
    ("insufficient peers:", "quorum"),
    ("close-group PUT shortfall (dial churn):", "quorum"),
    # A remote node refused the PUT at the application layer (disk full, stale
    # quote, merkle-pool rejection). The write fell short of its close group,
    # which is what a shortfall is; which node and why is in `reason`.
    ("remote PUT rejected for", "quorum"),
    ("timeout:", "timeout"),
    ("network error:", "connection"),
    ("protocol error:", "connection"),
    # The `partial upload:` WRAPPER, tested after every marker that can appear
    # inside it. Some chunks did not store, so a shortfall is the honest
    # reading of what is left.
    ("partial upload:", "quorum"),
    # Deliberately `other`, and LISTED rather than left to fall through, so it
    # is visible that each was considered: these are local or data faults, and
    # `other` is the one kind that means "a fault we cannot attribute".
    ("invalid data:", "other"),
    ("encryption error:", "other"),
    ("serialization error:", "other"),
    ("I/O error:", "other"),
    ("storage error:", "other"),
    ("operation cancelled:", "other"),
)

#: Same cap, and the same shape of value, as the browser path's
#: `describeError` (`resources/wasm-client/src/errors.ts`). A native `reason`
#: and a browser `reason` have to be the same size and shape of thing, or the
#: column holds two incomparable kinds of text.
REASON_LIMIT = 500
#: Anything this long is calldata or a signed payload, never an identifier --
#: `errors.ts`'s own threshold.
REASON_HEX_BLOB_MIN = 130
_REASON_HEX_DIGITS = "0123456789abcdefABCDEF"


def _elide_hex_blobs(text):
    """Replace every `0x` run of >=130 hex characters with its length.

    Deliberately a scanner rather than a regex: this block is mirrored into
    three service scripts and one shell heredoc, and the fewer module-level
    imports it needs the fewer ways a copy can fail to run.
    """
    out = []
    index = 0
    length = len(text)
    while index < length:
        if text[index] == "0" and index + 1 < length and text[index + 1] == "x":
            end = index + 2
            while end < length and text[end] in _REASON_HEX_DIGITS:
                end += 1
            if end - (index + 2) >= REASON_HEX_BLOB_MIN:
                out.append("0x…(%d hex chars)" % (end - (index + 2)))
                index = end
                continue
        out.append(text[index])
        index += 1
    return "".join(out)


def clip_reason(text, limit=REASON_LIMIT):
    """One line, at most ``limit`` characters, keeping the head AND the tail.

    Both ends, because the context is at the start and the reason is usually at
    the end -- a plain prefix is what lost the RPC's own reason on DEV-03 run
    590 and is why `errors.ts` clips this way. The split is the same 60/40.
    """
    collapsed = _elide_hex_blobs(" ".join((text or "").split()))
    if len(collapsed) <= limit:
        return collapsed
    marker = " … "
    tail = int((limit - len(marker)) * 0.4)
    head = limit - len(marker) - tail
    return collapsed[:head] + marker + collapsed[len(collapsed) - tail :]


def classify_transfer_error(text):
    """The `error_kind` for a failure's text, or `other` when nothing matches.

    Never raises and never returns an empty string: a failure always has a
    kind, and `other` is a finding in its own right -- a rising `other` share
    is how a client release that moved these strings announces itself.
    """
    haystack = text or ""
    for marker, kind in TRANSFER_ERROR_MARKERS:
        if marker in haystack:
            return kind
    return "other"


def failure_reason(stderr, limit=REASON_LIMIT, require_error_line=False):
    """The clipped one-line reason out of `ant`'s stderr.

    `ant-cli` prints exactly one `Error: ...` line, last, on every failure
    path; everything before it is `-v` trace, which on a download runs to tens
    of thousands of lines. The last such line is therefore the reason, and the
    fallback to the last non-empty line covers a failure that left no `Error:`
    line at all.

    ``require_error_line`` turns that fallback OFF, and is for a child the OS
    KILLED. Its last stderr line is then whatever trace it had happened to
    reach -- "Connected to autonomi network (found 5 peers)" is a real example
    -- which is not a reason for anything, and classifying it reports a
    connection message as the cause of an OOM kill. A caller that has
    something true to say instead (which signal it was) asks for the `Error:`
    line alone and gets "" when the child printed none.
    """
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("Error: "):
            return clip_reason(line[len("Error: ") :], limit)
    if require_error_line:
        return ""
    return clip_reason(lines[-1], limit) if lines else ""


# --- end transfer error classification ---
