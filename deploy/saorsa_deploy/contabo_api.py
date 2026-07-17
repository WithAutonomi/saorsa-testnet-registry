"""Direct Contabo REST API client — the Contabo provider bypasses Terraform.

Contabo instance provisioning is a slow, asynchronous "order": each VM can take
well over the ``contabo/contabo`` Terraform provider's hard-coded 20-minute
create timeout (the resource has no ``timeouts`` block to raise it), so a
``terraform apply`` times out and *taints* the resource even though the create
actually succeeded. For Contabo we therefore create/cancel instances directly
via the REST API: the create call returns an ``instanceId`` immediately and we
poll for readiness ourselves — no timeout wall. The resulting IP is written into
the same S3 state the Terraform path would have produced, so every downstream
stage (provisioning, inventory, destroy) is unchanged.

Auth is the OAuth2 password flow from the ``CNTB_OAUTH2_*`` env vars (validated by
``saorsa_deploy.contabo.require_contabo_env`` before this is used).
"""

import os
import time
import uuid
from pathlib import Path

import requests
from rich.console import Console

API_BASE = os.environ.get("CNTB_API", "https://api.contabo.com").rstrip("/")
TOKEN_URL = os.environ.get(
    "CNTB_OAUTH2_TOKEN_URL",
    "https://auth.contabo.com/auth/realms/contabo/protocol/openid-connect/token",
)

# Poll cadence + default cap for waiting on a slow "order". A cap being hit is
# NOT fatal to the instance — it keeps provisioning server-side and a re-run
# adopts it by display name — so the cap only bounds how long we block.
_POLL_SECS = 20
_DEFAULT_MAX_WAIT_SECS = 10800  # 180 min — Contabo "orders" can be very slow


class ContaboError(RuntimeError):
    pass


def _get_token() -> str:
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": os.environ["CNTB_OAUTH2_CLIENT_ID"],
                "client_secret": os.environ["CNTB_OAUTH2_CLIENT_SECRET"],
                "username": os.environ["CNTB_OAUTH2_USER"],
                "password": os.environ["CNTB_OAUTH2_PASS"],
                "grant_type": "password",
            },
            timeout=30,
        )
    except KeyError as e:
        raise ContaboError(f"Missing Contabo credential env var: {e}") from e
    if resp.status_code != 200:
        raise ContaboError(
            f"Contabo OAuth token request failed ({resp.status_code}): "
            f"{resp.text[:300]}"
        )
    token = resp.json().get("access_token")
    if not token:
        raise ContaboError("No access_token in the Contabo OAuth response")
    return token


class ContaboClient:
    """Thin wrapper over the Contabo compute + secrets REST API."""

    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self._token: str | None = None

    # -- low-level ---------------------------------------------------------- #
    def _headers(self) -> dict:
        if self._token is None:
            self._token = _get_token()
        return {
            "Authorization": f"Bearer {self._token}",
            "x-request-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kw) -> dict:
        url = f"{API_BASE}{path}"
        resp = requests.request(method, url, headers=self._headers(), timeout=60, **kw)
        if resp.status_code == 401:
            # The cached OAuth access token is short-lived (Contabo/Keycloak
            # tokens expire in minutes) and a multi-VM run — each VM a slow
            # "order" we poll for — easily outlives one token. A 401 means the
            # request was rejected, not processed, so refreshing the token and
            # retrying once is safe even for the create POST.
            self._token = None
            resp = requests.request(
                method, url, headers=self._headers(), timeout=60, **kw
            )
        if resp.status_code not in (200, 201):
            raise ContaboError(
                f"Contabo API {method} {path} failed ({resp.status_code}): "
                f"{resp.text[:400]}"
            )
        if not resp.text:
            return {}
        return resp.json()

    @staticmethod
    def _first(payload: dict) -> dict:
        data = payload.get("data")
        if isinstance(data, list):
            return data[0] if data else {}
        return data or {}

    # -- secrets ------------------------------------------------------------ #
    def find_secret(self, name: str) -> int | None:
        payload = self._request("GET", "/v1/secrets", params={"name": name, "size": 100})
        for s in payload.get("data") or []:
            if s.get("name") == name and s.get("type") == "ssh":
                return int(s["secretId"])
        return None

    def ensure_ssh_secret(self, name: str, public_key_path: str) -> int:
        """Return the secretId of an ssh secret named ``name``, creating it if
        absent from the operator's public key. Idempotent so re-runs reuse it."""
        existing = self.find_secret(name)
        if existing is not None:
            return existing
        value = Path(public_key_path).expanduser().read_text().strip()
        payload = self._request(
            "POST", "/v1/secrets",
            json={"name": name, "value": value, "type": "ssh"},
        )
        return int(self._first(payload)["secretId"])

    def delete_secret(self, secret_id: int) -> None:
        try:
            self._request("DELETE", f"/v1/secrets/{secret_id}")
        except ContaboError as e:
            self.console.print(
                f"[yellow]Warning: could not delete secret {secret_id}: {e}[/yellow]"
            )

    # -- instances ---------------------------------------------------------- #
    def find_instance(self, display_name: str) -> dict | None:
        payload = self._request(
            "GET", "/v1/compute/instances",
            params={"displayName": display_name, "size": 100},
        )
        for inst in payload.get("data") or []:
            if inst.get("displayName") == display_name:
                return inst
        return None

    def get_instance(self, instance_id: int) -> dict:
        return self._first(self._request("GET", f"/v1/compute/instances/{instance_id}"))

    def create_instance(
        self,
        *,
        product_id: str,
        image_id: str,
        region: str,
        ssh_secret_id: int,
        display_name: str,
        period: int = 1,
        default_user: str = "root",
    ) -> int:
        """Create an instance (async — returns immediately) and return its id."""
        payload = self._request(
            "POST", "/v1/compute/instances",
            json={
                "imageId": image_id,
                "productId": product_id,
                "region": region,
                "period": period,
                "defaultUser": default_user,
                "sshKeys": [ssh_secret_id],
                "displayName": display_name,
            },
        )
        return int(self._first(payload)["instanceId"])

    def cancel_instance(self, instance_id: int) -> None:
        """Cancel an instance's contract (Contabo has no immediate delete)."""
        try:
            self._request("POST", f"/v1/compute/instances/{instance_id}/cancel")
        except ContaboError as e:
            self.console.print(
                f"[yellow]Warning: could not cancel instance {instance_id}: {e}[/yellow]"
            )

    @staticmethod
    def instance_ip(inst: dict) -> str | None:
        ipc = inst.get("ipConfig") or {}
        v4 = ipc.get("v4") or {}
        return v4.get("ip") or None

    def wait_until_running(
        self, instance_id: int, *, max_wait_secs: int = _DEFAULT_MAX_WAIT_SECS
    ) -> str:
        """Poll until the instance is ``running`` with an IPv4; return the IP.

        Absorbs the slow Contabo "order" with no timeout wall (unlike the TF
        provider). Raises on ``error`` status or if ``max_wait_secs`` is exceeded
        — but the instance persists, so a re-run adopts it by display name.
        """
        waited = 0
        while True:
            inst = self.get_instance(instance_id)
            status = inst.get("status")
            ip = self.instance_ip(inst)
            if status == "running" and ip:
                return ip
            if status == "error":
                raise ContaboError(
                    f"Contabo instance {instance_id} entered 'error' status"
                )
            if waited >= max_wait_secs:
                raise ContaboError(
                    f"Contabo instance {instance_id} still '{status}' after "
                    f"{waited}s. It is still provisioning server-side — re-run to "
                    "resume once it reaches 'running' (it will be adopted)."
                )
            self.console.print(
                f"  [dim]Contabo instance {instance_id}: {status} "
                f"({waited}s elapsed)...[/dim]"
            )
            time.sleep(_POLL_SECS)
            waited += _POLL_SECS
