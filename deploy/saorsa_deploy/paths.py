"""The one location saorsa-deploy keeps its own working state in.

Terraform working copies (``workspaces/``) and the parallel-lane logs
(``infra-logs/``) used to live under ``<cwd>/.saorsa``, which made the state of
a deployment a property of wherever the command that created it happened to be
run from: a ``destroy`` from another directory re-copied and re-``init``ed
everything, and every directory a run was launched from grew its own
multi-gigabyte set of Terraform copies. Both are resolved by owning a single
location regardless of the invocation's cwd.
"""

import os
from pathlib import Path

DEFAULT_STATE_DIR = "~/.local/share/saorsa-deploy"
STATE_DIR_ENV_VAR = "SAORSA_DEPLOY_STATE_DIR"


def state_dir() -> Path:
    """Return the directory saorsa-deploy keeps its working state in.

    ``SAORSA_DEPLOY_STATE_DIR`` overrides the default; it is also how the tests
    isolate themselves. Not created here — each caller makes the subdirectory
    it needs.
    """
    return Path(os.environ.get(STATE_DIR_ENV_VAR) or DEFAULT_STATE_DIR).expanduser()
