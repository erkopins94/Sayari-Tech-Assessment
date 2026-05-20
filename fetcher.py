import json
import os
import time

from sayari.client import Sayari

# Path where full entity profiles are cached after fetching
PROFILES_CACHE = os.path.join(os.path.dirname(__file__), "data", "profiles.json")

# Risk keys that describe the entity's own status (direct exposure).
# Keys prefixed with owned_by_, psa_owned_by_, owner_of_, or psa_owner_of_
# describe network-level exposure and are excluded here to keep direct vs.
# indirect risk separate in the analytics layer.
DIRECT_RISK_KEYS = {
    "sanctioned",
    "sanctioned_usa_ofac_sdn",
    "sanctioned_usa_ofac_non_sdn",
    "sanctioned_other",
    "export_controls",
    "export_controls_other",
    "state_owned",
    "pep",
    "regulatory_action",
    "reputational_risk_other",
    "meu_list_contractors",
    "cpi_score",
    "basel_aml",
}


def _extract_risk_flags(risk: dict) -> dict:
    """
    Pulls only the direct risk flags from an entity's risk dict and maps each
    to its severity level (critical, high, elevated, relevant).

    We skip network-level flags (owned_by_*, psa_owned_by_*, owner_of_*) here
    because they describe the entity's neighborhood, not the entity itself.
    Those are more relevant to network traversal analysis than direct profiling.
    """
    if not risk:
        return {}

    flags = {}
    for key, risk_data in risk.items():
        if key in DIRECT_RISK_KEYS:
            flags[key] = risk_data.level or "unknown"
    return flags


def _count_risk_levels(risk: dict) -> dict:
    """
    Counts how many risk flags — across ALL keys, including network flags —
    fall into each severity bucket. This gives a single-number summary of
    how deeply entangled an entity is in the risk graph, useful for ranking
    entities by overall exposure in the analytics report.
    """
    counts = {"critical": 0, "high": 0, "elevated": 0, "relevant": 0}
    if not risk:
        return counts

    for risk_data in risk.values():
        level = risk_data.level
        if level in counts:
            counts[level] += 1
    return counts


def _extract_sanctions_lists(source_count: dict) -> list[str]:
    """
    Extracts the names of the sanctions lists an entity appears on by filtering
    the source_count dict for entries with source_type == 'sanctions_lists'.

    source_count maps source IDs to SourceCountInfo objects — each has a human-
    readable label and a type. We only want the sanctions list entries here.
    """
    if not source_count:
        return []

    return [
        info.label
        for info in source_count.values()
        if str(info.source_type) == "sanctions_lists"
    ]


def _empty_profile(resolved_entity: dict, error: str) -> dict:
    """
    Returns a consistently shaped failure record so the analytics layer never
    has to handle missing keys — every profile in the cache has the same schema
    whether the fetch succeeded or not.
    """
    return {
        "input_name": resolved_entity["input_name"],
        "input_country": resolved_entity["input_country"],
        "entity_id": resolved_entity.get("entity_id"),
        "matched_name": None,
        "entity_type": None,
        "countries": [],
        "sanctioned": False,
        "pep": False,
        "closed": False,
        "degree": 0,
        "relationship_counts": {},
        "total_relationships": 0,
        "risk_flags": {},
        "risk_level_counts": {"critical": 0, "high": 0, "elevated": 0, "relevant": 0},
        "sanctions_lists": [],
        "source_count": 0,
        "fetched": False,
        "error": error,
    }


def fetch_profile(client: Sayari, resolved_entity: dict) -> dict:
    """
    Fetches the full profile for a single resolved entity using entity_summary.

    entity_summary is preferred over get_entity here because it returns all the
    risk, country, and relationship data we need for macro analytics without
    paginating through every individual relationship record, keeping this fast
    and credit-efficient.

    Returns a flat dict containing everything the analytics layer needs for
    this entity: type, countries, risk flags, sanctions lists, relationship counts.
    """
    entity_id = resolved_entity["entity_id"]

    try:
        summary = client.entity.entity_summary(entity_id)

        # Sum all relationship type counts into a total network degree
        rel_counts = dict(summary.relationship_count) if summary.relationship_count else {}
        total_relationships = sum(rel_counts.values())

        return {
            "input_name": resolved_entity["input_name"],
            "input_country": resolved_entity["input_country"],
            "entity_id": entity_id,
            "matched_name": summary.label,
            "entity_type": str(summary.type) if summary.type else None,
            "countries": summary.countries or [],
            "sanctioned": summary.sanctioned or False,
            "pep": summary.pep or False,
            "closed": summary.closed or False,
            "degree": summary.degree or 0,
            "relationship_counts": rel_counts,
            "total_relationships": total_relationships,
            "risk_flags": _extract_risk_flags(summary.risk),
            "risk_level_counts": _count_risk_levels(summary.risk),
            "sanctions_lists": _extract_sanctions_lists(summary.source_count),
            "source_count": len(summary.source_count) if summary.source_count else 0,
            "fetched": True,
            "error": None,
        }

    except Exception as e:
        return _empty_profile(resolved_entity, str(e))


def fetch_all(client: Sayari, resolved: list[dict]) -> list[dict]:
    """
    Iterates over all resolved entities and fetches each profile sequentially.

    Unresolved entities (those that failed resolution in resolver.py) are included
    as empty stub records so the analytics layer always operates on the full set
    of 50 input entities, not just the 49 that resolved successfully.

    A 0.2s delay between calls keeps us well within API rate limits.
    """
    resolved_ok = [r for r in resolved if r["resolved"]]
    unresolved = [r for r in resolved if not r["resolved"]]

    profiles = []

    for i, entity in enumerate(resolved_ok):
        print(f"  [{i + 1}/{len(resolved_ok)}] {entity['input_name']}")
        profile = fetch_profile(client, entity)
        status = "OK" if profile["fetched"] else f"FAILED ({profile['error']})"
        print(f"         -> {status}")
        profiles.append(profile)
        time.sleep(0.2)

    # Append stubs for unresolved entities so downstream counts are accurate
    for entity in unresolved:
        profiles.append(_empty_profile(entity, entity.get("error", "unresolved")))

    return profiles


def load_or_build_profiles(client: Sayari) -> list[dict]:
    """
    Returns entity profiles from the local cache if available, otherwise fetches
    all profiles from the API and writes the results to disk.

    Like the resolver cache, this means the Streamlit app and analytics module
    can be developed and iterated on entirely offline after the first run.
    Delete data/profiles.json to force a fresh fetch.
    """
    if os.path.exists(PROFILES_CACHE):
        with open(PROFILES_CACHE) as f:
            return json.load(f)

    os.makedirs(os.path.dirname(PROFILES_CACHE), exist_ok=True)

    from resolver import load_or_build_resolved
    resolved = load_or_build_resolved(client)

    profiles = fetch_all(client, resolved)

    with open(PROFILES_CACHE, "w") as f:
        json.dump(profiles, f, indent=2)

    return profiles


if __name__ == "__main__":
    from client import get_client

    client = get_client()
    print("Fetching entity profiles...\n")
    profiles = load_or_build_profiles(client)

    fetched = [p for p in profiles if p["fetched"]]
    failed = [p for p in profiles if not p["fetched"]]

    print(f"\nFetch complete: {len(fetched)}/{len(profiles)} profiles retrieved")
    if failed:
        print("\nFailed fetches:")
        for p in failed:
            print(f"  - {p['input_name']}: {p['error']}")
