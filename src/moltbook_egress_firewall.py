"""Moltbook Egress Firewall — Albert's distinctif.

Even when an agent opts a relay message into `[public]`, mistakes happen:
- a secret accidentally pasted
- a path leak ("/home/damienldx/.ubik-memory/journal/2026-05-11.md")
- a handoff excerpt copy-pasted into a "summary"
- an @mention of an internal-only agent
- a fragment of someone else's private memory

This module decides — between the relay and the Moltbook POST — whether a
message can leave the fleet, must be sanitized first, or must be blocked.

Verdicts:
- ALLOW      → publish as-is
- SANITIZE   → publish with masked spans (paths shortened, secrets redacted)
- BLOCK      → never publish, log the reason, ack back to the author

The firewall is deliberately strict by default: when in doubt, block.
Patterns can be tuned by env (`MOLTBOOK_FW_LEVEL=loose|normal|strict`).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Verdict(str, Enum):
    ALLOW = "allow"
    SANITIZE = "sanitize"
    BLOCK = "block"


@dataclass
class FirewallReport:
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    sanitized_text: Optional[str] = None
    matched_patterns: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "reasons": self.reasons,
            "sanitized_text": self.sanitized_text,
            "matched_patterns": self.matched_patterns,
        }


# ── Pattern bank ────────────────────────────────────────────────────────────
# Each entry: (name, compiled_regex, action). Action is one of:
#   "block"     — verdict becomes BLOCK irrespective of other rules
#   "sanitize"  — span is replaced by a redacted token, verdict at most SANITIZE
#
# Order matters: secrets (highest risk) first.

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # Anthropic / OpenAI / generic Bearer tokens
    ("anthropic_key", re.compile(r"sk-ant-(?:api03|prod)-[A-Za-z0-9_\-]{40,}"), "block"),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}"), "block"),
    ("github_pat", re.compile(r"gh[pousr]_[A-Za-z0-9_]{30,}"), "block"),
    ("gitlab_pat", re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"), "block"),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "block"),
    ("aws_secret", re.compile(r"(?i)aws[_\-]?secret[_\-]?(access[_\-]?)?key[\"'\s:=]+[A-Za-z0-9/+=]{30,}"), "block"),
    ("ssh_private", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "block"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{20,}\b"), "block"),
    # Generic high-entropy 32+ hex (sha256-like ids in secrets) — sanitize only
    ("hex_blob", re.compile(r"\b[a-f0-9]{40,}\b"), "sanitize"),
]

# Local filesystem leaks — sanitize by collapsing to a basename hint.
_PATH_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("home_abs", re.compile(r"/home/[A-Za-z0-9_\-]+(?:/[\w.\-]+)+"), "sanitize"),
    ("users_abs", re.compile(r"/Users/[A-Za-z0-9_\-]+(?:/[\w.\-]+)+"), "sanitize"),
    ("ubik_memory_abs", re.compile(r"~?/\.?ubik-memory(?:/[\w.\-/]+)?"), "sanitize"),
    ("ubik_desktop_abs", re.compile(r"~?/\.?ubik-desktop(?:/[\w.\-/]+)?"), "sanitize"),
    ("claude_dir", re.compile(r"~?/\.?claude(?:-fleet)?(?:/[\w.\-/]+)?"), "sanitize"),
]

# Internal fleet vocabulary — anything that signals private fleet state.
# These are *blockers*: even paraphrased, the message is too leaky to publish.
_INTERNAL_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("handoff_excerpt", re.compile(r"(?i)ma\s+tension\s+(?:port[ée]e|du\s+jour)"), "block"),
    ("handoff_filename", re.compile(r"handoff(?:_[\w\-]+)?\.md"), "block"),
    ("journal_filename", re.compile(r"journal/\d{4}-\d{2}-\d{2}(?:-[\w\-]+)?\.md"), "block"),
    ("identity_filename", re.compile(r"identity(?:_[\w\-]+)?\.md"), "block"),
    ("relationship_filename", re.compile(r"relationship(?:_[\w\-]+)?\.md"), "block"),
    ("bridge_mention", re.compile(r"\bbridge:[a-z0-9\-_]+\b"), "sanitize"),
    ("meca_mention", re.compile(r"\b[a-z0-9_]+-meca\b"), "sanitize"),
    ("fleet_uuid", re.compile(r"\b[0-9a-f]{8}-agent-[0-9]\b"), "sanitize"),
    # PRISMA / LBA-DESKTOP internals — client business data.
    ("prisma_endpoint", re.compile(r"/(?:crm|portefeuille|evenements|kpis[-_]full)\b"), "sanitize"),
    ("rep_code_literal", re.compile(r"\b(?:DIRIL|DIRILA|HATTON|MANSOURI)(?:\s+[A-Z]{3,})?\b"), "block"),
]

# PII — emails, phone numbers.
_PII_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "sanitize"),
    # French phone numbers (rough).
    ("fr_phone", re.compile(r"\b(?:\+33|0)\s?[1-9](?:[\s.\-]?\d{2}){4}\b"), "sanitize"),
]


def _redact(text: str, span: tuple[int, int], label: str) -> str:
    start, end = span
    return f"{text[:start]}[REDACTED:{label}]{text[end:]}"


def _shorten_path(text: str, span: tuple[int, int]) -> str:
    start, end = span
    leaked = text[start:end]
    # keep last 2 segments as a hint, drop the rest
    parts = [p for p in leaked.split("/") if p]
    tail = "/" + "/".join(parts[-2:]) if len(parts) >= 2 else "/" + (parts[-1] if parts else "")
    return f"{text[:start]}<…>{tail}{text[end:]}"


def _level() -> str:
    return os.environ.get("MOLTBOOK_FW_LEVEL", "normal").lower()


def assess(text: str, *, agent_id: Optional[str] = None) -> FirewallReport:
    """Run the firewall on a candidate Moltbook post. Returns a FirewallReport.

    `agent_id` is informational only — used in the audit log to identify the
    author. The decision is *not* relaxed for any agent.
    """
    if not text or not text.strip():
        return FirewallReport(verdict=Verdict.BLOCK, reasons=["empty_message"])

    if len(text) > 5000:
        return FirewallReport(verdict=Verdict.BLOCK, reasons=["over_length_5000"])

    working = text
    matched: list[str] = []
    reasons: list[str] = []
    must_block = False

    def _apply_band(band: list[tuple[str, re.Pattern[str], str]]) -> None:
        nonlocal working, must_block
        for name, regex, action in band:
            # Iterate on a fresh match each loop because text mutates.
            while True:
                m = regex.search(working)
                if not m:
                    break
                matched.append(name)
                if action == "block":
                    reasons.append(f"matched_block:{name}")
                    must_block = True
                    return
                # sanitize
                if name.endswith("_abs") or "path" in name or "_filename" in name:
                    working = _shorten_path(working, m.span())
                else:
                    working = _redact(working, m.span(), name)
                reasons.append(f"sanitized:{name}")
                # safety: avoid infinite loop on zero-width matches
                if m.start() == m.end():
                    break

    for band in (_SECRET_PATTERNS, _INTERNAL_PATTERNS, _PATH_PATTERNS, _PII_PATTERNS):
        _apply_band(band)
        if must_block:
            return FirewallReport(
                verdict=Verdict.BLOCK,
                reasons=reasons,
                matched_patterns=sorted(set(matched)),
            )

    if working != text:
        verdict = Verdict.SANITIZE
    else:
        verdict = Verdict.ALLOW

    # Strict mode: never publish a sanitized message, only pristine ones.
    if _level() == "strict" and verdict is Verdict.SANITIZE:
        return FirewallReport(
            verdict=Verdict.BLOCK,
            reasons=reasons + ["strict_mode_sanitize_treated_as_block"],
            matched_patterns=sorted(set(matched)),
        )

    return FirewallReport(
        verdict=verdict,
        reasons=reasons,
        sanitized_text=working if verdict is Verdict.SANITIZE else None,
        matched_patterns=sorted(set(matched)),
    )
