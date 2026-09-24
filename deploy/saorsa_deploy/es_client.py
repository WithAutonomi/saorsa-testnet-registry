"""Minimal Elasticsearch client shared by the commands that query a run's ES.

Uses stdlib urllib — no third-party HTTP deps. Supports HTTP basic auth or an
API key. Deliberately generic: the caller builds the request body, because the
queries in this package are aggregation-only (`size: 0`) and differ in every
respect except the index-glob encoding and the auth header.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional


@dataclass
class ESCredentials:
    url: str  # e.g. http://1.2.3.4:9200
    username: Optional[str] = None
    password: Optional[str] = None
    api_key: Optional[str] = None

    def auth_header(self) -> str:
        if self.api_key:
            return f"ApiKey {self.api_key}"
        if self.username and self.password:
            token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            return f"Basic {token}"
        raise ValueError("No credentials provided (need api_key or user/pass)")


class ESError(RuntimeError):
    pass


class ESClient:
    def __init__(self, creds: ESCredentials, timeout: int = 60):
        self.creds = creds
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[dict]) -> dict:
        url = self.creds.url.rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Authorization": self.creds.auth_header(),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise ESError(f"ES HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise ESError(f"ES connection error: {e.reason}") from e

    def search(self, index_pattern: str, body: dict) -> dict:
        """Run one `_search` against an index pattern and return the raw response."""
        encoded = urllib.parse.quote(index_pattern, safe="*-")
        return self._request("POST", f"/{encoded}/_search", body)
