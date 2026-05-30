"""Minimal Elasticsearch client for the upgrade tracker.

Uses stdlib urllib — no third-party HTTP deps. Supports HTTP basic auth or
API key. Handles `_search` with `search_after` pagination sized for the
volume of upgrade-related logs (small compared to the overall index).
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterator, Optional

TARGET_AT_STARTUP = "ant_node"
TARGET_UPGRADE_PREFIX = "ant_node::upgrade"

# Keep-alive for the point-in-time used by paginated scans. Each search that
# carries the PIT resets this window, so it only has to exceed the gap between
# consecutive page fetches, not the whole scan duration.
PIT_KEEP_ALIVE = "2m"


@dataclass
class ESCredentials:
    url: str                          # e.g. http://1.2.3.4:9200
    username: Optional[str] = None
    password: Optional[str] = None
    api_key: Optional[str] = None

    def auth_header(self) -> str:
        if self.api_key:
            return f"ApiKey {self.api_key}"
        if self.username and self.password:
            token = base64.b64encode(
                f"{self.username}:{self.password}".encode()
            ).decode()
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

    def _target_filter(self) -> dict:
        """The shared `should` clause matching startup + upgrade-module docs."""
        return {
            "bool": {
                "should": [
                    {"term": {"tag.target.keyword": TARGET_AT_STARTUP}},
                    {
                        "prefix": {
                            "tag.target.keyword": f"{TARGET_UPGRADE_PREFIX}::",
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        }

    def _open_pit(self, index_pattern: str) -> str:
        encoded = urllib.parse.quote(index_pattern, safe="*-")
        resp = self._request(
            "POST", f"/{encoded}/_pit?keep_alive={PIT_KEEP_ALIVE}", None
        )
        pit_id = resp.get("id")
        if not pit_id:
            raise ESError("Elasticsearch did not return a point-in-time id")
        return pit_id

    def _close_pit(self, pit_id: str) -> None:
        # Best-effort: the PIT also auto-expires after PIT_KEEP_ALIVE, so a
        # failure here only leaks a short-lived reader, never data.
        try:
            self._request("DELETE", "/_pit", {"id": pit_id})
        except ESError:
            pass

    def _scan(
        self, index_pattern: str, query: dict, page_size: int = 1000,
    ) -> Iterator[dict]:
        """Yield every `_source` matching `query`, in @timestamp order.

        Pages with `search_after` over a point-in-time, tie-broken by the
        PIT-provided `_shard_doc`. Unlike a plain `search_after` on
        `(@timestamp, host, service)`, this tie-breaker is GUARANTEED unique
        and stable, so documents sharing a millisecond `@timestamp` are never
        dropped at a page boundary (the failure mode that previously stranded
        completion events emitted during dense upgrade bursts).
        """
        pit_id = self._open_pit(index_pattern)
        sort = [{"@timestamp": "asc"}, {"_shard_doc": "asc"}]
        search_after: Optional[list] = None
        try:
            while True:
                body: dict = {
                    "size": page_size,
                    "query": query,
                    "sort": sort,
                    "pit": {"id": pit_id, "keep_alive": PIT_KEEP_ALIVE},
                }
                if search_after is not None:
                    body["search_after"] = search_after
                resp = self._request("POST", "/_search", body)
                # The PIT id can be refreshed across requests; keep the latest.
                pit_id = resp.get("pit_id", pit_id)
                hits = resp.get("hits", {}).get("hits", [])
                if not hits:
                    return
                for hit in hits:
                    yield hit["_source"]
                if len(hits) < page_size:
                    return
                search_after = hits[-1]["sort"]
        finally:
            self._close_pit(pit_id)

    def scan_upgrade_events(
        self,
        index_pattern: str,
        testnet_name: str,
        since: str,
        page_size: int = 1000,
    ) -> Iterator[dict]:
        """Yield every upgrade-lifecycle + startup doc with @timestamp >= since."""
        query = {
            "bool": {
                "filter": [
                    {"range": {"@timestamp": {"gte": since}}},
                    {"term": {"tag.testnet_name.keyword": testnet_name}},
                    self._target_filter(),
                ]
            }
        }
        yield from self._scan(index_pattern, query, page_size)

    def scan_node_events(
        self,
        index_pattern: str,
        testnet_name: str,
        host: str,
        service_number: str,
        page_size: int = 1000,
    ) -> Iterator[dict]:
        """Yield a single node's COMPLETE upgrade-lifecycle + startup history.

        Unbounded in time (no `since`) and scoped to one (host, service) so the
        caller can re-derive that row's state from scratch — used by
        `track-upgrades reconcile` to recover rows that incremental sync left
        stuck behind its forward-only cursor.
        """
        query = {
            "bool": {
                "filter": [
                    {"term": {"tag.testnet_name.keyword": testnet_name}},
                    {"term": {"tag.host.keyword": host}},
                    {"term": {"tag.service_number.keyword": str(service_number)}},
                    self._target_filter(),
                ]
            }
        }
        yield from self._scan(index_pattern, query, page_size)
