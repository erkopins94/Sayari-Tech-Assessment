"""
tools.py — Data-access functions exposed to the LLM as callable tools.

Each function accepts structured inputs, queries the profiles list, and returns
a plain dict the LLM can read and summarise into a natural language response.

Design principles:
  - Pure reads: no side effects, no API calls, no file I/O
  - Fuzzy matching: users rarely type exact entity names, so every tool that
    accepts a name routes through _fuzzy_match() before querying profiles
  - Structured returns: always dicts or lists — never raw objects
  - Graceful failure: return a clear {"error": True, "message": ...} dict
    rather than raising exceptions, so the LLM can relay the issue naturally
  - Profiles are passed in: tools do not load data themselves, keeping them
    stateless, testable, and consistent with analytics.py's design
"""

import difflib
from collections.abc import Callable

from analytics import (
    RISK_FLAG_LABELS,
    SECTOR_MAP,
    sanctions_list_breakdown,
    sector_breakdown,
    summary_stats,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetched_profiles(profiles: list[dict]) -> list[dict]:
    """Return only the profiles that were successfully fetched from Sayari."""
    return [p for p in profiles if p.get("fetched")]


def _build_name_index(profiles: list[dict]) -> dict[str, dict]:
    """
    Build a normalised-name → profile lookup used by fuzzy matching.

    Both input_name (the name we searched for) and matched_name (the name
    Sayari resolved to) are indexed so that abbreviations and aliases both
    resolve to the same profile.
    """
    index = {}
    for p in profiles:
        index[p["input_name"].lower()] = p
        if p.get("matched_name"):
            index[p["matched_name"].lower()] = p
    return index


def _fuzzy_match(
    name: str,
    profiles: list[dict],
    index: dict[str, dict] | None = None,
) -> dict | None:
    """
    Find the best-matching entity profile for a user-supplied name.

    Strategy:
      1. Exact match (case-insensitive) — fastest path, handles most cases
      2. difflib.get_close_matches — handles abbreviations, typos, partial names
         with a cutoff of 0.6 (0 = accept anything, 1 = exact only)

    Args:
        name:     The entity name to search for.
        profiles: Full profiles list — used to build the index if one isn't supplied.
        index:    Optional pre-built name index from _build_name_index(). Pass this
                  when calling _fuzzy_match in a loop (e.g. compare_entities) to
                  avoid rebuilding the same index on every iteration.

    Returns None if no match clears the similarity threshold so callers can
    return a clean "not found" message rather than a wrong result.
    """
    # Build the index only if the caller hasn't supplied one already
    if index is None:
        index = _build_name_index(profiles)

    query = name.lower().strip()

    # Fast path — exact case-insensitive match
    if query in index:
        return index[query]

    # Fuzzy fallback — handles "rosneft", "Russian Railway", "ZTE corp", etc.
    matches = difflib.get_close_matches(query, list(index.keys()), n=1, cutoff=0.6)
    if matches:
        return index[matches[0]]

    return None


def _format_profile(profile: dict) -> dict:
    """
    Convert a raw profile dict into the clean, LLM-readable shape returned
    by all tools. Translates raw API risk flag keys into readable labels and
    adds sector classification.

    Keeping this in one place means every tool returns a consistent structure.
    """
    return {
        "input_name":          profile["input_name"],
        "matched_name":        profile["matched_name"],
        "entity_type":         profile["entity_type"],
        "sector":              SECTOR_MAP.get(profile["input_name"], "Other"),
        "sanctioned":          profile["sanctioned"],
        "state_owned":         "state_owned" in profile["risk_flags"],
        "pep":                 profile["pep"],
        "countries":           profile["countries"],
        "country_count":       len(profile["countries"]),
        "sanctions_lists":     profile["sanctions_lists"],
        "sanctions_list_count": len(profile["sanctions_lists"]),
        "risk_flags": {
            # Translate raw API keys ("state_owned") → readable labels ("State Owned Enterprise")
            RISK_FLAG_LABELS.get(k, k): v
            for k, v in profile["risk_flags"].items()
        },
        "risk_level_counts":   profile["risk_level_counts"],
        "network_degree":      profile["degree"],
    }


# ---------------------------------------------------------------------------
# Tool 1 — get_entity
# ---------------------------------------------------------------------------

def get_entity(name: str, profiles: list[dict]) -> dict:
    """
    Look up a single entity by name and return its full analytics profile.

    This is the primary tool for any question about a specific entity:
    sanctions status, which lists it appears on, country footprint, risk flags,
    network degree, sector classification, and state ownership.

    Args:
        name:     Entity name as typed by the user. Fuzzy matched against
                  both the input name and the Sayari-resolved name.
        profiles: Full profiles list loaded from the cache.

    Returns:
        On success — a dict with the entity's full profile.
        On failure — {"error": True, "message": "..."} describing why.

    Example questions this tool covers:
        "Is ZTE sanctioned?"
        "What sanctions lists is Rosneft on?"
        "How many countries does Russian Railways operate in?"
        "Tell me about Minfin Russia"
    """
    profile = _fuzzy_match(name, profiles)

    if not profile:
        return {
            "error":   True,
            "message": (
                f"No entity matching '{name}' found in the dataset. "
                "The dataset contains 49 resolved entities. "
                "Try a different spelling, abbreviation, or partial name."
            ),
        }

    # Entity was found in the name list but Sayari returned no data for it
    if not profile.get("fetched"):
        return {
            "error":   True,
            "message": (
                f"'{profile['input_name']}' is in the dataset but could not be "
                "resolved in Sayari — no profile data is available for this entity."
            ),
        }

    return {"error": False, **_format_profile(profile)}


# ---------------------------------------------------------------------------
# Country name → ISO-3 code lookup (used by filter_entities)
#
# app.py holds the inverse mapping (ISO-3 → display name) for chart labels.
# This dict handles the reverse direction so users can type "Russia" instead
# of "RUS" when filtering. Covers every country code present in the dataset.
# ---------------------------------------------------------------------------

_COUNTRY_CODES: dict[str, str] = {
    "russia": "RUS", "united states": "USA", "us": "USA", "usa": "USA",
    "cyprus": "CYP", "china": "CHN", "germany": "DEU", "belarus": "BLR",
    "kazakhstan": "KAZ", "canada": "CAN", "netherlands": "NLD",
    "uae": "ARE", "united arab emirates": "ARE", "hong kong": "HKG",
    "ukraine": "UKR", "australia": "AUS", "myanmar": "MMR",
    "united kingdom": "GBR", "uk": "GBR", "switzerland": "CHE",
    "france": "FRA", "singapore": "SGP", "austria": "AUT",
    "belgium": "BEL", "luxembourg": "LUX", "ireland": "IRL",
    "czech republic": "CZE", "poland": "POL", "finland": "FIN",
    "sweden": "SWE", "denmark": "DNK", "norway": "NOR", "latvia": "LVA",
    "estonia": "EST", "lithuania": "LTU", "georgia": "GEO",
    "armenia": "ARM", "azerbaijan": "AZE", "uzbekistan": "UZB",
    "turkmenistan": "TKM", "turkey": "TUR", "iran": "IRN", "iraq": "IRQ",
    "syria": "SYR", "north korea": "PRK", "venezuela": "VEN",
    "cuba": "CUB", "panama": "PAN", "bahamas": "BHS",
    "british virgin islands": "VGB", "bvi": "VGB", "malta": "MLT",
    "gibraltar": "GIB", "isle of man": "IMN", "liechtenstein": "LIE",
    "monaco": "MCO", "san marino": "SMR",
}


# Precomputed once at import time — SECTOR_MAP never changes at runtime so
# there is no reason to recompute set(SECTOR_MAP.values()) on every call.
_KNOWN_SECTORS: frozenset[str] = frozenset(SECTOR_MAP.values())


def _resolve_sector(sector: str) -> str | None:
    """
    Resolve a user-supplied sector string to a canonical sector label.

    Case-insensitive partial match against _KNOWN_SECTORS so that "defense"
    resolves to "Defense & Aerospace", "banking" to "Banking & Finance", etc.

    Returns None if no sector contains the input as a substring.
    """
    query = sector.lower()
    return next((s for s in _KNOWN_SECTORS if query in s.lower()), None)


def _resolve_country(country: str) -> str | None:
    """
    Resolve a user-supplied country string to an ISO-3 code.

    Accepts:
      - ISO-3 codes directly ("RUS", "CHN") — returned as-is after uppercasing
      - Common English names and abbreviations ("Russia", "UK", "UAE")

    Returns None if the input cannot be resolved, so filter_entities can
    return a helpful error rather than silently returning zero results.
    """
    normalised = country.strip().lower()

    # If the user typed a 3-letter code, treat it as ISO-3 directly
    if len(normalised) == 3 and normalised.upper().isalpha():
        return normalised.upper()

    return _COUNTRY_CODES.get(normalised)


# ---------------------------------------------------------------------------
# Tool 2 — filter_entities
# ---------------------------------------------------------------------------

def filter_entities(
    profiles: list[dict],
    sector: str | None = None,
    country: str | None = None,
    sanctions_list: str | None = None,
    risk_flag: str | None = None,
    sanctioned: bool | None = None,
    state_owned: bool | None = None,
) -> dict:
    """
    Filter the dataset by one or more criteria and return matching entities.

    All parameters are optional — any combination can be used together (AND
    logic: an entity must satisfy every supplied filter to be included).

    Args:
        profiles:       Full profiles list loaded from the cache.
        sector:         Sector name, e.g. "Defense", "Banking". Case-insensitive
                        partial match against known sector labels.
        country:        Country name ("Russia") or ISO-3 code ("RUS"). Matches
                        entities that have any presence in that country.
        sanctions_list: Sanctions list name, e.g. "OFAC SDN". Fuzzy matched
                        against the full list of known sanctions list names.
        risk_flag:      Readable risk flag label, e.g. "State Owned Enterprise",
                        or raw API key, e.g. "state_owned".
        sanctioned:     True → only sanctioned entities. False → only unsanctioned.
        state_owned:    True → only state-owned enterprises. False → only private.

    Returns:
        On success — {"error": False, "count": N, "filters_applied": {...},
                      "entities": [summary dicts]}
        On failure — {"error": True, "message": "..."} for unresolvable inputs.

    Example questions this tool covers:
        "Show me all defense sector entities"
        "Which entities operate in China?"
        "Which entities are on the OFAC SDN list?"
        "Show me state-owned enterprises that are not sanctioned"
        "Which entities have export controls?"
    """
    fetched = _fetched_profiles(profiles)
    filters_applied = {}

    # --- Sector filter ---
    # Routed through _resolve_sector() for consistent partial matching
    if sector is not None:
        matched_sector = _resolve_sector(sector)
        if not matched_sector:
            known_sectors = sorted(_KNOWN_SECTORS)
            return {
                "error":   True,
                "message": (
                    f"Unknown sector '{sector}'. "
                    f"Known sectors: {', '.join(known_sectors)}."
                ),
            }
        fetched = [p for p in fetched if SECTOR_MAP.get(p["input_name"], "Other") == matched_sector]
        filters_applied["sector"] = matched_sector

    # --- Country filter ---
    # Resolves readable names to ISO-3 codes before filtering
    if country is not None:
        iso3 = _resolve_country(country)
        if not iso3:
            return {
                "error":   True,
                "message": (
                    f"Could not resolve '{country}' to a known country. "
                    "Try the ISO-3 code (e.g. 'RUS') or the full English name."
                ),
            }
        fetched = [p for p in fetched if iso3 in p["countries"]]
        filters_applied["country"] = iso3

    # --- Sanctions list filter ---
    # Collect every distinct list name from the dataset, then fuzzy match the
    # user's input so they don't need to type the exact official list name
    if sanctions_list is not None:
        all_lists = {sl for p in fetched for sl in p["sanctions_lists"]}
        list_query = sanctions_list.lower().strip()
        # Try substring match first (fast, handles common abbreviations)
        match = next((sl for sl in all_lists if list_query in sl.lower()), None)
        # Fall back to difflib if no substring match
        if not match:
            close = difflib.get_close_matches(list_query, [sl.lower() for sl in all_lists], n=1, cutoff=0.5)
            if close:
                match = next(sl for sl in all_lists if sl.lower() == close[0])
        if not match:
            return {
                "error":   True,
                "message": (
                    f"Could not match '{sanctions_list}' to a known sanctions list. "
                    f"Known lists include: {', '.join(sorted(all_lists)[:5])} and more."
                ),
            }
        fetched = [p for p in fetched if match in p["sanctions_lists"]]
        filters_applied["sanctions_list"] = match

    # --- Risk flag filter ---
    # Accepts both readable labels ("State Owned Enterprise") and raw API keys
    # ("state_owned") by checking both RISK_FLAG_LABELS and its inverse
    if risk_flag is not None:
        label_to_key = {v.lower(): k for k, v in RISK_FLAG_LABELS.items()}
        flag_query = risk_flag.lower().strip()
        # Check readable label first, then raw key
        raw_key = label_to_key.get(flag_query) or (risk_flag if risk_flag in RISK_FLAG_LABELS else None)
        if not raw_key:
            # Fuzzy match against readable labels as a last resort
            close = difflib.get_close_matches(flag_query, list(label_to_key.keys()), n=1, cutoff=0.5)
            raw_key = label_to_key.get(close[0]) if close else None
        if not raw_key:
            return {
                "error":   True,
                "message": (
                    f"Could not match '{risk_flag}' to a known risk flag. "
                    f"Known flags: {', '.join(RISK_FLAG_LABELS.values())}."
                ),
            }
        fetched = [p for p in fetched if raw_key in p["risk_flags"]]
        filters_applied["risk_flag"] = RISK_FLAG_LABELS.get(raw_key, raw_key)

    # --- Boolean filters ---
    if sanctioned is not None:
        fetched = [p for p in fetched if p["sanctioned"] == sanctioned]
        filters_applied["sanctioned"] = sanctioned

    if state_owned is not None:
        fetched = [p for p in fetched if ("state_owned" in p["risk_flags"]) == state_owned]
        filters_applied["state_owned"] = state_owned

    # --- Build summary rows (abbreviated profile — full profile would be too verbose for a list) ---
    entities = [
        {
            "name":                 p["input_name"],
            "sector":               SECTOR_MAP.get(p["input_name"], "Other"),
            "sanctioned":           p["sanctioned"],
            "state_owned":          "state_owned" in p["risk_flags"],
            "country_count":        len(p["countries"]),
            "sanctions_list_count": len(p["sanctions_lists"]),
            "network_degree":       p["degree"],
        }
        for p in sorted(fetched, key=lambda p: p["input_name"])
    ]

    return {
        "error":           False,
        "count":           len(entities),
        "filters_applied": filters_applied,
        "entities":        entities,
    }


# ---------------------------------------------------------------------------
# Metric map for rank_entities
#
# Maps normalised user-supplied metric names → (display label, extractor fn).
# The extractor receives a raw profile dict and returns a numeric value.
# Multiple aliases per metric let users say "connections", "degree", or
# "network degree" and all resolve to the same ranking logic.
# ---------------------------------------------------------------------------

_METRIC_MAP: dict[str, tuple[str, Callable[[dict], int]]] = {
    "degree":               ("Network Degree",       lambda p: p["degree"]),
    "network degree":       ("Network Degree",       lambda p: p["degree"]),
    "connections":          ("Network Degree",       lambda p: p["degree"]),
    "network connections":  ("Network Degree",       lambda p: p["degree"]),
    "sanctions lists":      ("Sanctions List Count", lambda p: len(p["sanctions_lists"])),
    "sanctions list count": ("Sanctions List Count", lambda p: len(p["sanctions_lists"])),
    "lists":                ("Sanctions List Count", lambda p: len(p["sanctions_lists"])),
    "countries":            ("Country Count",        lambda p: len(p["countries"])),
    "country count":        ("Country Count",        lambda p: len(p["countries"])),
    "geographic footprint": ("Country Count",        lambda p: len(p["countries"])),
    "critical":             ("Critical Risk Flags",  lambda p: p["risk_level_counts"].get("critical", 0)),
    "critical flags":       ("Critical Risk Flags",  lambda p: p["risk_level_counts"].get("critical", 0)),
    "high":                 ("High Risk Flags",      lambda p: p["risk_level_counts"].get("high", 0)),
    "high flags":           ("High Risk Flags",      lambda p: p["risk_level_counts"].get("high", 0)),
    "elevated":             ("Elevated Risk Flags",  lambda p: p["risk_level_counts"].get("elevated", 0)),
    "elevated flags":       ("Elevated Risk Flags",  lambda p: p["risk_level_counts"].get("elevated", 0)),
}


# ---------------------------------------------------------------------------
# Tool 3 — rank_entities
# ---------------------------------------------------------------------------

def rank_entities(
    profiles: list[dict],
    metric: str,
    n: int = 10,
    ascending: bool = False,
    sector: str | None = None,
) -> dict:
    """
    Rank entities by a numeric metric and return the top (or bottom) N results.

    Args:
        profiles:  Full profiles list loaded from the cache.
        metric:    What to rank by. Accepts natural language aliases:
                     "degree" / "connections" / "network degree"
                     "sanctions lists" / "lists"
                     "countries" / "geographic footprint"
                     "critical" / "critical flags"
                     "high" / "high flags"
                     "elevated" / "elevated flags"
        n:         How many results to return (default 10).
        ascending: False (default) = highest first. True = lowest first.
        sector:    Optional sector filter applied before ranking. Same partial
                   matching as filter_entities ("defense", "banking", etc.).

    Returns:
        On success — {"error": False, "metric": label, "ascending": bool,
                      "count": N, "ranking": [ranked rows]}
        On failure — {"error": True, "message": "..."}

    Example questions this tool covers:
        "Which entity has the most sanctions lists?"
        "Top 5 entities by network connections"
        "Which defense firms have the widest geographic footprint?"
        "Rank banking entities by number of countries"
        "Which entities have the fewest critical risk flags?"
    """
    # --- Resolve metric name ---
    # Normalise input and try exact lookup first, then fuzzy match
    query = metric.lower().strip()
    entry = _METRIC_MAP.get(query)

    if not entry:
        close = difflib.get_close_matches(query, list(_METRIC_MAP.keys()), n=1, cutoff=0.5)
        entry = _METRIC_MAP.get(close[0]) if close else None

    if not entry:
        return {
            "error":   True,
            "message": (
                f"Unknown metric '{metric}'. "
                f"Valid metrics: {', '.join(sorted({label for label, _ in _METRIC_MAP.values()}))}."
            ),
        }

    display_label, extractor = entry

    # --- Optional sector pre-filter ---
    fetched = _fetched_profiles(profiles)
    if sector is not None:
        matched_sector = _resolve_sector(sector)
        if not matched_sector:
            known_sectors = sorted(_KNOWN_SECTORS)
            return {
                "error":   True,
                "message": (
                    f"Unknown sector '{sector}'. "
                    f"Known sectors: {', '.join(known_sectors)}."
                ),
            }
        fetched = [p for p in fetched if SECTOR_MAP.get(p["input_name"], "Other") == matched_sector]

    # --- Sort and slice ---
    ranked = sorted(fetched, key=extractor, reverse=not ascending)[:n]

    rows = [
        {
            "rank":                 i + 1,
            "name":                 p["input_name"],
            "sector":               SECTOR_MAP.get(p["input_name"], "Other"),
            "value":                extractor(p),
            "sanctioned":           p["sanctioned"],
            "state_owned":          "state_owned" in p["risk_flags"],
            "country_count":        len(p["countries"]),
            "sanctions_list_count": len(p["sanctions_lists"]),
        }
        for i, p in enumerate(ranked)
    ]

    return {
        "error":     False,
        "metric":    display_label,
        "ascending": ascending,
        "sector":    sector,
        "count":     len(rows),
        "ranking":   rows,
    }


# ---------------------------------------------------------------------------
# Tool 4 — compare_entities
# ---------------------------------------------------------------------------

def compare_entities(names: list[str], profiles: list[dict]) -> dict:
    """
    Compare two or more entities side by side across all key metrics.

    Each name is fuzzy matched so users don't need exact spelling. Entities
    that cannot be resolved are reported in a `not_found` list rather than
    blocking the whole comparison — the LLM can acknowledge the gap and still
    return results for the entities that did resolve.

    Args:
        names:    List of entity names as typed by the user (2–10 entities).
                  Fuzzy matched against both input_name and matched_name fields.
        profiles: Full profiles list loaded from the cache.

    Returns:
        On success — {
            "error": False,
            "entities_compared": N,
            "not_found": [...],        # names that couldn't be resolved
            "comparison": [            # one full profile dict per resolved entity
                { full _format_profile() output }, ...
            ]
        }
        On failure — {"error": True, "message": "..."} when fewer than 2
        names are supplied or none of the names resolve.

    Example questions this tool covers:
        "Compare ZTE and Huawei"
        "How do Rosneft and Gazprom differ?"
        "Compare all Iranian entities in the dataset"
        "Which is more heavily sanctioned — Russian Railways or Rostec?"
    """
    # --- Input validation ---
    if len(names) < 2:
        return {
            "error":   True,
            "message": "Please provide at least two entity names to compare.",
        }

    # Cap at 10 to keep LLM responses manageable
    if len(names) > 10:
        return {
            "error":   True,
            "message": (
                f"Too many entities to compare at once ({len(names)} provided). "
                "Please compare up to 10 entities at a time."
            ),
        }

    # --- Resolve each name, tracking hits and misses separately ---
    # Build the name index once here rather than inside _fuzzy_match on every
    # iteration — avoids reconstructing the same 49-entry dict for each name.
    index = _build_name_index(profiles)
    resolved: list[dict] = []
    not_found: list[str] = []

    for name in names:
        profile = _fuzzy_match(name, profiles, index=index)
        if not profile:
            not_found.append(name)
        elif not profile.get("fetched"):
            # Entity exists in the name list but has no Sayari data
            not_found.append(f"{profile['input_name']} (no Sayari data)")
        else:
            # Deduplicate: skip if this profile is already in the results
            # (handles cases where two input names resolve to the same entity)
            already_added = any(
                r["input_name"] == profile["input_name"] for r in resolved
            )
            if not already_added:
                resolved.append(profile)

    # Need at least 2 resolved entities to produce a meaningful comparison
    if len(resolved) < 2:
        return {
            "error":   True,
            "message": (
                f"Could not resolve enough entities to compare. "
                f"Unresolved: {', '.join(not_found)}. "
                "Try different spellings or check the entity names."
            ),
        }

    return {
        "error":             False,
        "entities_compared": len(resolved),
        "not_found":         not_found,
        # _format_profile() gives each entity the same consistent shape used
        # across all tools — readable risk flag labels, sector, counts, etc.
        "comparison":        [_format_profile(p) for p in resolved],
    }


# ---------------------------------------------------------------------------
# Tool 5 — get_summary_stats
# ---------------------------------------------------------------------------

def get_summary_stats(profiles: list[dict]) -> dict:
    """
    Return dataset-wide aggregate statistics for macro-level questions.

    Wraps analytics.summary_stats() and supplements it with sector breakdown,
    top entity by network degree, and the most widely applied sanctions list —
    giving the LLM everything it needs to answer any "big picture" question in
    a single tool call without chaining multiple tools together.

    Args:
        profiles: Full profiles list loaded from the cache.

    Returns:
        A single flat dict with all macro aggregates. Never returns an error —
        if the profiles list is empty all counts will be zero.

    Example questions this tool covers:
        "Give me a summary of the dataset"
        "What percentage of entities are sanctioned?"
        "How many state-owned enterprises are in the dataset?"
        "What is the total network footprint?"
        "Which sector has the most entities?"
        "What's the most common sanctions list?"
    """
    stats = summary_stats(profiles)
    fetched = _fetched_profiles(profiles)

    # --- Top entity by network degree ---
    # Surfaces the single most connected node for "who has the biggest footprint" questions
    top_entity = max(fetched, key=lambda p: p["degree"], default=None)

    # --- Most common sanctions list ---
    # The list that appears across the most entities — useful for jurisdiction questions
    sl_counts = sanctions_list_breakdown(profiles)
    top_list = max(sl_counts, key=sl_counts.get, default=None) if sl_counts else None

    # --- Sector breakdown ---
    # Full count per sector so the LLM can answer "how many defense firms?" etc.
    sectors = sector_breakdown(profiles)

    # --- Distinct sanctions list count ---
    # Derived here rather than in summary_stats so that function stays focused
    distinct_lists = len(sl_counts)

    return {
        "error": False,

        # Core counts from analytics.summary_stats()
        "total_entities":          stats["total_entities"],
        "fetched":                 stats["fetched"],
        "sanctioned_count":        stats["sanctioned_count"],
        "sanctioned_pct":          stats["sanctioned_pct"],
        "state_owned_count":       stats["state_owned_count"],
        "export_controls_count":   stats["export_controls_count"],
        "countries_represented":   stats["countries_represented"],
        "avg_sanctions_lists":     stats["avg_sanctions_lists"],
        "total_network_connections": stats["total_network_connections"],

        # Supplementary aggregates for richer LLM responses
        "distinct_sanctions_lists": distinct_lists,
        "most_common_sanctions_list": top_list,
        "most_common_sanctions_list_entity_count": sl_counts.get(top_list, 0) if top_list else 0,
        "top_entity_by_degree":    top_entity["input_name"] if top_entity else None,
        "top_entity_degree":       top_entity["degree"] if top_entity else 0,
        "sector_breakdown":        sectors,
    }
