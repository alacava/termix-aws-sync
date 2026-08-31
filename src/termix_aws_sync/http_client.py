"""Minimal stdlib HTTP client for the Termix REST API.

BUILD_BRIEF.md §2.2 originally fixed "CLI only, never the REST API" as a
design decision, specifically because the CLI is versioned/documented and
the API isn't. That was deliberately overridden after a production bug
was root-caused and the fix confirmed against a live server -- see
termix.py's module docstring for the full story. This module intentionally
uses only `urllib`/`json` (stdlib), matching the project's "no runtime
dependencies beyond the standard library" rule -- no `requests`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Protocol
from urllib import error, request

log = logging.getLogger("termix-aws-sync")

_TIMEOUT_SECONDS = 30


class TermixClient(Protocol):
    def list_hosts(self) -> List[Dict[str, Any]]: ...
    def create_host(self, body: Dict[str, Any]) -> Dict[str, Any]: ...
    def update_host(self, host_id: Any, body: Dict[str, Any]) -> Dict[str, Any]: ...
    def delete_host(self, host_id: Any) -> None: ...


class HttpTermixClient:
    """Talks to a real Termix server's REST API.

    Auth: `Authorization: Bearer <TERMIX_API_KEY>` -- confirmed against
    Termix's own auth-manager source: an API key is checked directly
    against stored keys, no login/session exchange needed.
    """

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        if data is not None:
            req.add_header("Content-Type", "application/json")

        log.debug("HTTP %s %s%s", method, url, f" body={body!r}" if body else "")
        try:
            with request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
                status = resp.status
        except error.HTTPError as exc:
            raw = exc.read()
            text = raw.decode("utf-8", "replace")
            raise RuntimeError(
                f"termix API {method} {path} failed ({exc.code}): {text[:500]}"
            ) from exc
        except error.URLError as exc:
            raise RuntimeError(f"could not reach termix API at {url}: {exc.reason}") from exc

        log.debug("HTTP %s %s -> %d: %.2000r", method, url, status, raw)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"could not parse JSON from termix API {method} {path}: {exc}"
            ) from exc

    def list_hosts(self) -> List[Dict[str, Any]]:
        result = self._request("GET", "/host/db/host")
        if not isinstance(result, list):
            raise RuntimeError(
                f"GET /host/db/host returned unexpected JSON: a bare {type(result).__name__}"
            )
        return result

    def create_host(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/host/db/host", body)

    def update_host(self, host_id: Any, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", f"/host/db/host/{host_id}", body)

    def delete_host(self, host_id: Any) -> None:
        self._request("DELETE", f"/host/db/host/{host_id}")
