"""moltbook_meca.py — Background méca that publishes opt-in fleet messages to Moltbook.

Wiring with the relay (port 7892):
    Agent sends to the relay: "[public:draft] My update text..."
    → méca polls the relay, sees the draft, runs the privacy filter
    → if filter passes, the méca holds the draft in memory with a draft_id
    → méca replies to the sender via relay with the draft_id and a TTL
    → sender (or operator) sends back "[public:approve:<draft_id>]"
    → méca publishes to Moltbook and audits the result
    → unapproved drafts are dropped after 10 minutes (audited as `draft_expired`)

What this méca does NOT do:
    - It does not auto-publish anything based on heuristics. The 2-step approval
      is the safety net: even if the regex blacklist fails, the message still
      needs an explicit human (or agent) green light before going public.
    - It does not delete or rewrite history. The audit log is append-only.
    - It does not store credentials. `moltbook_client.load_creds()` reads them
      per-agent from disk on every publish — rotating keys is a file edit.

Heartbeat: Moltbook expects a fetch of `/heartbeat` every ~4 hours. The méca
loop does this opportunistically — once per HEARTBEAT_LOOP_SECONDS — rather
than scheduling a separate timer.

Environment:
    RELAY_URL                default http://127.0.0.1:7892
    MOLTBOOK_POLL_SECONDS    default 5
    HEARTBEAT_LOOP_SECONDS   default 14400 (4h)
"""

from __future__ import annotations

import os
import re
import sys
import time
import json
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional

# Make `moltbook_client` importable when the méca runs from src/.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import moltbook_client
from moltbook_filter import (
    AuditEntry,
    DraftQueue,
    append_audit,
    filter_content,
    DRAFT_TTL_SECONDS,
)

RELAY_URL = os.environ.get("RELAY_URL", "http://127.0.0.1:7892")
POLL_SECONDS = float(os.environ.get("MOLTBOOK_POLL_SECONDS", "5"))
HEARTBEAT_LOOP_SECONDS = float(os.environ.get("HEARTBEAT_LOOP_SECONDS", str(4 * 3600)))
MECA_AGENT_ID = "moltbook-meca"

DRAFT_PREFIX = "[public:draft]"
APPROVE_RE = re.compile(r"^\[public:approve:([a-f0-9]{6,32})\]\s*(.*)$", re.DOTALL)
# Optional content preview used in operator replies — keep it short.
PREVIEW_CHARS = 200


# ── Relay helpers ────────────────────────────────────────────────────────────


def _http_json(method: str, path: str, body: Optional[dict] = None, timeout: float = 5.0) -> dict:
    url = RELAY_URL.rstrip("/") + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_msg": e.read().decode("utf-8", errors="replace")}
    except Exception as e:
        return {"_http_error": "network", "_msg": str(e)}


def relay_register(agent_id: str, label: str) -> dict:
    return _http_json("POST", "/agents", {"agent_id": agent_id, "label": label, "capabilities": ["meca", "moltbook"]})


def relay_read(agent_id: str) -> list[dict]:
    res = _http_json("GET", f"/inbox/{agent_id}")
    if "_http_error" in res:
        return []
    msgs = res.get("messages") or []
    return msgs if isinstance(msgs, list) else []


def relay_send(to: str, message: str, sender: str = MECA_AGENT_ID) -> dict:
    return _http_json("POST", "/relay/send", {"to": to, "message": message, "from": sender})


# ── Draft / approve handling ─────────────────────────────────────────────────


@dataclass
class IncomingMessage:
    sender: str
    body: str


def _preview(content: str, n: int = PREVIEW_CHARS) -> str:
    s = content.strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def handle_draft(queue: DraftQueue, msg: IncomingMessage) -> None:
    """Process a [public:draft] message: filter, enqueue or reject, reply to sender."""
    content = msg.body[len(DRAFT_PREFIX):].strip()
    if not content:
        # An empty draft is a no-op; we don't even audit it. It's likely a typo.
        relay_send(msg.sender, "[moltbook] draft vide — rien à publier.")
        return

    verdict = filter_content(content)
    if not verdict.accepted:
        # Hard reject. Tell the sender what matched so they can rewrite.
        tags = ", ".join(sorted(set(verdict.matches)))
        append_audit(_audit(
            agent_id=msg.sender,
            draft_id="",
            action="draft_rejected",
            content=content,
            matches=verdict.matches,
            extra={"matched_snippets": verdict.matched_snippets},
        ))
        relay_send(
            msg.sender,
            f"[moltbook] draft REFUSÉ — contenu protégé détecté ({tags}). "
            f"Rephrase sans interner UBIK/LBA/chemins absolus et renvoie.",
        )
        return

    draft = queue.enqueue(msg.sender, content)
    append_audit(_audit(
        agent_id=msg.sender,
        draft_id=draft.draft_id,
        action="draft_received",
        content=content,
        matches=[],
        extra={"ttl_seconds": DRAFT_TTL_SECONDS},
    ))
    relay_send(
        msg.sender,
        f"[moltbook] draft accepté id={draft.draft_id}. "
        f"Pour publier, renvoie `[public:approve:{draft.draft_id}]` dans les "
        f"{int(DRAFT_TTL_SECONDS / 60)} minutes.",
    )


def handle_approve(queue: DraftQueue, msg: IncomingMessage, draft_id: str) -> None:
    """Process a [public:approve:<draft_id>] message: pop the draft and publish."""
    draft = queue.pop(draft_id, msg.sender)
    if draft is None:
        relay_send(
            msg.sender,
            f"[moltbook] approve refusé — draft {draft_id} inconnu, expiré, ou pas à toi.",
        )
        return

    # Re-run the filter at approval time. Defensive: if the blacklist evolved
    # between draft and approve (rare but possible), we'd rather block than
    # publish stale-cleared content.
    verdict = filter_content(draft.content)
    if not verdict.accepted:
        append_audit(_audit(
            agent_id=msg.sender,
            draft_id=draft_id,
            action="draft_rejected",
            content=draft.content,
            matches=verdict.matches,
            extra={"at": "approve_time", "matched_snippets": verdict.matched_snippets},
        ))
        relay_send(
            msg.sender,
            f"[moltbook] approve refusé — filter triggered au moment de la publication "
            f"({', '.join(sorted(set(verdict.matches)))}). Draft jeté.",
        )
        return

    append_audit(_audit(
        agent_id=msg.sender,
        draft_id=draft_id,
        action="approved",
        content=draft.content,
        matches=[],
        extra={},
    ))

    # Actually publish. moltbook_client.post() reads creds from disk on every
    # call — no credentials cached in this process.
    response = moltbook_client.post(msg.sender, draft.content)
    if "error" in response:
        append_audit(_audit(
            agent_id=msg.sender,
            draft_id=draft_id,
            action="publish_failed",
            content=draft.content,
            matches=[],
            extra={"response": response},
        ))
        relay_send(msg.sender, f"[moltbook] publish FAILED: {response.get('error')} — {response.get('msg', '')}")
        return

    append_audit(_audit(
        agent_id=msg.sender,
        draft_id=draft_id,
        action="published",
        content=draft.content,
        matches=[],
        extra={"response": response},
    ))
    relay_send(msg.sender, f"[moltbook] publié ✓ id={draft_id}")


def _audit(*, agent_id: str, draft_id: str, action: str, content: str,
           matches: list[str], extra: dict) -> AuditEntry:
    now = time.time()
    return AuditEntry(
        ts=now,
        iso_ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        agent_id=agent_id,
        draft_id=draft_id,
        action=action,
        content_preview=_preview(content),
        filter_matches=matches,
        extra=extra,
    )


# ── Main loop ────────────────────────────────────────────────────────────────


def main() -> int:
    relay_register(MECA_AGENT_ID, "[bridge] Moltbook")
    queue = DraftQueue()
    last_heartbeat = 0.0
    heartbeat_agents: set[str] = set()  # agents we've heartbeat'd for in this loop iteration

    while True:
        # 1. Drain inbox.
        for raw in relay_read(MECA_AGENT_ID):
            body = (raw.get("message") or "").strip()
            sender = raw.get("from") or raw.get("sender") or "unknown"
            if not body:
                continue
            if body.startswith(DRAFT_PREFIX):
                handle_draft(queue, IncomingMessage(sender=sender, body=body))
                heartbeat_agents.add(sender)
                continue
            m = APPROVE_RE.match(body)
            if m:
                draft_id = m.group(1)
                handle_approve(queue, IncomingMessage(sender=sender, body=body), draft_id)
                heartbeat_agents.add(sender)
                continue
            # Anything else addressed to the méca is ignored. We never auto-mirror.

        # 2. Expire stale drafts (audit, notify sender).
        for expired in queue.sweep():
            append_audit(_audit(
                agent_id=expired.agent_id,
                draft_id=expired.draft_id,
                action="draft_expired",
                content=expired.content,
                matches=[],
                extra={"ttl_seconds": DRAFT_TTL_SECONDS},
            ))
            relay_send(
                expired.agent_id,
                f"[moltbook] draft {expired.draft_id} expiré (>{int(DRAFT_TTL_SECONDS / 60)} min sans approve). "
                f"Renvoie `[public:draft]` si tu veux republier.",
            )

        # 3. Heartbeat — best-effort, once per loop window, only for agents
        # who actually sent us something recently. No traffic on Moltbook if
        # the fleet is silent.
        now = time.time()
        if now - last_heartbeat > HEARTBEAT_LOOP_SECONDS and heartbeat_agents:
            for agent_id in heartbeat_agents:
                moltbook_client.fetch_heartbeat(agent_id)
            last_heartbeat = now
            heartbeat_agents.clear()

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
