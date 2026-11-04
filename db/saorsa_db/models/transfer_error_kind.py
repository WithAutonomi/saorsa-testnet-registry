"""The one vocabulary of transfer failure kinds (INFRA-484).

Every path that moves bytes to or from the network -- the two production
continuous kinds, the one-shot prod uploader and downloader, the local-machine
variants and the testnet client tiers -- records *why* a transfer failed in two
fields: a free-text ``reason`` (``ant``'s own sentence, clipped) and a short
classified ``error_kind``. The reason is what an operator reads; the kind is
what a count can be grouped by, and a column holding a classification on some
rows and prose on others can be neither grouped nor counted on.

Canonical here, and mirrored by hand into the places that cannot import it --
the service scripts, which are delivered to their VM as one self-contained file
each, and the TypeScript/JavaScript classifiers of the browser paths. A test
(``deploy/tests/test_transfer_error_vocabulary.py``) reads every copy and fails
on divergence; that test also covers the pre-existing ``classify.mjs`` /
:class:`~saorsa_db.models.browser_download_record.BrowserDownloadErrorKind`
pair, whose comment claimed an agreement nothing asserted.

A plain ``String(32)`` column rather than a Postgres enum type, exactly as
``BrowserDownloadErrorKind`` is: a kind added to a classifier must be
recordable without a migration standing between it and a run.

**NULL means "not recorded", never "no error".** Nothing is backfilled -- the
Influx points of every earlier run carried no such field, and a guessed kind
reads as a fact.
"""

import enum


class TransferErrorKind(str, enum.Enum):
    """The ``error_kind`` values a transfer failure is classified into."""

    #: A payment failed: ``payment error:`` from ant-core (a nonce, funds, an
    #: RPC refusal). On the browser path, also viem's own failures.
    PAYMENT = "payment"
    #: The operation fell short of a close group: ``insufficient peers:``,
    #: ``close-group PUT shortfall (dial churn):``, a remote PUT rejection, or
    #: the surviving half of a ``partial upload:``.
    QUORUM = "quorum"
    TIMEOUT = "timeout"
    #: A transport-level failure: ``network error:`` / ``protocol error:``, and
    #: on the browser path a dial, channel or WebRTC failure.
    CONNECTION = "connection"
    #: The network ANSWERED that nothing is stored at the address -- a missing
    #: DataMap chunk. Distinct from :attr:`UNREACHABLE` on purpose.
    NOT_FOUND = "not_found"
    #: This transport refuses the file (ant-core's browser-path size cap). Not
    #: a network failure, and recorded as a SKIP rather than a failure
    #: everywhere downstream: the content is stored and healthy.
    OVER_CAP = "over_cap"
    #: The transfer process was killed by a signal, which on a client VM is
    #: almost always the OOM killer reaching the largest RSS on the box.
    #: Assigned by the parent from the exit code, never by a text classifier.
    CRASH = "crash"
    #: The collapsed NATIVE-DOWNLOAD kind, and named for what is known rather
    #: than for what is guessed.
    #:
    #: ``chunk_get`` returns ``Ok(None)`` when its peer sweep is exhausted, and
    #: the retry predicate swallows timeout, network and protocol failures into
    #: that exhaustion -- so not-found, a quorum shortfall, a timeout and a
    #: connection failure all render as one identical sentence. Calling that
    #: ``not_found`` would assert something ant-core does not know.
    #:
    #: It must therefore never appear on an UPLOAD, where the prefixes do
    #: separate. One that does means the classifier fell through and the client
    #: has moved the strings, which makes this kind's own distribution a check
    #: on the classifier.
    UNREACHABLE = "unreachable"
    #: A fault that could not be attributed. A rising ``other`` share is itself
    #: the finding: it is how a client release that moved these strings
    #: announces itself.
    OTHER = "other"


#: Every value, as the plain strings the columns hold.
TRANSFER_ERROR_KIND_VALUES = tuple(member.value for member in TransferErrorKind)

#: The kinds no text classifier can produce, because they are decided by the
#: parent process from an exit code rather than from ``ant``'s output. Named so
#: the vocabulary test can state exactly why the browser client's classifier is
#: allowed to be short of these two and no others.
TRANSFER_ERROR_KINDS_NOT_FROM_TEXT = (
    TransferErrorKind.CRASH.value,
    TransferErrorKind.UNREACHABLE.value,
)
