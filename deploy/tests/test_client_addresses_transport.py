"""Tests for the clock-skew-immune address-list transport.

Address lists live in the public ``saorsa-testnet-addresses`` bucket:
downloaders list+fetch them with unsigned HTTPS, and uploaders flush through
a presigned PUT URL minted at provision time — both paths avoid the VM
signing anything, so they keep working under clock skew (SigV4 tolerates
only ~15min).
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saorsa_deploy.provisioning.client import AntClientProvisioner

SCRIPTS_DIR = (
    Path(__file__).parent.parent / "saorsa_deploy" / "resources" / "scripts"
)


# --------------------------------------------------------------------------- #
# Presigned PUT URL minting (provisioner)
# --------------------------------------------------------------------------- #
def _provisioner(**overrides):
    kwargs = dict(
        clients=[],
        bootstrap_peers=["1.2.3.4:10000"],
        aws_access_key_id="AKIATEST",
        aws_secret_access_key="secret",
        addresses_s3_bucket="saorsa-testnet-addresses",
        addresses_s3_prefix="saorsa-deploy/addresses/DEV-01",
    )
    kwargs.update(overrides)
    return AntClientProvisioner(**kwargs)


class TestPresignedPutUrl:
    def test_mints_url_for_uploader_object(self):
        with patch("saorsa_deploy.provisioning.client.boto3.client") as mock_client:
            s3 = MagicMock()
            s3.generate_presigned_url.return_value = "https://example/presigned"
            mock_client.return_value = s3

            url = _provisioner()._presigned_put_url("lon1", 1)

        assert url == "https://example/presigned"
        s3.generate_presigned_url.assert_called_once_with(
            "put_object",
            Params={
                "Bucket": "saorsa-testnet-addresses",
                "Key": "saorsa-deploy/addresses/DEV-01/upload-lon1-1.txt",
            },
            ExpiresIn=7 * 24 * 3600,
        )

    def test_empty_when_no_credentials(self):
        with patch("saorsa_deploy.provisioning.client.boto3.client") as mock_client:
            url = _provisioner(aws_access_key_id="")._presigned_put_url("lon1", 1)
        assert url == ""
        mock_client.assert_not_called()


# --------------------------------------------------------------------------- #
# Bash-level behaviour, with stubbed curl/aws on PATH
# --------------------------------------------------------------------------- #
def _extract_function(script: str, func_name: str) -> str:
    """Return the named shell function's full text from a script."""
    text = (SCRIPTS_DIR / script).read_text()
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{func_name}()"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _run_function(
    script: str,
    func_name: str,
    call: str,
    env_vars: dict,
    stub_dir: Path,
) -> subprocess.CompletedProcess:
    body = _extract_function(script, func_name)
    exports = "\n".join(f'export {k}="{v}"' for k, v in env_vars.items())
    return subprocess.run(
        ["bash", "-c", f'export PATH="{stub_dir}:$PATH"\n{exports}\n{body}\n{call}'],
        capture_output=True,
        text=True,
    )


_LIST_XML = (
    '<?xml version="1.0"?><ListBucketResult>'
    "<Key>saorsa-deploy/addresses/DEV-01/upload-lon1-1.txt</Key>"
    "<Key>saorsa-deploy/addresses/DEV-01/upload-nyc1-2.txt</Key>"
    "</ListBucketResult>"
)


@pytest.fixture
def curl_stub(tmp_path):
    """A fake curl that serves a canned list-XML and object body, recording args."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    log = tmp_path / "curl.log"
    (stub_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        # Presigned flush: the script requests -w %{http_code} and insists
        # on a literal 200.
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "PUT" ]; then printf 200; exit 0; fi\n'
        "done\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        '    *"?list-type=2"*) printf %s \'' + _LIST_XML + "' ; exit 0 ;;\n"
        '    https://*.txt) printf "addr-one\\naddr-two\\n"; exit 0 ;;\n'
        "  esac\n"
        "done\n"
        "exit 0\n"
    )
    (stub_dir / "curl").chmod(0o755)
    # An aws stub that must never be reached on the unsigned read path.
    (stub_dir / "aws").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "aws $@" >> "{log}"\n'
        "exit 1\n"
    )
    (stub_dir / "aws").chmod(0o755)
    return stub_dir, log


class TestDownloaderPickRandomAddress:
    ENV = {
        "ADDRESSES_FILE": "/nonexistent/addresses.txt",
        "ADDRESSES_S3_PREFIX": "saorsa-deploy/addresses/DEV-01",
        "S3_HTTP_BASE": "https://saorsa-testnet-addresses.s3.eu-west-2.amazonaws.com",
    }

    def test_lists_and_fetches_via_unsigned_curl(self, curl_stub):
        stub_dir, log = curl_stub
        result = _run_function(
            "downloader.sh", "pick_random_address", "pick_random_address",
            self.ENV, stub_dir,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() in ("addr-one", "addr-two")
        log_text = log.read_text()
        assert "?list-type=2&prefix=saorsa-deploy/addresses/DEV-01/" in log_text
        assert "upload-" in log_text
        # signed CLI never used on the read path (aws stub logs lines as "aws ...")
        assert not any(line.startswith("aws ") for line in log_text.splitlines())

    def test_local_addresses_file_takes_priority(self, curl_stub, tmp_path):
        stub_dir, log = curl_stub
        addresses = tmp_path / "addresses.txt"
        addresses.write_text("local-addr\n")
        env = {**self.ENV, "ADDRESSES_FILE": str(addresses)}
        result = _run_function(
            "downloader.sh", "pick_random_address", "pick_random_address",
            env, stub_dir,
        )
        assert result.stdout.strip() == "local-addr"
        assert not log.exists()  # no network calls at all

    def test_no_http_base_yields_empty(self, curl_stub):
        stub_dir, log = curl_stub
        env = {**self.ENV, "S3_HTTP_BASE": ""}
        result = _run_function(
            "downloader.sh", "pick_random_address", "pick_random_address",
            env, stub_dir,
        )
        assert result.stdout.strip() == ""
        assert not log.exists()


class TestUploaderFlushToS3:
    def _env(self, tmp_path, presigned_url):
        addresses = tmp_path / "addresses.txt"
        addresses.write_text("addr-1\naddr-2\n")
        return {
            "ADDRESSES_FILE": str(addresses),
            "ADDRESSES_TAIL_SIZE": "10000",
            "S3_URI": "s3://saorsa-testnet-addresses/saorsa-deploy/addresses/DEV-01/upload-lon1-1.txt",
            "UPLOAD_PRESIGNED_URL": presigned_url,
        }

    def test_presigned_url_flushes_via_curl_put(self, curl_stub, tmp_path):
        stub_dir, log = curl_stub
        env = self._env(tmp_path, "https://bucket.s3/key?X-Amz-Signature=abc")
        result = _run_function(
            "uploader.sh", "flush_to_s3", "flush_to_s3", env, stub_dir
        )
        assert result.returncode == 0, result.stderr
        log_text = log.read_text()
        assert "-X PUT" in log_text
        assert "X-Amz-Signature=abc" in log_text
        assert not any(line.startswith("aws ") for line in log_text.splitlines())

    def test_no_presigned_url_falls_back_to_aws_cli(self, curl_stub, tmp_path):
        stub_dir, log = curl_stub
        # make the aws stub succeed for this case
        (stub_dir / "aws").write_text(
            "#!/usr/bin/env bash\n"
            f'echo "aws $@" >> "{log}"\n'
            "exit 0\n"
        )
        env = self._env(tmp_path, "")
        result = _run_function(
            "uploader.sh", "flush_to_s3", "flush_to_s3", env, stub_dir
        )
        assert result.returncode == 0, result.stderr
        log_text = log.read_text()
        assert "aws s3 cp" in log_text
        assert "PUT" not in log_text

    def test_presigned_put_failure_warns_and_returns_nonzero(self, curl_stub, tmp_path):
        stub_dir, log = curl_stub
        (stub_dir / "curl").write_text("#!/usr/bin/env bash\nexit 22\n")
        env = self._env(tmp_path, "https://bucket.s3/key?X-Amz-Signature=abc")
        result = _run_function(
            "uploader.sh", "flush_to_s3", "flush_to_s3", env, stub_dir
        )
        assert result.returncode != 0
        assert "WARN: S3 flush failed (presigned PUT" in result.stdout
