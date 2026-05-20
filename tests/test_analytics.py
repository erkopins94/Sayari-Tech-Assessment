"""
tests/test_analytics.py — unit tests for every function in analytics.py.

Tests run entirely against synthetic fixture data — no API calls are made
and the profiles cache is never read. This keeps the suite fast, offline,
and independent of credential availability.

Each test class maps 1-to-1 with an analytics function so failures are
immediately locatable.
"""

import pytest

from analytics import (
    SECTOR_MAP,
    country_breakdown,
    jurisdiction_exposure,
    risk_flag_frequency,
    risk_level_distribution,
    sanctions_coverage_per_entity,
    sanctions_list_breakdown,
    sector_breakdown,
    summary_stats,
    top_entities_by_degree,
)


# ---------------------------------------------------------------------------
# Fixture factory
# Centralises the construction of minimal valid profile dicts so individual
# tests only specify the fields relevant to what they're testing.
# ---------------------------------------------------------------------------

def make_profile(
    name: str,
    *,
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
    Any field not provided defaults to a sensible empty/false value so tests
    only need to specify the fields they actually care about.
    """
    return {
        "input_name": name,
        "input_country": input_country,
        "entity_id": f"id_{name}",
        "matched_name": name,
        "entity_type": "company",
        "countries": countries if countries is not None else ["RUS"],
        "sanctioned": sanctioned,
        "pep": False,
        "closed": False,
        "degree": degree,
        "relationship_counts": {},
        "total_relationships": degree,
        "risk_flags": risk_flags if risk_flags is not None else {},
        "risk_level_counts": risk_level_counts if risk_level_counts is not None else {
            "critical": 0, "high": 0, "elevated": 0, "relevant": 0
        },
        "sanctions_lists": sanctions_lists if sanctions_lists is not None else [],
        "source_count": 1,
        "fetched": fetched,
        "error": None,
    }


# ---------------------------------------------------------------------------
# summary_stats
# ---------------------------------------------------------------------------

class TestSummaryStats:
    """Covers the headline KPI numbers shown at the top of the dashboard."""

    def test_counts_sanctioned_entities(self):
        profiles = [
            make_profile("A", sanctioned=True),
            make_profile("B", sanctioned=True),
            make_profile("C", sanctioned=False),
        ]
        assert summary_stats(profiles)["sanctioned_count"] == 2

    def test_sanctioned_percentage_calculation(self):
        # 1 of 2 fetched = 50.0%
        profiles = [
            make_profile("A", sanctioned=True),
            make_profile("B", sanctioned=False),
        ]
        assert summary_stats(profiles)["sanctioned_pct"] == 50.0

    def test_counts_state_owned_via_risk_flag(self):
        profiles = [
            make_profile("A", risk_flags={"state_owned": "high"}),
            make_profile("B", risk_flags={}),
        ]
        assert summary_stats(profiles)["state_owned_count"] == 1

    def test_counts_export_controls_via_risk_flag(self):
        profiles = [
            make_profile("A", risk_flags={"export_controls": "critical"}),
            make_profile("B", risk_flags={"export_controls": "critical"}),
            make_profile("C", risk_flags={}),
        ]
        assert summary_stats(profiles)["export_controls_count"] == 2

    def test_unique_countries_deduplicated_across_entities(self):
        # RUS appears in both — should only count once
        profiles = [
            make_profile("A", countries=["RUS", "USA"]),
            make_profile("B", countries=["RUS", "CHN"]),
        ]
        assert summary_stats(profiles)["countries_represented"] == 3

    def test_total_network_connections_summed(self):
        profiles = [
            make_profile("A", degree=100),
            make_profile("B", degree=250),
        ]
        assert summary_stats(profiles)["total_network_connections"] == 350

    def test_average_sanctions_lists_per_entity(self):
        profiles = [
            make_profile("A", sanctions_lists=["L1", "L2"]),
            make_profile("B", sanctions_lists=["L1"]),
        ]
        assert summary_stats(profiles)["avg_sanctions_lists"] == 1.5

    def test_unfetched_profiles_excluded_from_all_counts(self):
        # The unfetched profile should not affect any metric
        profiles = [
            make_profile("A", sanctioned=True, degree=100, fetched=True),
            make_profile("B", sanctioned=True, degree=999, fetched=False),
        ]
        stats = summary_stats(profiles)
        assert stats["sanctioned_count"] == 1
        assert stats["total_network_connections"] == 100

    def test_empty_input_returns_safe_zero_defaults(self):
        stats = summary_stats([])
        assert stats["sanctioned_count"] == 0
        assert stats["sanctioned_pct"] == 0
        assert stats["countries_represented"] == 0
        assert stats["total_network_connections"] == 0


# ---------------------------------------------------------------------------
# sector_breakdown
# ---------------------------------------------------------------------------

class TestSectorBreakdown:
    """Validates sector classification logic and full coverage of SECTOR_MAP."""

    def test_all_sector_map_entities_are_classified(self):
        # Every entity in SECTOR_MAP must map to a named sector, never "Other"
        profiles = [make_profile(name) for name in SECTOR_MAP]
        breakdown = sector_breakdown(profiles)
        assert "Other" not in breakdown
        assert sum(breakdown.values()) == len(SECTOR_MAP)

    def test_unknown_entity_falls_back_to_other(self):
        profiles = [make_profile("Unknown Corp That Is Not In Any List XYZ")]
        assert sector_breakdown(profiles).get("Other", 0) == 1

    def test_correct_sectors_assigned_for_known_entities(self):
        profiles = [
            make_profile("Sberbank"),
            make_profile("Gazprom"),
            make_profile("Kalashnikov Concern"),
            make_profile("Huawei Technologies Co. Ltd."),
        ]
        breakdown = sector_breakdown(profiles)
        assert breakdown["Banking & Finance"] == 1
        assert breakdown["Energy & Oil/Gas"] == 1
        assert breakdown["Defense & Aerospace"] == 1
        assert breakdown["Technology"] == 1

    def test_includes_unfetched_profiles_in_count(self):
        # Sector breakdown covers the full input list, not just successfully fetched profiles
        profiles = [
            make_profile("Sberbank", fetched=True),
            make_profile("Gazprom",  fetched=False),
        ]
        breakdown = sector_breakdown(profiles)
        assert breakdown.get("Banking & Finance", 0) == 1
        assert breakdown.get("Energy & Oil/Gas", 0) == 1

    def test_original_list_name_aliases_are_mapped(self):
        # These two names exist in the cache under their original (pre-alias) spellings
        profiles = [
            make_profile("Venezuelan State-Owned Oil Company (PDVSA)"),
            make_profile("Belorusskaya Kaliynaya Companya"),
        ]
        breakdown = sector_breakdown(profiles)
        assert breakdown.get("Energy & Oil/Gas", 0) == 1
        assert breakdown.get("Mining & Resources", 0) == 1


# ---------------------------------------------------------------------------
# country_breakdown
# ---------------------------------------------------------------------------

class TestCountryBreakdown:
    """Ensures entity presence per country is counted and sorted correctly."""

    def test_counts_entities_per_country(self):
        profiles = [
            make_profile("A", countries=["RUS", "USA"]),
            make_profile("B", countries=["RUS", "CHN"]),
            make_profile("C", countries=["USA"]),
        ]
        result = country_breakdown(profiles)
        assert result["RUS"] == 2
        assert result["USA"] == 2
        assert result["CHN"] == 1

    def test_result_sorted_highest_to_lowest(self):
        profiles = [
            make_profile("A", countries=["RUS"]),
            make_profile("B", countries=["RUS", "USA"]),
            make_profile("C", countries=["RUS", "USA", "CHN"]),
        ]
        counts = list(country_breakdown(profiles).values())
        assert counts == sorted(counts, reverse=True)

    def test_unfetched_profiles_excluded(self):
        profiles = [
            make_profile("A", countries=["RUS"], fetched=True),
            make_profile("B", countries=["CHN"], fetched=False),
        ]
        result = country_breakdown(profiles)
        assert "RUS" in result
        assert "CHN" not in result

    def test_entity_with_empty_countries_does_not_error(self):
        profiles = [make_profile("A", countries=[])]
        assert country_breakdown(profiles) == {}


# ---------------------------------------------------------------------------
# sanctions_list_breakdown
# ---------------------------------------------------------------------------

class TestSanctionsListBreakdown:
    """Ensures per-sanctions-list entity counts are aggregated correctly."""

    def test_counts_entities_per_list(self):
        profiles = [
            make_profile("A", sanctions_lists=["OFAC SDN", "EU Sanctions"]),
            make_profile("B", sanctions_lists=["OFAC SDN"]),
            make_profile("C", sanctions_lists=["EU Sanctions", "UK Sanctions"]),
        ]
        result = sanctions_list_breakdown(profiles)
        assert result["OFAC SDN"] == 2
        assert result["EU Sanctions"] == 2
        assert result["UK Sanctions"] == 1

    def test_entity_on_no_lists_does_not_contribute(self):
        profiles = [
            make_profile("A", sanctions_lists=[]),
            make_profile("B", sanctions_lists=["OFAC SDN"]),
        ]
        result = sanctions_list_breakdown(profiles)
        assert result.get("OFAC SDN") == 1
        assert len(result) == 1

    def test_unfetched_profiles_excluded(self):
        profiles = [
            make_profile("A", sanctions_lists=["OFAC SDN"], fetched=True),
            make_profile("B", sanctions_lists=["EU Sanctions"], fetched=False),
        ]
        result = sanctions_list_breakdown(profiles)
        assert "OFAC SDN" in result
        assert "EU Sanctions" not in result


# ---------------------------------------------------------------------------
# risk_flag_frequency
# ---------------------------------------------------------------------------

class TestRiskFlagFrequency:
    """Ensures risk flags are counted correctly and mapped to readable labels."""

    def test_counts_flags_across_entities(self):
        profiles = [
            make_profile("A", risk_flags={"sanctioned": "critical", "state_owned": "high"}),
            make_profile("B", risk_flags={"sanctioned": "critical"}),
        ]
        result = risk_flag_frequency(profiles)
        # "sanctioned" maps to "Sanctioned" in RISK_FLAG_LABELS
        assert result["Sanctioned"] == 2
        assert result["State-Owned Enterprise"] == 1

    def test_uses_human_readable_labels(self):
        profiles = [make_profile("A", risk_flags={"sanctioned_usa_ofac_sdn": "critical"})]
        result = risk_flag_frequency(profiles)
        # Raw key should not appear — only the readable label
        assert "sanctioned_usa_ofac_sdn" not in result
        assert "OFAC SDN" in result

    def test_unfetched_profiles_excluded(self):
        profiles = [
            make_profile("A", risk_flags={"sanctioned": "critical"}, fetched=True),
            make_profile("B", risk_flags={"sanctioned": "critical"}, fetched=False),
        ]
        result = risk_flag_frequency(profiles)
        assert result.get("Sanctioned") == 1


# ---------------------------------------------------------------------------
# risk_level_distribution
# ---------------------------------------------------------------------------

class TestRiskLevelDistribution:
    """Ensures risk severity counts are summed correctly across all entities."""

    def test_sums_counts_across_entities(self):
        profiles = [
            make_profile("A", risk_level_counts={"critical": 2, "high": 5, "elevated": 1, "relevant": 0}),
            make_profile("B", risk_level_counts={"critical": 1, "high": 3, "elevated": 0, "relevant": 2}),
        ]
        result = risk_level_distribution(profiles)
        assert result["critical"] == 3
        assert result["high"] == 8
        assert result["elevated"] == 1
        assert result["relevant"] == 2

    def test_empty_input_returns_all_zeros(self):
        result = risk_level_distribution([])
        assert all(v == 0 for v in result.values())

    def test_unfetched_profiles_excluded(self):
        profiles = [
            make_profile("A", risk_level_counts={"critical": 5, "high": 0, "elevated": 0, "relevant": 0}, fetched=True),
            make_profile("B", risk_level_counts={"critical": 99, "high": 0, "elevated": 0, "relevant": 0}, fetched=False),
        ]
        assert risk_level_distribution(profiles)["critical"] == 5


# ---------------------------------------------------------------------------
# top_entities_by_degree
# ---------------------------------------------------------------------------

class TestTopEntitiesByDegree:
    """Ensures network degree ranking logic is correct."""

    def test_returns_exactly_n_results(self):
        profiles = [make_profile(f"E{i}", degree=i * 10) for i in range(20)]
        assert len(top_entities_by_degree(profiles, n=5)) == 5

    def test_sorted_by_degree_descending(self):
        profiles = [
            make_profile("Low",  degree=10),
            make_profile("High", degree=500),
            make_profile("Mid",  degree=100),
        ]
        result = top_entities_by_degree(profiles, n=3)
        degrees = [e["degree"] for e in result]
        assert degrees == sorted(degrees, reverse=True)

    def test_top_result_is_highest_degree(self):
        profiles = [
            make_profile("A", degree=50),
            make_profile("B", degree=9999),
            make_profile("C", degree=200),
        ]
        result = top_entities_by_degree(profiles, n=1)
        assert result[0]["name"] == "B"
        assert result[0]["degree"] == 9999

    def test_result_contains_required_fields(self):
        profiles = [make_profile("Sberbank", degree=1000)]
        result = top_entities_by_degree(profiles, n=1)[0]
        for field in ("name", "degree", "total_relationships", "sanctioned", "sector"):
            assert field in result

    def test_n_larger_than_dataset_returns_all(self):
        profiles = [make_profile(f"E{i}") for i in range(3)]
        assert len(top_entities_by_degree(profiles, n=100)) == 3


# ---------------------------------------------------------------------------
# jurisdiction_exposure
# ---------------------------------------------------------------------------

class TestJurisdictionExposure:
    """Ensures entities are correctly ranked by cross-border presence."""

    def test_sorted_by_country_count_descending(self):
        profiles = [
            make_profile("A", countries=["RUS"]),
            make_profile("B", countries=["RUS", "USA", "CHN"]),
            make_profile("C", countries=["RUS", "USA"]),
        ]
        counts = [e["country_count"] for e in jurisdiction_exposure(profiles)]
        assert counts == sorted(counts, reverse=True)

    def test_country_count_matches_countries_list_length(self):
        profiles = [make_profile("A", countries=["RUS", "USA", "CHN"])]
        result = jurisdiction_exposure(profiles)[0]
        assert result["country_count"] == 3
        assert result["countries"] == ["RUS", "USA", "CHN"]

    def test_unfetched_profiles_excluded(self):
        profiles = [
            make_profile("A", countries=["RUS", "USA"], fetched=True),
            make_profile("B", countries=["RUS", "USA", "CHN", "DEU"], fetched=False),
        ]
        result = jurisdiction_exposure(profiles)
        assert len(result) == 1
        assert result[0]["name"] == "A"


# ---------------------------------------------------------------------------
# sanctions_coverage_per_entity
# ---------------------------------------------------------------------------

class TestSanctionsCoveragePerEntity:
    """Ensures per-entity sanctions list counts are calculated and filtered correctly."""

    def test_only_includes_sanctioned_entities(self):
        profiles = [
            make_profile("A", sanctioned=True,  sanctions_lists=["L1", "L2"]),
            make_profile("B", sanctioned=False, sanctions_lists=[]),
        ]
        names = [e["name"] for e in sanctions_coverage_per_entity(profiles)]
        assert "A" in names
        assert "B" not in names

    def test_sorted_by_list_count_descending(self):
        profiles = [
            make_profile("A", sanctioned=True, sanctions_lists=["L1"]),
            make_profile("B", sanctioned=True, sanctions_lists=["L1", "L2", "L3"]),
            make_profile("C", sanctioned=True, sanctions_lists=["L1", "L2"]),
        ]
        counts = [e["list_count"] for e in sanctions_coverage_per_entity(profiles)]
        assert counts == sorted(counts, reverse=True)

    def test_list_count_matches_sanctions_lists_length(self):
        profiles = [make_profile("A", sanctioned=True, sanctions_lists=["L1", "L2", "L3"])]
        result = sanctions_coverage_per_entity(profiles)
        assert result[0]["list_count"] == 3

    def test_result_contains_required_fields(self):
        profiles = [make_profile("A", sanctioned=True, sanctions_lists=["L1"])]
        result = sanctions_coverage_per_entity(profiles)[0]
        for field in ("name", "list_count", "sanctions_lists", "sector"):
            assert field in result

    def test_unfetched_sanctioned_entity_excluded(self):
        profiles = [
            make_profile("A", sanctioned=True, sanctions_lists=["L1"], fetched=True),
            make_profile("B", sanctioned=True, sanctions_lists=["L1", "L2"], fetched=False),
        ]
        result = sanctions_coverage_per_entity(profiles)
        assert len(result) == 1
        assert result[0]["name"] == "A"
