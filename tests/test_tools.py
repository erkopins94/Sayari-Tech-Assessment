"""
tests/test_tools.py — unit tests for every tool function and critical private
helper in tools.py.

Tests run entirely against synthetic fixture data — no API calls, no file I/O,
no Sayari or Anthropic credentials required. The make_profile factory mirrors
the schema produced by fetcher.py; make_relationship mirrors the schema written
by rel_fetcher.py. Both are self-contained in this file.

Test class layout (one class per unit under test):
  TestBuildNameIndex          — two-pass name priority logic
  TestFuzzyMatch              — exact and fuzzy name resolution
  TestResolveCountry          — alias, ISO-3 fast path, pycountry, failures
  TestResolveSector           — partial match, case-insensitivity, failures
  TestGetEntity               — profile lookup and error paths
  TestFilterEntities          — all filter types, combinations, error cases
  TestRankEntities            — metric aliases, ordering, sector pre-filter
  TestCompareEntities         — side-by-side comparison, dedup, error cases
  TestGetEntityRelationships  — connection lookup, filters, missing cache
  TestGetSummaryStats         — macro aggregation, top entity, top list
"""

import pytest

from tools import (
    _build_name_index,
    _fuzzy_match,
    _resolve_country,
    _resolve_sector,
    compare_entities,
    filter_entities,
    get_entity,
    get_entity_relationships,
    get_summary_stats,
    rank_entities,
)


# ---------------------------------------------------------------------------
# Fixture factories
#
# make_profile mirrors the schema produced by fetcher.py so tests only specify
# the fields relevant to the behaviour under test.
#
# make_relationship mirrors the schema written by rel_fetcher._extract_relationship
# so relationship-filter tests don't need to care about the Sayari SDK objects.
# ---------------------------------------------------------------------------

def make_profile(
    name: str,
    *,
    matched_name: str | None = None,
    sanctioned: bool = True,
    countries: list[str] | None = None,
    risk_flags: dict | None = None,
    risk_level_counts: dict | None = None,
    sanctions_lists: list[str] | None = None,
    degree: int = 100,
    fetched: bool = True,
    input_country: str = "RUS",
) -> dict:
    """
    Returns a minimal profile dict that matches the schema produced by fetcher.py.
    matched_name defaults to name so most tests don't need to specify it;
    pass a different value to test the two-pass name index collision logic.
    """
    return {
        "input_name":        name,
        "input_country":     input_country,
        "entity_id":         f"id_{name}",
        "matched_name":      matched_name if matched_name is not None else name,
        "entity_type":       "company",
        "countries":         countries if countries is not None else ["RUS"],
        "sanctioned":        sanctioned,
        "pep":               False,
        "closed":            False,
        "degree":            degree,
        "relationship_counts": {},
        "total_relationships": degree,
        "risk_flags":        risk_flags if risk_flags is not None else {},
        "risk_level_counts": risk_level_counts if risk_level_counts is not None else {
            "critical": 0, "high": 0, "elevated": 0, "relevant": 0
        },
        "sanctions_lists":   sanctions_lists if sanctions_lists is not None else [],
        "source_count":      1,
        "fetched":           fetched,
        "error":             None,
    }


def make_relationship(
    target_name: str = "Counterparty Corp",
    *,
    target_id: str = "id_target",
    target_type: str = "company",
    target_countries: list[str] | None = None,
    relationship_types: list[str] | None = None,
    sanctioned: bool = False,
) -> dict:
    """
    Returns a minimal relationship record matching the schema written by
    rel_fetcher._extract_relationship.
    """
    return {
        "target_id":          target_id,
        "target_name":        target_name,
        "target_type":        target_type,
        "target_countries":   target_countries if target_countries is not None else ["RUS"],
        "relationship_types": relationship_types if relationship_types is not None else ["has_shareholder"],
        "sanctioned":         sanctioned,
    }


# ---------------------------------------------------------------------------
# _build_name_index
# ---------------------------------------------------------------------------

class TestBuildNameIndex:
    """Validates the two-pass priority logic that prevents silent name collisions."""

    def test_input_name_always_in_index(self):
        p = make_profile("Rosneft")
        index = _build_name_index([p])
        assert "rosneft" in index
        assert index["rosneft"] is p

    def test_matched_name_added_as_alias_when_unclaimed(self):
        # matched_name differs from input_name and the key is otherwise free
        p = make_profile("Rosneft", matched_name="Rosneft PJSC")
        index = _build_name_index([p])
        assert "rosneft pjsc" in index
        assert index["rosneft pjsc"] is p

    def test_input_name_beats_matched_name_on_same_key(self):
        # p1's input_name and p2's matched_name both normalise to "rosneft"
        # p1's input_name wins because Pass 1 runs before Pass 2
        p1 = make_profile("Rosneft")
        p2 = make_profile("Other Entity", matched_name="Rosneft")
        index = _build_name_index([p1, p2])
        assert index["rosneft"] is p1

    def test_none_matched_name_does_not_raise_or_pollute_index(self):
        p = make_profile("Entity A", matched_name=None)
        # Pass 2 should skip profiles with no matched_name without raising
        index = _build_name_index([p])
        assert "entity a" in index
        assert "none" not in index


# ---------------------------------------------------------------------------
# _fuzzy_match
# ---------------------------------------------------------------------------

class TestFuzzyMatch:
    """Validates exact and fuzzy entity name resolution against the profiles list."""

    def test_exact_case_insensitive_match(self):
        p = make_profile("Gazprom")
        assert _fuzzy_match("GAZPROM", [p]) is p
        assert _fuzzy_match("gazprom", [p]) is p
        assert _fuzzy_match("Gazprom", [p]) is p

    def test_fuzzy_match_handles_typo(self):
        p = make_profile("Sberbank")
        # "Sberbonk" is close enough to clear the 0.6 cutoff
        result = _fuzzy_match("Sberbonk", [p])
        assert result is p

    def test_matched_name_alias_resolves_to_same_profile(self):
        p = make_profile("Rosneft", matched_name="Rosneft PJSC")
        # Querying by matched_name should return the same profile
        assert _fuzzy_match("Rosneft PJSC", [p]) is p

    def test_returns_none_when_no_match_clears_threshold(self):
        p = make_profile("Rosneft")
        # "XYZABC Corp" shares nothing with "rosneft" — well below 0.6
        assert _fuzzy_match("XYZABC Corp", [p]) is None

    def test_pre_built_index_is_used_when_supplied(self):
        p = make_profile("Gazprom")
        index = _build_name_index([p])
        # If the pre-built index is used, the result is the same
        assert _fuzzy_match("Gazprom", [p], index=index) is p


# ---------------------------------------------------------------------------
# _resolve_country
# ---------------------------------------------------------------------------

class TestResolveCountry:
    """Validates the three-stage country resolution: aliases → ISO-3 → pycountry."""

    def test_alias_uae_resolved_before_iso3_fast_path(self):
        # "uae" is 3 letters — aliases must be checked first or it passes through
        # the ISO-3 fast path unchanged, returning "UAE" instead of "ARE"
        assert _resolve_country("uae") == "ARE"

    def test_alias_turkey_bypasses_pycountry_rename(self):
        # pycountry updated Turkey → Türkiye in 2022; alias ensures backward compat
        assert _resolve_country("turkey") == "TUR"

    def test_alias_uk_resolves_to_gbr(self):
        assert _resolve_country("uk") == "GBR"

    def test_alias_usa_resolves_to_usa(self):
        assert _resolve_country("usa") == "USA"

    def test_iso3_fast_path_for_three_letter_code(self):
        assert _resolve_country("RUS") == "RUS"
        assert _resolve_country("chn") == "CHN"  # case-insensitive

    def test_full_name_resolved_via_pycountry(self):
        assert _resolve_country("Russia") == "RUS"
        assert _resolve_country("China") == "CHN"

    def test_unresolvable_string_returns_none(self):
        assert _resolve_country("NotACountryAtAll9999") is None


# ---------------------------------------------------------------------------
# _resolve_sector
# ---------------------------------------------------------------------------

class TestResolveSector:
    """Validates partial and case-insensitive sector name matching."""

    def test_partial_match_resolves_correctly(self):
        assert _resolve_sector("defense") == "Defense & Aerospace"
        assert _resolve_sector("banking") == "Banking & Finance"
        assert _resolve_sector("energy") == "Energy & Oil/Gas"

    def test_case_insensitive_matching(self):
        assert _resolve_sector("DEFENSE") == "Defense & Aerospace"
        assert _resolve_sector("Banking") == "Banking & Finance"

    def test_full_sector_name_matches(self):
        assert _resolve_sector("Defense & Aerospace") == "Defense & Aerospace"

    def test_unknown_sector_returns_none(self):
        assert _resolve_sector("Quantum Computing") is None


# ---------------------------------------------------------------------------
# get_entity
# ---------------------------------------------------------------------------

class TestGetEntity:
    """Validates single-entity profile lookup and all failure paths."""

    def test_returns_formatted_profile_for_known_entity(self):
        p = make_profile("Rosneft", sanctioned=True, countries=["RUS", "DEU"])
        result = get_entity("Rosneft", [p])
        assert result["error"] is False
        assert result["input_name"] == "Rosneft"
        assert result["sanctioned"] is True
        assert "RUS" in result["countries"]

    def test_fuzzy_name_match_still_resolves(self):
        p = make_profile("Sberbank")
        result = get_entity("Sberbonk", [p])
        assert result["error"] is False
        assert result["input_name"] == "Sberbank"

    def test_not_found_returns_error_dict(self):
        p = make_profile("Rosneft")
        result = get_entity("XYZ Unknown Entity", [p])
        assert result["error"] is True
        assert "message" in result

    def test_unfetched_entity_returns_error_dict(self):
        p = make_profile("Rosneft", fetched=False)
        result = get_entity("Rosneft", [p])
        assert result["error"] is True
        assert "message" in result

    def test_returned_profile_includes_sector_and_sector_note(self):
        # "Sberbank" is in SECTOR_MAP → should have sector + sector_note
        p = make_profile("Sberbank")
        result = get_entity("Sberbank", [p])
        assert result["sector"] == "Banking & Finance"
        assert isinstance(result["sector_note"], str)
        assert len(result["sector_note"]) > 0


# ---------------------------------------------------------------------------
# filter_entities
# ---------------------------------------------------------------------------

class TestFilterEntities:
    """Validates all filter types, AND-logic combinations, and error returns."""

    def test_sector_filter_returns_matching_entities(self):
        profiles = [
            make_profile("Sberbank"),      # Banking & Finance
            make_profile("Rosneft"),       # Energy & Oil/Gas
            make_profile("Gazprom"),       # Energy & Oil/Gas
        ]
        result = filter_entities(profiles, sector="Energy")
        assert result["error"] is False
        assert result["count"] == 2
        names = [e["name"] for e in result["entities"]]
        assert "Rosneft" in names
        assert "Sberbank" not in names

    def test_country_filter_matches_entities_with_that_country(self):
        profiles = [
            make_profile("A", countries=["RUS", "CHN"]),
            make_profile("B", countries=["CHN"]),
            make_profile("C", countries=["RUS"]),
        ]
        result = filter_entities(profiles, country="CHN")
        assert result["error"] is False
        assert result["count"] == 2

    def test_country_filter_accepts_full_name_via_pycountry(self):
        profiles = [
            make_profile("A", countries=["RUS"]),
            make_profile("B", countries=["CHN"]),
        ]
        result = filter_entities(profiles, country="Russia")
        assert result["error"] is False
        assert result["count"] == 1
        assert result["entities"][0]["name"] == "A"

    def test_sanctions_list_filter_substring_match(self):
        profiles = [
            make_profile("A", sanctions_lists=["OFAC SDN List"]),
            make_profile("B", sanctions_lists=["EU Consolidated Sanctions"]),
            make_profile("C", sanctions_lists=["OFAC SDN List", "UK Sanctions"]),
        ]
        result = filter_entities(profiles, sanctions_list="OFAC")
        assert result["error"] is False
        assert result["count"] == 2

    def test_risk_flag_filter_accepts_readable_label(self):
        profiles = [
            make_profile("A", risk_flags={"state_owned": "high"}),
            make_profile("B", risk_flags={}),
        ]
        result = filter_entities(profiles, risk_flag="State-Owned Enterprise")
        assert result["error"] is False
        assert result["count"] == 1
        assert result["entities"][0]["name"] == "A"

    def test_risk_flag_filter_accepts_raw_api_key(self):
        profiles = [
            make_profile("A", risk_flags={"export_controls": "critical"}),
            make_profile("B", risk_flags={}),
        ]
        result = filter_entities(profiles, risk_flag="export_controls")
        assert result["error"] is False
        assert result["count"] == 1

    def test_sanctioned_boolean_filter(self):
        profiles = [
            make_profile("A", sanctioned=True),
            make_profile("B", sanctioned=False),
            make_profile("C", sanctioned=True),
        ]
        result = filter_entities(profiles, sanctioned=True)
        assert result["error"] is False
        assert result["count"] == 2

    def test_state_owned_false_filter_returns_non_state_owned(self):
        profiles = [
            make_profile("A", risk_flags={"state_owned": "high"}),
            make_profile("B", risk_flags={}),
        ]
        result = filter_entities(profiles, state_owned=False)
        assert result["error"] is False
        assert result["count"] == 1
        assert result["entities"][0]["name"] == "B"

    def test_combined_and_filters(self):
        profiles = [
            make_profile("A", sanctioned=True,  risk_flags={"state_owned": "high"}),
            make_profile("B", sanctioned=True,  risk_flags={}),
            make_profile("C", sanctioned=False, risk_flags={"state_owned": "high"}),
        ]
        # Only A satisfies both: sanctioned AND state_owned
        result = filter_entities(profiles, sanctioned=True, state_owned=True)
        assert result["error"] is False
        assert result["count"] == 1
        assert result["entities"][0]["name"] == "A"

    def test_unknown_sector_returns_error_dict(self):
        result = filter_entities([make_profile("A")], sector="Quantum Computing XYZ")
        assert result["error"] is True
        assert "message" in result

    def test_unresolvable_country_returns_error_dict(self):
        result = filter_entities([make_profile("A")], country="NotACountry9999")
        assert result["error"] is True
        assert "message" in result

    def test_no_matches_returns_count_zero_not_error(self):
        profiles = [make_profile("A", countries=["RUS"])]
        result = filter_entities(profiles, country="CHN")
        assert result["error"] is False
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# rank_entities
# ---------------------------------------------------------------------------

class TestRankEntities:
    """Validates metric resolution, sort order, n-limiting, and sector pre-filter."""

    def test_ranks_by_degree_descending_by_default(self):
        profiles = [
            make_profile("Low",  degree=10),
            make_profile("High", degree=500),
            make_profile("Mid",  degree=100),
        ]
        result = rank_entities(profiles, metric="degree")
        assert result["error"] is False
        assert result["ranking"][0]["name"] == "High"
        assert result["ranking"][-1]["name"] == "Low"

    def test_ascending_flag_reverses_order(self):
        profiles = [
            make_profile("Low",  degree=10),
            make_profile("High", degree=500),
        ]
        result = rank_entities(profiles, metric="degree", ascending=True)
        assert result["ranking"][0]["name"] == "Low"

    def test_n_parameter_limits_results(self):
        profiles = [make_profile(f"E{i}", degree=i * 10) for i in range(10)]
        result = rank_entities(profiles, metric="degree", n=3)
        assert len(result["ranking"]) == 3

    def test_n_larger_than_dataset_returns_all(self):
        profiles = [make_profile(f"E{i}") for i in range(4)]
        result = rank_entities(profiles, metric="degree", n=100)
        assert len(result["ranking"]) == 4

    def test_connections_alias_resolves_to_network_degree(self):
        profiles = [make_profile("A", degree=50), make_profile("B", degree=200)]
        result = rank_entities(profiles, metric="connections")
        assert result["error"] is False
        assert result["metric"] == "Network Degree"

    def test_lists_alias_resolves_to_sanctions_list_count(self):
        profiles = [
            make_profile("A", sanctions_lists=["L1", "L2", "L3"]),
            make_profile("B", sanctions_lists=["L1"]),
        ]
        result = rank_entities(profiles, metric="lists")
        assert result["error"] is False
        assert result["ranking"][0]["name"] == "A"

    def test_sector_pre_filter_applied_before_ranking(self):
        profiles = [
            make_profile("Sberbank", degree=999),     # Banking & Finance
            make_profile("Rosneft",  degree=50),      # Energy & Oil/Gas
            make_profile("Gazprom",  degree=100),     # Energy & Oil/Gas
        ]
        result = rank_entities(profiles, metric="degree", sector="energy")
        assert result["error"] is False
        # Sberbank (banking) excluded; only Gazprom and Rosneft remain
        names = [r["name"] for r in result["ranking"]]
        assert "Sberbank" not in names
        assert result["ranking"][0]["name"] == "Gazprom"

    def test_unknown_metric_returns_error_dict(self):
        result = rank_entities([make_profile("A")], metric="bananas")
        assert result["error"] is True
        assert "message" in result


# ---------------------------------------------------------------------------
# compare_entities
# ---------------------------------------------------------------------------

class TestCompareEntities:
    """Validates side-by-side comparison, deduplication, and error paths."""

    def test_returns_formatted_profiles_for_all_resolved_names(self):
        profiles = [make_profile("Rosneft"), make_profile("Gazprom")]
        result = compare_entities(["Rosneft", "Gazprom"], profiles)
        assert result["error"] is False
        assert result["entities_compared"] == 2
        names = [e["input_name"] for e in result["comparison"]]
        assert "Rosneft" in names
        assert "Gazprom" in names

    def test_unresolvable_name_goes_to_not_found_not_error(self):
        profiles = [make_profile("Rosneft"), make_profile("Gazprom")]
        result = compare_entities(["Rosneft", "Gazprom", "XYZABC Unknown"], profiles)
        assert result["error"] is False
        assert "XYZABC Unknown" in result["not_found"]
        assert result["entities_compared"] == 2

    def test_duplicate_names_resolve_to_same_entity_are_deduplicated(self):
        profiles = [make_profile("Rosneft"), make_profile("Gazprom")]
        # "Rosneft" and "rosneft" both resolve to the same profile
        result = compare_entities(["Rosneft", "rosneft", "Gazprom"], profiles)
        assert result["error"] is False
        assert result["entities_compared"] == 2

    def test_fewer_than_two_names_returns_error(self):
        result = compare_entities(["Rosneft"], [make_profile("Rosneft")])
        assert result["error"] is True
        assert "message" in result

    def test_more_than_ten_names_returns_error(self):
        names = [f"Entity{i}" for i in range(11)]
        result = compare_entities(names, [])
        assert result["error"] is True
        assert "message" in result

    def test_fewer_than_two_entities_resolve_returns_error(self):
        # Both names fail to match — not enough to compare
        result = compare_entities(["XYZABC", "XYZDEF"], [make_profile("Rosneft")])
        assert result["error"] is True
        assert "message" in result


# ---------------------------------------------------------------------------
# get_entity_relationships
# ---------------------------------------------------------------------------

class TestGetEntityRelationships:
    """Validates connection lookup, optional filters, and graceful cache-miss handling."""

    def test_returns_connection_list_for_known_entity(self):
        profile = make_profile("Rosneft", degree=5000)
        rels = {
            "Rosneft": [
                make_relationship("Shell plc"),
                make_relationship("BP p.l.c."),
            ]
        }
        result = get_entity_relationships("Rosneft", [profile], rels)
        assert result["error"] is False
        assert result["entity"] == "Rosneft"
        assert result["cached_connections"] == 2
        assert result["returned"] == 2

    def test_total_known_connections_comes_from_profile_degree(self):
        # total_known_connections should reflect the full network size, not just
        # what is cached — it comes from profile["degree"], not len(cache)
        profile = make_profile("Rosneft", degree=114371)
        rels = {"Rosneft": [make_relationship("Shell plc")]}
        result = get_entity_relationships("Rosneft", [profile], rels)
        assert result["total_known_connections"] == 114371
        assert result["cached_connections"] == 1

    def test_relationship_type_filter_substring_match(self):
        profile = make_profile("Rosneft")
        rels = {
            "Rosneft": [
                make_relationship("A", relationship_types=["has_shareholder"]),
                make_relationship("B", relationship_types=["has_director"]),
                make_relationship("C", relationship_types=["has_shareholder", "linked_to"]),
            ]
        }
        result = get_entity_relationships(
            "Rosneft", [profile], rels, relationship_type="shareholder"
        )
        assert result["returned"] == 2  # A and C match; B does not

    def test_sanctioned_only_filter(self):
        profile = make_profile("Rosneft")
        rels = {
            "Rosneft": [
                make_relationship("Sanctioned Co", sanctioned=True),
                make_relationship("Clean Co",      sanctioned=False),
                make_relationship("Also Sanctioned", sanctioned=True),
            ]
        }
        result = get_entity_relationships(
            "Rosneft", [profile], rels, sanctioned_only=True
        )
        assert result["returned"] == 2
        names = [r["target_name"] for r in result["relationships"]]
        assert "Clean Co" not in names

    def test_combined_relationship_type_and_sanctioned_filters(self):
        profile = make_profile("Rosneft")
        rels = {
            "Rosneft": [
                make_relationship("A", relationship_types=["has_shareholder"], sanctioned=True),
                make_relationship("B", relationship_types=["has_shareholder"], sanctioned=False),
                make_relationship("C", relationship_types=["has_director"],    sanctioned=True),
            ]
        }
        result = get_entity_relationships(
            "Rosneft", [profile], rels,
            relationship_type="shareholder",
            sanctioned_only=True,
        )
        # Only A satisfies both: shareholder type AND sanctioned
        assert result["returned"] == 1
        assert result["relationships"][0]["target_name"] == "A"

    def test_entity_absent_from_cache_returns_zero_gracefully(self):
        profile = make_profile("Rosneft", degree=5000)
        result = get_entity_relationships("Rosneft", [profile], {})
        assert result["error"] is False
        assert result["cached_connections"] == 0
        assert result["returned"] == 0
        assert result["relationships"] == []

    def test_unknown_entity_name_returns_error_dict(self):
        result = get_entity_relationships("XYZABC Unknown", [make_profile("Rosneft")], {})
        assert result["error"] is True
        assert "message" in result

    def test_unfetched_entity_returns_error_dict(self):
        profile = make_profile("Rosneft", fetched=False)
        result = get_entity_relationships("Rosneft", [profile], {})
        assert result["error"] is True
        assert "message" in result


# ---------------------------------------------------------------------------
# get_summary_stats
# ---------------------------------------------------------------------------

class TestGetSummaryStats:
    """Validates macro-level aggregation, top-entity selection, and empty-input safety."""

    def test_returns_all_required_keys(self):
        profiles = [make_profile("Rosneft", degree=1000)]
        result = get_summary_stats(profiles)
        required_keys = {
            "error", "total_entities", "fetched", "sanctioned_count",
            "sanctioned_pct", "state_owned_count", "export_controls_count",
            "countries_represented", "avg_sanctions_lists",
            "total_network_connections", "distinct_sanctions_lists",
            "most_common_sanctions_list", "most_common_sanctions_list_entity_count",
            "top_entity_by_degree", "top_entity_degree", "sector_breakdown",
        }
        assert required_keys.issubset(result.keys())

    def test_top_entity_by_degree_is_highest_degree_profile(self):
        profiles = [
            make_profile("Low",  degree=10),
            make_profile("High", degree=9999),
            make_profile("Mid",  degree=500),
        ]
        result = get_summary_stats(profiles)
        assert result["top_entity_by_degree"] == "High"
        assert result["top_entity_degree"] == 9999

    def test_most_common_sanctions_list_is_correct(self):
        profiles = [
            make_profile("A", sanctions_lists=["OFAC SDN", "EU Sanctions"]),
            make_profile("B", sanctions_lists=["OFAC SDN"]),
            make_profile("C", sanctions_lists=["EU Sanctions"]),
        ]
        result = get_summary_stats(profiles)
        # Both OFAC SDN and EU Sanctions appear on 2 entities — either is valid
        assert result["most_common_sanctions_list_entity_count"] == 2

    def test_sector_breakdown_present_and_non_empty(self):
        profiles = [make_profile("Sberbank"), make_profile("Rosneft")]
        result = get_summary_stats(profiles)
        assert isinstance(result["sector_breakdown"], dict)
        assert len(result["sector_breakdown"]) > 0

    def test_empty_profiles_returns_safe_zero_defaults(self):
        result = get_summary_stats([])
        assert result["total_entities"] == 0
        assert result["sanctioned_count"] == 0
        assert result["top_entity_by_degree"] is None
        assert result["top_entity_degree"] == 0
