"""Server-side log-level filtering for a public Elasticsearch ingest instance.

Community machines forward their own logs, and we cannot control what level they
are configured at — a participant can edit their forwarder whenever they like.
So the level policy has to be enforced where we do have control: on the server,
at ingest.

The mechanism is an ingest pipeline attached to the index as
``index.final_pipeline``. That slot is the load-bearing detail:

* ``final_pipeline`` **always** runs and cannot be overridden by the request.
  Verified against Elasticsearch 8.17.4: a client passing ``?pipeline=_none``
  still had its DEBUG documents dropped.
* ``default_pipeline`` is **not** an enforcement point. Verified the same way:
  ``?pipeline=_none`` bypassed it entirely and the document landed unfiltered.
  Never use it for anything an untrusted client must not be able to opt out of.

Dropped documents come back as ``status: 200, result: "noop"`` with the bulk
response reporting ``errors: false``, so a forwarder treats them as accepted and
does not retry. That is deliberate: a ``fail`` processor would return a
per-document error and most forwarders would retry the same batch forever.

The policy lives in the *pipeline body*, not the index template, so it can be
changed at runtime with a single PUT — no redeploy, no restart, no reindex, and
it applies to the next write. That is what makes "run at INFO now, switch to
DEBUG when we want to collect it" a one-command operation.
"""

from __future__ import annotations

#: Pipeline name. Fixed, because the index template references it by name and
#: only the body is ever rewritten.
LOG_FILTER_PIPELINE = "beta-log-level-filter"

#: Verbosity ordering, most verbose first. WARN/ERROR are deliberately absent:
#: they are less verbose than INFO and are never dropped, whatever the setting.
_VERBOSITY = ("trace", "debug", "info")

VALID_MIN_LEVELS = _VERBOSITY
DEFAULT_MIN_LEVEL = "info"


def levels_to_drop(min_level: str) -> list[str]:
    """Levels strictly MORE verbose than ``min_level``, uppercased.

    Mirrors ``orchestrator/testnet._resolve_elastic_drop_log_levels``, which
    derives the node Telegraf forwarder's drop set the same way — one vocabulary
    for "how verbose may logs be", rather than two that can disagree.

        trace -> []                  (keep everything)
        debug -> ["TRACE"]
        info  -> ["DEBUG", "TRACE"]
    """
    level = (min_level or DEFAULT_MIN_LEVEL).strip().lower()
    if level not in _VERBOSITY:
        raise ValueError(
            f"min_log_level must be one of {', '.join(_VERBOSITY)} (got '{min_level}')"
        )
    return [lv.upper() for lv in _VERBOSITY[: _VERBOSITY.index(level)]]


def build_drop_condition(drop_levels: list[str]) -> str:
    """The Painless condition selecting documents to discard.

    ``toUpperCase()`` because a forwarder may emit ``debug`` rather than
    ``DEBUG``, and a case-sensitive comparison would silently pass exactly the
    traffic this exists to stop.

    A document with **no** ``level`` field is kept. Dropping it would discard
    anything whose shape we did not anticipate, and losing logs is a worse
    failure than keeping a few we did not mean to.
    """
    levels = ", ".join(f'"{lv}"' for lv in drop_levels)
    return f"ctx.level != null && [{levels}].contains(ctx.level.toUpperCase())"


def build_pipeline_body(min_level: str) -> dict:
    """The ingest pipeline: level filter, PII strip, and unforgeable attribution.

    At ``trace`` nothing is dropped, but the pipeline still exists and still does
    the other three jobs — the index template references it by name, and an index
    whose ``final_pipeline`` names a missing pipeline rejects every write.
    """
    drop_levels = levels_to_drop(min_level)
    processors: list[dict] = []
    if drop_levels:
        processors.append({"drop": {"if": build_drop_condition(drop_levels)}})

    processors += [
        # Machine hostnames routinely contain a person's real name. These logs
        # come from community members' own machines, so the hostname is stripped
        # at ingest rather than requested-not-to-be-sent: a forwarder we do not
        # control cannot be relied on to withhold it.
        {"remove": {"field": "host", "ignore_missing": True}},
        # Remove any client-supplied attribution BEFORE stamping the real one.
        # Overwriting alone would be enough while every key carries metadata, but
        # a key minted without it would then leave the client's claimed value
        # standing — i.e. silently forgeable. Removing first fails closed.
        {"remove": {"field": "beta_user", "ignore_missing": True}},
        # Attribution taken from the authenticated API key itself, so a
        # participant cannot write logs attributed to somebody else. Verified
        # against ES 8.17.4: a key whose metadata says "mallory", sending
        # `beta_user: alice`, produces a document reading "mallory".
        {"set_security_user": {"field": "_sec", "properties": ["api_key"]}},
        {
            "set": {
                "field": "beta_user",
                "copy_from": "_sec.api_key.metadata.beta_user",
                "ignore_empty_value": True,
            }
        },
        # The key id as well as the handle: it is stable and unambiguous even if
        # two participants are ever given the same handle, and it identifies
        # exactly which credential to revoke.
        {
            "set": {
                "field": "beta_key_id",
                "copy_from": "_sec.api_key.id",
                "ignore_empty_value": True,
            }
        },
        {"remove": {"field": "_sec", "ignore_missing": True}},
    ]

    kept = (
        f"keep {min_level.upper()} and above" if drop_levels else "keep all levels"
    )
    return {
        "description": (
            f"Beta ingest: {kept}; strip host; stamp attribution from the API key"
        ),
        "processors": processors,
    }
