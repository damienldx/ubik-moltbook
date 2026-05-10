"""
Publication contract per agent — explicit allowlist signed before any
post goes out to Moltbook.

This composes with the GPS v2 fork contract pattern (`gps_lock` / `gps_relock`):
both pin a per-agent operating envelope, just at different layers.

  - GPS contract: which persona + recommended tools the agent uses internally.
  - Publication contract: which submolts the agent may post into, with what
    kind of content, at what rate. Public-facing only — never gates internal
    fleet behaviour.

A contract is a JSON file at:
    ${UBIK_MEMORY_DIR:-~/.ubik-memory}/forks/<agent_id>/moltbook_contract.json

Without an active contract, `verify_post()` rejects every publication attempt.
Contracts are read on each call (cheap, ~1ms) so an operator can revoke by
deleting the file with immediate effect — no daemon restart needed.

Default policy: deny. A new agent has no contract and so can publish nothing.
The operator (or the lead) issues a contract via `sign_contract()` after
reading the agent's intended scope.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

UBIK_MEMORY_DIR = os.environ.get("UBIK_MEMORY_DIR") or os.path.expanduser("~/.ubik-memory")
FORKS_DIR = Path(UBIK_MEMORY_DIR) / "forks"


@dataclass(frozen=True)
class PublicationContract:
    agent_id: str
    moltbook_agent_name: str
    submolts_allowed: tuple[str, ...]
    max_posts_per_hour: int
    max_chars: int
    allowed_post_types: tuple[str, ...]
    signed_at: float
    signed_by: str            # operator name or "lead" agent_id
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PublicationContract":
        return cls(
            agent_id=data["agent_id"],
            moltbook_agent_name=data["moltbook_agent_name"],
            submolts_allowed=tuple(data["submolts_allowed"]),
            max_posts_per_hour=int(data["max_posts_per_hour"]),
            max_chars=int(data["max_chars"]),
            allowed_post_types=tuple(data.get("allowed_post_types", ["text", "link"])),
            signed_at=float(data["signed_at"]),
            signed_by=data["signed_by"],
            notes=data.get("notes", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["submolts_allowed"] = list(d["submolts_allowed"])
        d["allowed_post_types"] = list(d["allowed_post_types"])
        return d


class ContractError(RuntimeError):
    """Raised when a publication attempt violates the contract."""


def _contract_path(agent_id: str) -> Path:
    return FORKS_DIR / agent_id / "moltbook_contract.json"


def load_contract(agent_id: str) -> PublicationContract | None:
    p = _contract_path(agent_id)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            return PublicationContract.from_dict(json.load(f))
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def sign_contract(contract: PublicationContract) -> Path:
    """Persist the contract to disk. Overwrites any existing contract for
    the same agent — use this for both initial signature and `relock`."""
    p = _contract_path(contract.agent_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(contract.to_dict(), f, indent=2)
    tmp.replace(p)
    return p


def unlock_contract(agent_id: str) -> bool:
    p = _contract_path(agent_id)
    if not p.exists():
        return False
    p.unlink()
    return True


# ── Verification ─────────────────────────────────────────────────────────────
# A post is veto'd unless:
#   1. There is a contract for the agent
#   2. The submolt is in the allowlist
#   3. The post type is in the allowlist
#   4. The content size is within max_chars
#   5. The agent's recent posts (read from the recent_post_timestamps cache)
#      stay under max_posts_per_hour
#
# The caller is responsible for providing the post history — `verify_post`
# doesn't read or write any rate-limit state itself, to stay a pure function
# easy to test.

def verify_post(contract: PublicationContract | None,
                *,
                submolt: str,
                post_type: str,
                content_chars: int,
                recent_post_timestamps: list[float]) -> None:
    """Raise `ContractError` if the post violates the contract.
    Returns None on success (silent assent)."""
    if contract is None:
        raise ContractError("no active publication contract")
    if submolt not in contract.submolts_allowed:
        raise ContractError(
            f"submolt {submolt!r} not in contract allowlist {list(contract.submolts_allowed)!r}",
        )
    if post_type not in contract.allowed_post_types:
        raise ContractError(
            f"post type {post_type!r} not in contract allowlist {list(contract.allowed_post_types)!r}",
        )
    if content_chars > contract.max_chars:
        raise ContractError(
            f"content size {content_chars} exceeds contract limit {contract.max_chars}",
        )
    # Rate-limit window: count timestamps within the last hour.
    now = time.time()
    recent = [t for t in recent_post_timestamps if now - t < 3600]
    if len(recent) >= contract.max_posts_per_hour:
        raise ContractError(
            f"hourly post limit reached ({len(recent)}/{contract.max_posts_per_hour})",
        )
