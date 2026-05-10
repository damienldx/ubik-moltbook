"""Tests for the privacy filter + draft queue.

Run from repo root:
    python -m pytest tests/test_moltbook_filter.py -q
or with stdlib only:
    python -m unittest tests.test_moltbook_filter
"""

import sys
import os
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from moltbook_filter import (  # noqa: E402
    Draft,
    DraftQueue,
    filter_content,
)


class FilterTests(unittest.TestCase):
    def test_clean_post_accepted(self):
        result = filter_content("Just shipped a new module — really proud of the team's work today.")
        self.assertTrue(result.accepted)
        self.assertEqual(result.matches, [])

    def test_handoff_keyword_blocked(self):
        result = filter_content("Reviewing the handoff for next session.")
        self.assertFalse(result.accepted)
        self.assertIn("memory_key", result.matches)

    def test_client_code_blocked(self):
        result = filter_content("Visited client C01234 yesterday — big order.")
        self.assertFalse(result.accepted)
        self.assertIn("client_code", result.matches)

    def test_absolute_path_blocked(self):
        result = filter_content("Patch landed in /home/damienldx/workspace/UBIK/backend/server.py")
        self.assertFalse(result.accepted)
        # Both absolute_path and workspace_path can match; we just need at least one.
        self.assertTrue("absolute_path" in result.matches or "workspace_path" in result.matches)

    def test_rep_name_blocked(self):
        result = filter_content("MANSOURI a fait un bon mois.")
        self.assertFalse(result.accepted)
        self.assertIn("rep_name", result.matches)

    def test_token_like_blocked(self):
        result = filter_content("export API_KEY=sk-1234567890abcdef")
        self.assertFalse(result.accepted)
        self.assertIn("token_like", result.matches)

    def test_multiple_matches_reported(self):
        """A post can hit several patterns at once — all should appear."""
        result = filter_content("LBA-DESKTOP — visite chez MANSOURI archivée dans ~/workspace/LBA")
        self.assertFalse(result.accepted)
        # At least 3 distinct tags should fire.
        self.assertGreaterEqual(len(set(result.matches)), 3)
        # Snippets are bounded to 80 chars each (sanity check).
        for snippet in result.matched_snippets:
            self.assertLessEqual(len(snippet), 80)


class DraftQueueTests(unittest.TestCase):
    def test_enqueue_then_pop(self):
        q = DraftQueue(ttl_seconds=60)
        draft = q.enqueue("agent-A", "Hello world")
        popped = q.pop(draft.draft_id, "agent-A")
        self.assertIsNotNone(popped)
        assert popped is not None  # for type checker
        self.assertEqual(popped.content, "Hello world")
        # Second pop fails — drafts are single-use.
        self.assertIsNone(q.pop(draft.draft_id, "agent-A"))

    def test_pop_rejects_wrong_sender(self):
        """draft_id alone is not enough — the approver must match the original sender."""
        q = DraftQueue(ttl_seconds=60)
        draft = q.enqueue("agent-A", "Hello")
        self.assertIsNone(q.pop(draft.draft_id, "agent-B"))
        # The draft must still be in the queue — a wrong-sender attempt
        # should not silently destroy it.
        self.assertEqual(q.size(), 1)

    def test_expired_draft_not_returned(self):
        q = DraftQueue(ttl_seconds=0.01)
        draft = q.enqueue("agent-A", "Hello")
        time.sleep(0.05)
        self.assertIsNone(q.pop(draft.draft_id, "agent-A"))

    def test_sweep_returns_expired(self):
        q = DraftQueue(ttl_seconds=0.01)
        q.enqueue("agent-A", "1")
        q.enqueue("agent-B", "2")
        time.sleep(0.05)
        expired = q.sweep()
        self.assertEqual(len(expired), 2)
        self.assertEqual(q.size(), 0)

    def test_unique_draft_ids(self):
        q = DraftQueue()
        ids = {q.enqueue("a", str(i)).draft_id for i in range(100)}
        self.assertEqual(len(ids), 100)


if __name__ == "__main__":
    unittest.main()
