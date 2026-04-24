import os

from saorsa_deploy.prod_addresses import (
    get_default_prod_addresses_path,
    load_default_prod_addresses,
)


def test_default_prod_addresses_file_exists_on_disk():
    path = get_default_prod_addresses_path()
    assert os.path.isfile(path), f"Default prod addresses file missing at {path}"


def test_load_default_prod_addresses_returns_non_empty_list_of_non_empty_strings():
    addresses = load_default_prod_addresses()
    assert addresses, "Shipped default prod addresses list must be non-empty."
    for addr in addresses:
        assert isinstance(addr, str)
        assert addr.strip() == addr, f"Addresses must have no leading/trailing whitespace: {addr!r}"
        assert addr, "Addresses must be non-empty strings."


def test_load_default_prod_addresses_skips_blank_lines_and_whitespace(tmp_path, monkeypatch):
    custom_path = tmp_path / "custom_addresses.txt"
    custom_path.write_text("\naaa\n   \nbbb\n  ccc  \n\n")

    monkeypatch.setattr(
        "saorsa_deploy.prod_addresses.get_default_prod_addresses_path",
        lambda: str(custom_path),
    )

    addresses = load_default_prod_addresses()
    assert addresses == ["aaa", "bbb", "ccc"]
