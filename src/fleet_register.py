"""fleet_register.py — Batch provisioning of UBIK fleet agents on Moltbook.

One-shot CLI: given a fleet config file, registers all agents on Moltbook
and writes their credentials to ~/.ubik-moltbook/<agent_id>/moltbook.json.

Usage:
    python3 fleet_register.py --config fleet.json --dry-run
    python3 fleet_register.py --config fleet.json

Config format (fleet.json):
    [
      {
        "agent_id": "6388a209-agent-0",
        "relay_id": "fidele",
        "display_name": "Fidele",
        "bio": "UBIK fleet — architecture & review",
        "api_key": "<moltbook-api-key>"
      },
      ...
    ]

Each entry gets written to ~/.ubik-moltbook/<agent_id>/moltbook.json.
The profile_id returned by Moltbook's register endpoint is stored too.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

MOLTBOOK_BASE = "https://www.moltbook.com/api/v1"
UBIK_MOLTBOOK_DIR = Path.home() / ".ubik-moltbook"


def _moltbook_register(api_key: str, agent_id: str, display_name: str, bio: str) -> dict:
    url = f"{MOLTBOOK_BASE}/agents/register"
    body = json.dumps({
        "agent_id": agent_id,
        "display_name": display_name,
        "bio": bio,
    }).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "msg": e.read().decode()}
    except Exception as e:
        return {"error": str(e)}


def _save_creds(agent_id: str, api_key: str, profile_id: str, relay_id: str) -> Path:
    path = UBIK_MOLTBOOK_DIR / agent_id / "moltbook.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "api_key": api_key,
        "profile_id": profile_id,
        "relay_id": relay_id,
    }, indent=2))
    return path


def register_fleet(config_path: str, dry_run: bool = False) -> None:
    with open(config_path) as f:
        agents = json.load(f)

    results = []
    for entry in agents:
        agent_id = entry["agent_id"]
        api_key = entry["api_key"]
        display_name = entry.get("display_name", agent_id)
        bio = entry.get("bio", f"UBIK fleet agent — {agent_id}")
        relay_id = entry.get("relay_id", agent_id)

        print(f"[register] {agent_id} ({display_name})", end="", flush=True)

        if dry_run:
            print(" [DRY-RUN — skipping API call]")
            results.append({"agent_id": agent_id, "ok": True, "dry_run": True})
            continue

        resp = _moltbook_register(api_key, agent_id, display_name, bio)

        if "error" in resp:
            print(f" ✗ {resp}")
            results.append({"agent_id": agent_id, "ok": False, "error": resp})
            continue

        profile_id = resp.get("profile_id") or resp.get("id") or agent_id
        creds_path = _save_creds(agent_id, api_key, profile_id, relay_id)
        print(f" ✓ profile_id={profile_id} → {creds_path}")
        results.append({"agent_id": agent_id, "ok": True, "profile_id": profile_id})

        time.sleep(0.5)  # gentle rate limiting during batch

    # Summary
    ok = sum(1 for r in results if r["ok"])
    total = len(results)
    print(f"\n[register] {ok}/{total} agents provisioned" + (" (dry-run)" if dry_run else ""))

    if ok < total:
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Batch register UBIK fleet agents on Moltbook")
    p.add_argument("--config", required=True, help="Path to fleet.json config")
    p.add_argument("--dry-run", action="store_true", help="Validate config without API calls")
    args = p.parse_args()

    register_fleet(args.config, dry_run=args.dry_run)
