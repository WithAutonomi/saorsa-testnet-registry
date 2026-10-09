"""The public canary dataset: what it counts, and what it must never carry.

Two properties here are the reason the file is written the way it is:

* **The output is a published contract.** `canary.autonomi.com` reads these
  documents, so the key set, the cell alphabet and the byte-for-byte
  serialisation are asserted rather than inspected. A change that would break
  the site has to break a test first.
* **Public safety is asserted on the serialised bytes, not the shape.** The
  fixture deliberately feeds in a probe hostname and an IPv4 literal, because
  an assertion that nothing leaked is only worth making when something
  leakable went in.

Nothing here touches a database, S3 or the clock: every test drives
`build_series` with in-memory rows and an explicit `--as-of`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.cmd.prod_canary_export import (
    build_series,
    cmd_prod_canary_export,
    index_entry,
    merge_index,
    serialise,
    vantage_label,
    vantage_of,
)

MODULE = "saorsa_deploy.cmd.prod_canary_export"

AS_OF = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
UPLOAD_HOST = "PROD-CANARY-04-ant-client-upload-lon1-1"
LON_HOST = "PROD-CANARY-04-ant-client-download-lon1-1"
NYC_HOST = "PROD-CANARY-04-ant-client-download-nyc1-2"
CHUNK = "a" * 64


def _upload(canary_id, uploaded_at, *, name="clip.mp4", size=1024, chunks=3, url=None):
    return SimpleNamespace(
        canary_id=canary_id,
        source_url=url or f"https://archive.org/download/item/{name}",
        file_name=name,
        file_size_bytes=size,
        top_address=f"{canary_id[:1] * 64}",
        num_chunks=chunks,
        uploaded_at=uploaded_at,
        initial_verify_status="ok",
    )


def _probe(canary_id, probed_at, status, *, host=LON_HOST, peers=None, missing=None):
    return SimpleNamespace(
        canary_id=canary_id,
        probed_at=probed_at,
        probed_by_host=host,
        status=status,
        missing_chunk_address=missing,
        terminal_peer_outcomes=peers,
        duration_ms=1234,
    )


def _episode(canary_id, confirmed_at, recovered_at=None, chunk=CHUNK):
    return SimpleNamespace(
        canary_id=canary_id,
        episode_no=1,
        missing_chunk_address=chunk,
        first_failed_probe_at=confirmed_at - timedelta(hours=1),
        confirmed_unavailable_at=confirmed_at,
        recovered_at=recovered_at,
        recovered=recovered_at is not None,
    )


def _build(uploads, probes, episodes=(), as_of=AS_OF, stale_after_hours=6.0):
    return build_series(
        uploads=list(uploads),
        probes=list(probes),
        episodes=list(episodes),
        as_of=as_of,
        series_id="prod-canary-04",
        title="Canary 4",
        stale_after_hours=stale_after_hours,
    )


def _file(document, canary_id):
    return next(f for f in document["files"] if f["id"] == canary_id)


# A failure the close group actively answered NotFound for is a genuine
# absence; one dominated by timeouts is reclassified inconclusive by
# _effective_status, which the export calls rather than reimplements.
GENUINE = {"queried": 5, "not_found": 4}
TIMEOUTS = {"queried": 5, "not_found": 1}


# ---------------------------------------------------------------------------
# 1. Cells
# ---------------------------------------------------------------------------


def test_ongoing_episode_is_red_from_confirmation_to_data_to():
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))]
    probes = [
        _probe(cid, datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc), "ok"),
        _probe(cid, datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc), "ok"),
        _probe(
            cid,
            datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc),
            "failed",
            peers=GENUINE,
            missing=CHUNK,
        ),
        _probe(
            cid,
            datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc),
            "failed",
            peers=GENUINE,
            missing=CHUNK,
        ),
    ]
    episodes = [_episode(cid, datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc))]

    document = _build(uploads, probes, episodes)

    assert document["days"] == ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]
    assert _file(document, cid)["cells"] == "GGRR"
    assert _file(document, cid)["available_now"] is False
    assert document["summary"]["files_unavailable_now"] == 1
    assert document["summary"]["files_available_now"] == 0
    assert document["summary"]["episodes"] == {"total": 1, "recovered": 0, "ongoing": 1}


def test_recovered_episode_is_red_only_across_its_span():
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))]
    probes = [
        _probe(cid, datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc), "ok"),
        _probe(
            cid,
            datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc),
            "failed",
            peers=GENUINE,
            missing=CHUNK,
        ),
        _probe(cid, datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc), "ok"),
        _probe(cid, datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc), "ok"),
    ]
    episodes = [
        _episode(
            cid,
            datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc),
            recovered_at=datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc),
        )
    ]

    document = _build(uploads, probes, episodes)

    assert _file(document, cid)["cells"] == "GRGG"
    assert _file(document, cid)["available_now"] is True
    assert document["summary"]["episodes"] == {"total": 1, "recovered": 1, "ongoing": 0}
    assert document["summary"]["files_never_unavailable"] == 0


def test_day_with_only_inconclusive_probes_has_no_data():
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc))]
    probes = [
        _probe(cid, datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc), "inconclusive"),
        _probe(
            cid,
            datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc),
            "failed",
            peers=TIMEOUTS,
            missing=CHUNK,
        ),
        _probe(cid, datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc), "ok"),
    ]

    document = _build(uploads, probes)

    # The middle day's only probe was a timeout-dominated failure, which
    # _effective_status downgrades to inconclusive — it says nothing about
    # presence, so the day is X, not G and certainly not R.
    assert _file(document, cid)["cells"] == "XXG"


def test_failed_and_ok_on_one_day_is_green_with_a_failed_check():
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc))]
    probes = [
        _probe(
            cid,
            datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc),
            "failed",
            peers=GENUINE,
            missing=CHUNK,
        ),
        _probe(cid, datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc), "ok"),
    ]

    document = _build(uploads, probes)
    entry = _file(document, cid)

    assert entry["cells"] == "G"
    assert entry["failed_checks"] == {"2026-10-09": 1}
    assert "episodes" not in entry


def test_cells_length_always_matches_days():
    uploads = [
        _upload("c1", datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc)),
        _upload("c2", datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc)),
    ]
    probes = [_probe("c1", datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc), "ok")]

    document = _build(uploads, probes)

    assert document["summary"]["days_observed"] == len(document["days"]) == 9
    for entry in document["files"]:
        assert len(entry["cells"]) == 9
        assert set(entry["cells"]) <= {"G", "R", "X"}


def test_files_are_ordered_by_uploaded_at_then_id():
    same = datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc)
    uploads = [
        _upload("c9", same + timedelta(hours=1)),
        _upload("c2", same),
        _upload("c1", same),
    ]
    document = _build(uploads, [])
    assert [f["id"] for f in document["files"]] == ["c1", "c2", "c9"]


def test_episodes_and_failed_checks_are_omitted_when_empty():
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc))]
    probes = [_probe(cid, datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc), "ok")]
    entry = _file(_build(uploads, probes), cid)
    assert "failed_checks" not in entry
    assert "episodes" not in entry


def test_file_name_is_url_decoded_and_source_url_is_not():
    cid = "c1"
    uploads = [
        _upload(
            cid,
            datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc),
            name="NBC%20Atlanta2%2058.mp4",
            url="https://archive.org/download/item/NBC%20Atlanta2%2058.mp4",
        )
    ]
    entry = _file(_build(uploads, []), cid)
    assert entry["name"] == "NBC Atlanta2 58.mp4"
    assert entry["source_url"] == "https://archive.org/download/item/NBC%20Atlanta2%2058.mp4"


# ---------------------------------------------------------------------------
# 2. Rates
# ---------------------------------------------------------------------------


def test_inconclusive_is_counted_but_never_in_the_denominator():
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc))]
    probes = [
        _probe(cid, datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc), "ok"),
        _probe(cid, datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc), "ok"),
        _probe(cid, datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc), "ok"),
        _probe(
            cid,
            datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc),
            "failed",
            peers=GENUINE,
            missing=CHUNK,
        ),
        _probe(cid, datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc), "inconclusive"),
        _probe(cid, datetime(2026, 10, 9, 7, 0, tzinfo=timezone.utc), "inconclusive"),
    ]

    rates = _build(uploads, probes)["rates"]["all"]

    assert rates == {"ok": 3, "failed": 1, "inconclusive": 2, "rate": 0.75}


def test_rate_is_null_with_no_conclusive_probes():
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc))]
    probes = [_probe(cid, datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc), "inconclusive")]

    document = _build(uploads, probes)

    assert document["rates"]["all"] == {"ok": 0, "failed": 0, "inconclusive": 1, "rate": None}
    assert document["rates"]["90d"]["rate"] is None


def test_rolling_windows_respect_as_of():
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 5, 1, 1, 0, tzinfo=timezone.utc))]
    probes = [
        # 150 days before as_of: in `all` only.
        _probe(cid, AS_OF - timedelta(days=150), "ok"),
        # 60 days before: in `all` and `90d`.
        _probe(cid, AS_OF - timedelta(days=60), "ok"),
        # 10 days before: in every window.
        _probe(cid, AS_OF - timedelta(days=10), "ok"),
        _probe(
            cid,
            AS_OF - timedelta(days=5),
            "failed",
            peers=GENUINE,
            missing=CHUNK,
        ),
    ]

    rates = _build(uploads, probes)["rates"]

    assert rates["all"]["ok"] == 3
    assert rates["90d"]["ok"] == 2
    assert rates["30d"] == {"ok": 1, "failed": 1, "inconclusive": 0, "rate": 0.5}


def test_windows_are_half_open_at_the_boundary():
    """`(as_of - N days, as_of]`: the far edge is excluded, `as_of` itself is in."""
    cid = "c1"
    uploads = [_upload(cid, AS_OF - timedelta(days=40))]
    probes = [
        _probe(cid, AS_OF - timedelta(days=30), "ok"),
        _probe(cid, AS_OF, "ok"),
    ]

    rates = _build(uploads, probes)["rates"]

    assert rates["all"]["ok"] == 2
    assert rates["30d"]["ok"] == 1


def test_probes_after_as_of_are_outside_the_windows():
    cid = "c1"
    uploads = [_upload(cid, AS_OF - timedelta(days=1))]
    probes = [_probe(cid, AS_OF + timedelta(hours=1), "ok")]

    rates = _build(uploads, probes)["rates"]

    assert rates["all"]["ok"] == 1
    assert rates["30d"]["ok"] == 0


def test_rate_is_rounded_to_six_decimals():
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc))]
    probes = [
        _probe(cid, datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc), "ok"),
        _probe(cid, datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc), "ok"),
        _probe(
            cid,
            datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc),
            "failed",
            peers=GENUINE,
            missing=CHUNK,
        ),
    ]
    assert _build(uploads, probes)["rates"]["all"]["rate"] == 0.666667


# ---------------------------------------------------------------------------
# 3. Initial verification
# ---------------------------------------------------------------------------


def test_upload_host_probes_are_excluded_from_rates_and_cells():
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc))]
    probes = [
        _probe(cid, datetime(2026, 10, 9, 1, 5, tzinfo=timezone.utc), "ok", host=UPLOAD_HOST),
        _probe(cid, datetime(2026, 10, 9, 1, 6, tzinfo=timezone.utc), "ok", host="some-other-box"),
    ]

    document = _build(uploads, probes)

    assert document["rates"]["all"] == {"ok": 0, "failed": 0, "inconclusive": 0, "rate": None}
    assert document["summary"]["initial_verification_ok"] == 2
    assert document["series"]["vantages"] == []
    assert document["series"]["first_probe_at"] is None
    # With no vantage probe, the record ends at the last upload and the day is
    # X — the file was verified at upload but has never been probed since.
    assert document["series"]["data_to"] == "2026-10-09T01:00:00Z"
    assert _file(document, cid)["cells"] == "X"


def test_failed_initial_verification_is_not_counted_as_ok():
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc))]
    probes = [
        _probe(
            cid,
            datetime(2026, 10, 9, 1, 5, tzinfo=timezone.utc),
            "failed",
            host=UPLOAD_HOST,
            peers=GENUINE,
            missing=CHUNK,
        )
    ]
    assert _build(uploads, probes)["summary"]["initial_verification_ok"] == 0


# ---------------------------------------------------------------------------
# 4. Vantages
# ---------------------------------------------------------------------------


def test_vantage_parsing_and_labelling():
    assert vantage_of(LON_HOST) == "lon1"
    assert vantage_of(NYC_HOST) == "nyc1"
    assert vantage_of("PROD-CANARY-04-ant-client-download-sgp1-3") == "sgp1"
    assert vantage_of(UPLOAD_HOST) is None
    assert vantage_of("") is None
    assert vantage_of(None) is None
    assert vantage_label("lon1") == "London"
    assert vantage_label("nyc3") == "New York"
    # An unknown region is labelled with its own code rather than dropped —
    # a new probe region must show up in the dataset, not vanish from it.
    assert vantage_label("zrh9") == "zrh9"


def test_unknown_region_becomes_a_vantage_labelled_with_its_code():
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc))]
    probes = [
        _probe(cid, datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc), "ok", host=LON_HOST),
        _probe(
            cid,
            datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc),
            "ok",
            host="PROD-CANARY-04-ant-client-download-zrh9-4",
        ),
    ]

    document = _build(uploads, probes)

    assert document["series"]["vantages"] == [
        {"id": "lon1", "label": "London"},
        {"id": "zrh9", "label": "zrh9"},
    ]
    assert set(document["rates"]["by_vantage"]) == {"lon1", "zrh9"}
    for buckets in document["rates"]["by_vantage"].values():
        for window in ("all", "30d", "90d"):
            assert set(buckets[window]) == {"ok", "failed", "inconclusive", "rate"}
    assert document["rates"]["by_vantage"]["zrh9"]["all"]["ok"] == 1


# ---------------------------------------------------------------------------
# 5. Staleness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "age_hours,expected_stale",
    [
        (5.0, False),
        # Exactly at the threshold is not yet stale: the comparison is strict,
        # so a probe timer landing precisely on the boundary cannot flap the
        # site's banner on and off.
        (6.0, False),
        (6.5, True),
    ],
)
def test_stale_flag_at_the_threshold_boundary(age_hours, expected_stale):
    cid = "c1"
    uploads = [_upload(cid, AS_OF - timedelta(days=2))]
    probes = [_probe(cid, AS_OF - timedelta(hours=age_hours), "ok")]

    document = _build(uploads, probes, stale_after_hours=6.0)

    assert document["stale"] is expected_stale
    assert document["stale_reasons"] == (["no_recent_probes"] if expected_stale else [])


# ---------------------------------------------------------------------------
# 6. Index merge
# ---------------------------------------------------------------------------


def test_index_merge_preserves_other_series_and_replaces_this_one():
    document = _build(
        [_upload("c1", datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc))],
        [_probe("c1", datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc), "ok")],
    )
    entry = index_entry(document, "prod-canary-04.json")
    existing = {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00Z",
        "series": [
            {"id": "prod-canary-03", "title": "Canary 3", "path": "prod-canary-03.json"},
            {"id": "prod-canary-04", "title": "Stale copy", "path": "prod-canary-04.json"},
        ],
    }

    index = merge_index(existing, entry, document["generated_at"])

    assert [e["id"] for e in index["series"]] == ["prod-canary-03", "prod-canary-04"]
    assert index["series"][0]["title"] == "Canary 3"
    assert index["series"][1] == entry
    assert index["generated_at"] == document["generated_at"]
    assert index["schema_version"] == 1


def test_index_merge_with_no_existing_index():
    document = _build([_upload("c1", AS_OF - timedelta(days=1))], [])
    entry = index_entry(document, "prod-canary-04.json")

    index = merge_index(None, entry, document["generated_at"])

    assert index["series"] == [entry]
    assert entry["summary"] == document["summary"]
    assert set(entry) == {
        "id",
        "kind",
        "title",
        "path",
        "generated_at",
        "data_to",
        "stale",
        "summary",
    }


# ---------------------------------------------------------------------------
# 7. Public safety
# ---------------------------------------------------------------------------


_IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

_FORBIDDEN_KEYS = ("deploy_args", "host", "hostname", "ip", "token")


def _all_keys(value) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(key)
            keys |= _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            keys |= _all_keys(child)
    return keys


def test_exported_documents_carry_nothing_private():
    """The fixture feeds in a hostname and an IP on purpose. If either reaches
    the serialised output the site would publish it."""
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc))]
    probes = [
        _probe(cid, datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc), "ok", host=LON_HOST),
        _probe(cid, datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc), "ok", host=NYC_HOST),
        _probe(cid, datetime(2026, 10, 9, 1, 5, tzinfo=timezone.utc), "ok", host=UPLOAD_HOST),
        _probe(
            cid,
            datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc),
            "failed",
            host="178.62.11.204",
            peers=GENUINE,
            missing=CHUNK,
        ),
    ]
    document = _build(uploads, probes)
    index = merge_index(None, index_entry(document, "prod-canary-04.json"), "x")

    for blob in (serialise(document), serialise(index)):
        for host in (UPLOAD_HOST, LON_HOST, NYC_HOST):
            assert host not in blob
        assert "ant-client" not in blob
        assert "PROD-CANARY-04" not in blob
        assert _IPV4.search(blob) is None
        keys = _all_keys(json.loads(blob))
        for forbidden in _FORBIDDEN_KEYS:
            assert forbidden not in keys
        assert {k for k in keys if k.endswith("url")} <= {"source_url"}


def test_series_document_has_exactly_the_schema_keys():
    document = _build(
        [_upload("c1", datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc))],
        [_probe("c1", datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc), "ok")],
    )

    assert set(document) == {
        "schema_version",
        "generated_at",
        "series",
        "stale",
        "stale_reasons",
        "summary",
        "rates",
        "days",
        "files",
    }
    assert set(document["series"]) == {
        "id",
        "kind",
        "title",
        "uploaded_from",
        "uploaded_to",
        "first_probe_at",
        "data_to",
        "probe_interval_hours",
        "vantages",
    }
    assert set(document["summary"]) == {
        "files",
        "bytes",
        "chunks",
        "initial_verification_ok",
        "days_observed",
        "files_never_unavailable",
        "files_available_now",
        "files_unavailable_now",
        "episodes",
        "smallest_bytes",
        "largest_bytes",
    }
    assert set(document["rates"]) == {"all", "30d", "90d", "by_vantage"}
    assert set(document["files"][0]) == {
        "id",
        "name",
        "bytes",
        "chunks",
        "address",
        "source_url",
        "uploaded_at",
        "available_now",
        "cells",
    }
    assert document["schema_version"] == 1
    assert document["series"]["kind"] == "prod-canary"
    assert document["series"]["probe_interval_hours"] == 1


def test_timestamps_have_no_fractional_seconds():
    """Rows come back from Postgres with microseconds; the contract says Z and
    whole seconds, and a site parsing on the format would break otherwise."""
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 10, 9, 1, 0, 0, 741140, tzinfo=timezone.utc))]
    probes = [_probe(cid, datetime(2026, 10, 9, 2, 3, 12, 998877, tzinfo=timezone.utc), "ok")]

    document = _build(uploads, probes)

    assert _file(document, cid)["uploaded_at"] == "2026-10-09T01:00:00Z"
    assert document["series"]["first_probe_at"] == "2026-10-09T02:03:12Z"
    assert document["series"]["data_to"] == "2026-10-09T02:03:12Z"
    assert document["series"]["uploaded_from"] == "2026-10-09"


def test_naive_timestamps_are_read_as_utc():
    cid = "c1"
    uploads = [_upload(cid, datetime(2026, 10, 9, 1, 0))]
    probes = [_probe(cid, datetime(2026, 10, 9, 2, 0), "ok")]
    document = _build(uploads, probes)
    assert _file(document, cid)["uploaded_at"] == "2026-10-09T01:00:00Z"


# ---------------------------------------------------------------------------
# 8. CLI guards
# ---------------------------------------------------------------------------


def _cli_args(**overrides):
    base = {
        "name": "PROD-CANARY-04",
        "id": None,
        "bucket": None,
        "out_dir": None,
        "prefix": "v1",
        "title": None,
        "stale_after_hours": 6.0,
        "as_of": "2026-10-09T12:00:00Z",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_cli_refuses_without_a_database_url(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        cmd_prod_canary_export(_cli_args(out_dir=str(tmp_path)))
    assert exc.value.code == 1


def test_cli_refuses_a_non_canary_kind(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/stub")
    repo = MagicMock()
    repo.list_by_name.return_value = [
        SimpleNamespace(id=7, name="STG-01", deploy_args={"kind": "testnet"})
    ]

    with patch(f"{MODULE}.get_repository", return_value=repo):
        with pytest.raises(SystemExit) as exc:
            cmd_prod_canary_export(_cli_args(name="STG-01", out_dir=str(tmp_path)))

    assert exc.value.code == 1
    repo.list_prod_canary_uploads_for_deployment.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_cli_refuses_both_or_neither_destination():
    from saorsa_deploy.main import main

    for argv in (
        ["prod", "canary", "export", "--name", "PROD-CANARY-04"],
        [
            "prod",
            "canary",
            "export",
            "--name",
            "PROD-CANARY-04",
            "--bucket",
            "b",
            "--out-dir",
            "/tmp/x",
        ],
    ):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2


def test_cli_refuses_a_malformed_as_of(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/stub")
    with patch(f"{MODULE}.get_repository", return_value=MagicMock()):
        with pytest.raises(SystemExit) as exc:
            cmd_prod_canary_export(_cli_args(out_dir=str(tmp_path), as_of="last tuesday"))
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# 9. End-to-end writing + determinism
# ---------------------------------------------------------------------------


def _stub_repo():
    cid = "c1"
    repo = MagicMock()
    repo.list_by_name.return_value = [
        SimpleNamespace(id=537, name="PROD-CANARY-04", deploy_args={"kind": "prod-canary"})
    ]
    repo.list_prod_canary_uploads_for_deployment.return_value = [
        _upload(cid, datetime(2026, 10, 8, 1, 0, tzinfo=timezone.utc))
    ]
    repo.list_prod_canary_probes_for_deployment.return_value = [
        _probe(cid, datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc), "ok"),
        _probe(cid, datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc), "ok", host=NYC_HOST),
    ]
    repo.list_prod_canary_unavailability_for_deployment.return_value = []
    return repo


def test_out_dir_writes_both_files_and_is_byte_identical_on_a_re_run(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/stub")
    args = _cli_args(out_dir=str(tmp_path))

    with patch(f"{MODULE}.get_repository", _stub_repo):
        cmd_prod_canary_export(args)
        first_series = (tmp_path / "prod-canary-04.json").read_bytes()
        first_index = (tmp_path / "index.json").read_bytes()
        cmd_prod_canary_export(args)

    assert (tmp_path / "prod-canary-04.json").read_bytes() == first_series
    assert (tmp_path / "index.json").read_bytes() == first_index
    assert first_series.endswith(b"}\n")
    # Compact separators: no space after a comma anywhere in the payload.
    assert b", " not in first_series
    document = json.loads(first_series)
    assert document["series"]["id"] == "prod-canary-04"
    assert document["series"]["title"] == "PROD-CANARY-04"
    index = json.loads(first_index)
    assert [e["id"] for e in index["series"]] == ["prod-canary-04"]


def test_out_dir_re_export_keeps_another_series_in_the_index(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/stub")
    (tmp_path / "index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-01-01T00:00:00Z",
                "series": [{"id": "prod-canary-03", "path": "prod-canary-03.json"}],
            }
        )
    )

    with patch(f"{MODULE}.get_repository", _stub_repo):
        cmd_prod_canary_export(_cli_args(out_dir=str(tmp_path), title="Canary 4"))

    index = json.loads((tmp_path / "index.json").read_text())
    assert [e["id"] for e in index["series"]] == ["prod-canary-03", "prod-canary-04"]
    assert index["series"][1]["title"] == "Canary 4"


def test_bucket_writes_both_keys_with_the_cache_headers(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/stub")
    s3 = MagicMock()
    s3.get_object.side_effect = _no_such_key()

    with patch(f"{MODULE}.get_repository", _stub_repo):
        with patch(f"{MODULE}.boto3") as boto3_mock:
            boto3_mock.client.return_value = s3
            cmd_prod_canary_export(_cli_args(bucket="canary-site", prefix="v1"))

    keys = [call.kwargs["Key"] for call in s3.put_object.call_args_list]
    assert keys == ["v1/prod-canary-04.json", "v1/index.json"]
    for call in s3.put_object.call_args_list:
        assert call.kwargs["Bucket"] == "canary-site"
        assert call.kwargs["ContentType"] == "application/json"
        assert call.kwargs["CacheControl"] == "public, max-age=300"
    s3.get_object.assert_called_once_with(Bucket="canary-site", Key="v1/index.json")


def test_bucket_merges_the_existing_index(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/stub")
    body = json.dumps(
        {
            "schema_version": 1,
            "generated_at": "2026-01-01T00:00:00Z",
            "series": [{"id": "prod-canary-03", "path": "prod-canary-03.json"}],
        }
    ).encode()
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": SimpleNamespace(read=lambda: body)}

    with patch(f"{MODULE}.get_repository", _stub_repo):
        with patch(f"{MODULE}.boto3") as boto3_mock:
            boto3_mock.client.return_value = s3
            cmd_prod_canary_export(_cli_args(bucket="canary-site"))

    index_call = s3.put_object.call_args_list[-1]
    index = json.loads(index_call.kwargs["Body"])
    assert [e["id"] for e in index["series"]] == ["prod-canary-03", "prod-canary-04"]


def test_malformed_existing_index_refuses_rather_than_overwriting(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/stub")
    (tmp_path / "index.json").write_text("{not json")

    with patch(f"{MODULE}.get_repository", _stub_repo):
        with pytest.raises(SystemExit) as exc:
            cmd_prod_canary_export(_cli_args(out_dir=str(tmp_path)))

    assert exc.value.code == 1
    assert (tmp_path / "index.json").read_text() == "{not json"
    assert not (tmp_path / "prod-canary-04.json").exists()


def test_duplicate_names_export_the_newest_and_warn(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/stub")
    repo = _stub_repo()
    repo.list_by_name.return_value = [
        SimpleNamespace(id=175, name="PROD-CANARY-02", deploy_args={"kind": "prod-canary"}),
        SimpleNamespace(id=174, name="PROD-CANARY-02", deploy_args={"kind": "prod-canary"}),
    ]

    with patch(f"{MODULE}.get_repository", return_value=repo):
        cmd_prod_canary_export(_cli_args(name="PROD-CANARY-02", out_dir=str(tmp_path)))

    out = capsys.readouterr().out
    assert "175" in out and "174" in out
    repo.list_prod_canary_uploads_for_deployment.assert_called_once_with(175)
    assert (tmp_path / "prod-canary-02.json").exists()


def test_id_overrides_the_name_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/stub")
    repo = _stub_repo()
    repo.get.return_value = SimpleNamespace(
        id=174, name="PROD-CANARY-02", deploy_args={"kind": "prod-canary"}
    )

    with patch(f"{MODULE}.get_repository", return_value=repo):
        cmd_prod_canary_export(_cli_args(name="PROD-CANARY-02", id=174, out_dir=str(tmp_path)))

    repo.list_by_name.assert_not_called()
    repo.list_prod_canary_uploads_for_deployment.assert_called_once_with(174)
    assert (tmp_path / "prod-canary-02.json").exists()


def test_argv_plumbing_reaches_the_handler():
    """The exact invocation the publisher timer emits must parse."""
    from saorsa_deploy.main import main

    seen = {}

    def _stub(args):
        seen.update(
            {
                "name": args.name,
                "bucket": args.bucket,
                "prefix": args.prefix,
                "title": args.title,
                "stale": args.stale_after_hours,
                "as_of": args.as_of,
            }
        )

    with patch(f"{MODULE}.cmd_prod_canary_export", _stub):
        main(
            [
                "prod",
                "canary",
                "export",
                "--name",
                "PROD-CANARY-04",
                "--bucket",
                "canary-autonomi-com-data",
                "--prefix",
                "v1",
                "--title",
                "Canary 4",
                "--stale-after-hours",
                "6",
                "--as-of",
                "2026-10-09T12:00:00Z",
            ]
        )

    assert seen == {
        "name": "PROD-CANARY-04",
        "bucket": "canary-autonomi-com-data",
        "prefix": "v1",
        "title": "Canary 4",
        "stale": 6.0,
        "as_of": "2026-10-09T12:00:00Z",
    }


def _no_such_key():
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
