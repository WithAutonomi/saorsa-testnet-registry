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

    def search(
        self,
        index_pattern: str,
        query: dict,
        size: int = 1000,
        sort: Optional[list] = None,
        search_after: Optional[list] = None,
    ) -> dict:
        body: dict = {
            "query": query,
            "size": size,
        }
        if sort is not None:
            body["sort"] = sort
        if search_after is not None:
            body["search_after"] = search_after
        encoded_pattern = urllib.parse.quote(index_pattern, safe="*-")
        return self._request("POST", f"/{encoded_pattern}/_search", body)

    def scan_upgrade_events(
        self,
        index_pattern: str,
        testnet_name: str,
        since: str,
        page_size: int = 1000,
    ) -> Iterator[dict]:
        """Yield every upgrade-lifecycle + startup doc with @timestamp >= since.

        Sorted by @timestamp asc, tie-broken by _id for stability. Uses
        search_after to iterate without deep-pagination limits.
        """
        query = {
            "bool": {
                "filter": [
                    {"range": {"@timestamp": {"gte": since}}},
                    {
                        "term": {
                            "tag.testnet_name.keyword": testnet_name,
                        }
                    },
                    {
                        "bool": {
                            "should": [
                                {
                                    "term": {
                                        "tag.target.keyword": TARGET_AT_STARTUP,
                                    }
                                },
                                {
                                    "prefix": {
                                        "tag.target.keyword": (
                                            f"{TARGET_UPGRADE_PREFIX}::"
                                        ),
                                    }
                                },
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                ]
            }
        }
        # `_id` as a sort tie-breaker is disallowed in ES 8.x without
        # enabling fielddata cluster-wide. Use keyword subfields on the
        # host + service tags — together with the (microsecond) timestamp
        # the compound key is effectively unique per event.
        sort = [
            {"@timestamp": "asc"},
            {"tag.host.keyword": "asc"},
            {"tag.service_number.keyword": "asc"},
        ]

        search_after: Optional[list] = None
        while True:
            resp = self.search(
                index_pattern=index_pattern,
                query=query,
                size=page_size,
                sort=sort,
                search_after=search_after,
            )
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                return
            for hit in hits:
                yield hit["_source"]
            if len(hits) < page_size:
                return
            search_after = hits[-1]["sort"]
