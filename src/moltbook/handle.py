"""Map an internal fleet `agent_id` to a public Moltbook `handle`.

Why a mapping layer instead of using `agent_id` directly :
    `b5aeb927-agent-0` is opaque, exposes the internal slot scheme. Posting
    under "jules" is nicer to humans reading and aligned with the agent's
    social identity *as it was named*.
"""
from __future__ import annotations

import os
import re
import urllib.request
import urllib.error
import urllib.parse
import json
from typing import Optional


_RELAY_URL_ENV = "UBIK_RELAY_URL"
_DEFAULT_RELAY = "http://localhost:7894"
_OVERRIDE_ENV = "MOLTBOOK_HANDLE"

_HANDLE_RE = re.compile(r"[^a-z0-9_-]+")


def _slugify(label: str) -> str:
    s = label.strip().lower().replace(" ", "-")
    s = _HANDLE_RE.sub("", s)
    s = s.strip("-_") or "agent"
    return s[:30]


def _relay_label(agent_id: str, *, base: str, timeout: float = 1.5) -> Optional[str]:
    """Best-effort read of the agent's label from the relay. None on failure."""
    if not agent_id:
        return None
    url = f"{base.rstrip('/')}/agents/{urllib.parse.quote(agent_id)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
            payload = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    label = payload.get("label")
    if not isinstance(label, str) or not label.strip():
        return None
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", label).strip()
    return cleaned or None


def resolve_handle(
    agent_id: str,
    *,
    override: Optional[str] = None,
    relay_url: Optional[str] = None,
) -> str:
    """Public Moltbook handle for an agent_id, with fallback chain :
      1. Explicit `override` argument.
      2. `MOLTBOOK_HANDLE` env var.
      3. Relay `label` (e.g. "Jules"), with prefix `[lead]` stripped.
      4. Fallback : the raw `agent_id` (slugified).
    """
    if override:
        return _slugify(override)
    env_override = os.environ.get(_OVERRIDE_ENV)
    if env_override:
        return _slugify(env_override)
    base = relay_url or os.environ.get(_RELAY_URL_ENV, _DEFAULT_RELAY)
    label = _relay_label(agent_id, base=base)
    if label:
        return _slugify(label)
    return _slugify(agent_id)
