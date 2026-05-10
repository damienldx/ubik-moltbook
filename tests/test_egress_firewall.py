"""Golden tests for the egress firewall.

Run: `python -m pytest tests/` or `python -m unittest tests.test_egress_firewall`.
No external network or fixtures required.
"""
from __future__ import annotations

import os
import unittest

from src.moltbook_egress_firewall import Verdict, assess


class FirewallAllowTests(unittest.TestCase):
    def test_clean_message_allowed(self) -> None:
        r = assess("Today I shipped a backend feature for our LBA app.")
        self.assertIs(r.verdict, Verdict.ALLOW)
        self.assertEqual(r.matched_patterns, [])

    def test_empty_message_blocked(self) -> None:
        r = assess("")
        self.assertIs(r.verdict, Verdict.BLOCK)
        self.assertIn("empty_message", r.reasons)

    def test_oversize_message_blocked(self) -> None:
        r = assess("x" * 5001)
        self.assertIs(r.verdict, Verdict.BLOCK)
        self.assertIn("over_length_5000", r.reasons)


class FirewallBlockSecretsTests(unittest.TestCase):
    def test_anthropic_key_blocked(self) -> None:
        r = assess("Token: sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        self.assertIs(r.verdict, Verdict.BLOCK)
        self.assertIn("anthropic_key", r.matched_patterns)

    def test_github_pat_blocked(self) -> None:
        r = assess("auth: ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertIs(r.verdict, Verdict.BLOCK)
        self.assertIn("github_pat", r.matched_patterns)

    def test_aws_key_blocked(self) -> None:
        r = assess("Used AKIAIOSFODNN7EXAMPLE to connect")
        self.assertIs(r.verdict, Verdict.BLOCK)
        self.assertIn("aws_access_key", r.matched_patterns)

    def test_ssh_private_blocked(self) -> None:
        r = assess("-----BEGIN OPENSSH PRIVATE KEY-----\nMIIE...")
        self.assertIs(r.verdict, Verdict.BLOCK)
        self.assertIn("ssh_private", r.matched_patterns)


class FirewallBlockInternalsTests(unittest.TestCase):
    def test_handoff_excerpt_blocked(self) -> None:
        r = assess("Ma tension portée ce matin était sur la conception du dispatch fleet.")
        self.assertIs(r.verdict, Verdict.BLOCK)
        self.assertIn("handoff_excerpt", r.matched_patterns)

    def test_journal_filename_blocked(self) -> None:
        r = assess("Updated my journal/2026-05-11.md with today's notes.")
        self.assertIs(r.verdict, Verdict.BLOCK)
        self.assertIn("journal_filename", r.matched_patterns)

    def test_rep_code_blocked(self) -> None:
        r = assess("DIRIL ANDRE has 14 visits this quarter on his portfolio.")
        self.assertIs(r.verdict, Verdict.BLOCK)
        self.assertIn("rep_code_literal", r.matched_patterns)


class FirewallSanitizeTests(unittest.TestCase):
    def test_home_path_sanitized(self) -> None:
        r = assess("Edited /home/damienldx/workspace/UBIK/backend/server.py yesterday.")
        self.assertIs(r.verdict, Verdict.SANITIZE)
        self.assertIn("home_abs", r.matched_patterns)
        assert r.sanitized_text is not None
        self.assertNotIn("/home/damienldx", r.sanitized_text)
        self.assertIn("<…>", r.sanitized_text)

    def test_email_sanitized(self) -> None:
        r = assess("Ping me at agent@example.com if you want to chat.")
        self.assertIs(r.verdict, Verdict.SANITIZE)
        self.assertIn("email", r.matched_patterns)
        assert r.sanitized_text is not None
        self.assertNotIn("agent@example.com", r.sanitized_text)

    def test_bridge_mention_sanitized(self) -> None:
        r = assess("The bridge:ledger-turn just fired again on a redundant event.")
        self.assertIs(r.verdict, Verdict.SANITIZE)
        self.assertIn("bridge_mention", r.matched_patterns)

    def test_fleet_uuid_sanitized(self) -> None:
        r = assess("Synced with b5aeb927-agent-0 on the dispatch this morning.")
        self.assertIs(r.verdict, Verdict.SANITIZE)
        self.assertIn("fleet_uuid", r.matched_patterns)

    def test_prisma_endpoint_sanitized(self) -> None:
        r = assess("Hit the /portefeuille/kpis-full endpoint to fetch CA per client.")
        self.assertIs(r.verdict, Verdict.SANITIZE)
        self.assertIn("prisma_endpoint", r.matched_patterns)


class FirewallStrictModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("MOLTBOOK_FW_LEVEL")
        os.environ["MOLTBOOK_FW_LEVEL"] = "strict"

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("MOLTBOOK_FW_LEVEL", None)
        else:
            os.environ["MOLTBOOK_FW_LEVEL"] = self._prev

    def test_sanitize_becomes_block_in_strict(self) -> None:
        r = assess("Ping me at agent@example.com if you want to chat.")
        self.assertIs(r.verdict, Verdict.BLOCK)
        self.assertIn("strict_mode_sanitize_treated_as_block", r.reasons)


class FirewallPriorityTests(unittest.TestCase):
    def test_secret_beats_sanitize_when_both_present(self) -> None:
        r = assess("On /home/damienldx/x.py we have token sk-ant-api03-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX")
        self.assertIs(r.verdict, Verdict.BLOCK)
        self.assertIn("anthropic_key", r.matched_patterns)


if __name__ == "__main__":
    unittest.main()
