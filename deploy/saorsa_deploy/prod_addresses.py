"""Locate and load the bundled default list of production file addresses.

The list is used by ``prod-continuous-downloader`` when the operator does not
override it with ``--addresses-file``. New public addresses can be appended to
``resources/prod_addresses.txt`` in the saorsa-deploy package.
"""

from saorsa_deploy.resources import get_resources_dir

DEFAULT_PROD_ADDRESSES_FILENAME = "prod_addresses.txt"


def get_default_prod_addresses_path() -> str:
    """Return the absolute path to the shipped default prod addresses file."""
    return str(get_resources_dir() / DEFAULT_PROD_ADDRESSES_FILENAME)


def load_default_prod_addresses() -> list[str]:
    """Return the bundled default address list as a list of non-empty lines."""
    path = get_default_prod_addresses_path()
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]
