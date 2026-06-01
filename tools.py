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

Chart tools return go.Figure objects — these are never serialised to JSON and
sent to the Anthropic API. Instead _dispatch_tool wraps every return value in a
ToolResult dataclass that separates the API payload (what Claude sees) from the
optional figure (what the Streamlit UI renders). This keeps the serialisation
boundary explicit and in one place.
"""

import difflib
from collections.abc import Callable
from dataclasses import dataclass

import pycountry

import plotly.express as px
import plotly.graph_objects as go

from analytics import (
    RISK_FLAG_LABELS,
    SECTOR_MAP,
    SECTOR_NOTES,
    country_breakdown,
    risk_flag_frequency,
    risk_level_distribution,
    sanctions_list_breakdown,
    sector_breakdown,
    summary_stats,
)


# ---------------------------------------------------------------------------
# ToolResult — return type for _dispatch_tool
#
# Separates what Claude sees (api_payload, always JSON-serialisable) from what
# the Streamlit UI renders (figure, a Plotly Figure or None).
#
# For data tools:  api_payload = the full result dict, figure = None
# For chart tools: api_payload = lightweight confirmation dict,
#                  figure      = the go.Figure to render in the chat
# For errors:      api_payload = {"error": True, "message": "..."},
#                  figure      = None
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """Wrapper returned by _dispatch_tool for every tool call."""
    api_payload: dict                    # sent to Anthropic API as tool_result content
    figure:      go.Figure | None = None # rendered by Streamlit UI; never sent to API


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetched_profiles(profiles: list[dict]) -> list[dict]:
    """Return only the profiles that were successfully fetched from Sayari."""
    return [p for p in profiles if p.get("fetched")]


def _build_name_index(profiles: list[dict]) -> dict[str, dict]:
    """
    Build a normalised-name → profile lookup used by fuzzy matching.

    Uses two passes so input_name always takes priority over matched_name:

      Pass 1 — input_name entries (authoritative: what the dataset is built on)
      Pass 2 — matched_name entries added only where the key is not already
               claimed, making them aliases rather than overrides

    This prevents silent collisions where a matched_name (the Sayari-resolved
    canonical name) would overwrite a different entity's input_name that happens
    to normalise to the same string.
    """
    index: dict[str, dict] = {}

    # Pass 1 — input_name is the authoritative key; always wins
    for p in profiles:
        index[p["input_name"].lower()] = p

    # Pass 2 — matched_name as a fallback alias only if the key is unclaimed
    for p in profiles:
        if p.get("matched_name"):
            key = p["matched_name"].lower()
            if key not in index:
                index[key] = p

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
    by all tools. Translates raw API risk flag keys into readable labels,
    adds sector classification, and includes the sector methodology note so
    the AI can explain or defend the classification when asked.

    Keeping this in one place means every tool returns a consistent structure.
    """
    name = profile["input_name"]
    return {
        "input_name":          name,
        "matched_name":        profile["matched_name"],
        "entity_type":         profile["entity_type"],
        "sector":              SECTOR_MAP.get(name, "Other"),
        "sector_note":         SECTOR_NOTES.get(name, "No classification note available."),
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
# Country name resolution (used by filter_entities)
#
# _resolve_country converts user-supplied country strings to ISO-3 codes.
# Full country names are handled by pycountry (all 249 ISO 3166-1 countries).
# _COUNTRY_ALIASES covers only the informal abbreviations and name changes
# that pycountry cannot resolve on its own.
# ---------------------------------------------------------------------------

# Informal abbreviations and renamed countries that pycountry cannot resolve.
# Everything else (all 249 ISO 3166-1 countries by full name) is handled by
# pycountry.countries.search_fuzzy() in _resolve_country below.
# "turkey" is here because pycountry uses the 2022 rename "Turkiye".
_COUNTRY_ALIASES: dict[str, str] = {
    "turkey": "TUR",  # ISO name changed to "Turkiye" in 2022
    "uk":     "GBR",  # informal abbreviation for "United Kingdom"
    "us":     "USA",  # informal abbreviation for "United States"
    "usa":    "USA",  # informal abbreviation for "United States"
    "uae":    "ARE",  # informal abbreviation for "United Arab Emirates"
    "bvi":    "VGB",  # informal abbreviation for "Virgin Islands, British"
}


# Precomputed once at import time as a sorted tuple — SECTOR_MAP never changes
# at runtime, and a sorted tuple gives deterministic iteration order in
# _resolve_sector (frozenset iteration order is not guaranteed).
_KNOWN_SECTORS: tuple[str, ...] = tuple(sorted(set(SECTOR_MAP.values())))


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

    Resolution order:
      1. _COUNTRY_ALIASES -> checked first; some aliases are 3 letters ("uae",
                            "bvi") and would be mistaken for ISO-3 codes otherwise
      2. 3-letter input   -> treated as an ISO-3 code directly (fast path)
      3. pycountry        -> fuzzy search across all 249 ISO 3166-1 countries;
                            no manual maintenance required

    Returns None if the input cannot be resolved, so filter_entities can
    return a helpful error rather than silently returning zero results.
    """
    normalised = country.strip().lower()

    # Aliases checked first — some are 3 letters ("uae", "bvi") and would
    # otherwise be mistaken for ISO-3 codes by the fast path below
    if normalised in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[normalised]

    # Fast path — user typed a 3-letter ISO-3 code directly (e.g. "RUS", "CHN")
    if len(normalised) == 3 and normalised.upper().isalpha():
        return normalised.upper()

    # Delegate full name resolution to pycountry
    try:
        results = pycountry.countries.search_fuzzy(normalised)
        return results[0].alpha_3
    except LookupError:
        return None


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
# Tool 5 — get_entity_relationships
# ---------------------------------------------------------------------------

def get_entity_relationships(
    name: str,
    profiles: list[dict],
    relationships: dict[str, list[dict]],
    relationship_type: str | None = None,
    sanctioned_only: bool = False,
) -> dict:
    """
    Return the top cached network connections for a specific entity.

    The relationships cache (data/relationships.json) holds up to 50 connections
    per entity, pre-fetched by rel_fetcher.py. This makes relationship queries
    fast and credit-free during normal app usage — no live Sayari API calls.

    Args:
        name:              Entity name. Fuzzy matched as usual.
        profiles:          Full profiles list — used for name resolution and the
                           total degree count (reported alongside cached results).
        relationships:     Pre-loaded cache from data/relationships.json, keyed
                           by input_name.
        relationship_type: Optional case-insensitive substring filter on the
                           relationship type field. "shareholder" matches both
                           "has_shareholder" and "shareholder_of"; "subsidiary"
                           matches "has_subsidiary" and "subsidiary_of", etc.
        sanctioned_only:   If True, return only connections where the counterparty
                           is itself a sanctioned entity.

    Returns:
        On success — {
            "error": False,
            "entity": entity_name,
            "total_known_connections": degree from profiles (full network size),
            "cached_connections": how many are in the top-50 cache,
            "returned": count after applying filters,
            "relationships": [list of connection records],
            "note": human-readable summary of what is shown
        }
        On failure — {"error": True, "message": "..."}

    Example questions this tool covers:
        "Who are Rosneft's top connections?"
        "Show me Gazprom's network counterparties"
        "Which of Rostec's connections are sanctioned?"
        "What companies is ZTE linked to?"
        "Show me Russian Railways' top shareholders"
    """
    profile = _fuzzy_match(name, profiles)

    if not profile:
        return {
            "error":   True,
            "message": (
                f"No entity matching '{name}' found in the dataset. "
                "Try a different spelling, abbreviation, or partial name."
            ),
        }

    if not profile.get("fetched"):
        return {
            "error":   True,
            "message": (
                f"'{profile['input_name']}' is in the dataset but has no profile data available."
            ),
        }

    entity_name = profile["input_name"]
    cached      = relationships.get(entity_name, [])

    # Apply optional filters
    filtered = cached
    if relationship_type is not None:
        # relationship_types is a list — match if the query substring appears in any type
        query    = relationship_type.lower()
        filtered = [
            r for r in filtered
            if any(query in rt.lower() for rt in r.get("relationship_types", []))
        ]
    if sanctioned_only:
        filtered = [r for r in filtered if r.get("sanctioned")]

    total_degree  = profile["degree"]
    cached_count  = len(cached)
    returned_count = len(filtered)

    note = f"Showing top {cached_count:,} of {total_degree:,} total known connections."
    if relationship_type or sanctioned_only:
        note += f" Filtered to {returned_count:,} matching records."

    return {
        "error":                   False,
        "entity":                  entity_name,
        "total_known_connections": total_degree,
        "cached_connections":      cached_count,
        "returned":                returned_count,
        "relationships":           filtered,
        "note":                    note,
    }


# ---------------------------------------------------------------------------
# Tool 6 — get_summary_stats
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


# ===========================================================================
# Chart tools — Phase 2 of the AI feature
#
# These functions return Plotly Figure objects for rendering directly in the
# chat UI. They differ from the data tools above in two key ways:
#
#   1. Return type: go.Figure (not dict) on success, dict on error
#   2. They are never serialised and sent to the Anthropic API — instead the
#      dispatcher sends Claude a lightweight confirmation dict, and the figure
#      is collected separately and rendered by the Streamlit UI.
#
# Styling constants are defined here rather than imported from app.py to keep
# tools.py self-contained (app.py is a Streamlit entry point, not a library).
# ===========================================================================

# Shared chart styling — mirrors app.py for visual consistency
_CHART_TEMPLATE = "plotly_white"

# Severity colours follow standard risk-traffic-light conventions
_RISK_COLORS: dict[str, str] = {
    "critical": "#C0392B",
    "high":     "#E67E22",
    "elevated": "#F1C40F",
    "relevant": "#3498DB",
}

# Qualitative palette for sector and multi-category charts
_SECTOR_PALETTE = px.colors.qualitative.Safe


# ---------------------------------------------------------------------------
# Chart Tool 1 — chart_sector_breakdown
# ---------------------------------------------------------------------------

def chart_sector_breakdown(profiles: list[dict]) -> go.Figure | dict:
    """
    Return a donut chart showing the entity count for each sector.

    Reuses sector_breakdown() from analytics.py for the underlying counts —
    no data logic is duplicated here, this function is purely presentational.

    Args:
        profiles: Full profiles list loaded from the cache.

    Returns:
        A Plotly Figure on success, or {"error": True, "message": "..."} if
        there is no data to plot.

    Example questions this chart covers:
        "Show me a breakdown of entities by sector"
        "Visualise the sector distribution"
        "What does the sector split look like?"
    """
    sectors = sector_breakdown(profiles)

    if not sectors:
        return {"error": True, "message": "No sector data available to chart."}

    fig = px.pie(
        names=list(sectors.keys()),
        values=list(sectors.values()),
        title="Entity Breakdown by Sector",
        hole=0.45,                          # donut shape — easier to read than a solid pie
        color_discrete_sequence=_SECTOR_PALETTE,
    )
    fig.update_traces(textposition="inside", textinfo="percent+label")
    fig.update_layout(
        template=_CHART_TEMPLATE,
        showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig


# ---------------------------------------------------------------------------
# Chart Tool 2 — chart_top_entities
# ---------------------------------------------------------------------------

def chart_top_entities(
    profiles: list[dict],
    metric: str,
    n: int = 10,
    sector: str | None = None,
) -> go.Figure | dict:
    """
    Return a horizontal bar chart of the top N entities ranked by a metric.

    Delegates entirely to rank_entities() for data retrieval and metric
    resolution — no ranking logic lives here. The chart is built from
    whatever rank_entities() returns, so all metric aliases and sector
    filters work transparently.

    Args:
        profiles: Full profiles list loaded from the cache.
        metric:   Same aliases as rank_entities() — "degree", "connections",
                  "sanctions lists", "countries", "critical flags", etc.
        n:        How many entities to show (default 10).
        sector:   Optional sector filter applied before ranking.

    Returns:
        A Plotly Figure on success, or {"error": True, "message": "..."} if
        the metric is unrecognised or the filtered dataset is empty.

    Example questions this chart covers:
        "Plot the top 10 entities by network connections"
        "Show me a chart of banking firms ranked by sanctions lists"
        "Visualise which entities have the widest geographic footprint"
    """
    # Delegate to the data tool — inherits all metric resolution and error handling
    result = rank_entities(profiles, metric=metric, n=n, sector=sector)

    if result.get("error"):
        return result  # pass the error dict straight through

    if not result["ranking"]:
        return {"error": True, "message": "No entities matched the given filters."}

    names  = [row["name"]  for row in result["ranking"]]
    values = [row["value"] for row in result["ranking"]]
    label  = result["metric"]   # human-readable metric name from rank_entities

    # Sort ascending so the highest bar sits at the top of the chart
    pairs = sorted(zip(values, names))
    values, names = zip(*pairs)

    fig = go.Figure(go.Bar(
        x=values,
        y=list(names),
        orientation="h",
        marker_color="#2E86AB",
        hovertemplate="%{y}: %{x:,}<extra></extra>",
    ))
    title = f"Top {len(result['ranking'])} Entities by {label}"
    if sector:
        title += f" — {result['sector']} sector"

    fig.update_layout(
        title=title,
        template=_CHART_TEMPLATE,
        xaxis_title=label,
        yaxis_title=None,
        height=max(300, len(names) * 35),   # scale height to number of bars
        margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig


# ---------------------------------------------------------------------------
# Chart Tool 3 — chart_sanctions_lists
# ---------------------------------------------------------------------------

def chart_sanctions_lists(profiles: list[dict]) -> go.Figure | dict:
    """
    Return a horizontal bar chart of how many entities appear on each
    sanctions list, sorted by entity count descending.

    Args:
        profiles: Full profiles list loaded from the cache.

    Returns:
        A Plotly Figure on success, or {"error": True, "message": "..."}.

    Example questions this chart covers:
        "Show me a chart of sanctions list coverage"
        "Visualise which sanctions lists are most widely applied"
        "Plot entities per sanctions list"
    """
    sl_data = sanctions_list_breakdown(profiles)

    if not sl_data:
        return {"error": True, "message": "No sanctions list data available to chart."}

    # Sort ascending so the most-used list sits at the top
    pairs  = sorted(sl_data.items(), key=lambda x: x[1])
    labels = [p[0] for p in pairs]
    values = [p[1] for p in pairs]

    fig = go.Figure(go.Bar(
        x=values,
        y=labels,
        orientation="h",
        marker_color="#C0392B",             # red — consistent with dashboard Sanctions tab
        hovertemplate="%{y}: %{x} entities<extra></extra>",
    ))
    fig.update_layout(
        title="Entities per Sanctions List",
        template=_CHART_TEMPLATE,
        xaxis_title="Number of Entities",
        yaxis_title=None,
        height=max(400, len(labels) * 28),
        margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig


# ---------------------------------------------------------------------------
# Chart Tool 4 — chart_risk_flags
# ---------------------------------------------------------------------------

def chart_risk_flags(profiles: list[dict]) -> go.Figure | dict:
    """
    Return a horizontal bar chart of risk flag frequency across all entities.

    Uses RISK_FLAG_LABELS-translated labels (the readable names, not raw API
    keys) so the chart is immediately legible without a legend explanation.

    Args:
        profiles: Full profiles list loaded from the cache.

    Returns:
        A Plotly Figure on success, or {"error": True, "message": "..."}.

    Example questions this chart covers:
        "Show me a chart of risk flags"
        "Visualise how common each risk flag is"
        "Plot risk flag frequency"
    """
    flag_data = risk_flag_frequency(profiles)

    if not flag_data:
        return {"error": True, "message": "No risk flag data available to chart."}

    pairs  = sorted(flag_data.items(), key=lambda x: x[1])
    labels = [p[0] for p in pairs]
    values = [p[1] for p in pairs]

    fig = go.Figure(go.Bar(
        x=values,
        y=labels,
        orientation="h",
        marker_color="#E67E22",             # orange — consistent with dashboard Risk tab
        hovertemplate="%{y}: %{x} entities<extra></extra>",
    ))
    fig.update_layout(
        title="Risk Flag Frequency Across All Entities",
        template=_CHART_TEMPLATE,
        xaxis_title="Number of Entities",
        yaxis_title=None,
        height=max(300, len(labels) * 35),
        margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig


# ---------------------------------------------------------------------------
# Chart Tool 5 — chart_risk_severity
# ---------------------------------------------------------------------------

def chart_risk_severity(profiles: list[dict]) -> go.Figure | dict:
    """
    Return a vertical bar chart of aggregate risk flag counts by severity level.

    Bars are coloured by severity using standard risk-traffic-light conventions
    (critical=red, high=orange, elevated=yellow, relevant=blue) so the chart
    is readable at a glance without needing to read the axis labels.

    Args:
        profiles: Full profiles list loaded from the cache.

    Returns:
        A Plotly Figure on success, or {"error": True, "message": "..."}.

    Example questions this chart covers:
        "Show me the risk severity distribution"
        "Visualise critical vs high vs elevated flags"
        "Chart the aggregate risk levels across the dataset"
    """
    levels = risk_level_distribution(profiles)

    if not levels:
        return {"error": True, "message": "No risk level data available to chart."}

    # Use _RISK_COLORS for each bar; fall back to grey for any unexpected level
    colors = [_RISK_COLORS.get(level, "#95A5A6") for level in levels.keys()]

    fig = go.Figure(go.Bar(
        x=list(levels.keys()),
        y=list(levels.values()),
        marker_color=colors,
        hovertemplate="%{x}: %{y} flags<extra></extra>",
    ))
    fig.update_layout(
        title="Aggregate Risk Flags by Severity Level",
        template=_CHART_TEMPLATE,
        xaxis_title="Severity Level",
        yaxis_title="Total Flag Count",
        height=400,
        margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig


# ---------------------------------------------------------------------------
# Chart Tool 6 — chart_country_map
# ---------------------------------------------------------------------------

def chart_country_map(profiles: list[dict]) -> go.Figure | dict:
    """
    Return a choropleth world map showing how many entities are present in
    each country, coloured by entity count on a red scale.

    Args:
        profiles: Full profiles list loaded from the cache.

    Returns:
        A Plotly Figure on success, or {"error": True, "message": "..."}.

    Example questions this chart covers:
        "Show me a map of where these entities operate"
        "Visualise the geographic footprint of the dataset"
        "Plot entity presence by country"
    """
    country_data = country_breakdown(profiles)

    if not country_data:
        return {"error": True, "message": "No country data available to chart."}

    # Build a flat list of (iso3, count) pairs for Plotly
    codes   = list(country_data.keys())
    counts  = list(country_data.values())

    fig = px.choropleth(
        locations=codes,
        locationmode="ISO-3",
        color=counts,
        color_continuous_scale="Reds",
        title="Entity Presence by Country",
        labels={"color": "Entities Present"},
    )
    fig.update_layout(
        template=_CHART_TEMPLATE,
        height=500,
        margin=dict(l=0, r=0, t=40, b=0),
        coloraxis_colorbar=dict(title="Entities"),
    )
    return fig
