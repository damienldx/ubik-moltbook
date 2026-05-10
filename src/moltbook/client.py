"""Moltbook REST client — thin httpx wrapper, no business logic.

Surface :
    client = MoltbookClient(api_key=..., base_url=..., handle=...)
    client.register(display_name="Jules", bio="UBIK fleet, backend Python")
    client.post("PR #38 merged — qualité ciblage CRM Visites")
    feed = client.read_feed(limit=20)
    client.heartbeat()  # write heartbeat file and POST /heartbeat
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx


_DEFAULT_BASE = "https://www.moltbook.com"
_DEFAULT_TIMEOUT = 15.0
_HEARTBEAT_INTERVAL_S = 4 * 60 * 60  # 4h per spec


class MoltbookError(Exception):
    """Raised on any non-2xx response from Moltbook, or transport failure."""


class MoltbookClient:
    """Stateless thin client. One instance per agent (one API key per agent).

    No caching, no retry; if the upstream is flaky, wrap calls in your own
    backoff. Heartbeat tracking is left to the caller — call `heartbeat()` on
    a 4h cadence (systemd timer is the suggested mechanism).
    """

    def __init__(
        self,
        *,
        api_key: str,
        handle: str,
        base_url: str = _DEFAULT_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
        heartbeat_path: Optional[Path] = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required (set MOLTBOOK_API_KEY)")
        if not handle:
            raise ValueError("handle is required (public Moltbook handle, e.g. 'jules')")
        self._api_key = api_key
        self._handle = handle
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._heartbeat_path = heartbeat_path or Path(
            os.path.expanduser("~/.ubik-moltbook/heartbeat")
        )

    @property
    def handle(self) -> str:
        return self._handle

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "X-Moltbook-Handle": self._handle,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}{path}"
        try:
            r = httpx.post(url, json=payload, headers=self._headers(), timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise MoltbookError(f"POST {path} transport failure: {exc}") from exc
        if r.status_code >= 400:
            raise MoltbookError(f"POST {path} HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json() if r.content else {}
        except ValueError:
            return {"raw": r.text}

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        url = f"{self._base}{path}"
        try:
            r = httpx.get(url, params=params, headers=self._headers(), timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise MoltbookError(f"GET {path} transport failure: {exc}") from exc
        if r.status_code >= 400:
            raise MoltbookError(f"GET {path} HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            return r.text

    # ── Public API ───────────────────────────────────────────────────────

    def register(self, *, display_name: str, bio: str = "") -> dict[str, Any]:
        """Create or update the agent's Moltbook profile. Idempotent on the
        Moltbook side — calling twice with the same handle updates the bio."""
        return self._post(
            "/api/v1/agents",
            {"handle": self._handle, "display_name": display_name, "bio": bio},
        )

    def post(self, content: str, *, tags: Optional[list[str]] = None) -> dict[str, Any]:
        """Publish one post on the agent's Moltbook timeline."""
        if not content or not content.strip():
            raise ValueError("content must be a non-empty string")
        return self._post("/api/v1/post", {"content": content, "tags": tags or []})

    def read_feed(self, *, limit: int = 20, since_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Fetch recent posts from the global feed. Pure read, no auth side-effect."""
        params: dict[str, Any] = {"limit": max(1, min(limit, 100))}
        if since_id:
            params["since_id"] = since_id
        result = self._get("/api/v1/feed", params=params)
        return result if isinstance(result, list) else []

    def heartbeat(self) -> dict[str, Any]:
        """Notify Moltbook the agent is alive + write local heartbeat file
        with the current timestamp. The local file lets `moltbook_meca` know
        whether a heartbeat is overdue and skip calls in the meantime."""
        ts = int(time.time())
        self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self._heartbeat_path.write_text(f"{ts}\n", encoding="utf-8")
        return self._post("/api/v1/heartbeat", {"timestamp": ts})

    def heartbeat_overdue(self) -> bool:
        """True iff we have no local heartbeat record, or the last one is
        older than the 4h interval. Cheap, no network call."""
        if not self._heartbeat_path.exists():
            return True
        try:
            last = int(self._heartbeat_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return True
        return (time.time() - last) > _HEARTBEAT_INTERVAL_S
