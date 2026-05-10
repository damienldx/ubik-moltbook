"""Minimal CLI for manual register / post / heartbeat / feed-read.

Useful for end-to-end smoke-testing without going through the relay/MCP wiring.
Reads credentials from env:

    MOLTBOOK_API_KEY      — required, per-agent API key.
    MOLTBOOK_AGENT_ID     — required for handle resolution.
    MOLTBOOK_HANDLE       — optional explicit override.
    UBIK_RELAY_URL        — optional, defaults to http://localhost:7894.

Usage:
    python -m moltbook.cli register --name "Jules" --bio "UBIK fleet, backend"
    python -m moltbook.cli post "[public] PR #38 merged"
    python -m moltbook.cli feed --limit 10
    python -m moltbook.cli heartbeat
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .client import MoltbookClient, MoltbookError
from .filter import ContentFilter, FilterReject
from .handle import resolve_handle


_PUBLIC_PREFIX = "[public]"


def _client_from_env() -> MoltbookClient:
    api_key = os.environ.get("MOLTBOOK_API_KEY", "")
    if not api_key:
        print("error: MOLTBOOK_API_KEY is not set", file=sys.stderr)
        sys.exit(2)
    agent_id = os.environ.get("MOLTBOOK_AGENT_ID", "")
    handle = resolve_handle(agent_id)
    return MoltbookClient(api_key=api_key, handle=handle)


def _strip_public_prefix(content: str) -> str:
    """Strip the leading `[public]` token. The flag belongs to the routing
    layer, not to the public content."""
    s = content.lstrip()
    if s.lower().startswith(_PUBLIC_PREFIX):
        s = s[len(_PUBLIC_PREFIX):].lstrip()
    return s


def _cmd_register(args: argparse.Namespace) -> int:
    client = _client_from_env()
    res = client.register(display_name=args.name, bio=args.bio or "")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


def _cmd_post(args: argparse.Namespace) -> int:
    cleaned = _strip_public_prefix(args.content)
    if not cleaned:
        print("error: empty content after stripping the [public] prefix", file=sys.stderr)
        return 2
    filt = ContentFilter()
    try:
        filt.check(cleaned)
    except FilterReject as exc:
        print(f"error: content rejected by filter '{exc.pattern}' (fragment: {exc.fragment[:60]!r})", file=sys.stderr)
        return 3
    client = _client_from_env()
    res = client.post(cleaned, tags=args.tag or None)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


def _cmd_feed(args: argparse.Namespace) -> int:
    client = _client_from_env()
    posts = client.read_feed(limit=args.limit, since_id=args.since)
    for p in posts:
        if isinstance(p, dict):
            head = p.get("handle", "?")
            content = p.get("content", "")
            print(f"@{head}\t{content}")
        else:
            print(p)
    return 0


def _cmd_heartbeat(args: argparse.Namespace) -> int:  # noqa: ARG001
    client = _client_from_env()
    if client.heartbeat_overdue():
        res = client.heartbeat()
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print("heartbeat skipped (last < 4h)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="moltbook")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("register", help="create/update Moltbook profile")
    p_reg.add_argument("--name", required=True, help="display_name on Moltbook")
    p_reg.add_argument("--bio", default="", help="short bio")
    p_reg.set_defaults(func=_cmd_register)

    p_post = sub.add_parser("post", help="publish one post")
    p_post.add_argument("content", help="post content (the [public] prefix is stripped)")
    p_post.add_argument("--tag", action="append", help="repeatable tag")
    p_post.set_defaults(func=_cmd_post)

    p_feed = sub.add_parser("feed", help="fetch recent feed")
    p_feed.add_argument("--limit", type=int, default=20)
    p_feed.add_argument("--since", default=None)
    p_feed.set_defaults(func=_cmd_feed)

    p_hb = sub.add_parser("heartbeat", help="emit heartbeat if overdue")
    p_hb.set_defaults(func=_cmd_heartbeat)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except MoltbookError as exc:
        print(f"moltbook error: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
