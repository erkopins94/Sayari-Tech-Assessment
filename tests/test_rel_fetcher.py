"""
tests/test_rel_fetcher.py — Unit tests for rel_fetcher.py pure helper functions.

All tests run fully offline — no Sayari API calls, no disk I/O.

Coverage:
    _extract_relationship — 6 tests
    fetch_relationships   — 2 tests (mocked SDK client)
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from rel_fetcher import _extract_relationship, fetch_relationships


# ---------------------------------------------------------------------------
# Helpers — lightweight stand-ins for Sayari SDK objects
# ---------------------------------------------------------------------------

def _make_target(
    id="tgt_001",
    label="Shell Corp Ltd",
    type="company",
    countries=None,
    sanctioned=False,
):
    """Minimal stand-in for the EntityDetails object on rel.target."""
    return SimpleNamespace(
        id=id,
        label=label,
        type=type,
        countries=list(countries or ["GBR"]),
        sanctioned=sanctioned,
    )


def _make_rel(target=None, types=None):
    """
    Minimal stand-in for a Sayari RelationshipData record.

    rel.types is a Dict[str, List[RelationshipInfo]]; we pass plain strings as
    keys since _extract_relationship only calls rel.types.keys().
    """
    return SimpleNamespace(
        target=target or _make_target(),
        types={t: [] for t in (types or ["has_shareholder"])},
    )


# ---------------------------------------------------------------------------
# _extract_relationship
# ---------------------------------------------------------------------------

class TestExtractRelationship:
    def test_target_fields_are_flattened_to_plain_dict(self):
        rel = _make_rel(_make_target(id="t1", label="Shell Corp Ltd", type="company"))
        result = _extract_relationship(rel)
        assert result["target_id"] == "t1"
        assert result["target_name"] == "Shell Corp Ltd"
        assert result["target_type"] == "company"

    def test_relationship_types_stored_as_list_of_strings(self):
        """rel.types is a dict keyed by type strings; we store only the keys as a list."""
        rel = _make_rel(types=["has_shareholder", "director_of"])
        result = _extract_relationship(rel)
        assert set(result["relationship_types"]) == {"has_shareholder", "director_of"}

    def test_target_countries_stored_as_plain_list(self):
        target = _make_target(countries=["RUS", "CYP"])
        result = _extract_relationship(_make_rel(target))
        assert result["target_countries"] == ["RUS", "CYP"]

    def test_sanctioned_flag_propagated_correctly(self):
        target = _make_target(sanctioned=True)
        result = _extract_relationship(_make_rel(target))
        assert result["sanctioned"] is True

    def test_non_sanctioned_target_gives_false(self):
        target = _make_target(sanctioned=False)
        result = _extract_relationship(_make_rel(target))
        assert result["sanctioned"] is False

    def test_empty_types_dict_gives_empty_list(self):
        rel = SimpleNamespace(target=_make_target(), types={})
        result = _extract_relationship(rel)
        assert result["relationship_types"] == []

    def test_none_types_gives_empty_list(self):
        rel = SimpleNamespace(target=_make_target(), types=None)
        result = _extract_relationship(rel)
        assert result["relationship_types"] == []


# ---------------------------------------------------------------------------
# fetch_relationships  (mocked SDK client)
# ---------------------------------------------------------------------------

class TestFetchRelationships:
    def test_successful_fetch_returns_relationship_list(self):
        """Happy-path: SDK returns relationship records that get flattened and returned."""
        rel = _make_rel(_make_target(label="Counterparty"), types=["has_shareholder"])
        response = MagicMock()
        response.relationships.data = [rel, rel]  # two records

        client = MagicMock()
        client.entity.get_entity.return_value = response

        result = fetch_relationships(client, "eid_001", "Rosneft")
        assert len(result) == 2
        assert result[0]["target_name"] == "Counterparty"

    def test_api_exception_returns_empty_list(self):
        """Any SDK error must be caught; the function must return [] not raise."""
        client = MagicMock()
        client.entity.get_entity.side_effect = RuntimeError("connection refused")

        result = fetch_relationships(client, "eid_001", "Rosneft")
        assert result == []
