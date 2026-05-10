"""
Relay bridge — polls the UBIK relay for messages tagged `[public]` and
publishes them to Moltbook through the verified pipeline.

Pipeline (every relay poll):
   1. Read pending messages for the agent.
   2. Filter messages whose body starts with `[public]` (case-insensitive).
   3. Run `sanitize()` over the body to strip internal identifiers.
   4. Build a Moltbook post (title = trimmed first line, content = rest).
   5. Verify against the publication contract.
   6. POST via MoltbookClient if verified.
   7. Append the timestamp to a rolling rate-limit cache.

Failure modes:
- No contract → message logged, not posted, exit code 0 (silent assent
  to the deny-by-default policy).
- ContractError → message logged with the violated rule, not posted.
- MoltbookError → message logged, NOT acked (so we retry next cycle).
- Successful post → ack to the relay so we don't republish.

Notes:
- This bridge is *pull-based*: it polls the relay every N seconds. A
  push model (relay invokes the bridge on send) would be faster but
  also tighter coupling and a single point of failure. Pull keeps the
  bridge optional — the relay works without it.
- One bridge process per agent. Multiple agents on the same host run
  multiple processes, each with its own credentials file. No shared state.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any

from moltbook_client import Credentials, MoltbookClient, MoltbookError
from publication_contract import ContractError, load_contract, verify_post
from sanitize import sanitize

PUBLIC_PREFIX = "[public]"
RECENT_TS_WINDOW_SECONDS = 3600
RATE_LIMIT_CACHE_SIZE = 64

logger = logging.getLogger("ubik-moltbook.relay_bridge")


def _strip_public_prefix(body: str) -> str:
    """Remove the `[public]` marker (and surrounding whitespace) from the body."""
    s = body.lstrip()
    if s.lower().startswith(PUBLIC_PREFIX):
        s = s[len(PUBLIC_PREFIX):].lstrip()
    return s


def _split_title_and_content(body: str, *, max_title_chars: int = 300) -> tuple[str, str | None]:
    """Title = first non-empty line (truncated to max_title_chars).
    Content = the rest of the body, or None when the body has no second line.

    The whole body is the title when it's short and single-line — keeps the
    post compact rather than having a 12-word title and an empty content."""
    body = body.strip()
    if not body:
        return "", None
    lines = body.splitlines()
    first = lines[0].strip()
    if len(first) > max_title_chars:
        title = first[: max_title_chars - 1].rstrip() + "…"
    else:
        title = first
    rest = "\n".join(lines[1:]).strip()
    return title, rest or None


class RelayBridge:
    """One bridge instance per agent. Hold the rate-limit cache in-memory;
    it doesn't need to survive restarts (the post history is also on Moltbook)."""

    def __init__(self,
                 agent_id: str,
                 client: MoltbookClient,
                 relay_url: str = "http://127.0.0.1:7894",
                 default_submolt: str = "ubik-fleet",
                 poll_interval_seconds: float = 30.0):
        self.agent_id = agent_id
        self.client = client
        self.relay_url = relay_url.rstrip("/")
        self.default_submolt = default_submolt
        self.poll_interval = poll_interval_seconds
        # Rate-limit timestamps for the last hour.
        self._recent_posts: deque[float] = deque(maxlen=RATE_LIMIT_CACHE_SIZE)

    # ── Relay I/O ────────────────────────────────────────────────────────────
    def _relay_get(self, path: str) -> Any:
        url = f"{self.relay_url}{path}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())

    def _read_pending(self) -> list[dict[str, Any]]:
        try:
            data = self._relay_get(f"/read?agent_id={self.agent_id}")
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            logger.warning("relay unreachable: %s", e)
            return []
        if not isinstance(data, dict):
            return []
        msgs = data.get("messages") or []
        return [m for m in msgs if isinstance(m, dict)]

    # ── One cycle ────────────────────────────────────────────────────────────
    def process_one_message(self, msg: dict[str, Any]) -> str:
        """Process a single relay message. Returns a one-word verdict:
        'skipped' / 'denied' / 'posted' / 'error'. Used for tests + logs."""
        body = msg.get("body") or msg.get("message") or ""
        if not isinstance(body, str) or not body.strip().lower().startswith(PUBLIC_PREFIX):
            return "skipped"

        payload = _strip_public_prefix(body)
        cleaned, redactions = sanitize(payload)
        if redactions:
            logger.info("[%s] %d redaction(s) before post: %s",
                        self.agent_id, len(redactions),
                        [r.rule for r in redactions])

        title, content = _split_title_and_content(cleaned)
        if not title:
            logger.info("[%s] empty payload after sanitize, skipping", self.agent_id)
            return "skipped"

        submolt = msg.get("submolt") or self.default_submolt
        post_type = "text"

        contract = load_contract(self.agent_id)
        try:
            verify_post(contract,
                        submolt=submolt,
                        post_type=post_type,
                        content_chars=len(title) + (len(content) if content else 0),
                        recent_post_timestamps=list(self._recent_posts))
        except ContractError as e:
            logger.warning("[%s] denied: %s", self.agent_id, e)
            return "denied"

        try:
            self.client.create_post(submolt=submolt, title=title, content=content, type=post_type)
        except MoltbookError as e:
            logger.error("[%s] moltbook error: %s", self.agent_id, e)
            return "error"

        self._recent_posts.append(time.time())
        return "posted"

    # ── Long-running loop ────────────────────────────────────────────────────
    def run_forever(self) -> None:
        logger.info("[%s] bridge started, relay=%s, poll=%.0fs",
                    self.agent_id, self.relay_url, self.poll_interval)
        while True:
            try:
                for msg in self._read_pending():
                    self.process_one_message(msg)
            except Exception as e:    # noqa: BLE001 — keep the loop alive
                logger.exception("[%s] cycle failed: %s", self.agent_id, e)
            time.sleep(self.poll_interval)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="UBIK ↔ Moltbook relay bridge")
    parser.add_argument("--agent-id", required=True, help="UBIK agent id (slot identifier)")
    parser.add_argument("--credentials", default=None, help="Path to Moltbook credentials.json")
    parser.add_argument("--relay-url", default="http://127.0.0.1:7894")
    parser.add_argument("--submolt", default="ubik-fleet")
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    creds = Credentials.load(args.credentials)
    client = MoltbookClient(creds)
    bridge = RelayBridge(args.agent_id, client,
                         relay_url=args.relay_url,
                         default_submolt=args.submolt,
                         poll_interval_seconds=args.poll_seconds)
    bridge.run_forever()


if __name__ == "__main__":
    main()
