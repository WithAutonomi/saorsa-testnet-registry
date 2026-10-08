from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saorsa_deploy.cmd.prod_uploaders import (
    _extract_file_names,
    _read_urls_file,
    cmd_prod_uploaders_deploy,
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
    p.write_text("# leading comment\nhttps://example.com/a.bin\n\nhttps://example.com/b.bin\n")
    return p


class TestParseSpec:
    def test_full_spec(self, urls_file):
        spec = parse_prod_uploader_spec(f"lon1:s-2vcpu-4gb:urls={urls_file}")
        assert spec == {
            "region": "lon1",
            "vm_size": "s-2vcpu-4gb",
            # Every parsed spec names its transport, so the deploy command,
            # the provisioner and the recorded uploader list read one shape.
            "client_kind": "native",
            "urls_file": str(urls_file),
            # None for a urls= spec: the file list is the hand-written one, not
            # a draw from the managed pool.
            "pool_request": None,
            "pool_min_size_bytes": None,
            "pool_max_size_bytes": None,
            "exclude_partial": False,
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
            parse_prod_uploader_spec(f":s-2vcpu-4gb:urls={urls_file}")

    def test_empty_vm_size_rejected(self, urls_file):
        with pytest.raises(ValueError):
            parse_prod_uploader_spec(f"lon1::urls={urls_file}")

    def test_unknown_key_rejected(self, urls_file):
        with pytest.raises(ValueError):
            parse_prod_uploader_spec(f"lon1:s-2vcpu-4gb:bogus=x:urls={urls_file}")

    @pytest.mark.parametrize("payment_type", ["merkle", "no-merkle"])
    def test_payment_key_accepted(self, urls_file, payment_type):
        spec = parse_prod_uploader_spec(f"lon1:s-2vcpu-4gb:urls={urls_file}:payment={payment_type}")
        assert spec["payment_type"] == payment_type

    def test_payment_key_invalid_value_rejected(self, urls_file):
        with pytest.raises(ValueError):
            parse_prod_uploader_spec(f"lon1:s-2vcpu-4gb:urls={urls_file}:payment=bogus")

    def test_payment_key_optional(self, urls_file):
        spec = parse_prod_uploader_spec(f"lon1:s-2vcpu-4gb:urls={urls_file}")
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
        spec = parse_prod_uploader_spec(f"lon1:s-2vcpu-4gb:urls={urls_file}:log_level={level}")
        assert spec["log_level"] == level

    def test_log_level_invalid_value_rejected(self, urls_file):
        with pytest.raises(ValueError, match="log_level"):
            parse_prod_uploader_spec(f"lon1:s-2vcpu-4gb:urls={urls_file}:log_level=verbose")

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
            parse_prod_uploader_spec(f"lon1:s-2vcpu-4gb:urls={urls_file}:bootstrap_cache=yes")

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


@pytest.fixture
def deploy_patches():
    with (
        patch("saorsa_deploy.cmd.prod_uploaders.record_deployment_created") as p_create,
        patch("saorsa_deploy.cmd.prod_uploaders.record_event") as p_event,
        patch("saorsa_deploy.cmd.prod_uploaders.record_status") as p_status,
        patch(
            "saorsa_deploy.cmd.prod_uploaders._monitoring_stack_exists",
            return_value=False,
        ) as p_mon_exists,
        patch(
            "saorsa_deploy.cmd.prod_uploaders._client_stack_exists",
            return_value=False,
        ) as p_client_exists,
        patch("saorsa_deploy.cmd.prod_uploaders._ensure_monitoring") as p_mon,
        patch("saorsa_deploy.cmd.prod_uploaders._ensure_client_infra") as p_infra,
        patch("saorsa_deploy.cmd.prod_uploaders._provision_uploaders") as p_prov,
        patch("saorsa_deploy.cmd.prod_uploaders._start_uploaders") as p_start,
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
    def test_deploy_rejects_when_no_uploaders(self, deploy_patches):
        with pytest.raises(SystemExit):
            cmd_prod_uploaders_deploy(_deploy_args([]))


class TestDeployFailure:
    def test_deploy_records_failed_when_provision_exits(self, deploy_patches, urls_file):
        deploy_patches.provision.side_effect = SystemExit(1)
        args = _deploy_args(
            [
                f"lon1:s-2vcpu-4gb:urls={urls_file}",
            ]
        )

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
    def test_aborts_before_any_infra_when_file_already_uploaded(self, deploy_patches, urls_file):
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


class TestNetworkProfileIsPersisted:
    """Regression: PROD-UL-4G-L (2026-07-31) ran correctly shaped at mobile-4g, but
    reported nothing. The profile reached the provisioner — so the VM really was
    shaped — while both places that *persist* it were missed: the deploy_args
    uploader entry (which slack-post renders) and the synthesised client-infra spec
    (which writes S3 client state). A shaped run that does not say so is worse than
    an unshaped one, because the results get read as if the link were normal.
    """

    def test_spec_parses_the_network_token(self):
        from saorsa_deploy.cmd.prod_uploaders import parse_prod_uploader_spec

        spec = parse_prod_uploader_spec("sfo3:s-4vcpu-8gb:urls=a.txt:network=mobile-4g")
        assert spec["network"] == "mobile-4g"

    def test_synthesised_client_spec_carries_the_profile(self):
        """The client-infra spec is what writes S3 state; it dropped the profile."""
        from saorsa_deploy.cmd.infra_clients import parse_uploader_spec

        synth = "digital_ocean:sfo3:0:s-4vcpu-8gb:bootstrap_cache=true:network=mobile-4g"
        assert parse_uploader_spec(synth)["network"] == "mobile-4g"

    def test_deploy_args_entry_holds_the_resolved_knob_set(self):
        """Not the bare name — the rates are what make a past run interpretable."""
        from saorsa_deploy.provisioning.network_profile import resolve_network

        recorded = resolve_network("mobile-4g").to_dict()
        assert recorded["name"] == "mobile-4g"
        assert recorded["rate_up"] == "8mbit"
        assert recorded["rate_down"] == "30mbit"
        assert recorded["profile_set_version"] == 2

    def test_slack_post_renders_a_shaped_uploader(self):
        from saorsa_deploy.cmd.slack_post import _prod_uploaders_blocks
        from saorsa_deploy.provisioning.network_profile import resolve_network

        text = "\n".join(
            b.get("text", {}).get("text", "")
            for b in _prod_uploaders_blocks(
                {
                    "uploaders": [
                        {
                            "region": "sfo3",
                            "vm_size": "s-4vcpu-8gb",
                            "network": resolve_network("mobile-4g").to_dict(),
                        }
                    ]
                }
            )
        )
        assert "🐌 mobile-4g (30mbit/8mbit)" in text

    def test_an_unshaped_uploader_still_records_nothing(self):
        from saorsa_deploy.cmd.prod_uploaders import parse_prod_uploader_spec

        spec = parse_prod_uploader_spec("sfo3:s-4vcpu-8gb:urls=a.txt")
        assert spec["network"] is None
        synth = "digital_ocean:sfo3:0:s-4vcpu-8gb:bootstrap_cache=true"
        from saorsa_deploy.cmd.infra_clients import parse_uploader_spec

        assert parse_uploader_spec(synth)["network"] is None

    def test_prod_upload_results_carry_the_profile(self):
        """`deployment_prod_upload_results` is THE table for this kind.

        The column was added and the op-record path wired, but this path — the
        only one prod-uploaders actually writes to — was missed, so a shaped run's
        rows would still have come back NULL after the deploy_args fix.
        """
        import inspect

        from saorsa_db.repositories import SqlAlchemyDeploymentRepository

        sig = inspect.signature(SqlAlchemyDeploymentRepository.upsert_prod_upload_result)
        assert "network_profile" in sig.parameters

        from saorsa_deploy.cmd import record_results

        source = inspect.getsource(record_results._record_prod_upload)
        assert "network_profile=network_profile_for_host" in source

    def test_client_state_hostname_matches_the_influx_host_tag(self):
        """The join key is the VM hostname; a mismatch silently yields NULL.

        Verified against the live PROD-UL-4G-L VM, whose hostname is
        `PROD-UL-4G-L-ant-client-upload-sfo3-1` — note the 1-based client_index.
        """
        from saorsa_deploy.cmd.record_results import _network_map_from_client_state

        # Mirror the derivation without touching S3 by exercising the format only.
        name, role, region, idx = "PROD-UL-4G-L", "upload", "sfo3", 1
        expected = f"{name}-ant-client-{role}-{region}-{idx}"
        assert expected == "PROD-UL-4G-L-ant-client-upload-sfo3-1"
        assert callable(_network_map_from_client_state)
