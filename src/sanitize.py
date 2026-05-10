"""
Sanitize text destined for Moltbook (public network) — strip identifiers
that belong to internal UBIK fleet operations or to client/partner data.

This is a *deny-list* applied to message text on the way out of the fleet.
A deny-list is never complete by itself — it's combined with the publication
contract (whitelisting which message kinds may be posted) and the [public]
opt-in marker. The three layers together give defense in depth:

  1. Opt-in marker — only [public]-tagged messages enter the pipeline.
  2. Sanitizer (this file) — removes identifiers even from messages whose
     author meant to publish.
  3. Publication contract — limits which submolts and message kinds an
     agent is allowed to post; vetoes anything outside its scope.

If the sanitizer is unsure whether a token is safe (e.g. an opaque hex
string of unclear meaning), it errs on the side of redaction. Better a
slightly less informative post than a leaked code_client or fork_id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── Patterns ─────────────────────────────────────────────────────────────────
# Each rule has a name and a regex. Names show up in `Redaction` so the caller
# can audit *which* rule fired; useful for debugging false positives without
# logging the original text.

_RULES: list[tuple[str, re.Pattern[str]]] = [
    # UBIK fleet identifiers: 8-hex-vehicle + slot, e.g. 6388a209-agent-0
    ("ubik_agent_id",   re.compile(r"\b[0-9a-f]{8}-agent-\d+\b", re.IGNORECASE)),

    # Fork / project IDs: 8-12 hex chars in identifier context (preceded by
    # "fork", "project", "fork_id=", etc.). We refuse to match standalone
    # 8-hex tokens because those are too common (git SHAs in PR titles), and
    # SHAs in public PRs are not sensitive.
    ("ubik_fork_id",    re.compile(r"(?i)\b(?:fork_id|project_id|fork|project)[\s=:]+[0-9a-f]{8,12}\b")),

    # LBA client codes: C followed by 4-6 digits, common in PRISMA exports
    # (CRM Visites uses this format).
    ("lba_client_code", re.compile(r"\bC\d{4,6}\b")),

    # File paths under ~/.ubik-memory/, ~/workspace/, /home/ — these point
    # at internal state.
    ("internal_path",   re.compile(r"(?:~|/home/[^/\s]+)/\.?(?:ubik-memory|workspace|claude-fleet)/[^\s'\"]*")),

    # Bearer tokens / api_key fragments, just in case someone copy-pastes.
    ("bearer_token",    re.compile(r"(?i)\b(?:bearer|api[_-]?key|token)[\s=:]+[a-z0-9_\-]{16,}\b")),

    # Email addresses (always strip; if a fleet message mentions an email,
    # don't relay it publicly).
    ("email",           re.compile(r"\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
]


@dataclass(frozen=True)
class Redaction:
    """One redaction event. Returned by `sanitize` so callers can audit
    what was stripped before publishing."""
    rule: str
    span: tuple[int, int]
    replacement: str


def sanitize(text: str) -> tuple[str, list[Redaction]]:
    """Return (cleaned_text, list_of_redactions).

    Replacements use the rule name in brackets, e.g. "[redacted:ubik_agent_id]",
    so the resulting post still reads naturally and the redaction is visible.
    Empty input returns (empty, no redactions).
    """
    if not text:
        return text, []

    redactions: list[Redaction] = []
    # Apply rules in order; each rule operates on the output of the previous,
    # so an `lba_client_code` inside an `internal_path` would only be flagged
    # once (the path strip happens first, and the cleaned text no longer has
    # the code). Order is chosen so the broader rules run first.
    cleaned = text
    for rule_name, pattern in _RULES:
        def _replace(match: re.Match[str], _rule: str = rule_name) -> str:
            replacement = f"[redacted:{_rule}]"
            redactions.append(Redaction(rule=_rule, span=match.span(), replacement=replacement))
            return replacement
        cleaned = pattern.sub(_replace, cleaned)
    return cleaned, redactions


def has_private_signal(text: str) -> bool:
    """Cheap check: does the text contain at least one redaction-worthy pattern?
    Useful for early refusal in the publication pipeline before composing a post."""
    if not text:
        return False
    for _, pattern in _RULES:
        if pattern.search(text):
            return True
    return False
