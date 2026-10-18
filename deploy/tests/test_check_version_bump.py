"""The PR version-bump check (V2-1622).

antops pins a `saorsa-deploy` version in a Linear issue and fetches that wheel
from a GitHub release, so a change to the tool that does not move the version is
a change no run can ask for. `scripts/check_version_bump.py` is what stops one
reaching `main`: the PR declares its bump type in its body and CI checks the
four workspace versions against it.

The repository keeps no root `tests/` directory (it was removed when the CI gate
was fixed on 2026-09-27), so these live with the rest of the suite in
`deploy/tests/` even though the script under test is a repo-level one.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    """Repo-level scripts are standalone files, not importable modules."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check_version_bump():
    return _load("check_version_bump")


@pytest.fixture
def versions():
    """The four packages agreeing on one version, as they normally do."""

    def _versions(version: str) -> dict[str, str]:
        return {
            "deploy/pyproject.toml": version,
            "db/pyproject.toml": version,
            "web/pyproject.toml": version,
            "mcp/pyproject.toml": version,
        }

    return _versions


def body(value: str) -> str:
    """A PR body shaped like the template's, with the declaration in it."""
    return (
        "V2-1622 https://linear.app/autonominetwork/issue/V2-1622\n\n"
        f"Semantic version: {value}\n\n"
        "## Summary\n\n- something\n"
    )


# --- expected_version -------------------------------------------------------


@pytest.mark.parametrize(
    "base,declared,expected",
    [
        ("1.4.2", "breaking", "2.0.0"),
        ("1.4.2", "feature", "1.5.0"),
        ("1.4.2", "fix", "1.4.3"),
        # The bump that makes this repository's first release: a pre-1.0 base
        # does not get special treatment, breaking still means major+1.
        ("0.2.4", "breaking", "1.0.0"),
        ("0.2.4", "feature", "0.3.0"),
        ("0.2.4", "fix", "0.2.5"),
    ],
)
def test_expected_version(check_version_bump, base, declared, expected):
    assert check_version_bump.expected_version(base, declared) == expected


def test_expected_version_rejects_a_non_semver_base(check_version_bump):
    with pytest.raises(check_version_bump.CheckError, match="not a SemVer"):
        check_version_bump.expected_version("0.2", "fix")


# --- the Semantic version: line --------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ("Semantic version: breaking", "breaking"),
        ("Semantic version: feature", "feature"),
        ("Semantic version: fix", "fix"),
        ("Semantic version: none", "none"),
        ("semantic version: FIX", "fix"),
        ("SEMANTIC VERSION:Breaking", "breaking"),
        ("   Semantic version:    feature   ", "feature"),
        ("**Semantic version:** fix", "fix"),
        ("Semantic version: `none`", "none"),
    ],
)
def test_parse_declared_type(check_version_bump, line, expected):
    text = f"# A PR\n\n{line}\n\n## Summary\n"
    assert check_version_bump.parse_declared_type(text) == expected


def test_parse_declared_type_rejects_a_missing_line(check_version_bump):
    with pytest.raises(check_version_bump.CheckError, match="no `Semantic version:` line"):
        check_version_bump.parse_declared_type("## Summary\n\n- no declaration here\n")


def test_parse_declared_type_rejects_an_empty_body(check_version_bump):
    with pytest.raises(check_version_bump.CheckError, match="no `Semantic version:` line"):
        check_version_bump.parse_declared_type(None)


def test_parse_declared_type_rejects_the_unedited_template_placeholder(check_version_bump):
    """An author who never touched the line has declared nothing.

    The template ships `Semantic version: <breaking | feature | fix | none>`, so
    a parser lenient about the angle brackets would read the placeholder as a
    declaration of `breaking` and wave the PR through.
    """
    with pytest.raises(check_version_bump.CheckError, match="not a known type"):
        check_version_bump.parse_declared_type(
            "Semantic version: <breaking | feature | fix | none>"
        )


def test_parse_declared_type_rejects_a_typo(check_version_bump):
    with pytest.raises(check_version_bump.CheckError, match="not a known type"):
        check_version_bump.parse_declared_type("Semantic version: major")


# --- which paths are a change to the tool ----------------------------------


@pytest.mark.parametrize(
    "paths,touched",
    [
        (["deploy/saorsa_deploy/main.py"], True),
        (["db/saorsa_db/models/deployment.py"], True),
        (["db/alembic/versions/abc123_add_a_column.py"], True),
        (["deploy/pyproject.toml"], True),
        (["deploy/tests/test_main.py"], True),
        (["web/saorsa_web/app.py"], False),
        ([".github/workflows/ci.yml", "README.md"], False),
        # Documentation and run manifests live in the tool's trees but are not
        # the tool.
        (["deploy/README.md"], False),
        (["deploy/manifests/profiles/staging.yaml"], False),
        (["db/README.md"], False),
        (["deploy/README.md", "deploy/saorsa_deploy/state.py"], True),
        ([""], False),
        ([], False),
    ],
)
def test_paths_touch_tool(check_version_bump, paths, touched):
    assert check_version_bump.paths_touch_tool(paths) is touched


def test_paths_touch_tool_tolerates_trailing_newlines(check_version_bump):
    assert check_version_bump.paths_touch_tool(["deploy/saorsa_deploy/main.py\n", ""]) is True


# --- the four packages must agree ------------------------------------------


def test_single_head_version(check_version_bump, versions):
    assert check_version_bump.single_head_version(versions("1.0.0")) == "1.0.0"


def test_versions_that_disagree_are_named(check_version_bump, versions):
    head = versions("1.0.0")
    head["mcp/pyproject.toml"] = "0.2.4"
    with pytest.raises(check_version_bump.CheckError) as error:
        check_version_bump.single_head_version(head)
    message = str(error.value)
    assert "different versions" in message
    assert "mcp/pyproject.toml = 0.2.4" in message
    assert "deploy/pyproject.toml = 1.0.0" in message


# --- the check itself -------------------------------------------------------


@pytest.mark.parametrize(
    "declared,head",
    [("breaking", "1.0.0"), ("feature", "0.3.0"), ("fix", "0.2.5")],
)
def test_a_tool_pr_with_the_right_bump_passes(check_version_bump, versions, declared, head):
    summary = check_version_bump.check("0.2.4", versions(head), body(declared), tool_touched=True)
    assert summary == f"{declared}: 0.2.4 -> {head}"


@pytest.mark.parametrize(
    "declared,wrong,expected",
    [
        # The whole point: declaring breaking and bumping the patch.
        ("breaking", "0.2.5", "1.0.0"),
        ("feature", "1.0.0", "0.3.0"),
        ("fix", "0.3.0", "0.2.5"),
        # No bump at all is the common case this check exists to catch.
        ("fix", "0.2.4", "0.2.5"),
    ],
)
def test_a_tool_pr_with_the_wrong_bump_fails(
    check_version_bump, versions, declared, wrong, expected
):
    with pytest.raises(check_version_bump.CheckError) as error:
        check_version_bump.check("0.2.4", versions(wrong), body(declared), tool_touched=True)
    message = str(error.value)
    assert "0.2.4" in message
    assert expected in message
    assert f"scripts/bump_version.py {expected} --no-commit" in message


def test_a_tool_pr_declaring_none_fails(check_version_bump, versions):
    with pytest.raises(check_version_bump.CheckError) as error:
        check_version_bump.check("0.2.4", versions("0.2.4"), body("none"), tool_touched=True)
    assert "needs a version bump" in str(error.value)


def test_a_non_tool_pr_declaring_none_without_a_bump_passes(check_version_bump, versions):
    summary = check_version_bump.check("0.2.4", versions("0.2.4"), body("none"), tool_touched=False)
    assert summary == "none: version unchanged at 0.2.4"


def test_a_non_tool_pr_that_bumps_fails(check_version_bump, versions):
    with pytest.raises(check_version_bump.CheckError) as error:
        check_version_bump.check("0.2.4", versions("0.2.5"), body("none"), tool_touched=False)
    assert "revert the version bump and declare `none`" in str(error.value)


def test_a_non_tool_pr_declaring_a_bump_type_fails(check_version_bump, versions):
    """Declaring `fix` with the version unmoved is still wrong on a docs PR."""
    with pytest.raises(check_version_bump.CheckError) as error:
        check_version_bump.check("0.2.4", versions("0.2.4"), body("fix"), tool_touched=False)
    assert "revert the version bump and declare `none`" in str(error.value)


def test_the_check_requires_the_declaration_before_anything_else(check_version_bump, versions):
    with pytest.raises(check_version_bump.CheckError, match="no `Semantic version:` line"):
        check_version_bump.check("0.2.4", versions("1.0.0"), "## Summary\n", tool_touched=True)


# --- this repository's own state -------------------------------------------


def test_the_four_workspace_packages_share_one_version(check_version_bump):
    """The invariant the whole check rests on, asserted against the real tree.

    Not the literal version — that moves on every tool PR, and a test pinning it
    would have to be edited by every one of them.
    """
    repo_root = Path(__file__).resolve().parents[2]
    head = check_version_bump.collect_head_versions(repo_root)
    assert set(head) == set(check_version_bump.PYPROJECT_NAMES)
    version = check_version_bump.single_head_version(head)
    assert check_version_bump.SEMVER_RE.match(version), version


# --- the command-line entry point the workflow calls -----------------------


def _write_inputs(tmp_path, base_version, head_version, declared, changed):
    root = tmp_path / "head"
    for name in ("deploy", "db", "web", "mcp"):
        package = root / name
        package.mkdir(parents=True)
        (package / "pyproject.toml").write_text(
            f'[project]\nname = "saorsa-{name}"\nversion = "{head_version}"\n'
        )
    base = tmp_path / "base-deploy-pyproject.toml"
    base.write_text(f'[project]\nname = "saorsa-deploy"\nversion = "{base_version}"\n')
    body_file = tmp_path / "pr-body.md"
    body_file.write_text(body(declared))
    changed_file = tmp_path / "changed-files.txt"
    changed_file.write_text("\n".join(changed) + "\n")
    return [
        "--repo-root",
        str(root),
        "--base-pyproject",
        str(base),
        "--body-file",
        str(body_file),
        "--changed-files",
        str(changed_file),
    ]


def test_main_exits_zero_on_a_correct_tool_pr(check_version_bump, tmp_path, capsys):
    argv = _write_inputs(tmp_path, "0.2.4", "1.0.0", "breaking", ["deploy/saorsa_deploy/main.py"])
    assert check_version_bump.main(argv) == 0
    assert "version-bump: OK" in capsys.readouterr().out


def test_main_exits_one_and_says_what_to_do(check_version_bump, tmp_path, capsys):
    argv = _write_inputs(tmp_path, "0.2.4", "0.2.4", "fix", ["deploy/saorsa_deploy/main.py"])
    assert check_version_bump.main(argv) == 1
    captured = capsys.readouterr()
    assert "version-bump: FAIL" in captured.err
    assert "bump_version.py 0.2.5 --no-commit" in captured.err


def test_main_accepts_a_base_version_directly(check_version_bump, tmp_path):
    argv = _write_inputs(tmp_path, "0.2.4", "1.0.0", "breaking", ["db/saorsa_db/db.py"])
    argv = argv[:2] + ["--base-version", "0.2.4"] + argv[4:]
    assert check_version_bump.main(argv) == 0
