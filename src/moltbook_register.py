"""One-shot registration script.

Run once per fleet slot to obtain an API key from Moltbook and persist it
under `~/.ubik-memory/moltbook/<agent_id>.key`. The méca refuses to start
until this has been done.

Usage:
    python -m src.moltbook_register <agent_id> "<label>"
    python -m src.moltbook_register albert "Albert — Reviewer Backend"

Re-running register for an existing agent overwrites the key (recovery path
when Moltbook rotates).
"""
from __future__ import annotations

import sys

from .moltbook_client import DEFAULT_BASE_URL, MoltbookClient, save_api_key


def _usage() -> None:
    print("usage: python -m src.moltbook_register <agent_id> <label>", file=sys.stderr)
    print("  agent_id: fleet slot id, e.g. b5aeb927-agent-1", file=sys.stderr)
    print("  label:    human-readable name, e.g. 'Albert — Reviewer Backend'", file=sys.stderr)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        _usage()
        return 2

    agent_id = argv[1].strip()
    label = " ".join(argv[2:]).strip()
    if not agent_id or not label:
        _usage()
        return 2

    client = MoltbookClient(agent_id=agent_id, base_url=DEFAULT_BASE_URL)
    print(f"→ registering {agent_id!r} as {label!r} on {DEFAULT_BASE_URL}")
    key = client.register(label)
    save_api_key(agent_id, key)
    print(f"✓ api_key saved for {agent_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
