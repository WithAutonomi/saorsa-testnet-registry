"""Every `releases` invocation the skills emit must actually parse.

The release skills call these commands unattended, in the middle of a train.
A flag that only exists on the group parser, or a required argument nobody
supplies, would surface as an argparse error at the worst possible moment —
after the tag is pushed and the crates are published.
"""

import pytest

from saorsa_deploy.main import main

# Exactly the argv the four skill call sites emit.
SKILL_INVOCATIONS = [
    # ship-to-beta Step 7
    [
        "releases", "ship",
        "--binary-target", "ant-node",
        "--version", "0.17.1",
        "--rc-branch", "rc-2026.8.2",
        "--beta-tag", "v0.17.1-beta.1",
        "--beta-tag-sha", "a" * 40,
        "--shipped-at", "2026-08-19",
        "--planned-promotion", "2026-08-24",
        "--rc-version", "0.17.1-rc.1",
    ],
    [
        "releases", "ship",
        "--binary-target", "ant",
        "--version", "0.3.3",
        "--rc-branch", "rc-2026.8.2",
        "--beta-tag", "ant-cli-v0.3.3-beta.1",
        "--beta-tag-sha", "b" * 40,
    ],
    # release-from-rc Step 10.3
    ["releases", "promote", "--from-file", "/tmp/release-manifest.json"],
    # release-from-rc Step 10.3, pre-beta-structure train / backfill
    [
        "releases", "promote",
        "--from-file", "/tmp/release-manifest.json",
        "--allow-missing-ship",
    ],
    # beta-health-verdict Step 7, NO-PROMOTE
    [
        "releases", "rolled-past",
        "--binary-target", "ant-node",
        "--version", "0.17.1",
        "--reason", "NO-PROMOTE: chunk loss regression",
    ],
    # beta-health-verdict Step 1 / release-from-rc Step 0
    ["releases", "current"],
    ["releases", "current", "--binary-target", "ant-node"],
    # read-backs
    ["releases", "ls"],
    ["releases", "ls", "--binary-target", "ant-node", "--limit", "5"],
    ["releases", "ls", "--outcome", "promoted"],
    ["releases", "show", "--id", "1"],
    ["releases", "show", "--binary-target", "ant-node", "--version", "0.17.1"],
]


@pytest.mark.parametrize("argv", SKILL_INVOCATIONS, ids=lambda a: " ".join(a[:3]))
def test_skill_invocation_parses(argv, monkeypatch):
    """Parses and dispatches — the command body is stubbed out, so this is
    purely about argv reaching the right handler."""
    called = {}

    def _stub(args):
        called["verb"] = args.releases_verb

    for module, attr in (
        ("ship_cmd", "cmd_releases_ship"),
        ("promote_cmd", "cmd_releases_promote"),
        ("rolled_past_cmd", "cmd_releases_rolled_past"),
        ("current_cmd", "cmd_releases_current"),
        ("ls_cmd", "cmd_releases_ls"),
        ("show_cmd", "cmd_releases_show"),
    ):
        monkeypatch.setattr(
            f"saorsa_deploy.cmd.releases.{module}.{attr}", _stub
        )

    main(argv)
    assert called["verb"] == argv[1]


def test_rolled_past_requires_a_reason():
    """A rolled-past row with no reason answers nothing, so argparse enforces it."""
    with pytest.raises(SystemExit):
        main(["releases", "rolled-past", "--id", "1"])


def test_ship_requires_the_lineage_identifiers():
    with pytest.raises(SystemExit):
        main(["releases", "ship", "--binary-target", "ant-node"])


def test_ls_rejects_an_unknown_outcome():
    with pytest.raises(SystemExit):
        main(["releases", "ls", "--outcome", "abandoned"])
