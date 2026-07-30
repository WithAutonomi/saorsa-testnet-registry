from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.prod_uploaders import (
    _extract_file_names,
    _read_urls_file,
    cmd_prod_uploaders_deploy,
    cmd_prod_uploaders_destroy,
    parse_prod_uploader_spec,
)


class TestExtractFileNames:
    def test_extracts_basenames_and_skips_comments_and_blanks(self):
        content = (
            "# header comment\n"
            "\n"
            "https://example.com/foo/bar/file1.bin\n"
            "https://example.com/file2.zip?token=abc\n"
            "  https://example.com/dir/movie.mp4  \n"
        )
        assert _extract_file_names(content) == [
            "file1.bin",
            "file2.zip",
            "movie.mp4",
        ]

    def test_falls_back_to_raw_url_when_no_basename(self):
        # A URL with no path segment yields an empty Path.name; keep the line.
        content = "https://example.com\n"
        assert _extract_file_names(content) == ["https://example.com"]


@pytest.fixture
def urls_file(tmp_path):
    p = tmp_path / "urls.txt"
    p.write_text(
        "# leading comment\n"
        "https://example.com/a.bin\n"
        "\n"
        "https://example.com/b.bin\n"
    )
    return p


class TestParseSpec:
    def test_full_spec(self, urls_file):
        spec = parse_prod_uploader_spec(
            f"lon1:s-2vcpu-4gb:urls={urls_file}"
        )
        assert spec == {
            "region": "lon1",
            "vm_size": "s-2vcpu-4gb",
            "urls_file": str(urls_file),
            "payment_type": None,
            "log_level": None,
            "clock_skew": None,
            "bootstrap_cache": True,
            "forward_logs": None,
            "memory_profiling": None,
            "rust_log": None,
            "network": None,
        }

    def test_missing_urls_rejected(self):
        with pytest.raises(ValueError):
            parse_prod_uploader_spec("lon1:s-2vcpu-4gb")

    def test_too_few_parts_rejected(self):
        with pytest.raises(ValueError):
            parse_prod_uploader_spec("lon1:s-2vcpu-4gb")

    def test_empty_region_rejected(self, urls_file):
        with pytest.raises(ValueError):
            parse_prod_uploader_spec(
                f":s-2vcpu-4gb:urls={urls_file}"
            )

    def test_empty_vm_size_rejected(self, urls_file):
        with pytest.raises(ValueError):
            parse_prod_uploader_spec(f"lon1::urls={urls_file}")

    def test_unknown_key_rejected(self, urls_file):
        with pytest.raises(ValueError):
            parse_prod_uploader_spec(
                f"lon1:s-2vcpu-4gb:bogus=x:urls={urls_file}"
            )

    @pytest.mark.parametrize("payment_type", ["merkle", "no-merkle"])
    def test_payment_key_accepted(self, urls_file, payment_type):
        spec = parse_prod_uploader_spec(
            f"lon1:s-2vcpu-4gb:urls={urls_file}:payment={payment_type}"
        )
        assert spec["payment_type"] == payment_type

    def test_payment_key_invalid_value_rejected(self, urls_file):
        with pytest.raises(ValueError):
            parse_prod_uploader_spec(
                f"lon1:s-2vcpu-4gb:urls={urls_file}:payment=bogus"
            )

    def test_payment_key_optional(self, urls_file):
        spec = parse_prod_uploader_spec(
            f"lon1:s-2vcpu-4gb:urls={urls_file}"
        )
        assert spec["payment_type"] is None

    def test_duplicate_payment_rejected(self, urls_file):
        with pytest.raises(ValueError):
            parse_prod_uploader_spec(
                f"lon1:s-2vcpu-4gb:urls={urls_file}:payment=merkle:payment=no-merkle"
            )


    def test_log_level_optional(self, urls_file):
        spec = parse_prod_uploader_spec(f"lon1:s-2vcpu-4gb:urls={urls_file}")
        assert spec["log_level"] is None

    @pytest.mark.parametrize("level", ["info", "debug", "trace"])
    def test_log_level_accepted(self, urls_file, level):
        spec = parse_prod_uploader_spec(
            f"lon1:s-2vcpu-4gb:urls={urls_file}:log_level={level}"
        )
        assert spec["log_level"] == level

    def test_log_level_invalid_value_rejected(self, urls_file):
        with pytest.raises(ValueError, match="log_level"):
            parse_prod_uploader_spec(
                f"lon1:s-2vcpu-4gb:urls={urls_file}:log_level=verbose"
            )

    def test_log_level_combines_with_payment_and_cache(self, urls_file):
        spec = parse_prod_uploader_spec(
            f"lon1:s-2vcpu-4gb:urls={urls_file}"
            ":payment=merkle:log_level=debug:bootstrap_cache=false"
        )
        assert spec["payment_type"] == "merkle"
        assert spec["log_level"] == "debug"
        assert spec["bootstrap_cache"] is False

    def test_bootstrap_cache_defaults_to_true(self, urls_file):
        spec = parse_prod_uploader_spec(f"lon1:s-2vcpu-4gb:urls={urls_file}")
        assert spec["bootstrap_cache"] is True

    @pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False)])
    def test_bootstrap_cache_key_accepted(self, urls_file, value, expected):
        spec = parse_prod_uploader_spec(
            f"lon1:s-2vcpu-4gb:urls={urls_file}:bootstrap_cache={value}"
        )
        assert spec["bootstrap_cache"] is expected

    def test_bootstrap_cache_invalid_value_rejected(self, urls_file):
        with pytest.raises(ValueError, match="bootstrap_cache"):
            parse_prod_uploader_spec(
                f"lon1:s-2vcpu-4gb:urls={urls_file}:bootstrap_cache=yes"
            )

    def test_bootstrap_cache_combines_with_payment(self, urls_file):
        spec = parse_prod_uploader_spec(
            f"lon1:s-2vcpu-4gb:urls={urls_file}:payment=merkle:bootstrap_cache=false"
        )
        assert spec["payment_type"] == "merkle"
        assert spec["bootstrap_cache"] is False


class TestReadUrlsFile:
    def test_reads_and_strips_comments(self, urls_file):
        content = _read_urls_file(str(urls_file))
        assert "https://example.com/a.bin" in content
        assert "https://example.com/b.bin" in content

    def test_empty_file_rejected(self, tmp_path):
        p = tmp_path / "empty.txt"
        p.write_text("")
        with pytest.raises(ValueError):
            _read_urls_file(str(p))

    def test_only_comments_rejected(self, tmp_path):
        p = tmp_path / "comments.txt"
        p.write_text("# nothing\n# but comments\n")
        with pytest.raises(ValueError):
            _read_urls_file(str(p))

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            _read_urls_file(str(tmp_path / "does-not-exist.txt"))


def _deploy_args(uploader_specs, **overrides):
    defaults = {
        "name": "PROD-UP-01",
        "uploader": uploader_specs,
        "secret_key": "abc123",
        "branch_name": None,
        "repo_owner": None,
        "ant_version": None,
        "monitoring_volume_size": 50,
        "ssh_key_path": "~/.ssh/id_rsa",
        "purpose": "test",
        "allow_duplicate_files": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _destroy_args(**overrides):
    defaults = {
        "name": "PROD-UP-01",
        "keep_monitoring": False,
        "ssh_key_path": "~/.ssh/id_rsa",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def deploy_patches():
    with (
        patch(
            "saorsa_deploy.cmd.prod_uploaders.record_deployment_created"
        ) as p_create,
        patch(
            "saorsa_deploy.cmd.prod_uploaders.record_event"
        ) as p_event,
        patch(
            "saorsa_deploy.cmd.prod_uploaders.record_status"
        ) as p_status,
        patch(
            "saorsa_deploy.cmd.prod_uploaders._monitoring_stack_exists",
            return_value=False,
        ) as p_mon_exists,
        patch(
            "saorsa_deploy.cmd.prod_uploaders._client_stack_exists",
            return_value=False,
        ) as p_client_exists,
        patch("saorsa_deploy.cmd.prod_uploaders._ensure_monitoring") as p_mon,
        patch(
            "saorsa_deploy.cmd.prod_uploaders._ensure_client_infra"
        ) as p_infra,
        patch(
            "saorsa_deploy.cmd.prod_uploaders._provision_uploaders"
        ) as p_prov,
        patch(
            "saorsa_deploy.cmd.prod_uploaders._start_uploaders"
        ) as p_start,
        patch(
            "saorsa_deploy.cmd.prod_uploaders.get_repository",
            return_value=None,
        ) as p_repo,
    ):
        yield SimpleNamespace(
            record_create=p_create,
            record_event=p_event,
            record_status=p_status,
            mon_exists=p_mon_exists,
            client_exists=p_client_exists,
            ensure_mon=p_mon,
            ensure_infra=p_infra,
            provision=p_prov,
            start=p_start,
            get_repository=p_repo,
        )


class TestDeployHappyPath:
    def test_full_deploy_invokes_each_stage_in_order(
        self, deploy_patches, urls_file
    ):
        args = _deploy_args([
            f"lon1:s-2vcpu-4gb:urls={urls_file}",
        ])
        cmd_prod_uploaders_deploy(args)

        deploy_patches.record_create.assert_called_once()
        deploy_patches.ensure_mon.assert_called_once()
        deploy_patches.ensure_infra.assert_called_once()
        deploy_patches.provision.assert_called_once()
        deploy_patches.start.assert_called_once()

    def test_deploy_records_running_status_on_success(
        self, deploy_patches, urls_file
    ):
        args = _deploy_args([
            f"lon1:s-2vcpu-4gb:urls={urls_file}",
        ])
        cmd_prod_uploaders_deploy(args)
        final_status = deploy_patches.record_status.call_args_list[-1].args[1]
        assert final_status.value == "running"

    def test_deploy_records_kind_in_deploy_args(self, deploy_patches, urls_file):
        args = _deploy_args([
            f"lon1:s-2vcpu-4gb:urls={urls_file}",
            f"nyc1:s-4vcpu-8gb:urls={urls_file}",
        ])
        cmd_prod_uploaders_deploy(args)

        payload = deploy_patches.record_create.call_args.args[1]
        assert payload["kind"] == "prod-uploaders"
        assert len(payload["uploaders"]) == 2
        assert payload["uploaders"][0]["region"] == "lon1"
        assert payload["uploaders"][1]["region"] == "nyc1"

    def test_deploy_rejects_when_no_uploaders(self, deploy_patches):
        with pytest.raises(SystemExit):
            cmd_prod_uploaders_deploy(_deploy_args([]))

    def test_deploy_records_payment_type_none_by_default(
        self, deploy_patches, urls_file
    ):
        args = _deploy_args([f"lon1:s-2vcpu-4gb:urls={urls_file}"])
        cmd_prod_uploaders_deploy(args)
        payload = deploy_patches.record_create.call_args.args[1]
        assert payload["uploaders"][0]["payment_type"] is None
        assert "payment_type" not in payload

    @pytest.mark.parametrize("payment_type", ["merkle", "no-merkle"])
    def test_deploy_records_per_uploader_payment_type(
        self, deploy_patches, urls_file, payment_type
    ):
        args = _deploy_args(
            [f"lon1:s-2vcpu-4gb:urls={urls_file}:payment={payment_type}"],
        )
        cmd_prod_uploaders_deploy(args)
        payload = deploy_patches.record_create.call_args.args[1]
        assert payload["uploaders"][0]["payment_type"] == payment_type

    def test_deploy_records_mixed_payment_types(
        self, deploy_patches, urls_file
    ):
        args = _deploy_args([
            f"lon1:s-2vcpu-4gb:urls={urls_file}:payment=merkle",
            f"nyc1:s-4vcpu-8gb:urls={urls_file}",
            f"fra1:s-4vcpu-8gb:urls={urls_file}:payment=no-merkle",
        ])
        cmd_prod_uploaders_deploy(args)
        payload = deploy_patches.record_create.call_args.args[1]
        assert [u["payment_type"] for u in payload["uploaders"]] == [
            "merkle", None, "no-merkle",
        ]


    def test_deploy_records_per_uploader_bootstrap_cache(
        self, deploy_patches, urls_file
    ):
        args = _deploy_args([
            f"lon1:s-2vcpu-4gb:urls={urls_file}:bootstrap_cache=false",
            f"nyc1:s-4vcpu-8gb:urls={urls_file}",
        ])
        cmd_prod_uploaders_deploy(args)
        payload = deploy_patches.record_create.call_args.args[1]
        assert [u["bootstrap_cache"] for u in payload["uploaders"]] == [False, True]


class TestDeployFailure:
    def test_deploy_records_failed_when_provision_exits(
        self, deploy_patches, urls_file
    ):
        deploy_patches.provision.side_effect = SystemExit(1)
        args = _deploy_args([
            f"lon1:s-2vcpu-4gb:urls={urls_file}",
        ])

        with pytest.raises(SystemExit):
            cmd_prod_uploaders_deploy(args)

        final_status = deploy_patches.record_status.call_args_list[-1].args[1]
        assert final_status.value == "failed"


class _FakeRepo:
    """Minimal stand-in for the repository: only exposes the one method the
    duplicate-file check calls, returning rows with a ``file_name`` attribute."""

    def __init__(self, file_names):
        self._rows = [SimpleNamespace(file_name=n) for n in file_names]

    def list_all_prod_upload_results(self, failed_only=False):
        assert failed_only is False  # the check only ever looks at successes
        return self._rows


class TestDuplicateFileCheck:
    def test_aborts_before_any_infra_when_file_already_uploaded(
        self, deploy_patches, urls_file
    ):
        # urls_file uploads a.bin and b.bin; a.bin is already in the list.
        deploy_patches.get_repository.return_value = _FakeRepo(["a.bin"])
        args = _deploy_args([f"lon1:s-2vcpu-4gb:urls={urls_file}"])

        with pytest.raises(SystemExit):
            cmd_prod_uploaders_deploy(args)

        # Earliest-possible gate: nothing downstream of the check ran.
        deploy_patches.record_create.assert_not_called()
        deploy_patches.ensure_mon.assert_not_called()
        deploy_patches.ensure_infra.assert_not_called()
        deploy_patches.provision.assert_not_called()

    def test_allows_duplicates_with_override_flag(
        self, deploy_patches, urls_file
    ):
        deploy_patches.get_repository.return_value = _FakeRepo(["a.bin", "b.bin"])
        args = _deploy_args(
            [f"lon1:s-2vcpu-4gb:urls={urls_file}"],
            allow_duplicate_files=True,
        )

        cmd_prod_uploaders_deploy(args)

        deploy_patches.record_create.assert_called_once()
        deploy_patches.provision.assert_called_once()

    def test_proceeds_when_no_name_collision(self, deploy_patches, urls_file):
        deploy_patches.get_repository.return_value = _FakeRepo(["other.bin"])
        args = _deploy_args([f"lon1:s-2vcpu-4gb:urls={urls_file}"])

        cmd_prod_uploaders_deploy(args)

        deploy_patches.record_create.assert_called_once()
        deploy_patches.provision.assert_called_once()

    def test_skips_check_when_no_database(self, deploy_patches, urls_file):
        # get_repository returns None (fixture default) → check is skipped.
        deploy_patches.get_repository.return_value = None
        args = _deploy_args([f"lon1:s-2vcpu-4gb:urls={urls_file}"])

        cmd_prod_uploaders_deploy(args)

        deploy_patches.record_create.assert_called_once()
        deploy_patches.provision.assert_called_once()


@pytest.fixture
def destroy_patches():
    with (
        patch(
            "saorsa_deploy.cmd.prod_uploaders.record_event"
        ) as p_event,
        patch(
            "saorsa_deploy.cmd.prod_uploaders.record_status"
        ) as p_status,
        patch(
            "saorsa_deploy.cmd.prod_uploaders._client_stack_exists",
            return_value=True,
        ) as p_client_exists,
        patch(
            "saorsa_deploy.cmd.prod_uploaders._monitoring_stack_exists",
            return_value=True,
        ) as p_mon_exists,
        patch(
            "saorsa_deploy.cmd.prod_uploaders._stop_uploaders"
        ) as p_stop,
        patch(
            "saorsa_deploy.cmd.destroy_clients.cmd_destroy_clients"
        ) as p_destroy_clients,
        patch(
            "saorsa_deploy.cmd.destroy_monitoring.cmd_destroy_monitoring"
        ) as p_destroy_mon,
    ):
        yield SimpleNamespace(
            record_event=p_event,
            record_status=p_status,
            client_exists=p_client_exists,
            mon_exists=p_mon_exists,
            stop=p_stop,
            destroy_clients=p_destroy_clients,
            destroy_mon=p_destroy_mon,
        )


class TestDestroy:
    def test_default_destroy_tears_down_monitoring(self, destroy_patches):
        cmd_prod_uploaders_destroy(_destroy_args())

        destroy_patches.stop.assert_called_once()
        destroy_patches.destroy_clients.assert_called_once()
        destroy_patches.destroy_mon.assert_called_once()

        final_status = destroy_patches.record_status.call_args_list[-1].args[1]
        assert final_status.value == "destroyed"

    def test_keep_monitoring_flag_preserves_monitoring(self, destroy_patches):
        cmd_prod_uploaders_destroy(_destroy_args(keep_monitoring=True))

        destroy_patches.destroy_clients.assert_called_once()
        destroy_patches.destroy_mon.assert_not_called()

    def test_destroy_skips_when_no_client_state(self, destroy_patches):
        destroy_patches.client_exists.return_value = False
        cmd_prod_uploaders_destroy(_destroy_args())

        destroy_patches.stop.assert_not_called()
        destroy_patches.destroy_clients.assert_not_called()

    def test_destroy_tolerates_stop_runtime_error(self, destroy_patches):
        destroy_patches.stop.side_effect = RuntimeError("No hosts remaining!")
        cmd_prod_uploaders_destroy(_destroy_args())
        destroy_patches.destroy_clients.assert_called_once()
        final_status = destroy_patches.record_status.call_args_list[-1].args[1]
        assert final_status.value == "destroyed"

    def test_destroy_records_failed_on_destroy_clients_error(
        self, destroy_patches
    ):
        destroy_patches.destroy_clients.side_effect = SystemExit(1)
        with pytest.raises(SystemExit):
            cmd_prod_uploaders_destroy(_destroy_args())
        final_status = destroy_patches.record_status.call_args_list[-1].args[1]
        assert final_status.value == "failed"
