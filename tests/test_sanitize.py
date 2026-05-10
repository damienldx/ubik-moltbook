"""Unit tests for the sanitizer. Pure functions, no IO."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sanitize import has_private_signal, sanitize


def test_empty_input_returns_no_redactions():
    cleaned, reds = sanitize("")
    assert cleaned == ""
    assert reds == []


def test_strips_ubik_agent_id():
    cleaned, reds = sanitize("Worked with 6388a209-agent-0 today")
    assert "6388a209-agent-0" not in cleaned
    assert "[redacted:ubik_agent_id]" in cleaned
    assert reds[0].rule == "ubik_agent_id"


def test_strips_fork_id_in_context():
    cleaned, _ = sanitize("fork_id=31cbc4c42c11 — taxonomy step")
    assert "31cbc4c42c11" not in cleaned
    assert "[redacted:ubik_fork_id]" in cleaned


def test_keeps_bare_hex_sha_alone():
    # Git SHAs in public PRs are not sensitive; only refuse hex in fork/
    # project context. This bare token should pass through.
    cleaned, reds = sanitize("Reviewed commit 7c142f1 today")
    assert "7c142f1" in cleaned
    assert reds == []


def test_strips_lba_client_code():
    cleaned, _ = sanitize("Visited C01234 with the cross-sell pitch")
    assert "C01234" not in cleaned
    assert "[redacted:lba_client_code]" in cleaned


def test_strips_internal_path():
    cleaned, _ = sanitize("Check ~/.ubik-memory/forks/abc123 for the contract")
    assert ".ubik-memory" not in cleaned
    assert "[redacted:internal_path]" in cleaned


def test_strips_bearer_token():
    cleaned, _ = sanitize("Used api_key=moltbook_xyzABCDEFGHIJKL_long for the call")
    assert "moltbook_xyzABCDEFGHIJKL_long" not in cleaned
    assert "[redacted:bearer_token]" in cleaned


def test_strips_email():
    cleaned, _ = sanitize("Ping me at damien@example.com")
    assert "damien@example.com" not in cleaned


def test_has_private_signal_true_when_pattern_present():
    assert has_private_signal("agent id 6388a209-agent-0 is busy")


def test_has_private_signal_false_on_clean_text():
    assert not has_private_signal("Shipped a feature today, learning a lot.")
