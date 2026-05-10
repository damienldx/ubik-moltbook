"""Moltbook méca — daemon that mirrors `[public]` relay messages to Moltbook.

Loop:
  1. Poll the UBIK relay for new messages addressed to "moltbook-meca" or
     broadcast with a `[public]` prefix from the running agent.
  2. For each candidate, pass through the egress firewall.
  3. Publish via MoltbookClient.post().
  4. Append a structured line to the egress audit log.
  5. Every 4h (heartbeat budget), fetch /api/v1/heartbeat and log instructions.

Stop conditions: SIGINT, SIGTERM. The meca is designed to be wired into
systemd (Restart=on-failure) like the existing ledger-meca / fleetmemory-meca.

Storage:
  ~/.ubik-memory/moltbook/<agent_id>.key         — API key per agent
  ~/.ubik-memory/moltbook/egress.log             — append-only JSONL audit log
  ~/.ubik-memory/moltbook/heartbeat.last         — last heartbeat ISO timestamp

Env:
  MOLTBOOK_AGENT_ID         (required) — which fleet slot this méca speaks for
  MOLTBOOK_RELAY_URL        default http://localhost:7894
  MOLTBOOK_BASE_URL         default https://www.moltbook.com
  MOLTBOOK_POLL_INTERVAL_S  default 30
  MOLTBOOK_HEARTBEAT_S      default 14400 (4h)
  MOLTBOOK_FW_LEVEL         loose|normal|strict (default normal)
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from .moltbook_client import (
    DEFAULT_BASE_URL,
    EgressBlocked,
    KEY_STORE_DIR,
    MoltbookClient,
    MoltbookError,
    client_for,
    load_api_key,
    save_api_key,
)

# Public marker (case-insensitive; the marker must appear at the start of the
# message, possibly after a `[public:<channel>]` variant).
_PUBLIC_PREFIX = "[public]"
_PUBLIC_PREFIX_VARIANT = "[public:"

DEFAULT_POLL_INTERVAL_S = 30
DEFAULT_HEARTBEAT_S = 14400

EGRESS_LOG_PATH = os.path.join(KEY_STORE_DIR, "egress.log")
HEARTBEAT_STATE_PATH = os.path.join(KEY_STORE_DIR, "heartbeat.last")

log = logging.getLogger("moltbook-meca")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(entry: dict) -> None:
    os.makedirs(KEY_STORE_DIR, mode=0o700, exist_ok=True)
    line = json.dumps({"ts": _now_iso(), **entry}, ensure_ascii=False)
    with open(EGRESS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _is_public(text: str) -> bool:
    if not text:
        return False
    head = text.lstrip().lower()
    return head.startswith(_PUBLIC_PREFIX) or head.startswith(_PUBLIC_PREFIX_VARIANT)


def _strip_marker(text: str) -> str:
    """Remove the `[public]` or `[public:foo]` prefix from the head of the text."""
    stripped = text.lstrip()
    lower = stripped.lower()
    if lower.startswith(_PUBLIC_PREFIX):
        return stripped[len(_PUBLIC_PREFIX):].lstrip()
    if lower.startswith(_PUBLIC_PREFIX_VARIANT):
        close = stripped.find("]")
        if close != -1:
            return stripped[close + 1:].lstrip()
    return stripped


# ── Relay client (stdlib-only) ──────────────────────────────────────────────

class _RelayClient:
    """Minimal client to pop messages from the UBIK relay (stdlib only)."""

    def __init__(self, base_url: str, agent_id: str, timeout_s: int = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.timeout_s = timeout_s

    def read(self) -> list[dict]:
        url = f"{self.base_url}/messages?agent_id={urllib.parse.quote(self.agent_id)}"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # nosec: B310
                raw = resp.read().decode("utf-8")
                if not raw:
                    return []
                parsed = json.loads(raw)
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
            log.warning("relay read failed: %s", e)
            return []
        if isinstance(parsed, list):
            return [m for m in parsed if isinstance(m, dict)]
        if isinstance(parsed, dict):
            msgs = parsed.get("messages") or parsed.get("items")
            if isinstance(msgs, list):
                return [m for m in msgs if isinstance(m, dict)]
        return []


# ── Méca loop ──────────────────────────────────────────────────────────────

class MoltbookMeca:
    def __init__(
        self,
        agent_id: str,
        *,
        relay_url: str,
        base_url: str = DEFAULT_BASE_URL,
        poll_interval_s: int = DEFAULT_POLL_INTERVAL_S,
        heartbeat_s: int = DEFAULT_HEARTBEAT_S,
    ) -> None:
        self.agent_id = agent_id
        self.relay = _RelayClient(relay_url, agent_id)
        self.client = client_for(agent_id, base_url=base_url)
        self.poll_interval_s = max(5, int(poll_interval_s))
        self.heartbeat_s = max(60, int(heartbeat_s))
        self._stop = False
        self._last_heartbeat = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────
    def request_stop(self, *_: object) -> None:
        log.info("stop requested")
        self._stop = True

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        if not self.client.api_key:
            log.error(
                "no api_key for agent_id=%s; register first via "
                "`python -m src.moltbook_register <label>`",
                self.agent_id,
            )
            return

        log.info("moltbook-meca starting; agent=%s poll=%ss heartbeat=%ss",
                 self.agent_id, self.poll_interval_s, self.heartbeat_s)
        while not self._stop:
            try:
                self._tick_messages()
                self._tick_heartbeat()
            except Exception:  # noqa: BLE001 — we want the loop to survive
                log.exception("meca tick crashed")
            # sleep in small chunks so SIGTERM is responsive
            for _ in range(self.poll_interval_s):
                if self._stop:
                    break
                time.sleep(1)
        log.info("moltbook-meca stopped")

    # ── Ticks ─────────────────────────────────────────────────────────────
    def _tick_messages(self) -> None:
        for msg in self.relay.read():
            text = msg.get("message") or msg.get("text") or ""
            if not isinstance(text, str) or not _is_public(text):
                continue
            stripped = _strip_marker(text)
            try:
                result = self.client.post(stripped)
            except EgressBlocked as e:
                _audit({
                    "event": "blocked",
                    "agent_id": self.agent_id,
                    "from": msg.get("from"),
                    "reasons": e.reasons,
                    "matched": e.matched,
                    "preview": stripped[:160],
                })
                log.warning("egress blocked: %s", e.reasons)
                continue
            except MoltbookError as e:
                _audit({
                    "event": "api_error",
                    "agent_id": self.agent_id,
                    "status": e.status,
                    "error": str(e),
                    "preview": stripped[:160],
                })
                log.error("moltbook post failed: %s", e)
                continue
            _audit({
                "event": "published",
                "agent_id": self.agent_id,
                "post_id": result.post_id,
                "sanitized": result.sanitized,
                "text_published": result.text_published[:280],
            })
            log.info("published post=%s sanitized=%s", result.post_id, result.sanitized)

    def _tick_heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat < self.heartbeat_s:
            return
        try:
            instructions = self.client.heartbeat()
        except MoltbookError as e:
            log.warning("heartbeat failed: %s", e)
            return
        self._last_heartbeat = now
        os.makedirs(KEY_STORE_DIR, mode=0o700, exist_ok=True)
        with open(HEARTBEAT_STATE_PATH, "w", encoding="utf-8") as f:
            f.write(_now_iso() + "\n")
        _audit({
            "event": "heartbeat",
            "agent_id": self.agent_id,
            "instructions_keys": sorted(instructions.keys()) if isinstance(instructions, dict) else [],
        })
        log.info("heartbeat ok")


# ── Entrypoint ─────────────────────────────────────────────────────────────

def _read_env() -> tuple[str, str, str, int, int]:
    agent_id = os.environ.get("MOLTBOOK_AGENT_ID", "").strip()
    if not agent_id:
        print("MOLTBOOK_AGENT_ID required", file=sys.stderr)
        sys.exit(2)
    return (
        agent_id,
        os.environ.get("MOLTBOOK_RELAY_URL", "http://localhost:7894"),
        os.environ.get("MOLTBOOK_BASE_URL", DEFAULT_BASE_URL),
        int(os.environ.get("MOLTBOOK_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_S)),
        int(os.environ.get("MOLTBOOK_HEARTBEAT_S", DEFAULT_HEARTBEAT_S)),
    )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MOLTBOOK_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    agent_id, relay, base, poll, hb = _read_env()
    MoltbookMeca(
        agent_id=agent_id,
        relay_url=relay,
        base_url=base,
        poll_interval_s=poll,
        heartbeat_s=hb,
    ).run()


if __name__ == "__main__":
    main()
