from importlib import resources as importlib_resources
from pathlib import Path


def get_resources_dir() -> Path:
    """Return the path to the bundled resources directory.

    Works both in development (running from the repo) and when installed as a package.
    """
    return Path(str(importlib_resources.files("saorsa_deploy") / "resources"))
