"""Contabo-specific helpers (credential preflight, region list).

Contabo's Terraform provider authenticates via an OAuth2 password flow, reading
four credentials straight from the environment (like the OVH provider reads its
``OVH_*`` vars). We therefore don't map anything to ``TF_VAR_*`` in
``terraform.py``; we just validate the env is present before invoking terraform
so a missing credential fails fast with a clear message instead of an opaque
provider error.
"""

import os
import sys

from rich.console import Console

# All four are required by the contabo/contabo provider's OAuth2 password flow.
# CNTB_OAUTH2_USER is the account email; CNTB_OAUTH2_PASS is the separate API
# password set on the account-security page (NOT the login password).
CONTABO_REQUIRED_ENV_VARS = (
    "CNTB_OAUTH2_CLIENT_ID",
    "CNTB_OAUTH2_CLIENT_SECRET",
    "CNTB_OAUTH2_USER",
    "CNTB_OAUTH2_PASS",
)

# Region codes accepted by the create-instance API. The Terraform provider docs
# list only the first five, but the API accepts the wider set.
CONTABO_REGIONS = (
    "EU", "US-central", "US-east", "US-west", "UK", "SIN", "AUS", "JPN", "IND",
)


def missing_contabo_env() -> list[str]:
    """Return the names of any required Contabo env vars that are unset/empty."""
    return [v for v in CONTABO_REQUIRED_ENV_VARS if not os.environ.get(v)]


def require_contabo_env(console: Console) -> None:
    """Exit with a clear error if any Contabo OAuth2 credential is missing."""
    missing = missing_contabo_env()
    if missing:
        console.print(
            "[bold red]Error:[/bold red] provider 'contabo' requires the "
            f"following environment variable(s): {', '.join(missing)}. "
            "Contabo authenticates via an OAuth2 password flow — set "
            "CNTB_OAUTH2_CLIENT_ID / CNTB_OAUTH2_CLIENT_SECRET (the API client "
            "credentials) and CNTB_OAUTH2_USER (your account email) / "
            "CNTB_OAUTH2_PASS (the API password from the account-security page) "
            "and retry."
        )
        sys.exit(1)
