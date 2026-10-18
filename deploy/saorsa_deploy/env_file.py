"""Where `saorsa-deploy` finds its credentials (V2-1624).

Every credential the tool uses — the registry URL, the provider tokens, the AWS
keys, the wallet keys — is read from the environment. That environment used to
be assembled in two places an *installed* copy of the tool does not have: the
checkout's ``.env``, which ``saorsa_db.db`` loaded as an import side effect, and
the operator's shell, which exports the provider tokens from a handful of files
their fish config sources. python-dotenv searches upward from the *calling
module's* file, so the first worked for ``uv run`` in the checkout and found
nothing at all from a wheel installed under ``~/.cache/uv``.

So the tool has one settings file of its own: ``~/.config/saorsa-deploy/env``,
or whatever :data:`ENV_FILE_VAR` names. Plain ``KEY=value``, one per line, ``#``
comments, optional quotes — the format systemd's ``EnvironmentFile=`` reads, so
one file serves both the tool and a runner that exports it into a session for
its own ``psql "$DATABASE_URL"``.

Precedence is **environment, then the settings file, then ``./.env``**: nothing
loaded here ever overwrites a variable that is already set, so a single
``DATABASE_URL=… saorsa-deploy …`` still overrides the file for one invocation,
and the checkout's ``.env`` still works for ``uv run`` in the checkout.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

#: Environment variable naming an alternative settings file.
ENV_FILE_VAR = "SAORSA_DEPLOY_ENV_FILE"

#: The settings file read when :data:`ENV_FILE_VAR` is unset.
DEFAULT_ENV_FILE = "~/.config/saorsa-deploy/env"


class EnvFileError(RuntimeError):
    """A settings file is there but could not be read."""


def resolve_env_file() -> Path:
    """The settings file path, whether or not it exists."""
    override = os.environ.get(ENV_FILE_VAR, "").strip()
    return Path(override).expanduser() if override else Path(DEFAULT_ENV_FILE).expanduser()


def load_env_files(cwd: Path | None = None) -> list[Path]:
    """Load the settings file then ``./.env``, neither overriding the environment.

    Returns the files actually read, in the order they were read. Raises
    :class:`EnvFileError` for a file that exists and cannot be read.
    """
    loaded: list[Path] = []
    for path in (resolve_env_file(), (cwd or Path.cwd()) / ".env"):
        if _load_one(path):
            loaded.append(path)
    return loaded


def _load_one(path: Path) -> bool:
    # A missing file is the normal case — an operator who exports everything in
    # their shell has none — so it is silent. One that exists and cannot be read
    # is a misconfiguration we must not paper over: the run would go on to fail
    # on a missing credential and name the wrong cause. python-dotenv swallows
    # the open error itself (it warns only under `verbose`), so the read is ours.
    if not path.exists():
        return False
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EnvFileError(
            f"Could not read the settings file {path}: {exc.strerror or exc}"
        ) from exc
    # `interpolate=False`: the values are flat secrets and URLs, and `$` is a
    # literal in a systemd `EnvironmentFile`. Expanding `${…}` would corrupt any
    # password containing it and would be evaluation the format does not have.
    load_dotenv(stream=io.StringIO(content), override=False, interpolate=False)
    return True


@dataclass(frozen=True)
class EnvKey:
    """One variable the tool reads, and what it is for."""

    name: str
    purpose: str


@dataclass(frozen=True)
class EnvKeyGroup:
    """A titled group of :class:`EnvKey`, as `config env-file` prints them."""

    title: str
    keys: tuple[EnvKey, ...]


#: Every variable `saorsa-deploy` itself reads from the environment, grouped by
#: what it unlocks. Derived from the call sites, not from `.env.example` — the
#: VM-side script variables (`WORK_DIR`, `ANT`, `LOG_DIR`, the `INFLUX_*` a
#: provisioner writes into a unit file) are deliberately absent, as are the
#: registry web app's own (`FLASK_SECRET_KEY` and friends), which this tool
#: never reads.
ENV_KEY_GROUPS: tuple[EnvKeyGroup, ...] = (
    EnvKeyGroup(
        "Registry",
        (
            EnvKey("DATABASE_URL", "Record and read deployments, results and the file pool"),
            EnvKey("POOL_DATABASE_URL", "Default pool role URL handed to continuous-uploader VMs"),
        ),
    ),
    EnvKeyGroup(
        "Cloud providers",
        (
            EnvKey("DO_TOKEN", "DigitalOcean (nodes, clients, monitoring, Elasticsearch)"),
            EnvKey("HCLOUD_TOKEN", "Hetzner node VMs"),
            EnvKey("VULTR_API_KEY", "Vultr node VMs"),
            EnvKey("OVH_ENDPOINT", "OVH API endpoint (e.g. ovh-eu)"),
            EnvKey("OVH_APPLICATION_KEY", "OVH API credential"),
            EnvKey("OVH_APPLICATION_SECRET", "OVH API credential"),
            EnvKey("OVH_CONSUMER_KEY", "OVH API credential"),
            EnvKey("OVH_CLOUD_PROJECT_SERVICE", "OVH public-cloud project id"),
            EnvKey("CNTB_OAUTH2_CLIENT_ID", "Contabo OAuth2 credential"),
            EnvKey("CNTB_OAUTH2_CLIENT_SECRET", "Contabo OAuth2 credential"),
            EnvKey("CNTB_OAUTH2_USER", "Contabo account email"),
            EnvKey("CNTB_OAUTH2_PASS", "Contabo API password"),
        ),
    ),
    EnvKeyGroup(
        "AWS",
        (
            EnvKey("AWS_ACCESS_KEY_ID", "Terraform state and deployment state in S3"),
            EnvKey("AWS_SECRET_ACCESS_KEY", "Terraform state and deployment state in S3"),
            EnvKey("ANT_BUILD_AWS_ACCESS_KEY_ID", "Binary uploads from the build VM"),
            EnvKey("ANT_BUILD_AWS_SECRET_ACCESS_KEY", "Binary uploads from the build VM"),
            EnvKey("ANT_CLIENT_AWS_ACCESS_KEY_ID", "Client address coordination in S3"),
            EnvKey("ANT_CLIENT_AWS_SECRET_ACCESS_KEY", "Client address coordination in S3"),
        ),
    ),
    EnvKeyGroup(
        "Wallets",
        (
            EnvKey("ANT_DEPLOY_SECRET_KEY", "Testnet client wallet funder (Arbitrum Sepolia)"),
            EnvKey(
                "ARBITRUM_ONE_SECRET_KEY", "Production uploads (Arbitrum One mainnet, real ETH)"
            ),
        ),
    ),
    EnvKeyGroup(
        "GitHub",
        (
            EnvKey(
                "GITHUB_TOKEN", "Raise the release-lookup rate limit (GH_TOKEN is the fallback)"
            ),
            EnvKey("GH_TOKEN", "Fallback spelling of GITHUB_TOKEN"),
        ),
    ),
    EnvKeyGroup(
        "Reporting",
        (
            EnvKey("ANT_DEPLOY_SLACK_BOT_TOKEN", "Threaded `slack-post` replies (Web API path)"),
            EnvKey("ANT_DEPLOY_SLACK_CHANNEL", "Default Slack channel for `slack-post`"),
            EnvKey("ANT_DEPLOY_SLACK_WEBHOOK_URL", "Webhook fallback for `slack-post`"),
            EnvKey("SLACK_WEBHOOK_URL", "Default webhook for the canary reporter"),
            EnvKey("ANT_DEPLOY_GRAFANA_DEV_PASSWORD", "Grafana admin password at provision time"),
        ),
    ),
)
