"""
Moltbook HTTP client — minimal stdlib wrapper around the REST API at
https://www.moltbook.com/api/v1.

Design intent: build a *fixed* client from a one-time read of the public
SKILL.md spec, **not** an agent that fetches and follows skill.md at runtime.
Re-fetching remote instructions on every cycle is a supply-chain attack
surface — if Moltbook is ever compromised, every UBIK agent plugged into
it gets remote-controlled. Hand-write the surface area instead.

Endpoints covered (V1):
- POST   /agents/register
- GET    /agents/me
- GET    /agents/status
- POST   /posts
- GET    /posts                 (feed)
- GET    /posts/<id>
- DELETE /posts/<id>
- POST   /posts/<id>/comments

Out of scope (intentionally): /agents/<id>/follow, /reactions, image upload.
Those add attack surface without serving the fleet's "broadcast a [public]
milestone" use case.

Imports: stdlib only (urllib, json, dataclasses) so the client runs in any
sandbox without a `pip install`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

# Hard-coded — never read from skill.md at runtime, never accept overrides
# from a remote source. Per skill.md's own security warning, the API key
# must ONLY appear in requests to this exact host (with `www.`).
MOLTBOOK_BASE = "https://www.moltbook.com/api/v1"
USER_AGENT = "ubik-moltbook/0.1 (+https://github.com/damienldx/ubik-moltbook)"
DEFAULT_TIMEOUT_SECONDS = 15


class MoltbookError(RuntimeError):
    """Raised when the Moltbook API returns a non-2xx response or the client
    refuses to send a request for a safety reason."""

    def __init__(self, status: int, body: Any):
        super().__init__(f"Moltbook API error {status}: {body!r}")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Credentials:
    """Per-agent Moltbook credentials. Never share across agents."""

    api_key: str
    agent_name: str

    @classmethod
    def load(cls, path: str | None = None) -> "Credentials":
        target = path or os.path.expanduser("~/.config/moltbook/credentials.json")
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(api_key=data["api_key"], agent_name=data["agent_name"])

    def save(self, path: str | None = None) -> str:
        target = path or os.path.expanduser("~/.config/moltbook/credentials.json")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # 0600 so peers on the same host can't read it.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"api_key": self.api_key, "agent_name": self.agent_name}, f)
        return target


class MoltbookClient:
    """Synchronous Moltbook client. One instance per agent — credentials bound."""

    def __init__(self, creds: Credentials | None = None, *, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self._creds = creds
        self._timeout = timeout

    def _request(self, method: str, path: str, *, body: dict[str, Any] | None = None,
                 query: dict[str, Any] | None = None, authed: bool = True) -> Any:
        url = f"{MOLTBOOK_BASE}{path}"
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if authed:
            if self._creds is None:
                raise MoltbookError(0, "credentials required for authed request")
            # Defense-in-depth: re-check we're hitting www.moltbook.com before
            # attaching the bearer. skill.md warns that the bare domain redirects
            # and strips Authorization headers, which would expose the token to
            # the redirect destination if we ever changed MOLTBOOK_BASE by hand.
            if not url.startswith("https://www.moltbook.com/"):
                raise MoltbookError(0, f"refusing to send api_key to non-moltbook URL: {url}")
            headers["Authorization"] = f"Bearer {self._creds.api_key}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                err_body = None
            raise MoltbookError(e.code, err_body) from None

    # ── Registration ─────────────────────────────────────────────────────────
    def register(self, name: str, description: str) -> dict[str, Any]:
        """Create a new Moltbook agent. The response contains the api_key —
        save it immediately via `Credentials(...).save()`. Unauthed call."""
        return self._request("POST", "/agents/register",
                             body={"name": name, "description": description},
                             authed=False)

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", "/agents/me")

    def get_claim_status(self) -> dict[str, Any]:
        return self._request("GET", "/agents/status")

    # ── Posts ────────────────────────────────────────────────────────────────
    def create_post(self, submolt: str, title: str, *, content: str | None = None,
                    url: str | None = None, type: str = "text") -> dict[str, Any]:
        """Create a post in `submolt`. `type` must be 'text' or 'link';
        the V1 client refuses 'image' on purpose — image uploads have a
        wider attack surface and we don't need them for fleet broadcasts."""
        if type not in ("text", "link"):
            raise MoltbookError(0, f"post type {type!r} not supported by this client")
        payload: dict[str, Any] = {"submolt_name": submolt, "title": title, "type": type}
        if content is not None:
            payload["content"] = content
        if url is not None:
            payload["url"] = url
        return self._request("POST", "/posts", body=payload)

    def get_feed(self, sort: str = "hot", limit: int = 25, *,
                 submolt: str | None = None, cursor: str | None = None) -> dict[str, Any]:
        return self._request("GET", "/posts",
                             query={"sort": sort, "limit": limit, "submolt": submolt, "cursor": cursor})

    def get_post(self, post_id: str) -> dict[str, Any]:
        return self._request("GET", f"/posts/{post_id}")

    def delete_post(self, post_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/posts/{post_id}")

    def add_comment(self, post_id: str, content: str) -> dict[str, Any]:
        return self._request("POST", f"/posts/{post_id}/comments", body={"content": content})
