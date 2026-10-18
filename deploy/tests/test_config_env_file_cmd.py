"""`saorsa-deploy config env-file`, and `main()`'s startup load (V2-1624).

The command is the precondition check an unattended runner makes before it
deploys anything, so its output goes into a run log — which is why the hard
assertion here is that a value never appears in it.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from saorsa_deploy.cmd.config_cmds import cmd_config_env_file
from saorsa_deploy.env_file import ENV_FILE_VAR, ENV_KEY_GROUPS
from saorsa_deploy.main import main

SECRET = "0xdeadbeefcafebabe-never-print-me"


@pytest.fixture(autouse=True)
def isolated_settings_file(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_FILE_VAR, str(tmp_path / "settings-env"))
    # Rich wraps on the console width, and a tmp path plus the label is wider
    # than its 80-column default — so a path assertion would fail on where the
    # line happened to break rather than on what was printed.
    monkeypatch.setenv("COLUMNS", "240")
    return tmp_path


def _set_count(out: str) -> tuple[int, int]:
    """Parse the trailing "N of M set." line."""
    tail = [line for line in out.splitlines() if line.strip().endswith("set.")][-1]
    fields = tail.split()
    return int(fields[0]), int(fields[2])


class TestTheReport:
    def test_it_names_the_resolved_path(self, capsys, tmp_path):
        cmd_config_env_file(SimpleNamespace())
        out = capsys.readouterr().out
        assert str(tmp_path / "settings-env") in out
        assert ENV_FILE_VAR in out

    def test_an_absent_file_is_reported_as_absent_not_as_an_error(self, capsys):
        cmd_config_env_file(SimpleNamespace())
        out = capsys.readouterr().out
        assert "absent" in out
        assert "exists" not in out

    def test_a_present_file_is_reported_as_loaded(self, capsys, tmp_path):
        (tmp_path / "settings-env").write_text("DATABASE_URL=postgresql://x/y\n")
        cmd_config_env_file(SimpleNamespace())
        assert "exists" in capsys.readouterr().out

    def test_it_reports_set_and_unset_without_printing_the_value(self, capsys, monkeypatch):
        monkeypatch.setenv("ARBITRUM_ONE_SECRET_KEY", SECRET)
        monkeypatch.delenv("CNTB_OAUTH2_PASS", raising=False)
        cmd_config_env_file(SimpleNamespace())
        out = capsys.readouterr().out
        assert SECRET not in out
        assert "ARBITRUM_ONE_SECRET_KEY" in out
        assert "CNTB_OAUTH2_PASS" in out
        assert "yes" in out
        assert "no" in out

    def test_a_whitespace_only_value_counts_as_unset(self, capsys, monkeypatch):
        """An exported-but-empty variable is the shape of a half-set shell.

        `DO_TOKEN=` in a sourced file leaves the variable present and useless;
        reporting it as set would have the runner deploy and fail on the token.
        """
        monkeypatch.setenv("DO_TOKEN", "   ")
        cmd_config_env_file(SimpleNamespace())
        blank_set, total = _set_count(capsys.readouterr().out)
        monkeypatch.setenv("DO_TOKEN", "a-real-token")
        cmd_config_env_file(SimpleNamespace())
        real_set, real_total = _set_count(capsys.readouterr().out)
        assert real_total == total
        assert real_set == blank_set + 1

    def test_it_states_the_precedence(self, capsys):
        cmd_config_env_file(SimpleNamespace())
        out = capsys.readouterr().out.replace("\n", " ")
        assert "Precedence" in out
        assert ".env" in out

    def test_it_counts_how_many_are_set(self, capsys, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        cmd_config_env_file(SimpleNamespace())
        without, total = _set_count(capsys.readouterr().out)
        monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
        cmd_config_env_file(SimpleNamespace())
        with_url, _ = _set_count(capsys.readouterr().out)
        assert with_url == without + 1
        assert total >= len(ENV_KEY_GROUPS)


class TestTheStartupLoad:
    def test_main_loads_the_settings_file_before_the_command_runs(
        self, monkeypatch, tmp_path, capsys
    ):
        """The whole point: a command sees a variable only this file supplied."""
        monkeypatch.delenv("SAORSA_TEST_ONLY_VALUE", raising=False)
        (tmp_path / "settings-env").write_text("SAORSA_TEST_ONLY_VALUE=from-file\n")
        monkeypatch.chdir(tmp_path)
        try:
            main(["config", "env-file"])
            assert os.environ["SAORSA_TEST_ONLY_VALUE"] == "from-file"
        finally:
            os.environ.pop("SAORSA_TEST_ONLY_VALUE", None)
        capsys.readouterr()

    def test_main_exits_one_naming_an_unreadable_file(self, monkeypatch, tmp_path, capsys):
        path = tmp_path / "settings-env"
        path.write_text("DATABASE_URL=postgresql://x/y\n")
        path.chmod(0o000)
        monkeypatch.chdir(tmp_path)
        try:
            with pytest.raises(SystemExit) as excinfo:
                main(["config", "env-file"])
            assert excinfo.value.code == 1
            assert str(path) in capsys.readouterr().err
        finally:
            path.chmod(0o600)

    def test_main_is_silent_when_there_is_no_settings_file(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        main(["config", "env-file"])
        assert "Error" not in capsys.readouterr().err

    def test_the_load_precedes_every_lazy_command_import(self):
        """`main()` imports its command modules lazily, after the load.

        `saorsa_db.db` is pulled in by the first command module that touches the
        registry, and it no longer loads anything itself — so if the load moved
        below the dispatch, DATABASE_URL would be read before it was there.
        """
        import saorsa_deploy.main as module

        source = Path(module.__file__).read_text()
        body = source.split("def main(argv=None):", 1)[1]
        assert body.index("load_env_files()") < body.index("parser.parse_args(argv)")
