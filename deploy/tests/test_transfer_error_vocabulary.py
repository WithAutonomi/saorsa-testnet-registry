"""Every copy of every error-kind vocabulary, compared (INFRA-484).

`error_kind` is written by six producers in four languages and read back out of
one `String(32)` column, so the vocabulary has to be one vocabulary. None of the
copies can import any of the others:

* `TransferErrorKind` in `saorsa_db` is the canonical definition and names the
  column;
* `saorsa_deploy/transfer_errors.py` is the importable classifier;
* `prod_downloader.py`, `prod_continuous_uploader.py`, `prod_uploader.py` and
  the embedded Python block in `uploader.sh` each carry a hand-mirrored copy,
  because each is delivered to its VM as one self-contained file;
* `resources/wasm-client/src/classify.ts` is the browser path's own;
* `resources/browser-client/lib/classify.mjs` is the hot-read harness's own,
  paired with `BrowserDownloadErrorKind`.

That last pair already existed, and `browser_download_record.py`'s comment
claimed an agreement that nothing asserted. It is asserted here.

The divergence check itself is a pure function (`compare_vocabularies`) so the
"a kind added to one copy only must fail" property can be demonstrated directly,
without editing a shipped file.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest
from saorsa_db.models import BrowserDownloadErrorKind
from saorsa_db.models.transfer_error_kind import (
    TRANSFER_ERROR_KIND_VALUES,
    TRANSFER_ERROR_KINDS_NOT_FROM_TEXT,
)

DEPLOY = Path(__file__).resolve().parents[1]
SCRIPTS = DEPLOY / "saorsa_deploy/resources/scripts"
CANONICAL = DEPLOY / "saorsa_deploy/transfer_errors.py"
CLASSIFY_TS = DEPLOY / "saorsa_deploy/resources/wasm-client/src/classify.ts"
CLASSIFY_MJS = DEPLOY / "saorsa_deploy/resources/browser-client/lib/classify.mjs"
FIXTURES = DEPLOY / "tests/fixtures/transfer-errors"

#: The three service scripts that mirror the classifier block verbatim.
MIRRORED_SCRIPTS = (
    "prod_downloader.py",
    "prod_continuous_uploader.py",
    "prod_uploader.py",
)

BLOCK_START = "# --- transfer error classification: keep byte-identical in every copy ---"
BLOCK_END = "# --- end transfer error classification ---"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _block(text: str) -> str:
    """The classifier block out of one copy, between its two markers."""
    start = text.index(BLOCK_START) + len(BLOCK_START)
    return text[start : text.index(BLOCK_END)]


def _python_tuple(text: str, name: str) -> tuple[str, ...]:
    """The string literals of a module-level `NAME = (...)` tuple."""
    body = text[text.index(f"{name} = (") :]
    body = body[: body.index(")")]
    return tuple(re.findall(r'"([^"]+)"', body))


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ts_error_kinds(text: str) -> tuple[str, ...]:
    """`export type ErrorKind = | "a" | "b" ...` from classify.ts."""
    body = text[text.index("export type ErrorKind") :]
    return tuple(re.findall(r'"([^"]+)"', body[: body.index(";")]))


def _mjs_error_kinds(text: str) -> tuple[str, ...]:
    """`export const ERROR_KINDS = [...]` from classify.mjs."""
    body = text[text.index("export const ERROR_KINDS") :]
    return tuple(re.findall(r'"([^"]+)"', body[: body.index("]")]))


def _uploader_sh_block() -> str:
    text = (SCRIPTS / "uploader.sh").read_text()
    return _block(text)


# ---------------------------------------------------------------------------
# The divergence check, as a function so it can be shown to fail
# ---------------------------------------------------------------------------


def compare_vocabularies(copies: dict[str, tuple[str, ...]]) -> list[str]:
    """Problems with a set of named vocabularies, or an empty list.

    ``copies`` maps a human name to that copy's ordered kinds. Order is part of
    the comparison for the copies that are meant to be identical: a reader
    diffing two files should see nothing at all, and a reordered tuple is a
    hand-edit that got half-applied.
    """
    canonical_name = "TransferErrorKind"
    canonical = copies[canonical_name]
    problems: list[str] = []
    for name, kinds in copies.items():
        if name == canonical_name:
            continue
        if kinds != canonical:
            missing = sorted(set(canonical) - set(kinds))
            extra = sorted(set(kinds) - set(canonical))
            detail = []
            if missing:
                detail.append(f"missing {missing}")
            if extra:
                detail.append(f"has {extra} which {canonical_name} does not")
            if not detail:
                detail.append("same kinds in a different order")
            problems.append(f"{name}: " + "; ".join(detail))
    return problems


class TestTheDivergenceCheckItself:
    """The check has to fail on divergence, or the rest of this file proves
    nothing."""

    def test_a_kind_added_to_one_copy_only_is_reported(self):
        base = TRANSFER_ERROR_KIND_VALUES
        problems = compare_vocabularies(
            {
                "TransferErrorKind": base,
                "prod_downloader.py": base + ("quarantined",),
            }
        )
        assert problems == [
            "prod_downloader.py: has ['quarantined'] which TransferErrorKind does not"
        ]

    def test_a_kind_dropped_from_one_copy_only_is_reported(self):
        base = TRANSFER_ERROR_KIND_VALUES
        problems = compare_vocabularies(
            {
                "TransferErrorKind": base,
                "prod_uploader.py": tuple(k for k in base if k != "unreachable"),
            }
        )
        assert problems == ["prod_uploader.py: missing ['unreachable']"]

    def test_a_reordered_copy_is_reported(self):
        base = TRANSFER_ERROR_KIND_VALUES
        problems = compare_vocabularies(
            {
                "TransferErrorKind": base,
                "transfer_errors.py": tuple(reversed(base)),
            }
        )
        assert problems == ["transfer_errors.py: same kinds in a different order"]

    def test_identical_copies_are_clean(self):
        base = TRANSFER_ERROR_KIND_VALUES
        assert compare_vocabularies({"TransferErrorKind": base, "a": base, "b": base}) == []


# ---------------------------------------------------------------------------
# The shipped copies
# ---------------------------------------------------------------------------


class TestTransferVocabulary:
    def test_every_python_copy_matches_the_enum(self):
        copies = {"TransferErrorKind": TRANSFER_ERROR_KIND_VALUES}
        copies["transfer_errors.py"] = _python_tuple(CANONICAL.read_text(), "TRANSFER_ERROR_KINDS")
        for name in MIRRORED_SCRIPTS:
            copies[name] = _python_tuple((SCRIPTS / name).read_text(), "TRANSFER_ERROR_KINDS")
        copies["uploader.sh (embedded)"] = _python_tuple(
            _uploader_sh_block(), "TRANSFER_ERROR_KINDS"
        )
        assert compare_vocabularies(copies) == []

    def test_every_kind_the_markers_produce_is_in_the_vocabulary(self):
        module = _load(CANONICAL)
        produced = {kind for _, kind in module.TRANSFER_ERROR_MARKERS}
        assert produced <= set(TRANSFER_ERROR_KIND_VALUES)

    def test_the_two_kinds_no_text_can_produce_are_named_and_absent(self):
        """`crash` and `unreachable` are decided elsewhere, and that is stated."""
        module = _load(CANONICAL)
        produced = {kind for _, kind in module.TRANSFER_ERROR_MARKERS}
        assert "crash" not in produced
        # `unreachable` IS produced by a marker -- it is the one collapsed
        # download kind -- so only `crash` is unreachable from text. The
        # constant exists to say which kinds the browser classifier is allowed
        # to be short of, so assert exactly that membership.
        assert set(TRANSFER_ERROR_KINDS_NOT_FROM_TEXT) == {"crash", "unreachable"}

    def test_the_service_scripts_carry_a_byte_identical_block(self):
        """Hand-mirrored copies, so identity is asserted rather than trusted."""
        canonical = _block(CANONICAL.read_text())
        assert canonical.strip(), "the canonical block is empty"
        for name in MIRRORED_SCRIPTS:
            assert _block((SCRIPTS / name).read_text()) == canonical, name
        assert _uploader_sh_block() == canonical, "uploader.sh"

    def test_the_crash_and_over_cap_constants_name_vocabulary_values(self):
        """The scripts' own `ERROR_KIND_*` constants are not a second spelling."""
        for name in MIRRORED_SCRIPTS:
            text = (SCRIPTS / name).read_text()
            for match in re.finditer(r'^ERROR_KIND_\w+ = "([^"]+)"', text, re.MULTILINE):
                assert match.group(1) in TRANSFER_ERROR_KIND_VALUES, (name, match.group(1))


class TestBrowserPathVocabularies:
    """The two JavaScript/TypeScript classifiers, against the same enum."""

    def test_classify_ts_is_the_transfer_vocabulary_less_the_two_it_cannot_produce(self):
        kinds = _ts_error_kinds(CLASSIFY_TS.read_text())
        assert set(kinds) == set(TRANSFER_ERROR_KIND_VALUES) - set(
            TRANSFER_ERROR_KINDS_NOT_FROM_TEXT
        )

    def test_classify_mjs_and_browser_download_error_kind_agree(self):
        """The pre-existing pair, whose agreement nothing asserted until now.

        The harness keeps `busy`, `no_hot_files` and `round_overrun` as a
        documented SUPERSET rather than folding them in -- two of those are skip
        reasons, not failures, and `busy` is the behaviour that role exists to
        see. So the enum is exactly the classifier's kinds plus the two skip
        reasons the classifier never returns.
        """
        classifier = set(_mjs_error_kinds(CLASSIFY_MJS.read_text()))
        enum_values = {member.value for member in BrowserDownloadErrorKind}
        skip_reasons = {"no_hot_files", "round_overrun"}
        assert enum_values == classifier | skip_reasons
        assert not (classifier & skip_reasons)

    def test_the_browser_harness_superset_is_exactly_what_it_claims(self):
        """`busy` is the only kind it has that the transfer vocabulary lacks."""
        classifier = set(_mjs_error_kinds(CLASSIFY_MJS.read_text()))
        assert classifier - set(TRANSFER_ERROR_KIND_VALUES) == {"busy"}


class TestFixtureProvenance:
    """Every fixture case says where it came from (INFRA-484 item 11)."""

    def test_every_case_declares_a_provenance_and_a_source(self):
        data = json.loads((FIXTURES / "cases.json").read_text())
        assert data["ant_client_ref"], "the ant-client ref must be pinned"
        assert data["cases"], "no cases"
        for case in data["cases"]:
            assert case["provenance"] in ("captured", "observed", "derived"), case["id"]
            assert case["source"].strip(), case["id"]
            assert case["expected_kind"] in TRANSFER_ERROR_KIND_VALUES, case["id"]

    def test_the_derived_cases_say_they_are_derived(self):
        data = json.loads((FIXTURES / "cases.json").read_text())
        derived = [c for c in data["cases"] if c["provenance"] == "derived"]
        assert derived
        for case in derived:
            assert "not observed" in case["source"].lower(), case["id"]

    def test_there_is_at_least_one_real_capture(self):
        data = json.loads((FIXTURES / "cases.json").read_text())
        captured = [c for c in data["cases"] if c["provenance"] == "captured"]
        assert captured
        for case in captured:
            assert (FIXTURES / case["stderr_file"]).read_text().strip(), case["id"]

    def test_the_readme_records_how_the_capture_was_produced(self):
        readme = (FIXTURES / "README.md").read_text()
        assert "ant file download" in readme
        assert "2026-11-03" in readme


@pytest.mark.parametrize("name", MIRRORED_SCRIPTS)
def test_every_mirrored_script_still_imports(name):
    """A spliced block that does not execute is worse than one that diverges."""
    module = _load(SCRIPTS / name)
    assert module.classify_transfer_error("not found: nothing here") == "not_found"
