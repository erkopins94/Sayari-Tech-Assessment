"""
analytics.py — pure functions that transform the profiles cache into macro-level
insights for the Streamlit dashboard. Every function here is stateless: it takes
the profiles list and returns plain Python dicts or lists that Plotly and
Streamlit can consume directly.

No API calls are made here. All input comes from data/profiles.json.
"""

from collections import Counter

# ---------------------------------------------------------------------------
# Sector mapping
# Entities in List 1 are manually classified into sectors based on their
# known business activities. Sayari does not provide sector/industry tags
# directly, so this mapping is derived from public knowledge of each entity.
# ---------------------------------------------------------------------------

SECTOR_MAP = {
    # Banking & Finance
    "Russian Direct Investment Fund": "Banking & Finance",
    "Sberbank": "Banking & Finance",
    "VTB Bank": "Banking & Finance",
    "State Development Bank VEB.RF": "Banking & Finance",
    "Promsvyazbank": "Banking & Finance",
    "Alfa-Bank": "Banking & Finance",
    "Bank Rossiya": "Banking & Finance",
    "Novikombank": "Banking & Finance",
    "Sovcombank": "Banking & Finance",
    "Bank Otkritie": "Banking & Finance",
    # Energy & Oil/Gas
    "Rosneft": "Energy & Oil/Gas",
    "Gazprom": "Energy & Oil/Gas",
    "National Iranian Oil Company": "Energy & Oil/Gas",
    "PDVSA": "Energy & Oil/Gas",
    "Venezuelan State-Owned Oil Company (PDVSA)": "Energy & Oil/Gas",  # original list name
    "Transneft": "Energy & Oil/Gas",
    "Belneftegaz": "Energy & Oil/Gas",
    # Defense & Aerospace
    "Rostec": "Defense & Aerospace",
    "Almaz-Antey": "Defense & Aerospace",
    "Uralvagonzavod": "Defense & Aerospace",
    "Kalashnikov Concern": "Defense & Aerospace",
    "NPO High Precision Systems": "Defense & Aerospace",
    "Tactical Missiles Corporation JSC": "Defense & Aerospace",
    "NPK Tekhmash OAO": "Defense & Aerospace",
    "Molot-Oruzhie": "Defense & Aerospace",
    "Rustec": "Defense & Aerospace",
    "Rosoboronexport": "Defense & Aerospace",
    "Sukhoi Company": "Defense & Aerospace",
    "Irkut Corporation": "Defense & Aerospace",
    "MiG Corporation": "Defense & Aerospace",
    "Tupolev PJSC": "Defense & Aerospace",
    "Sevmash": "Defense & Aerospace",
    "Admiralty Shipyards": "Defense & Aerospace",
    "Zvezdochka Shipyard": "Defense & Aerospace",
    "Baltic Shipyard": "Defense & Aerospace",
    "United Aircraft Corporation": "Defense & Aerospace",
    "United Shipbuilding Corporation": "Defense & Aerospace",
    # Technology
    "Huawei Technologies Co. Ltd.": "Technology",
    "ZTE Corporation": "Technology",
    "Hangzhou Hikvision Digital Technology Co. Ltd.": "Technology",
    # Mining & Resources
    "Belaruskali OAO": "Mining & Resources",
    "Belarusian Potash Company": "Mining & Resources",
    "Belorusskaya Kaliynaya Companya": "Mining & Resources",  # original list name
    "Belnauchcompositit": "Mining & Resources",
    # Transport & Industrial
    "Russian Railways": "Transport & Industrial",
    "Kamaz": "Transport & Industrial",
    "Power Machines": "Transport & Industrial",
    "Syrian Arab Airlines": "Transport & Industrial",
    # State Trade & Other
    "Korea Mining Development Trading Corporation": "State Trade & Other",
    "Cubametales": "State Trade & Other",
    "Myanmar Economic Corporation": "State Trade & Other",
    "Myanmar Economic Holdings Limited": "State Trade & Other",
}

# Human-readable labels for the risk flag keys returned by the Sayari API
RISK_FLAG_LABELS = {
    "sanctioned": "Sanctioned",
    "sanctioned_usa_ofac_sdn": "OFAC SDN",
    "sanctioned_usa_ofac_non_sdn": "OFAC Non-SDN",
    "sanctioned_other": "Other Sanctions",
    "export_controls": "Export Controls",
    "export_controls_other": "Export Controls (Other)",
    "state_owned": "State-Owned Enterprise",
    "pep": "Politically Exposed Person",
    "regulatory_action": "Regulatory Action",
    "reputational_risk_other": "Reputational Risk",
    "meu_list_contractors": "Military End-Use Contractor",
    "cpi_score": "High CPI Risk Country",
    "basel_aml": "High Basel AML Risk",
}


def _fetched(profiles: list[dict]) -> list[dict]:
    """Returns only profiles that were successfully fetched from the API.
    Unresolved or failed profiles are excluded from analytics to avoid
    skewing counts with empty records."""
    return [p for p in profiles if p.get("fetched")]


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def summary_stats(profiles: list[dict]) -> dict:
    """
    Computes the headline numbers shown at the top of the dashboard.

    These give a viewer an immediate at-a-glance understanding of the dataset
    before they dive into the individual charts.
    """
    ok = _fetched(profiles)

    sanctioned_count = sum(1 for p in ok if p["sanctioned"])
    state_owned_count = sum(1 for p in ok if "state_owned" in p["risk_flags"])
    export_controls_count = sum(1 for p in ok if "export_controls" in p["risk_flags"])

    # Collect every unique country code across all entity profiles
    all_countries = set()
    for p in ok:
        all_countries.update(p["countries"])

    total_connections = sum(p["degree"] for p in ok)

    # Average number of sanctions lists each entity appears on
    avg_sanctions_lists = (
        sum(len(p["sanctions_lists"]) for p in ok) / len(ok) if ok else 0
    )

    return {
        "total_entities": len(profiles),
        "fetched": len(ok),
        "sanctioned_count": sanctioned_count,
        "sanctioned_pct": round(sanctioned_count / len(ok) * 100, 1) if ok else 0,
        "state_owned_count": state_owned_count,
        "export_controls_count": export_controls_count,
        "countries_represented": len(all_countries),
        "total_network_connections": total_connections,
        "avg_sanctions_lists": round(avg_sanctions_lists, 1),
    }


# ---------------------------------------------------------------------------
# Country breakdown
# ---------------------------------------------------------------------------

def country_breakdown(profiles: list[dict]) -> dict[str, int]:
    """
    Counts how many entities have a presence in each country.

    An entity's 'countries' field includes every jurisdiction it is registered,
    incorporated, or has known operations in — not just its home country. This
    reveals how far the reach of these entities extends beyond their origin nations.
    """
    counter: Counter = Counter()
    for p in _fetched(profiles):
        for country in p["countries"]:
            counter[country] += 1

    # Return sorted highest to lowest for easy chart rendering
    return dict(counter.most_common())


# ---------------------------------------------------------------------------
# Sanctions list breakdown
# ---------------------------------------------------------------------------

def sanctions_list_breakdown(profiles: list[dict]) -> dict[str, int]:
    """
    Counts how many entities appear on each named sanctions list.

    Because the same entity can appear on multiple lists (e.g. both OFAC SDN
    and EU Sanctions), totals across lists will exceed the entity count.
    This chart illustrates the breadth of international coordinated sanctions
    coverage across the dataset.
    """
    counter: Counter = Counter()
    for p in _fetched(profiles):
        for sl in p["sanctions_lists"]:
            counter[sl] += 1

    return dict(counter.most_common())


# ---------------------------------------------------------------------------
# Risk flag frequency
# ---------------------------------------------------------------------------

def risk_flag_frequency(profiles: list[dict]) -> dict[str, int]:
    """
    Counts how many entities carry each direct risk flag.

    Uses human-readable labels from RISK_FLAG_LABELS so the chart is
    immediately legible to a non-technical audience without needing to
    decode Sayari's internal key naming convention.
    """
    counter: Counter = Counter()
    for p in _fetched(profiles):
        for flag in p["risk_flags"]:
            label = RISK_FLAG_LABELS.get(flag, flag)
            counter[label] += 1

    return dict(counter.most_common())


# ---------------------------------------------------------------------------
# Aggregate risk level distribution
# ---------------------------------------------------------------------------

def risk_level_distribution(profiles: list[dict]) -> dict[str, int]:
    """
    Sums the total number of risk flags at each severity level (critical, high,
    elevated, relevant) across the entire dataset.

    This includes both direct and network-level flags, giving a sense of the
    overall risk density of the entity set — not just whether individual
    entities are sanctioned, but how deeply embedded they are in the risk graph.
    """
    totals = {"critical": 0, "high": 0, "elevated": 0, "relevant": 0}
    for p in _fetched(profiles):
        for level, count in p["risk_level_counts"].items():
            if level in totals:
                totals[level] += count

    return totals


# ---------------------------------------------------------------------------
# Sector breakdown
# ---------------------------------------------------------------------------

def sector_breakdown(profiles: list[dict]) -> dict[str, int]:
    """
    Groups all 50 input entities by industry sector using the SECTOR_MAP above.

    Sector classification is manual because Sayari does not provide SIC/NAICS
    codes — the mapping is based on each entity's publicly known business
    activities. Entities that are unresolved still count toward their sector
    so the totals always reflect the full input list.
    """
    counter: Counter = Counter()
    for p in profiles:
        sector = SECTOR_MAP.get(p["input_name"], "Other")
        counter[sector] += 1

    return dict(counter.most_common())


# ---------------------------------------------------------------------------
# Top entities by network degree
# ---------------------------------------------------------------------------

def top_entities_by_degree(profiles: list[dict], n: int = 10) -> list[dict]:
    """
    Returns the top N entities ranked by network degree (total number of known
    relationships in the Sayari graph).

    High-degree entities are the most deeply embedded in corporate ownership
    and trade networks, making them the most complex to investigate and the
    highest-value targets for sanctions enforcement or due diligence.
    """
    ok = _fetched(profiles)
    sorted_profiles = sorted(ok, key=lambda p: p["degree"], reverse=True)

    return [
        {
            "name": p["input_name"],
            "degree": p["degree"],
            "total_relationships": p["total_relationships"],
            "sanctioned": p["sanctioned"],
            "sector": SECTOR_MAP.get(p["input_name"], "Other"),
        }
        for p in sorted_profiles[:n]
    ]


# ---------------------------------------------------------------------------
# Multi-jurisdiction exposure
# ---------------------------------------------------------------------------

def jurisdiction_exposure(profiles: list[dict]) -> list[dict]:
    """
    Returns all entities sorted by the number of distinct countries they appear
    in, from most to least.

    A wide jurisdiction footprint indicates complex cross-border corporate
    structures, which increases sanctions evasion risk and makes enforcement
    more difficult. This is one of the key signals a compliance team would
    focus on.
    """
    ok = _fetched(profiles)
    sorted_profiles = sorted(ok, key=lambda p: len(p["countries"]), reverse=True)

    return [
        {
            "name": p["input_name"],
            "country_count": len(p["countries"]),
            "countries": p["countries"],
            "sanctioned": p["sanctioned"],
        }
        for p in sorted_profiles
    ]


# ---------------------------------------------------------------------------
# Sanctions list coverage per entity
# ---------------------------------------------------------------------------

def sanctions_coverage_per_entity(profiles: list[dict]) -> list[dict]:
    """
    Returns each entity alongside the number of sanctions lists it appears on,
    sorted from most to least.

    An entity appearing on many lists simultaneously signals coordinated
    international sanctions — a strong indicator of perceived severity.
    This helps a client prioritize which entities to focus on first.
    """
    ok = _fetched(profiles)
    sorted_profiles = sorted(
        ok, key=lambda p: len(p["sanctions_lists"]), reverse=True
    )

    return [
        {
            "name": p["input_name"],
            "list_count": len(p["sanctions_lists"]),
            "sanctions_lists": p["sanctions_lists"],
            "sector": SECTOR_MAP.get(p["input_name"], "Other"),
        }
        for p in sorted_profiles
        if p["sanctioned"]  # only include entities that are actually sanctioned
    ]
