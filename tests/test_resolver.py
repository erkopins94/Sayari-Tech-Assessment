"""
tests/test_resolver.py — Unit tests for resolver.py pure helper functions.

All tests run fully offline — no Sayari API calls, no disk I/O.

Coverage:
    _unresolved      — 4 tests
    resolve_entity   — 3 tests (mocked SDK client)
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from resolver import _unresolved, resolve_entity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_match(entity_id="eid_001", label="Rosneft PJSC", match_strength="strong", score=0.95, type="company"):
    """Minimal stand-in for a Sayari resolution match record."""
    return SimpleNamespace(
        entity_id=entity_id,
        label=label,
        match_strength=match_strength,
        score=score,
        type=type,
    )


# ---------------------------------------------------------------------------
# _unresolved
# ---------------------------------------------------------------------------

class TestUnresolved:
    def test_resolved_is_false(self):
        result = _unresolved("Rustec", "RUS", "no_match")
        assert result["resolved"] is False

    def test_error_message_is_preserved(self):
        result = _unresolved("Rustec", "RUS", "network_timeout")
        assert result["error"] == "network_timeout"

    def test_nullable_fields_are_none(self):
        """entity_id, matched_name, match_strength, score, and entity_type must all be None."""
        result = _unresolved("Rustec", "RUS", "no_match")
        for field in ("entity_id", "matched_name", "match_strength", "score", "entity_type"):
            assert result[field] is None, f"expected {field} to be None, got {result[field]}"

    def test_input_fields_are_preserved(self):
        result = _unresolved("Rustec", "RUS", "no_match")
        assert result["input_name"] == "Rustec"
        assert result["input_country"] == "RUS"


# ---------------------------------------------------------------------------
# resolve_entity  (mocked SDK client)
# ---------------------------------------------------------------------------

class TestResolveEntity:
    def test_successful_resolution_returns_resolved_true(self):
        match = _make_match()
        response = MagicMock()
        response.data = [match]

        client = MagicMock()
        client.resolution.resolution.return_value = response

        result = resolve_entity(client, "Rosneft", "RUS")
        assert result["resolved"] is True
        assert result["entity_id"] == "eid_001"
        assert result["matched_name"] == "Rosneft PJSC"
        assert result["error"] is None

    def test_empty_data_list_returns_unresolved(self):
        """If the API returns no candidates, resolve_entity must return a failure record."""
        response = MagicMock()
        response.data = []

        client = MagicMock()
        client.resolution.resolution.return_value = response

        result = resolve_entity(client, "Rustec", "RUS")
        assert result["resolved"] is False
        assert result["error"] == "no_match"

    def test_api_exception_returns_unresolved(self):
        """Any SDK exception must be caught and returned as a structured failure — never raised."""
        client = MagicMock()
        client.resolution.resolution.side_effect = RuntimeError("503 Service Unavailable")

        result = resolve_entity(client, "Rosneft", "RUS")
        assert result["resolved"] is False
        assert "503" in result["error"]
