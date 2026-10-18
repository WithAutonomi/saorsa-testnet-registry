"""The settings file `saorsa-deploy` reads its credentials from (V2-1624).

Every test names its own file and its own working directory. The default
resolution reaches `~/.config/saorsa-deploy/env` and the checkout's own `.env`,
both of which hold the operator's real credentials — a test that let either be
read would pass for a reason that has nothing to do with what it asserts, and
would put a live connection string in a failure message.
"""

import os
from pathlib import Path

import pytest

from saorsa_deploy.env_file import (
    DEFAULT_ENV_FILE,
    ENV_FILE_VAR,
    ENV_KEY_GROUPS,
    EnvFileError,
    load_env_files,
    resolve_env_file,
)

KEY = "SAORSA_TEST_ONLY_VALUE"


@pytest.fixture(autouse=True)
def clean_key(monkeypatch, tmp_path):
    """Unset the key under test and point the settings file at a tmp path."""
    monkeypatch.delenv(KEY, raising=False)
    monkeypatch.setenv(ENV_FILE_VAR, str(tmp_path / "settings-env"))
    return tmp_path


class TestResolution:
    def test_the_default_path_is_under_dot_config(self, monkeypatch):
        monkeypatch.delenv(ENV_FILE_VAR, raising=False)
        assert resolve_env_file() == Path(DEFAULT_ENV_FILE).expanduser()
        assert not str(resolve_env_file()).startswith("~")

    def test_the_env_var_overrides_the_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_FILE_VAR, str(tmp_path / "elsewhere"))
        assert resolve_env_file() == tmp_path / "elsewhere"

    def test_a_tilde_in_the_override_is_expanded(self, monkeypatch):
        monkeypatch.setenv(ENV_FILE_VAR, "~/somewhere/env")
        assert resolve_env_file() == Path.home() / "somewhere" / "env"

    def test_an_empty_override_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv(ENV_FILE_VAR, "   ")
        assert resolve_env_file() == Path(DEFAULT_ENV_FILE).expanduser()


class TestPrecedence:
    """Environment, then the settings file, then `./.env`.

    The order is what lets one invocation override one key, and what keeps the
    checkout's `.env` working for `uv run` in the checkout while an installed
    copy reads only the settings file.
    """

    def test_the_environment_beats_the_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv(KEY, "from-environment")
        (tmp_path / "settings-env").write_text(f"{KEY}=from-file\n")
        (tmp_path / ".env").write_text(f"{KEY}=from-dot-env\n")
        load_env_files(cwd=tmp_path)
        assert os.environ[KEY] == "from-environment"

    def test_the_file_beats_dot_env(self, tmp_path):
        (tmp_path / "settings-env").write_text(f"{KEY}=from-file\n")
        (tmp_path / ".env").write_text(f"{KEY}=from-dot-env\n")
        loaded = load_env_files(cwd=tmp_path)
        assert os.environ[KEY] == "from-file"
        assert loaded == [tmp_path / "settings-env", tmp_path / ".env"]

    def test_dot_env_still_supplies_what_the_file_does_not(self, tmp_path):
        (tmp_path / "settings-env").write_text("OTHER_KEY=x\n")
        (tmp_path / ".env").write_text(f"{KEY}=from-dot-env\n")
        load_env_files(cwd=tmp_path)
        assert os.environ[KEY] == "from-dot-env"

    def test_the_file_alone_supplies_the_value(self, tmp_path):
        (tmp_path / "settings-env").write_text(f"{KEY}=from-file\n")
        assert load_env_files(cwd=tmp_path) == [tmp_path / "settings-env"]
        assert os.environ[KEY] == "from-file"


class TestFormat:
    def test_quoted_values_are_unquoted(self, tmp_path):
        (tmp_path / "settings-env").write_text(
            f"{KEY}=\"double quoted\"\nSAORSA_TEST_ONLY_SINGLE='single quoted'\n"
        )
        load_env_files(cwd=tmp_path)
        try:
            assert os.environ[KEY] == "double quoted"
            assert os.environ["SAORSA_TEST_ONLY_SINGLE"] == "single quoted"
        finally:
            os.environ.pop("SAORSA_TEST_ONLY_SINGLE", None)

    def test_an_export_keyword_is_tolerated(self, tmp_path):
        (tmp_path / "settings-env").write_text(f"export {KEY}=exported\n")
        load_env_files(cwd=tmp_path)
        assert os.environ[KEY] == "exported"

    def test_comments_and_blank_lines_are_ignored(self, tmp_path):
        (tmp_path / "settings-env").write_text(f"# a comment\n\n{KEY}=value\n")
        load_env_files(cwd=tmp_path)
        assert os.environ[KEY] == "value"

    def test_a_dollar_sign_in_a_value_is_literal(self, tmp_path):
        """No evaluation: the format is systemd's, where `$` is a character.

        python-dotenv interpolates `${…}` by default, which would corrupt any
        password containing one — and would be evaluation this file format does
        not have.
        """
        (tmp_path / "settings-env").write_text(f"{KEY}=pa${{HOME}}ss\n")
        load_env_files(cwd=tmp_path)
        assert os.environ[KEY] == "pa${HOME}ss"


class TestFailureModes:
    def test_a_missing_file_is_silent(self, tmp_path):
        assert load_env_files(cwd=tmp_path) == []

    def test_an_unreadable_file_errors_and_names_the_path(self, tmp_path):
        path = tmp_path / "settings-env"
        path.write_text(f"{KEY}=value\n")
        path.chmod(0o000)
        try:
            with pytest.raises(EnvFileError) as excinfo:
                load_env_files(cwd=tmp_path)
            assert str(path) in str(excinfo.value)
        finally:
            path.chmod(0o600)

    def test_a_directory_at_the_path_errors_too(self, tmp_path):
        """ "Could not look" is not "nothing there"."""
        (tmp_path / "settings-env").mkdir()
        with pytest.raises(EnvFileError) as excinfo:
            load_env_files(cwd=tmp_path)
        assert str(tmp_path / "settings-env") in str(excinfo.value)

    def test_an_unreadable_dot_env_errors_as_well(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text(f"{KEY}=value\n")
        path.chmod(0o000)
        try:
            with pytest.raises(EnvFileError) as excinfo:
                load_env_files(cwd=tmp_path)
            assert str(path) in str(excinfo.value)
        finally:
            path.chmod(0o600)


class TestTheKeyInventory:
    def test_the_keys_the_tool_actually_reads_are_listed(self):
        names = {key.name for group in ENV_KEY_GROUPS for key in group.keys}
        # One per group, and each one is a real call site: a key dropped from
        # the inventory makes `config env-file` report a configured tool as
        # ready when the command it is about to run has nothing to read.
        for expected in (
            "DATABASE_URL",
            "DO_TOKEN",
            "HCLOUD_TOKEN",
            "VULTR_API_KEY",
            "OVH_APPLICATION_KEY",
            "CNTB_OAUTH2_CLIENT_ID",
            "AWS_ACCESS_KEY_ID",
            "ANT_CLIENT_AWS_ACCESS_KEY_ID",
            "ANT_DEPLOY_SECRET_KEY",
            "ARBITRUM_ONE_SECRET_KEY",
            "GITHUB_TOKEN",
            "ANT_DEPLOY_SLACK_BOT_TOKEN",
            "SAORSA_DEPLOY_STATE_DIR",
        ):
            assert expected in names

    def test_the_vm_side_script_variables_are_not_listed(self):
        """`WORK_DIR` and friends are read by scripts on a client VM.

        They are in the same `os.environ` grep as the credentials and belong to
        a different machine entirely; reporting them would ask the operator to
        set variables the tool never looks at.
        """
        names = {key.name for group in ENV_KEY_GROUPS for key in group.keys}
        for unexpected in (
            "WORK_DIR",
            "ANT",
            "LOG_DIR",
            "INFLUX_TOKEN",
            "ONE_SHOT",
            "FLASK_SECRET_KEY",
        ):
            assert unexpected not in names

    def test_every_key_has_a_purpose_and_no_duplicates(self):
        names = [key.name for group in ENV_KEY_GROUPS for key in group.keys]
        assert len(names) == len(set(names))
        assert all(key.purpose for group in ENV_KEY_GROUPS for key in group.keys)


class TestTheDbModuleHasNoImportSideEffect:
    def test_saorsa_db_db_does_not_load_dotenv(self):
        """The regression this feature exists to remove.

        `load_dotenv()` at import time searched upward from
        `db/saorsa_db/db.py`, which is a checkout for `uv run` and
        `~/.cache/uv/...` for a wheel — so an installed copy got nothing and
        reported "DATABASE_URL is not set" with a settings file sitting next to
        it.
        """
        import saorsa_db.db as module

        # Asserted on the import rather than on the call: the module cannot
        # call `load_dotenv` without one, and its docstring names the function
        # while explaining why it is gone.
        source = Path(module.__file__).read_text()
        assert "from dotenv" not in source
        assert "import dotenv" not in source
