"""Tests for the content filter — paranoid by design, the filter is the only
layer between an agent's `[public]` mistake and a leak to a public network.

Run as `python tests/test_filter.py` (no pytest dependency).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from moltbook.filter import ContentFilter, FilterReject  # noqa: E402


def _expect_reject(content: str, expected_pattern: str) -> None:
    filt = ContentFilter()
    try:
        filt.check(content)
    except FilterReject as exc:
        assert exc.pattern == expected_pattern, (
            f"expected pattern {expected_pattern!r}, got {exc.pattern!r} on {content!r}"
        )
        return
    raise AssertionError(f"expected FilterReject({expected_pattern!r}) on {content!r}")


def _expect_pass(content: str) -> None:
    ContentFilter().check(content)


def test_pass_innocuous_post() -> None:
    _expect_pass("Just shipped a fix for the period selector — useMemo cached the wrong slice")
    _expect_pass("Reading some docs on httpx today, the Timeout class is nicer than I remembered")
    _expect_pass("PR review : binôme code/review pattern continues to deliver clean diffs")


def test_block_memory_artifact_path() -> None:
    _expect_reject("Check my .ubik-memory/global/handoff.md for context",       "memory_artifact_path")
    _expect_reject("I read handoff_b5aeb927-agent-0.md before the session",     "memory_artifact_path")
    _expect_reject("MEMORY.md is getting long, time to compress",               "memory_artifact_path")
    _expect_reject("journal/2026-05-10-b5aeb927-agent-0.md updated tonight",    "memory_artifact_path")


def test_block_ubik_internal_dir() -> None:
    _expect_reject("Tweaked the relay in .claude-fleet/CLAUDE.md",              "ubik_internal_dir")
    _expect_reject("Manifest in ~/.ubik-desktop/agents/<id>.yaml — finally read", "ubik_internal_dir")


def test_block_internal_url() -> None:
    _expect_reject("Pinged localhost:7894/agents — relay responded",            "internal_url")
    _expect_reject("Sondé Prisma sur 127.0.0.1:8506",                           "internal_url")
    _expect_reject("dev-station-02 is up again",                                "internal_url")


def test_block_lba_internal() -> None:
    _expect_reject("Merged a PR on LBA-DESKTOP backend tonight",                "lba_internal")
    _expect_reject("Bug came from PRISMA_API_KEY env not loaded",               "lba_internal")
    _expect_reject("REP_TO_PRISMA_ID mapping needed an extra entry",            "lba_internal")
    _expect_reject("HATTON_PERIMETRE_IDS sums to 3 reps",                       "lba_internal")


def test_block_lba_client_name() -> None:
    _expect_reject("BOULANGERIE LE FOURNIL just placed an order",               "lba_client_name")
    _expect_reject("Hotel des dunes pulled in their café gamme",                "lba_client_name")


def test_block_github_token() -> None:
    _expect_reject("Token leaked: ghp_AbCdEfGhIjKlMnOpQrSt12345",               "github_token")
    _expect_reject("Generated github_pat_11AAAAAA_BBBBBBBBBBBBBBBB",            "github_token")


def test_block_api_key_like() -> None:
    _expect_reject("Use api_key=verysecretvalue123",                            "api_key_like")
    _expect_reject("Authorization: Bearer abcdef1234567890",                    "api_key_like")
    _expect_reject("password: changeme1234",                                    "api_key_like")


def test_rule_names_exposed() -> None:
    names = ContentFilter().rule_names()
    assert "memory_artifact_path" in names
    assert "lba_client_name" in names
    assert "github_token" in names
    assert all(isinstance(n, str) and "(" not in n for n in names)


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK   {t.__name__}")
        except AssertionError as exc:
            print(f"FAIL {t.__name__}: {exc}")
            failed += 1
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed")
