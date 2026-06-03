"""
tests/test_resolver.py — Unit tests for resolver.py pure helper functions.

All tests run fully offline — no Sayari API calls, no disk I/O.

Coverage:
    _unresolved           — 5 tests
    _strength_str         — 3 tests
    _classify_confidence  — 4 tests
    _candidate            — 2 tests
    apply_review_decision — 5 tests
    resolve_entity        — 6 tests (mocked SDK client)
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from resolver import (
    REVIEW_SCORE_THRESHOLD,
    _candidate,
    _classify_confidence,
    _strength_str,
    _unresolved,
    apply_review_decision,
    resolve_entity,
)

# A score comfortably above / below the review threshold, derived from the
# constant so these tests stay correct if the threshold is ever retuned.
HIGH_SCORE = REVIEW_SCORE_THRESHOLD + 50
LOW_SCORE = REVIEW_SCORE_THRESHOLD - 50


# ---------------------------------------------------------------------------
# Helpers — stand-ins for Sayari SDK objects
# ---------------------------------------------------------------------------

def _make_match(
    entity_id="eid_001",
    label="Rosneft PJSC",
    match_strength="strong",
    score=HIGH_SCORE,
    type="company",
    countries=None,
):
    """Minimal stand-in for a Sayari ResolutionResult record."""
    return SimpleNamespace(
        entity_id=entity_id,
        label=label,
        match_strength=match_strength,
        score=score,
        type=type,
        countries=list(countries or ["RUS"]),
    )


def _make_candidate(entity_id="cand_1", matched_name="Candidate Corp", score=HIGH_SCORE):
    """A plain candidate dict as stored in record['candidates']."""
    return {
        "entity_id": entity_id,
        "matched_name": matched_name,
        "score": score,
        "match_strength": "strong",
        "entity_type": "company",
        "countries": ["RUS"],
    }


# ---------------------------------------------------------------------------
# _unresolved
# ---------------------------------------------------------------------------

class TestUnresolved:
    def test_resolved_is_false(self):
        assert _unresolved("Rustec", "RUS", "no_match")["resolved"] is False

    def test_error_message_is_preserved(self):
        assert _unresolved("Rustec", "RUS", "network_timeout")["error"] == "network_timeout"

    def test_nullable_fields_are_none(self):
        result = _unresolved("Rustec", "RUS", "no_match")
        for field in ("entity_id", "matched_name", "match_strength", "score", "entity_type"):
            assert result[field] is None, f"expected {field} to be None, got {result[field]}"

    def test_input_fields_are_preserved(self):
        result = _unresolved("Rustec", "RUS", "no_match")
        assert result["input_name"] == "Rustec"
        assert result["input_country"] == "RUS"

    def test_unresolved_is_flagged_for_review(self):
        """A name we couldn't match at all must be queued for human review."""
        result = _unresolved("Rustec", "RUS", "no_match")
        assert result["needs_review"] is True
        assert result["confidence"] == "none"
        assert result["candidates"] == []


# ---------------------------------------------------------------------------
# _strength_str
# ---------------------------------------------------------------------------

class TestStrengthStr:
    def test_extracts_value_attribute(self):
        """A MatchStrength-like object exposes .value — we read that, not its repr."""
        obj = SimpleNamespace(value="weak")
        assert _strength_str(obj) == "weak"

    def test_plain_string_falls_back_to_str(self):
        assert _strength_str("strong") == "strong"

    def test_none_returns_none(self):
        assert _strength_str(None) is None


# ---------------------------------------------------------------------------
# _classify_confidence
# ---------------------------------------------------------------------------

class TestClassifyConfidence:
    def test_score_at_threshold_is_high(self):
        assert _classify_confidence(REVIEW_SCORE_THRESHOLD) == "high"

    def test_score_above_threshold_is_high(self):
        assert _classify_confidence(HIGH_SCORE) == "high"

    def test_score_below_threshold_is_low(self):
        assert _classify_confidence(LOW_SCORE) == "low"

    def test_none_score_is_none_band(self):
        assert _classify_confidence(None) == "none"


# ---------------------------------------------------------------------------
# _candidate
# ---------------------------------------------------------------------------

class TestCandidate:
    def test_flattens_result_fields(self):
        result = _make_match(entity_id="x1", label="Shell Corp", countries=["CYP", "RUS"])
        cand = _candidate(result)
        assert cand["entity_id"] == "x1"
        assert cand["matched_name"] == "Shell Corp"
        assert cand["countries"] == ["CYP", "RUS"]

    def test_match_strength_extracted_as_value(self):
        result = _make_match(match_strength=SimpleNamespace(value="weak"))
        assert _candidate(result)["match_strength"] == "weak"


# ---------------------------------------------------------------------------
# apply_review_decision
# ---------------------------------------------------------------------------

class TestApplyReviewDecision:
    def _base_record(self):
        return {
            "input_name": "VEB.RF",
            "input_country": "RUS",
            "entity_id": "wrong_id",
            "matched_name": "Bank Belveb",
            "score": LOW_SCORE,
            "confidence": "low",
            "needs_review": True,
            "reviewed": False,
            "manual_override": False,
            "candidates": [],
        }

    def test_choosing_different_candidate_sets_override(self):
        record = self._base_record()
        chosen = _make_candidate(entity_id="correct_id", matched_name="VEB.RF Corp")
        updated = apply_review_decision(record, chosen)
        assert updated["entity_id"] == "correct_id"
        assert updated["matched_name"] == "VEB.RF Corp"
        assert updated["manual_override"] is True
        assert updated["reviewed"] is True
        assert updated["needs_review"] is False
        assert updated["resolved"] is True

    def test_confirming_current_match_is_reviewed_not_overridden(self):
        record = self._base_record()
        # Candidate whose id equals the record's existing entity_id = confirmation
        chosen = _make_candidate(entity_id="wrong_id", matched_name="Bank Belveb")
        updated = apply_review_decision(record, chosen)
        assert updated["manual_override"] is False
        assert updated["reviewed"] is True
        assert updated["needs_review"] is False

    def test_marking_unresolved_clears_match(self):
        record = self._base_record()
        updated = apply_review_decision(record, None)
        assert updated["resolved"] is False
        assert updated["entity_id"] is None
        assert updated["confidence"] == "none"
        assert updated["manual_override"] is True
        assert updated["reviewed"] is True

    def test_confidence_recomputed_from_chosen_score(self):
        record = self._base_record()
        chosen = _make_candidate(entity_id="c", score=HIGH_SCORE)
        assert apply_review_decision(record, chosen)["confidence"] == "high"

    def test_original_record_is_not_mutated(self):
        """apply_review_decision returns a copy — the input dict stays untouched."""
        record = self._base_record()
        apply_review_decision(record, _make_candidate())
        assert record["reviewed"] is False
        assert record["entity_id"] == "wrong_id"


# ---------------------------------------------------------------------------
# resolve_entity  (mocked SDK client)
# ---------------------------------------------------------------------------

class TestResolveEntity:
    def _client_returning(self, *matches):
        response = MagicMock()
        response.data = list(matches)
        client = MagicMock()
        client.resolution.resolution.return_value = response
        return client

    def test_successful_resolution_returns_resolved_true(self):
        client = self._client_returning(_make_match())
        result = resolve_entity(client, "Rosneft", "RUS")
        assert result["resolved"] is True
        assert result["entity_id"] == "eid_001"
        assert result["matched_name"] == "Rosneft PJSC"
        assert result["error"] is None

    def test_candidates_captured_from_response(self):
        client = self._client_returning(
            _make_match(entity_id="a", label="A Corp"),
            _make_match(entity_id="b", label="B Corp"),
            _make_match(entity_id="c", label="C Corp"),
        )
        result = resolve_entity(client, "Rosneft", "RUS")
        assert len(result["candidates"]) == 3
        assert result["candidates"][0]["entity_id"] == "a"

    def test_high_score_match_is_not_flagged_for_review(self):
        client = self._client_returning(_make_match(score=HIGH_SCORE))
        result = resolve_entity(client, "Rosneft", "RUS")
        assert result["confidence"] == "high"
        assert result["needs_review"] is False

    def test_low_score_match_is_flagged_for_review(self):
        client = self._client_returning(_make_match(score=LOW_SCORE))
        result = resolve_entity(client, "VEB.RF", "RUS")
        assert result["confidence"] == "low"
        assert result["needs_review"] is True

    def test_empty_data_list_returns_unresolved(self):
        client = self._client_returning()  # no matches
        result = resolve_entity(client, "Rustec", "RUS")
        assert result["resolved"] is False
        assert result["error"] == "no_match"

    def test_api_exception_returns_unresolved(self):
        client = MagicMock()
        client.resolution.resolution.side_effect = RuntimeError("503 Service Unavailable")
        result = resolve_entity(client, "Rosneft", "RUS")
        assert result["resolved"] is False
        assert "503" in result["error"]
