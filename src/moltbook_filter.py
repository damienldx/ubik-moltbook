"""moltbook_filter.py — Privacy filter + append-only audit log for Moltbook publications.

Two responsibilities:

1. **Privacy filter** — refuse to publish any content that matches a regex
   blacklist. The blacklist covers UBIK-internal file paths, fleet memory keys,
   LBA business internals (Prisma, code clients), and absolute paths. A single
   match means the whole post is rejected — no partial redaction, no surprises.

2. **Audit log** — every publish attempt (accepted or rejected) is appended to
   `~/.ubik-memory/moltbook/audit.jsonl`. The log is append-only: we never
   rewrite or delete entries. If you want to revoke a post on Moltbook, do it
   server-side; the local audit trail stays as historical record.

The blacklist is intentionally over-aggressive. A false positive ("blocked
something harmless") just means the agent has to rephrase. A false negative
("published something sensitive") leaks data that can't be unleaked. We
optimize for the first kind of error.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

AUDIT_DIR = Path(os.path.expanduser("~/.ubik-memory/moltbook"))
AUDIT_LOG = AUDIT_DIR / "audit.jsonl"
DRAFT_TTL_SECONDS = 600  # 10 minutes

# ── Blacklist ────────────────────────────────────────────────────────────────
#
# Patterns that, if matched anywhere in the post, cause hard rejection.
# Order doesn't matter — we test all of them and report every match.

_BLACKLIST_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Fleet memory & handoff internals
    ("memory_key", re.compile(r"\b(handoff|MEMORY\.md|memory_recall|relationship\.md|identity\.md)\b", re.IGNORECASE)),
    ("journal", re.compile(r"\bjournal[_/-]?\d{4}", re.IGNORECASE)),
    # LBA / Prisma business internals
    ("lba_internal", re.compile(r"\b(LBA[-_]?DESKTOP|PRISMA|prisma|CRM_Visites|CRM_Client|suivi_crm)\b")),
    ("client_code", re.compile(r"\bC0\d{4,5}\b")),  # LBA client codes like C01234
    ("rep_name", re.compile(r"\b(MANSOURI|DIRILA|DIRIL ANDRE|HATTON)\b")),
    # Absolute paths & worktrees
    ("absolute_path", re.compile(r"/home/damienldx/\S+")),
    ("workspace_path", re.compile(r"~/workspace/\S+")),
    # Credentials / tokens / API keys (best-effort heuristic)
    ("token_like", re.compile(r"\b(api[_-]?key|token|bearer|secret)\b\s*[:=]\s*\S+", re.IGNORECASE)),
    ("ghp_token", re.compile(r"\bgh[ps]_[A-Za-z0-9]{20,}\b")),
    # Bridge / méca internals — interesting for fleet, noise for the public
    ("bridge_internal", re.compile(r"\b(bridge:|ledger-meca|memory-meca|console-meca|relay/server\.py)\b")),
]


@dataclass
class FilterResult:
    accepted: bool
    matches: list[str]            # blacklist tag names that matched, empty if accepted
    matched_snippets: list[str]   # actual substrings that triggered the rejection (for audit)

    def to_dict(self) -> dict:
        return asdict(self)


def filter_content(content: str) -> FilterResult:
    """Run the blacklist against `content`. Returns a FilterResult."""
    matches: list[str] = []
    snippets: list[str] = []
    for tag, pattern in _BLACKLIST_PATTERNS:
        for m in pattern.finditer(content):
            matches.append(tag)
            snippet = m.group(0)
            # Keep snippets short — we don't want to bloat the audit log with full quotes.
            snippets.append(snippet[:80])
    return FilterResult(accepted=(len(matches) == 0), matches=matches, matched_snippets=snippets)


# ── Audit log ────────────────────────────────────────────────────────────────


@dataclass
class AuditEntry:
    ts: float                     # unix epoch seconds
    iso_ts: str                   # ISO 8601 for human readers
    agent_id: str
    draft_id: str
    action: str                   # one of: draft_received, draft_rejected, approved, published, publish_failed, draft_expired
    content_preview: str          # first 200 chars of the original content
    filter_matches: list[str]     # blacklist tags that matched (empty when none)
    extra: dict                   # action-specific metadata (HTTP status, profile_id, etc.)


def append_audit(entry: AuditEntry) -> None:
    """Append a single audit entry as a JSONL line. Best-effort, never raises."""
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
    except Exception:
        # Audit logging must never break the publishing pipeline. If the disk
        # is full or the path unwritable, we lose the trace — operationally
        # noisy but better than crashing the méca.
        pass


def read_audit(limit: int = 50) -> list[dict]:
    """Read the tail of the audit log (most recent first). Returns up to `limit` entries."""
    if not AUDIT_LOG.exists():
        return []
    lines: list[str] = []
    try:
        with AUDIT_LOG.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    out: list[dict] = []
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


# ── Draft queue (in-memory, per-méca-process) ────────────────────────────────
#
# A draft is a [public:draft] message awaiting [public:approve:<draft_id>] from
# the same sender within DRAFT_TTL_SECONDS. The queue is intentionally
# in-memory: drafts are ephemeral, restarting the méca drops them. The audit
# log retains the trace either way.


@dataclass
class Draft:
    draft_id: str
    agent_id: str
    content: str
    created_at: float


class DraftQueue:
    """In-memory queue keyed by draft_id."""

    def __init__(self, ttl_seconds: float = DRAFT_TTL_SECONDS) -> None:
        self._drafts: dict[str, Draft] = {}
        self._ttl = ttl_seconds

    def enqueue(self, agent_id: str, content: str) -> Draft:
        draft = Draft(
            draft_id=uuid.uuid4().hex[:12],
            agent_id=agent_id,
            content=content,
            created_at=time.time(),
        )
        self._drafts[draft.draft_id] = draft
        return draft

    def pop(self, draft_id: str, agent_id: str) -> Optional[Draft]:
        """Return and remove the draft if it exists, belongs to `agent_id`, and isn't expired."""
        draft = self._drafts.get(draft_id)
        if draft is None:
            return None
        if draft.agent_id != agent_id:
            # Same draft_id but wrong sender — never honor cross-agent approvals.
            return None
        if time.time() - draft.created_at > self._ttl:
            # Don't return expired drafts even if asked; let sweep() emit the
            # audit entry. We do clean it up here so the in-memory queue stays
            # tight under load.
            self._drafts.pop(draft_id, None)
            return None
        self._drafts.pop(draft_id, None)
        return draft

    def sweep(self) -> list[Draft]:
        """Remove and return expired drafts. Call periodically from the méca loop."""
        now = time.time()
        expired: list[Draft] = []
        for did, draft in list(self._drafts.items()):
            if now - draft.created_at > self._ttl:
                expired.append(draft)
                self._drafts.pop(did, None)
        return expired

    def size(self) -> int:
        return len(self._drafts)
