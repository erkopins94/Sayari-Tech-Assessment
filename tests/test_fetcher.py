"""
tests/test_fetcher.py — Unit tests for fetcher.py pure helper functions.

All tests run fully offline — no Sayari API calls, no disk I/O, no Streamlit.

Coverage:
    _extract_risk_flags    — 5 tests
    _count_risk_levels     — 4 tests
    _extract_sanctions_lists — 4 tests
    _empty_profile         — 3 tests
    fetch_profile          — 3 tests (mocked SDK client)
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fetcher import (
    DIRECT_RISK_KEYS,
    _count_risk_levels,
    _empty_profile,
    _extract_risk_flags,
    _extract_sanctions_lists,
    fetch_profile,
)


# ---------------------------------------------------------------------------
# Helpers — lightweight stand-ins for Sayari SDK objects
# ---------------------------------------------------------------------------

def _risk_item(level: str):
    """Minimal stand-in for a Sayari risk entry — only .level is read."""
    return SimpleNamespace(level=level)


def _source_item(label: str, source_type: str):
    """Minimal stand-in for a Sayari SourceCountInfo — only .label and .source_type are read."""
    return SimpleNamespace(label=label, source_type=source_type)


def _make_resolved(name: str = "Rosneft", country: str = "RUS", entity_id: str = "eid_001") -> dict:
    """Minimal resolved-entity dict as produced by resolver.py."""
    return {"input_name": name, "input_country": country, "entity_id": entity_id}


# ---------------------------------------------------------------------------
# _extract_risk_flags
# ---------------------------------------------------------------------------

class TestExtractRiskFlags:
    def test_direct_key_is_included_with_its_level(self):
        risk = {"sanctioned": _risk_item("critical")}
        result = _extract_risk_flags(risk)
        assert result == {"sanctioned": "critical"}

    def test_network_key_is_excluded(self):
        """Keys prefixed with owned_by_ describe network exposure and must be dropped."""
        risk = {
            "sanctioned": _risk_item("critical"),
            "owned_by_sanctioned": _risk_item("high"),
        }
        result = _extract_risk_flags(risk)
        assert "owned_by_sanctioned" not in result
        assert "sanctioned" in result

    def test_all_known_direct_keys_are_captured(self):
        """Every key in DIRECT_RISK_KEYS should appear in the output when present."""
        risk = {k: _risk_item("elevated") for k in DIRECT_RISK_KEYS}
        result = _extract_risk_flags(risk)
        assert set(result.keys()) == DIRECT_RISK_KEYS

    def test_none_level_maps_to_unknown(self):
        """If risk_data.level is None the flag still appears with value 'unknown'."""
        risk = {"pep": SimpleNamespace(level=None)}
        result = _extract_risk_flags(risk)
        assert result.get("pep") == "unknown"

    def test_empty_risk_dict_returns_empty(self):
        assert _extract_risk_flags({}) == {}

    def test_none_risk_returns_empty(self):
        assert _extract_risk_flags(None) == {}


# ---------------------------------------------------------------------------
# _count_risk_levels
# ---------------------------------------------------------------------------

class TestCountRiskLevels:
    def test_counts_all_keys_including_network_flags(self):
        """_count_risk_levels intentionally includes network flags — different from _extract_risk_flags."""
        risk = {
            "sanctioned":          _risk_item("critical"),
            "owned_by_sanctioned": _risk_item("high"),
            "pep":                 _risk_item("elevated"),
        }
        counts = _count_risk_levels(risk)
        assert counts["critical"] == 1
        assert counts["high"] == 1
        assert counts["elevated"] == 1
        assert counts["relevant"] == 0

    def test_multiple_flags_at_same_level_accumulate(self):
        risk = {
            "sanctioned": _risk_item("critical"),
            "export_controls": _risk_item("critical"),
        }
        counts = _count_risk_levels(risk)
        assert counts["critical"] == 2

    def test_unknown_level_strings_are_not_counted(self):
        """Levels outside the four known buckets are silently ignored."""
        risk = {"some_flag": _risk_item("unknown_level")}
        counts = _count_risk_levels(risk)
        assert sum(counts.values()) == 0

    def test_empty_risk_returns_zero_counts(self):
        counts = _count_risk_levels({})
        assert counts == {"critical": 0, "high": 0, "elevated": 0, "relevant": 0}

    def test_none_risk_returns_zero_counts(self):
        counts = _count_risk_levels(None)
        assert counts == {"critical": 0, "high": 0, "elevated": 0, "relevant": 0}


# ---------------------------------------------------------------------------
# _extract_sanctions_lists
# ---------------------------------------------------------------------------

class TestExtractSanctionsLists:
    def test_sanctions_list_entry_is_returned(self):
        source_count = {
            "src_ofac": _source_item("OFAC SDN List", "sanctions_lists"),
        }
        result = _extract_sanctions_lists(source_count)
        assert result == ["OFAC SDN List"]

    def test_non_sanctions_entry_is_excluded(self):
        source_count = {
            "src_ofac":    _source_item("OFAC SDN List", "sanctions_lists"),
            "src_company": _source_item("Company Registry", "company_registry"),
        }
        result = _extract_sanctions_lists(source_count)
        assert result == ["OFAC SDN List"]

    def test_multiple_sanctions_lists_all_returned(self):
        source_count = {
            "a": _source_item("OFAC SDN", "sanctions_lists"),
            "b": _source_item("EU Consolidated", "sanctions_lists"),
            "c": _source_item("UN Sanctions", "sanctions_lists"),
        }
        result = _extract_sanctions_lists(source_count)
        assert len(result) == 3
        assert "OFAC SDN" in result

    def test_no_sanctions_entries_returns_empty_list(self):
        source_count = {
            "src_company": _source_item("Company Registry", "company_registry"),
        }
        result = _extract_sanctions_lists(source_count)
        assert result == []

    def test_none_source_count_returns_empty_list(self):
        assert _extract_sanctions_lists(None) == []

    def test_empty_source_count_returns_empty_list(self):
        assert _extract_sanctions_lists({}) == []


# ---------------------------------------------------------------------------
# _empty_profile
# ---------------------------------------------------------------------------

class TestEmptyProfile:
    def test_all_required_keys_present(self):
        """Every key that a successful fetch_profile emits must also appear in empty profiles."""
        resolved = _make_resolved()
        profile = _empty_profile(resolved, "test_error")

        required_keys = {
            "input_name", "input_country", "entity_id", "matched_name",
            "entity_type", "countries", "sanctioned", "pep", "closed",
            "degree", "relationship_counts", "total_relationships",
            "risk_flags", "risk_level_counts", "sanctions_lists",
            "source_count", "fetched", "error",
        }
        assert required_keys.issubset(profile.keys())

    def test_fetched_is_false(self):
        resolved = _make_resolved()
        assert _empty_profile(resolved, "err")["fetched"] is False

    def test_error_message_is_preserved(self):
        resolved = _make_resolved()
        profile = _empty_profile(resolved, "network_timeout")
        assert profile["error"] == "network_timeout"

    def test_input_fields_copied_from_resolved(self):
        resolved = _make_resolved("Rosneft", "RUS", "eid_x")
        profile = _empty_profile(resolved, "err")
        assert profile["input_name"] == "Rosneft"
        assert profile["input_country"] == "RUS"
        assert profile["entity_id"] == "eid_x"


# ---------------------------------------------------------------------------
# fetch_profile  (mocked SDK client)
# ---------------------------------------------------------------------------

class TestFetchProfile:
    def _make_summary(self):
        """Build a minimal mock of the object returned by client.entity.entity_summary()."""
        summary = MagicMock()
        summary.label = "Rosneft PJSC"
        summary.type = "company"
        summary.countries = ["RUS"]
        summary.sanctioned = True
        summary.pep = False
        summary.closed = False
        summary.degree = 500

        # relationship_count — dict-like; sum → total_relationships
        summary.relationship_count = {"shareholder": 200, "director": 100, "subsidiary": 200}

        # risk — two direct flags
        summary.risk = {
            "sanctioned": _risk_item("critical"),
            "pep":        _risk_item("elevated"),
        }

        # source_count — two sources, one sanctions list
        summary.source_count = {
            "src_ofac":    _source_item("OFAC SDN List", "sanctions_lists"),
            "src_company": _source_item("Company Registry", "company_registry"),
        }
        return summary

    def test_successful_fetch_returns_fetched_true(self):
        client = MagicMock()
        client.entity.entity_summary.return_value = self._make_summary()
        resolved = _make_resolved()
        profile = fetch_profile(client, resolved)
        assert profile["fetched"] is True
        assert profile["error"] is None

    def test_relationship_totals_are_summed_correctly(self):
        client = MagicMock()
        client.entity.entity_summary.return_value = self._make_summary()
        resolved = _make_resolved()
        profile = fetch_profile(client, resolved)
        assert profile["total_relationships"] == 500   # 200+100+200
        assert profile["relationship_counts"] == {"shareholder": 200, "director": 100, "subsidiary": 200}

    def test_api_exception_returns_empty_profile(self):
        """Any SDK exception must be caught and return a consistently-shaped failure record."""
        client = MagicMock()
        client.entity.entity_summary.side_effect = RuntimeError("connection refused")
        resolved = _make_resolved()
        profile = fetch_profile(client, resolved)
        assert profile["fetched"] is False
        assert "connection refused" in profile["error"]
        # Shape must still be complete
        assert "risk_flags" in profile
        assert "sanctions_lists" in profile
