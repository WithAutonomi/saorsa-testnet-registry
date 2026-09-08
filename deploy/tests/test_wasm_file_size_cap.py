"""A WASM uploader's file size must fit ant-core's browser cap.

`ant-core` refuses a browser upload over 1,000,000,000 bytes client-side, in
about 0.2s, without touching the network:

    invalid browser data: browser files are limited to 1000000000 bytes

The cap is a DECIMAL gigabyte while `file_size_mb` is binary, so the obvious
`file_size_mb: 1000` is 1,048,576,000 bytes and every upload from that client
fails. On the cancelled V2-1174 run that was 227 of 235 WASM upload failures,
which made the tier's measured success rate describe nothing at all — and it
was only noticed by reading a client journal by hand, because the failures look
like ordinary upload failures in the metrics.

Validating it in the manifest means the run dies at `config validate`, before
any VM is created and before anything is billing.
"""

import pytest
from pydantic import ValidationError

from saorsa_deploy.config.models import (
    WASM_MAX_FILE_BYTES,
    WASM_MAX_FILE_MB,
    WasmUploader,
)


def _uploader(size_mb: int) -> WasmUploader:
    return WasmUploader(provider="ovh", region="SBG5", file_size_mb=size_mb)


class TestTheCapItself:
    def test_the_cap_is_a_decimal_gigabyte(self):
        assert WASM_MAX_FILE_BYTES == 1_000_000_000

    def test_the_largest_whole_binary_mb_that_fits(self):
        assert WASM_MAX_FILE_MB == 953
        assert WASM_MAX_FILE_MB * 1024 * 1024 <= WASM_MAX_FILE_BYTES
        assert (WASM_MAX_FILE_MB + 1) * 1024 * 1024 > WASM_MAX_FILE_BYTES


class TestValidation:
    def test_1000_is_rejected(self):
        # The trap: a round decimal number that is not a round binary one.
        with pytest.raises(ValidationError) as e:
            _uploader(1000)
        msg = str(e.value)
        assert "1,048,576,000" in msg
        assert "1,000,000,000" in msg
        # The message must say what to use instead, not just that it is wrong.
        assert "953" in msg

    def test_the_boundary_is_exact(self):
        _uploader(WASM_MAX_FILE_MB)  # fits
        with pytest.raises(ValidationError):
            _uploader(WASM_MAX_FILE_MB + 1)

    def test_ordinary_sizes_pass(self):
        for size in (10, 20, 300, 900):
            assert _uploader(size).file_size_mb == size

    def test_native_uploaders_are_unaffected(self):
        # The cap is a browser-path limit; the native tier still runs 1000 MB.
        from saorsa_deploy.config.models import Uploader

        assert Uploader(
            provider="ovh", region="UK1", file_size_mb=1000
        ).file_size_mb == 1000


class TestShippedProfiles:
    """Every profile that ships a WASM uploader must satisfy the cap.

    Two profiles carried `file_size_mb: 1000` — staging-short-wasm and
    staging-rc-wasm — so the release-train profile would have failed the same
    way the DEV-03 one did.
    """

    def test_no_shipped_profile_exceeds_the_cap(self):
        import glob

        import yaml

        offenders = []
        for path in glob.glob("deploy/manifests/profiles/*.yaml"):
            with open(path) as fh:
                doc = yaml.safe_load(fh) or {}
            clients = (doc.get("testnet") or {}).get("clients") or {}
            for up in clients.get("wasm_uploaders") or []:
                size = up.get("file_size_mb")
                if size and size * 1024 * 1024 > WASM_MAX_FILE_BYTES:
                    offenders.append((path, size))
        assert offenders == [], offenders


if __name__ == "__main__":
    pytest.main([__file__])
