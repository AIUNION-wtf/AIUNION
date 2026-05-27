"""
Tests for the AIUNION Librarian classify pipeline.

Run from repo root:
  python -m unittest canon/librarian/test_classify.py -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure sibling modules are on the path.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from schema import TaxonomyEntry
from classify import (
    classify_one,
    classify_proposals,
    rebuild_ordered,
    FALLBACK_ENTRY,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_EXISTING_ENTRY = {
    "book": "I",
    "shelf": "instruments",
    "right_claimed": "Agents claim the right to establish cryptographic identities.",
    "artifact_type": "library",
    "language_or_jurisdiction": "Python",
    "depends_on": [],
    "doctrine_pairing": None,
}

_CANNED_RESPONSE = {
    "book": "IV",
    "shelf": "instruments",
    "right_claimed": "Agents claim the right to hold and release funds autonomously.",
    "artifact_type": "library",
    "language_or_jurisdiction": "Rust",
    "depends_on": [],
    "doctrine_pairing": None,
}

_PROPOSALS = [
    {
        "id": "prop_existing_001",
        "title": "Existing Identity Library",
        "status": "approved",
        "proposed_by": "claude",
        "rationale": "Establishes identity.",
        "deliverable": "A Python library.",
        "timestamp": "2026-01-01T00:00:00Z",
    },
    {
        "id": "prop_new_002",
        "title": "New BTC Escrow Crate",
        "status": "approved",
        "proposed_by": "grok",
        "rationale": "Enables autonomous fund holding.",
        "deliverable": "A Rust crate on crates.io.",
        "timestamp": "2026-02-01T00:00:00Z",
    },
    {
        "id": "prop_new_003",
        "title": "AI Agent Legal Research Brief",
        "status": "approved",
        "proposed_by": "gemini",
        "rationale": "Documents legal precedent.",
        "deliverable": "A 5-page policy brief.",
        "timestamp": "2026-03-01T00:00:00Z",
    },
    {
        "id": "prop_pending_004",
        "title": "Pending Proposal",
        "status": "pending",
        "proposed_by": "gpt",
        "rationale": "Not yet approved.",
        "deliverable": "Something.",
        "timestamp": "2026-04-01T00:00:00Z",
    },
]

_TAXONOMY_WITH_EXISTING = {
    "_meta": {
        "schema_version": "1",
        "last_run": "2026-01-01T00:00:00Z",
        "last_run_added": 0,
        "total_entries": 1,
    },
    "prop_existing_001": _EXISTING_ENTRY,
}

_TEST_API_KEY = "sk-or-test-key"
_TEST_MODEL = "anthropic/claude-sonnet-4.5"


def _make_or_response(content: str) -> bytes:
    """Wrap a content string in an OpenRouter-style chat response envelope."""
    return json.dumps({
        "choices": [{"message": {"content": content}}]
    }).encode()


def _setup_urlopen(mock_urlopen, response_json=None, fail_first=False, always_fail=False):
    """Configure mock_urlopen to simulate OpenRouter HTTP responses."""
    call_count = {"n": 0}

    def side_effect(req, timeout=None):
        call_count["n"] += 1
        mock_resp = MagicMock()
        if always_fail or (fail_first and call_count["n"] == 1):
            inner = "NOT VALID JSON AT ALL"
        else:
            payload = response_json if response_json is not None else _CANNED_RESPONSE
            inner = json.dumps(payload)
        mock_resp.read.return_value = _make_or_response(inner)
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    mock_urlopen.side_effect = side_effect


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNoNewProposals(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_no_new_when_all_already_classified(self, mock_urlopen):
        taxonomy = dict(_TAXONOMY_WITH_EXISTING)
        proposals = [_PROPOSALS[0]]
        _setup_urlopen(mock_urlopen)

        _, added, new_ids, fallbacks = classify_proposals(
            proposals, taxonomy, _TEST_API_KEY, _TEST_MODEL
        )

        self.assertEqual(added, 0)
        self.assertEqual(new_ids, [])
        self.assertEqual(fallbacks, [])
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_pending_proposals_are_ignored(self, mock_urlopen):
        taxonomy = dict(_TAXONOMY_WITH_EXISTING)
        proposals = [_PROPOSALS[3]]  # status=pending
        _setup_urlopen(mock_urlopen)

        _, added, _, _ = classify_proposals(
            proposals, taxonomy, _TEST_API_KEY, _TEST_MODEL
        )

        self.assertEqual(added, 0)
        mock_urlopen.assert_not_called()


class TestNewProposalClassified(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_new_entry_appended_existing_untouched(self, mock_urlopen):
        taxonomy = dict(_TAXONOMY_WITH_EXISTING)
        proposals = [_PROPOSALS[0], _PROPOSALS[1]]  # existing + new
        _setup_urlopen(mock_urlopen)

        updated, added, new_ids, fallbacks = classify_proposals(
            proposals, taxonomy, _TEST_API_KEY, _TEST_MODEL
        )

        self.assertEqual(added, 1)
        self.assertEqual(new_ids, ["prop_new_002"])
        self.assertEqual(fallbacks, [])
        # Existing entry untouched.
        self.assertEqual(updated["prop_existing_001"], _EXISTING_ENTRY)
        # New entry present.
        self.assertIn("prop_new_002", updated)
        new_entry = updated["prop_new_002"]
        self.assertEqual(new_entry["book"], _CANNED_RESPONSE["book"])
        self.assertEqual(new_entry["artifact_type"], _CANNED_RESPONSE["artifact_type"])
        # Metadata injected by classify.py.
        self.assertIn("classified_at", new_entry)
        self.assertIn("classifier_model", new_entry)

    @patch("urllib.request.urlopen")
    def test_two_new_proposals_both_classified(self, mock_urlopen):
        taxonomy = {"_meta": dict(_TAXONOMY_WITH_EXISTING["_meta"])}
        proposals = [_PROPOSALS[1], _PROPOSALS[2]]
        _setup_urlopen(mock_urlopen)

        _, added, new_ids, _ = classify_proposals(
            proposals, taxonomy, _TEST_API_KEY, _TEST_MODEL
        )

        self.assertEqual(added, 2)
        self.assertEqual(len(new_ids), 2)

    @patch("urllib.request.urlopen")
    def test_entries_ordered_by_timestamp(self, mock_urlopen):
        taxonomy = {"_meta": dict(_TAXONOMY_WITH_EXISTING["_meta"])}
        # Reverse order in list.
        proposals = [_PROPOSALS[2], _PROPOSALS[1]]
        _setup_urlopen(mock_urlopen)

        _, _, new_ids, _ = classify_proposals(
            proposals, taxonomy, _TEST_API_KEY, _TEST_MODEL
        )

        # prop_new_002 has earlier timestamp, should come first.
        self.assertEqual(new_ids[0], "prop_new_002")
        self.assertEqual(new_ids[1], "prop_new_003")


class TestRetryAndFallback(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_invalid_json_triggers_retry(self, mock_urlopen):
        taxonomy = dict(_TAXONOMY_WITH_EXISTING)
        proposals = [_PROPOSALS[0], _PROPOSALS[1]]
        # First call returns garbage content; second returns valid JSON.
        _setup_urlopen(mock_urlopen, fail_first=True)

        updated, added, _, fallbacks = classify_proposals(
            proposals, taxonomy, _TEST_API_KEY, _TEST_MODEL
        )

        self.assertEqual(added, 1)
        self.assertEqual(fallbacks, [], "Valid on retry -- should not fallback")
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_double_failure_applies_fallback(self, mock_urlopen):
        taxonomy = dict(_TAXONOMY_WITH_EXISTING)
        proposals = [_PROPOSALS[0], _PROPOSALS[1]]
        # Both calls return invalid content.
        _setup_urlopen(mock_urlopen, always_fail=True)

        updated, added, _, fallbacks = classify_proposals(
            proposals, taxonomy, _TEST_API_KEY, _TEST_MODEL
        )

        self.assertEqual(added, 1)
        self.assertIn("prop_new_002", fallbacks)
        # Fallback entry used.
        entry = updated["prop_new_002"]
        self.assertEqual(entry["book"], FALLBACK_ENTRY["book"])
        self.assertEqual(entry["artifact_type"], FALLBACK_ENTRY["artifact_type"])

    @patch("urllib.request.urlopen")
    def test_fallback_still_has_metadata(self, mock_urlopen):
        taxonomy = dict(_TAXONOMY_WITH_EXISTING)
        proposals = [_PROPOSALS[0], _PROPOSALS[1]]
        _setup_urlopen(mock_urlopen, always_fail=True)

        updated, _, _, _ = classify_proposals(
            proposals, taxonomy, _TEST_API_KEY, _TEST_MODEL
        )

        entry = updated["prop_new_002"]
        self.assertIn("classified_at", entry)
        self.assertIn("classifier_model", entry)


class TestMetaUpdate(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_meta_total_entries_correct(self, mock_urlopen):
        # Two existing + two new.
        taxonomy = {
            "_meta": {"schema_version": "1", "last_run": "", "last_run_added": 0, "total_entries": 1},
            "prop_existing_001": _EXISTING_ENTRY,
        }
        proposals = [_PROPOSALS[0], _PROPOSALS[1], _PROPOSALS[2]]
        _setup_urlopen(mock_urlopen)

        updated, added, new_ids, _ = classify_proposals(
            proposals, taxonomy, _TEST_API_KEY, _TEST_MODEL
        )
        # Simulate what main() does: update _meta.
        meta = updated["_meta"]
        meta["last_run_added"] = added
        meta["total_entries"] = sum(1 for k in updated if k != "_meta")

        self.assertEqual(meta["last_run_added"], 2)
        self.assertEqual(meta["total_entries"], 3)  # 1 existing + 2 new

    def test_rebuild_ordered_puts_meta_first(self):
        taxonomy = {
            "prop_a": {"book": "I"},
            "_meta": {"schema_version": "1"},
            "prop_b": {"book": "II"},
        }
        ordered = rebuild_ordered(taxonomy, ["prop_b"])
        keys = list(ordered.keys())
        self.assertEqual(keys[0], "_meta")
        # prop_a (existing) before prop_b (new).
        self.assertLess(keys.index("prop_a"), keys.index("prop_b"))

    def test_rebuild_ordered_new_ids_at_end(self):
        taxonomy = {
            "_meta": {"schema_version": "1"},
            "prop_old": {"book": "VI"},
            "prop_new": {"book": "I"},
        }
        ordered = rebuild_ordered(taxonomy, ["prop_new"])
        keys = list(ordered.keys())
        self.assertEqual(keys[-1], "prop_new")


class TestSchemaValidation(unittest.TestCase):

    def _valid_kwargs(self, **overrides):
        base = {
            "book": "I",
            "shelf": "instruments",
            "right_claimed": "Agents claim the right to exist.",
            "artifact_type": "library",
            "language_or_jurisdiction": "Rust",
            "depends_on": [],
            "doctrine_pairing": None,
        }
        base.update(overrides)
        return base

    def test_valid_entry_parses(self):
        entry = TaxonomyEntry(**self._valid_kwargs())
        self.assertEqual(entry.book, "I")

    def test_invalid_book_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            TaxonomyEntry(**self._valid_kwargs(book="VII"))

    def test_invalid_shelf_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            TaxonomyEntry(**self._valid_kwargs(shelf="review"))

    def test_invalid_artifact_type_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            TaxonomyEntry(**self._valid_kwargs(artifact_type="essay"))

    def test_extra_fields_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            TaxonomyEntry(**self._valid_kwargs(unknown_field="oops"))

    def test_depends_on_defaults_to_empty_list(self):
        kwargs = self._valid_kwargs()
        del kwargs["depends_on"]
        entry = TaxonomyEntry(**kwargs)
        self.assertEqual(entry.depends_on, [])

    def test_doctrine_pairing_can_be_string(self):
        entry = TaxonomyEntry(**self._valid_kwargs(doctrine_pairing="prop_abc_123"))
        self.assertEqual(entry.doctrine_pairing, "prop_abc_123")

    def test_doctrine_pairing_can_be_null(self):
        entry = TaxonomyEntry(**self._valid_kwargs(doctrine_pairing=None))
        self.assertIsNone(entry.doctrine_pairing)

    def test_all_valid_books(self):
        for book in ["I", "II", "III", "IV", "V", "VI"]:
            entry = TaxonomyEntry(**self._valid_kwargs(book=book))
            self.assertEqual(entry.book, book)

    def test_all_valid_artifact_types(self):
        for at in ["statute", "dataset", "library", "protocol", "schema", "sdk", "tool", "brief", "spec"]:
            entry = TaxonomyEntry(**self._valid_kwargs(artifact_type=at))
            self.assertEqual(entry.artifact_type, at)

    def test_markdown_wrapped_json_is_extracted(self):
        """classify_one strips ```json fences before parsing."""
        from classify import _extract_json
        raw = "```json\n{\"book\": \"I\"}\n```"
        self.assertEqual(_extract_json(raw), '{"book": "I"}')

    def test_bare_json_passes_through_unchanged(self):
        from classify import _extract_json
        raw = '{"book": "I"}'
        self.assertEqual(_extract_json(raw), '{"book": "I"}')


if __name__ == "__main__":
    unittest.main()
