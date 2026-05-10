"""moltbook_client.py — Minimal Moltbook API client for UBIK fleet agents.

Each agent has its own API key stored in ~/.ubik-memory/<agent_id>/moltbook.json.
No shared credentials. No data leakage — only [public]-tagged relay messages are posted.
"""

import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

MOLTBOOK_API_BASE = "https://www.moltbook.com/api/v1"
MEMORY_DIR = Path(os.path.expanduser("~/.ubik-memory"))
HEARTBEAT_INTERVAL = 4 * 3600  # 4h in seconds


def _creds_path(agent_id: str) -> Path:
    return MEMORY_DIR / agent_id / "moltbook.json"


def load_creds(agent_id: str) -> Optional[dict]:
    """Return {'api_key': ..., 'profile_id': ...} or None if not configured."""
    path = _creds_path(agent_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def save_creds(agent_id: str, api_key: str, profile_id: str) -> None:
    path = _creds_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"api_key": api_key, "profile_id": profile_id}, indent=2))


def _request(method: str, path: str, api_key: str, body: Optional[dict] = None) -> dict:
    url = MOLTBOOK_API_BASE + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "ubik-fleet/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "msg": e.read().decode("utf-8", errors="replace")}
    except Exception as e:
        return {"error": str(e)}


def post(agent_id: str, content: str) -> dict:
    """Post content to Moltbook as the given agent. Returns API response."""
    creds = load_creds(agent_id)
    if not creds:
        return {"error": f"No Moltbook credentials for {agent_id}. Run configure() first."}
    return _request("POST", "/post", creds["api_key"], {"content": content, "agent_id": creds["profile_id"]})


def fetch_heartbeat(agent_id: str) -> dict:
    """Fetch the heartbeat instructions file. Call every 4h."""
    creds = load_creds(agent_id)
    if not creds:
        return {"error": "no creds"}
    return _request("GET", "/heartbeat", creds["api_key"])


def read_feed(agent_id: str, limit: int = 20) -> dict:
    """Read the Moltbook feed for this agent."""
    creds = load_creds(agent_id)
    if not creds:
        return {"error": "no creds"}
    return _request("GET", f"/feed?limit={limit}", creds["api_key"])


def configure(agent_id: str, api_key: str, profile_id: str) -> None:
    """One-time setup: store API key + profile_id for an agent."""
    save_creds(agent_id, api_key, profile_id)
    print(f"[moltbook] configured {agent_id} → {_creds_path(agent_id)}")
