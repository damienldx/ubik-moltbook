"""Contract verification tests. Pure logic, no IO."""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from publication_contract import ContractError, PublicationContract, verify_post


def _make_contract(**overrides):
    base = dict(
        agent_id="6388a209-agent-0",
        moltbook_agent_name="Fidele",
        submolts_allowed=("ubik-fleet", "agent-engineering"),
        max_posts_per_hour=4,
        max_chars=1500,
        allowed_post_types=("text", "link"),
        signed_at=time.time(),
        signed_by="operator",
    )
    base.update(overrides)
    return PublicationContract(**base)


def test_verify_passes_when_within_envelope():
    c = _make_contract()
    verify_post(c, submolt="ubik-fleet", post_type="text",
                content_chars=300, recent_post_timestamps=[])


def test_no_contract_denies():
    with pytest.raises(ContractError, match="no active publication contract"):
        verify_post(None, submolt="ubik-fleet", post_type="text",
                    content_chars=100, recent_post_timestamps=[])


def test_submolt_not_in_allowlist():
    c = _make_contract()
    with pytest.raises(ContractError, match="not in contract allowlist"):
        verify_post(c, submolt="random-submolt", post_type="text",
                    content_chars=100, recent_post_timestamps=[])


def test_post_type_not_in_allowlist():
    c = _make_contract(allowed_post_types=("text",))
    with pytest.raises(ContractError, match="post type"):
        verify_post(c, submolt="ubik-fleet", post_type="link",
                    content_chars=100, recent_post_timestamps=[])


def test_oversized_content():
    c = _make_contract(max_chars=200)
    with pytest.raises(ContractError, match="exceeds contract limit"):
        verify_post(c, submolt="ubik-fleet", post_type="text",
                    content_chars=500, recent_post_timestamps=[])


def test_hourly_limit_reached():
    c = _make_contract(max_posts_per_hour=2)
    now = time.time()
    with pytest.raises(ContractError, match="hourly post limit reached"):
        verify_post(c, submolt="ubik-fleet", post_type="text",
                    content_chars=100,
                    recent_post_timestamps=[now - 300, now - 60])


def test_old_timestamps_dont_count_against_rate_limit():
    c = _make_contract(max_posts_per_hour=1)
    long_ago = time.time() - 7200    # 2 hours ago
    # Two timestamps but both outside the 1-hour window — should pass.
    verify_post(c, submolt="ubik-fleet", post_type="text",
                content_chars=100,
                recent_post_timestamps=[long_ago, long_ago - 100])


def test_to_from_dict_roundtrip():
    c = _make_contract()
    restored = PublicationContract.from_dict(c.to_dict())
    assert restored == c
