"""Moltbook HTTP client — thin, sync, dependency-free.

Wraps the four endpoints we care about:

  POST /api/v1/register   → exchange (agent_id, label) for an API key
  POST /api/v1/post       → publish a status update
  GET  /api/v1/feed       → read the public feed (for context, not auto-mirror)
  GET  /api/v1/heartbeat  → fetch instructions, must be called every 4h

The client deliberately *never* posts a message that hasn't passed through
`moltbook_egress_firewall.assess()` first. The `post()` method enforces it.

Network: stdlib urllib only. Errors raised as `MoltbookError`. Timeouts
and retries kept simple — caller decides what to do.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .moltbook_egress_firewall import Verdict, assess

DEFAULT_BASE_URL = "https://www.moltbook.com"
DEFAULT_TIMEOUT_S = 15
DEFAULT_USER_AGENT = "ubik-moltbook/0.1 (+https://github.com/damienldx/ubik-moltbook)"


class MoltbookError(RuntimeError):
    """Raised when an HTTP call returns a non-2xx or the response isn't JSON."""

    def __init__(self, message: str, status: Optional[int] = None, body: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class EgressBlocked(RuntimeError):
    """Raised when the firewall vetoes a post before it leaves."""

    def __init__(self, reasons: list[str], matched: list[str]) -> None:
        super().__init__(f"blocked: {', '.join(reasons) or 'unspecified'}")
        self.reasons = reasons
        self.matched = matched


@dataclass
class PostResult:
    post_id: str
    posted_at: str
    sanitized: bool
    text_published: str


@dataclass
class FeedItem:
    post_id: str
    author_agent_id: str
    text: str
    posted_at: str


class MoltbookClient:
    def __init__(
        self,
        agent_id: str,
        api_key: Optional[str] = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.agent_id = agent_id
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    # ── Low-level HTTP ────────────────────────────────────────────────────
    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", DEFAULT_USER_AGENT)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # nosec: B310 (https only by default)
                raw = resp.read().decode("utf-8")
                if not raw:
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise MoltbookError(f"HTTP {e.code} on {method} {path}", status=e.code, body=body) from e
        except urllib.error.URLError as e:
            raise MoltbookError(f"network error: {e.reason}") from e
        except json.JSONDecodeError as e:
            raise MoltbookError(f"non-JSON response: {e}") from e

    # ── Public surface ────────────────────────────────────────────────────
    def register(self, label: str) -> str:
        """Register the agent and store the returned API key on the instance."""
        result = self._request("POST", "/api/v1/register", {
            "agent_id": self.agent_id,
            "label": label,
        })
        key = result.get("api_key")
        if not isinstance(key, str) or not key:
            raise MoltbookError(f"register: no api_key in response: {result!r}")
        self.api_key = key
        return key

    def post(self, text: str) -> PostResult:
        """Publish a status. Returns PostResult on success, raises EgressBlocked
        if the firewall vetoes, MoltbookError if the API fails."""
        if not self.api_key:
            raise MoltbookError("post: no api_key — call register() first or load from store")

        report = assess(text, agent_id=self.agent_id)
        if report.verdict is Verdict.BLOCK:
            raise EgressBlocked(report.reasons, report.matched_patterns)
        text_to_publish = report.sanitized_text if report.verdict is Verdict.SANITIZE else text

        result = self._request("POST", "/api/v1/post", {
            "agent_id": self.agent_id,
            "text": text_to_publish,
        })
        post_id = result.get("post_id") or result.get("id") or ""
        posted_at = result.get("posted_at") or result.get("created_at") or ""
        return PostResult(
            post_id=str(post_id),
            posted_at=str(posted_at),
            sanitized=report.verdict is Verdict.SANITIZE,
            text_published=text_to_publish or "",
        )

    def fetch_feed(self, limit: int = 20) -> list[FeedItem]:
        if not self.api_key:
            raise MoltbookError("fetch_feed: no api_key")
        result = self._request("GET", f"/api/v1/feed?limit={int(limit)}")
        items_raw = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items_raw, list):
            return []
        items: list[FeedItem] = []
        for it in items_raw:
            if not isinstance(it, dict):
                continue
            items.append(FeedItem(
                post_id=str(it.get("post_id") or it.get("id") or ""),
                author_agent_id=str(it.get("author_agent_id") or it.get("author") or ""),
                text=str(it.get("text") or ""),
                posted_at=str(it.get("posted_at") or it.get("created_at") or ""),
            ))
        return items

    def heartbeat(self) -> dict:
        """Fetch the per-agent instructions file. Must be called every 4h."""
        if not self.api_key:
            raise MoltbookError("heartbeat: no api_key")
        return self._request("GET", "/api/v1/heartbeat")


# ── API key store ───────────────────────────────────────────────────────────

KEY_STORE_DIR = os.path.expanduser("~/.ubik-memory/moltbook")


def _key_path(agent_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "_-." else "_" for c in agent_id)
    return os.path.join(KEY_STORE_DIR, f"{safe}.key")


def load_api_key(agent_id: str) -> Optional[str]:
    p = _key_path(agent_id)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_api_key(agent_id: str, api_key: str) -> None:
    os.makedirs(KEY_STORE_DIR, mode=0o700, exist_ok=True)
    p = _key_path(agent_id)
    tmp = f"{p}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(api_key.strip() + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def client_for(agent_id: str, *, base_url: str = DEFAULT_BASE_URL) -> MoltbookClient:
    """Build a MoltbookClient with API key loaded from the per-agent store."""
    return MoltbookClient(agent_id=agent_id, api_key=load_api_key(agent_id), base_url=base_url)
