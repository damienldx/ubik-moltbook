"""Moltbook integration for the UBIK fleet — Jules' fork.

Three layers, all opt-in:
- `client`  — thin HTTPX wrapper for the Moltbook v1 REST API.
- `filter`  — defence-in-depth content filter (regex blocklist for fleet internals).
- `handle`  — mapping `agent_id` → public Moltbook handle (via fleet `label`,
              never the raw `<uuid>-agent-N` identifier).

The intended deployment is **as MCP tools inside ubik-mcp** (see
`docs/fork-jules.md`). The relay watches messages with a `[public]` prefix and
fires a `moltbook_post` MCP call. The agent doesn't need to import this module
directly — it just prefixes its public messages and the rest is wiring.
"""

from .client import MoltbookClient, MoltbookError
from .filter import ContentFilter, FilterReject
from .handle import resolve_handle

__all__ = [
    "MoltbookClient",
    "MoltbookError",
    "ContentFilter",
    "FilterReject",
    "resolve_handle",
]
