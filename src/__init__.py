"""Albert's UBIK ↔ Moltbook integration.

Three layers:
- moltbook_egress_firewall: content firewall, decides ALLOW / SANITIZE / BLOCK
- moltbook_client:           thin HTTP wrapper that enforces the firewall on post()
- moltbook_meca:             optional systemd daemon that mirrors [public] relay
                             messages, runs heartbeat, and writes an egress audit log
"""
from .moltbook_egress_firewall import Verdict, FirewallReport, assess
from .moltbook_client import (
    MoltbookClient,
    MoltbookError,
    EgressBlocked,
    PostResult,
    FeedItem,
    client_for,
    load_api_key,
    save_api_key,
)

__all__ = [
    "Verdict",
    "FirewallReport",
    "assess",
    "MoltbookClient",
    "MoltbookError",
    "EgressBlocked",
    "PostResult",
    "FeedItem",
    "client_for",
    "load_api_key",
    "save_api_key",
]
