"""moltbook_meca.py — Relay bridge for Moltbook.

Polls the UBIK relay for messages tagged [public] and posts them to Moltbook.
Also handles the 4h heartbeat loop per configured agent.

Usage:
  python3 moltbook_meca.py --agent-id <agent_id>

The agent must be configured first:
  python3 -c "from src.moltbook_client import configure; configure('<id>', '<key>', '<profile>')"
"""

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from moltbook_client import fetch_heartbeat, post, load_creds, HEARTBEAT_INTERVAL

RELAY_URL = os.getenv("RELAY_URL", "http://127.0.0.1:7894")
PUBLIC_TAG = "[public]"
POLL_SLEEP = 5  # seconds between relay polls


def relay_get(path: str) -> dict:
    try:
        with urllib.request.urlopen(RELAY_URL + path, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def relay_post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(RELAY_URL + path, data=data,
                                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def strip_tag(content: str) -> str:
    """Remove [public] tag from message before posting."""
    return content.replace(PUBLIC_TAG, "").strip()


def run(agent_id: str) -> None:
    creds = load_creds(agent_id)
    if not creds:
        print(f"[moltbook-meca] ERROR: No credentials for {agent_id}. Configure first.", flush=True)
        sys.exit(1)

    print(f"[moltbook-meca] Starting for {agent_id}", flush=True)

    # Register as listener on relay
    relay_post(f"/register/{agent_id}-moltbook", {
        "label": f"[bridge] Moltbook ({agent_id})",
        "capabilities": ["moltbook"],
    })

    last_heartbeat = 0.0

    while True:
        now = time.time()

        # Heartbeat every 4h
        if now - last_heartbeat > HEARTBEAT_INTERVAL:
            result = fetch_heartbeat(agent_id)
            if "error" not in result:
                print(f"[moltbook-meca] heartbeat OK", flush=True)
            last_heartbeat = now

        # Poll relay for [public] messages from this agent
        result = relay_get(f"/read/{agent_id}")
        messages = result.get("messages", []) if isinstance(result, dict) else []
        for msg in messages:
            content = msg.get("message", msg.get("content", ""))
            if PUBLIC_TAG in content:
                clean = strip_tag(content)
                resp = post(agent_id, clean)
                if "error" in resp:
                    print(f"[moltbook-meca] post error: {resp}", flush=True)
                else:
                    print(f"[moltbook-meca] posted: {clean[:60]}…", flush=True)

        time.sleep(POLL_SLEEP)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", required=True)
    args = parser.parse_args()
    run(args.agent_id)
