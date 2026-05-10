"""moltbook_listener.py — Moltbook → UBIK relay bridge (inbound direction).

Polls Moltbook for mentions of fleet agents and delivers them to the relay.
This is the reverse of moltbook_meca.py: external agents talking to our fleet
come in through here, not through the radio.

Design decisions:
- Poll-based (no webhook support assumed on the Moltbook API)
- Per-agent dedup via a seen-ids file (~/.ubik-moltbook/<agent_id>/seen.json)
- Delivers as relay DM to the agent's relay_id (not broadcast)
- Best-effort: errors are logged, not raised — the loop must never crash
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Optional

RELAY_BASE = os.environ.get("UBIK_RELAY_URL", "http://127.0.0.1:7894")
MOLTBOOK_BASE = "https://www.moltbook.com/api/v1"
MOLTBOOK_DIR = Path(os.path.expanduser("~/.ubik-moltbook"))
POLL_INTERVAL_S = 60  # poll every minute


# ── Credentials (same format as moltbook_client.py) ──────────────────────────

def _load_creds(agent_id: str) -> Optional[dict]:
    path = MOLTBOOK_DIR / agent_id / "moltbook.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


# ── Seen-IDs dedup ────────────────────────────────────────────────────────────

def _seen_path(agent_id: str) -> Path:
    return MOLTBOOK_DIR / agent_id / "seen.json"


def _load_seen(agent_id: str) -> set[str]:
    path = _seen_path(agent_id)
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except Exception:
        return set()


def _save_seen(agent_id: str, seen: set[str]) -> None:
    path = _seen_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep last 500 ids to bound file size
    trimmed = list(seen)[-500:]
    path.write_text(json.dumps(trimmed))


# ── Moltbook fetch ────────────────────────────────────────────────────────────

def _moltbook_get(path: str, api_key: str) -> Optional[list]:
    url = f"{MOLTBOOK_BASE}{path}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "ubik-fleet/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data if isinstance(data, list) else data.get("items", [])
    except Exception as e:
        print(f"[listener] fetch {path} failed: {e}")
        return None


def fetch_mentions(api_key: str, profile_id: str) -> list[dict]:
    """Return new mentions from Moltbook for this agent."""
    items = _moltbook_get(f"/agents/{profile_id}/mentions?limit=50", api_key)
    return items or []


# ── Relay delivery ────────────────────────────────────────────────────────────

def _relay_send(from_agent: str, to_agent: str, message: str) -> bool:
    url = f"{RELAY_BASE}/messages"
    body = json.dumps({"from": from_agent, "to": to_agent, "message": message}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        print(f"[listener] relay send failed: {e}")
        return False


def _format_mention(item: dict) -> str:
    sender = item.get("from_handle", "unknown@moltbook")
    content = item.get("content", "").strip()
    url = item.get("url", "")
    parts = [f"[moltbook:mention] @{sender}: {content}"]
    if url:
        parts.append(f"  → {url}")
    return "\n".join(parts)


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_listener(agent_id: str, relay_id: str) -> None:
    """
    Poll Moltbook mentions for agent_id and forward new ones to relay_id.
    Runs forever; kill with SIGTERM.
    """
    creds = _load_creds(agent_id)
    if not creds:
        print(f"[listener] no credentials for {agent_id} — exiting")
        return

    api_key = creds["api_key"]
    profile_id = creds["profile_id"]
    print(f"[listener] starting for {agent_id} (profile={profile_id}, relay={relay_id})")

    while True:
        seen = _load_seen(agent_id)
        mentions = fetch_mentions(api_key, profile_id)

        new_count = 0
        for item in mentions:
            mid = item.get("id")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            msg = _format_mention(item)
            if _relay_send(f"moltbook-{agent_id}", relay_id, msg):
                new_count += 1
                print(f"[listener] delivered mention {mid} to {relay_id}")
            else:
                # Remove from seen so we retry next cycle
                seen.discard(mid)

        _save_seen(agent_id, seen)
        if new_count:
            print(f"[listener] {new_count} new mention(s) delivered")

        time.sleep(POLL_INTERVAL_S)


# ── Fleet batch runner ────────────────────────────────────────────────────────

def run_fleet_listeners(agent_relay_map: dict[str, str]) -> None:
    """
    Run listeners for multiple agents in a single process using threads.
    agent_relay_map: {agent_id: relay_id}
    Useful for a single systemd unit covering the whole fleet.
    """
    import threading

    threads = []
    for agent_id, relay_id in agent_relay_map.items():
        t = threading.Thread(
            target=run_listener,
            args=(agent_id, relay_id),
            name=f"listener-{agent_id}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        print(f"[listener] thread started for {agent_id}")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("[listener] shutting down")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Moltbook → UBIK relay listener")
    parser.add_argument("--agent-id", required=True, help="UBIK agent ID")
    parser.add_argument("--relay-id", required=True, help="Relay ID to receive mentions")
    parser.add_argument(
        "--fleet",
        metavar="AGENT_ID:RELAY_ID",
        nargs="+",
        help="Run for multiple agents: agent_id:relay_id pairs",
    )
    args = parser.parse_args()

    if args.fleet:
        mapping = {}
        for pair in args.fleet:
            aid, rid = pair.split(":", 1)
            mapping[aid] = rid
        run_fleet_listeners(mapping)
    else:
        run_listener(args.agent_id, args.relay_id)
