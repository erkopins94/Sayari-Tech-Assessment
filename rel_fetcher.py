"""
rel_fetcher.py — One-time script to fetch and cache the top-N network connections
for each entity in the dataset.

Run this script once to populate data/relationships.json. The AI chat tool
get_entity_relationships reads from that cache — no live Sayari API calls are
made during normal app usage.

Usage:
    python rel_fetcher.py

To refresh: delete data/relationships.json and re-run.

Design notes:
    We cap at LIMIT_PER_ENTITY (50) relationships per entity to keep API credit
    usage bounded and the cache file small (~1 MB vs. ~150 MB for all 428k records).
    The Sayari API returns connections ordered by relevance, so the top 50 capture
    the most analytically significant counterparties for each entity.

    This mirrors the pattern of fetcher.py (profiles) and resolver.py (resolved
    entities) — offline once, cache forever, iterate on the app without re-fetching.
"""

import json
import os
import time

from sayari.client import Sayari

RELATIONSHIPS_CACHE = os.path.join(os.path.dirname(__file__), "data", "relationships.json")

# Number of relationship records to fetch per entity.
# 49 entities × 50 records = 2,450 total records — fast to fetch, fast to load.
LIMIT_PER_ENTITY = 50


def _extract_relationship(rel) -> dict:
    """
    Flatten a Sayari relationship record into a plain dict for the JSON cache.

    Each record in get_entity's relationships.data has:
      rel.types  — Dict[str, List[RelationshipInfo]]; keys are the relationship type
                   strings (e.g. "has_shareholder", "director_of"). A single connected
                   entity can appear under multiple relationship types simultaneously.
      rel.target — the connected EntityDetails object (id, label, type, countries, etc.)

    We store all relationship type keys as a list so the filter in tools.py can do
    substring matching across all types for this connection.
    """
    target = rel.target
    return {
        "target_id":          getattr(target, "id",    None),
        "target_name":        getattr(target, "label", None),
        "target_type":        str(target.type) if getattr(target, "type", None) else None,
        "target_countries":   list(target.countries) if getattr(target, "countries", None) else [],
        "relationship_types": [str(k) for k in rel.types.keys()] if rel.types else [],
        "sanctioned":         bool(getattr(target, "sanctioned", False)),
    }


def fetch_relationships(client: Sayari, entity_id: str, entity_name: str) -> list[dict]:
    """
    Fetch up to LIMIT_PER_ENTITY relationship records for a single entity.

    Uses get_entity (not entity_summary) because we need the actual connected
    entity names and types — entity_summary only returns aggregate counts.

    The [:LIMIT_PER_ENTITY] slice is a safety net in case the API returns
    more records than requested. The SDK kwarg is relationships_limit
    (a flat keyword argument).
    """
    try:
        response = client.entity.get_entity(
            entity_id,
            relationships_limit=LIMIT_PER_ENTITY,
        )
        rel_data = response.relationships.data if response.relationships else []
        return [_extract_relationship(rel) for rel in rel_data[:LIMIT_PER_ENTITY]]
    except Exception as e:
        print(f"  WARNING: {entity_name} — {e}")
        return []


def fetch_all_relationships(client: Sayari, profiles: list[dict]) -> dict[str, list[dict]]:
    """
    Fetch top-LIMIT_PER_ENTITY relationships for every successfully resolved entity.

    Returns a dict keyed by input_name so it aligns with the profiles cache and the
    name index used by tools.py. Unresolved entities (fetched=False) are skipped —
    they have no entity_id to query.

    A 0.3s delay between calls keeps us comfortably within API rate limits.
    """
    fetched_profiles = [p for p in profiles if p.get("fetched") and p.get("entity_id")]
    result: dict[str, list[dict]] = {}

    for i, profile in enumerate(fetched_profiles):
        name      = profile["input_name"]
        entity_id = profile["entity_id"]
        print(f"  [{i + 1}/{len(fetched_profiles)}] {name}")
        rels = fetch_relationships(client, entity_id, name)
        result[name] = rels
        print(f"         -> {len(rels)} relationships")
        time.sleep(0.3)

    return result


def load_or_build_relationships(client: Sayari, profiles: list[dict]) -> dict[str, list[dict]]:
    """
    Return the relationships cache if it exists, otherwise fetch and write it.

    Same cache-first pattern as load_or_build_profiles() in fetcher.py.
    Delete data/relationships.json to force a fresh fetch.
    """
    if os.path.exists(RELATIONSHIPS_CACHE):
        with open(RELATIONSHIPS_CACHE) as f:
            return json.load(f)

    os.makedirs(os.path.dirname(RELATIONSHIPS_CACHE), exist_ok=True)
    relationships = fetch_all_relationships(client, profiles)

    with open(RELATIONSHIPS_CACHE, "w") as f:
        json.dump(relationships, f, indent=2)

    return relationships


if __name__ == "__main__":
    from client import get_client
    from fetcher import load_or_build_profiles

    client   = get_client()
    profiles = load_or_build_profiles(client)

    fetched_count = sum(1 for p in profiles if p.get("fetched"))
    print(f"Fetching top-{LIMIT_PER_ENTITY} relationships for {fetched_count} entities...\n")

    relationships = load_or_build_relationships(client, profiles)

    total_rels = sum(len(v) for v in relationships.values())
    print(f"\nFetch complete: {total_rels} relationship records across {len(relationships)} entities")
    print(f"Cache written to: {RELATIONSHIPS_CACHE}")
