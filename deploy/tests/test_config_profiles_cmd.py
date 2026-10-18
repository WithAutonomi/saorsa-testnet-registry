"""Tests for `deployments config profiles` — the listing of bundled profiles.

The command exists so a run issue can be checked against what the *pinned* wheel
actually ships, which is only useful if the descriptions say what each profile
is — hence the assertions on the text, not just on the names.
"""

from argparse import Namespace

import pytest

from saorsa_deploy.cmd.config_cmds import _profile_description, cmd_config_profiles
from saorsa_deploy.config import list_profiles, profiles_dir


def _render(capsys):
    cmd_config_profiles(Namespace())
    return capsys.readouterr().out


def test_lists_every_bundled_profile(capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "400")
    out = _render(capsys)
    names = list_profiles()
    assert names
    for name in names:
        assert name in out
    assert f"{len(names)} profiles" in out
    assert "profile:<name>" in out


def test_describes_a_profile_from_its_leading_comment(capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "400")
    out = _render(capsys)
    assert "Small base profile" in out  # small.yaml's first comment line


def test_description_skips_a_comment_that_is_only_the_file_name():
    """staging.yaml opens with `# staging.yaml`, which says nothing."""
    description = _profile_description(profiles_dir() / "staging.yaml")
    assert description != "staging.yaml"
    assert "staging profile" in description


def test_description_is_empty_for_a_profile_with_no_leading_comment(tmp_path):
    path = tmp_path / "bare.yaml"
    path.write_text("kind: testnet\n\n# a comment after the first key\n")
    assert _profile_description(path) == ""


def test_exits_2_when_the_package_ships_no_profiles(monkeypatch, tmp_path, capsys):
    """A wheel built without the package data is a broken install, not an empty list."""
    monkeypatch.setattr("saorsa_deploy.cmd.config_cmds.list_profiles", lambda: [])
    monkeypatch.setattr("saorsa_deploy.cmd.config_cmds.profiles_dir", lambda: tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        cmd_config_profiles(Namespace())
    assert excinfo.value.code == 2
    assert "no profiles found" in capsys.readouterr().out


if __name__ == "__main__":
    pytest.main([__file__])
