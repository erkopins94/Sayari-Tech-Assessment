"""
resolver.py — Resolves entity names to Sayari entity IDs.

Takes the 50 entities in the ENTITIES list and calls the Sayari resolution
endpoint for each one, capturing the best-match entity ID, label, and match
strength. Results are written to data/resolved.json and reused on all
subsequent runs so no credits are spent after the first execution.

Run directly to (re-)build the resolved cache:
    python resolver.py
"""

import json
import os
import time

from sayari.client import Sayari

# Path where resolved entity IDs are cached to avoid re-hitting the API on every run
RESOLVED_CACHE = os.path.join(os.path.dirname(__file__), "data", "resolved.json")

# The 50 sanctioned/high-risk entities from List 1 of the assessment.
# Names are the best-match aliases confirmed to resolve in Sayari — a few required
# corrections from the original list (e.g. "PDVSA" instead of the full parenthetical name).
ENTITIES = [
    {"name": "Russian Direct Investment Fund", "country": "RUS"},
    {"name": "Sberbank", "country": "RUS"},
    {"name": "VTB Bank", "country": "RUS"},
    {"name": "Rostec", "country": "RUS"},
    {"name": "Rosneft", "country": "RUS"},
    {"name": "Gazprom", "country": "RUS"},
    {"name": "National Iranian Oil Company", "country": "IRN"},
    {"name": "Huawei Technologies Co. Ltd.", "country": "CHN"},
    {"name": "ZTE Corporation", "country": "CHN"},
    {"name": "Hangzhou Hikvision Digital Technology Co. Ltd.", "country": "CHN"},
    {"name": "PDVSA", "country": "VEN"},
    {"name": "Syrian Arab Airlines", "country": "SYR"},
    {"name": "Korea Mining Development Trading Corporation", "country": "PRK"},
    {"name": "Cubametales", "country": "CUB"},
    {"name": "Myanmar Economic Corporation", "country": "MMR"},
    {"name": "Myanmar Economic Holdings Limited", "country": "MMR"},
    {"name": "Belaruskali OAO", "country": "BLR"},
    {"name": "State Development Bank VEB.RF", "country": "RUS"},
    {"name": "Promsvyazbank", "country": "RUS"},
    {"name": "Alfa-Bank", "country": "RUS"},
    {"name": "Belnauchcompositit", "country": "BLR"},
    {"name": "Belarusian Potash Company", "country": "BLR"},
    {"name": "Belneftegaz", "country": "BLR"},
    {"name": "Bank Rossiya", "country": "RUS"},
    {"name": "Novikombank", "country": "RUS"},
    {"name": "Sovcombank", "country": "RUS"},
    {"name": "Bank Otkritie", "country": "RUS"},
    {"name": "Transneft", "country": "RUS"},
    {"name": "Russian Railways", "country": "RUS"},
    {"name": "United Aircraft Corporation", "country": "RUS"},
    {"name": "United Shipbuilding Corporation", "country": "RUS"},
    {"name": "Kamaz", "country": "RUS"},
    {"name": "Sevmash", "country": "RUS"},
    {"name": "Almaz-Antey", "country": "RUS"},
    {"name": "Uralvagonzavod", "country": "RUS"},
    {"name": "Kalashnikov Concern", "country": "RUS"},
    {"name": "NPO High Precision Systems", "country": "RUS"},
    {"name": "Tactical Missiles Corporation JSC", "country": "RUS"},
    {"name": "NPK Tekhmash OAO", "country": "RUS"},
    {"name": "Molot-Oruzhie", "country": "RUS"},
    {"name": "Rustec", "country": "RUS"},
    {"name": "Rosoboronexport", "country": "RUS"},
    {"name": "Power Machines", "country": "RUS"},
    {"name": "Sukhoi Company", "country": "RUS"},
    {"name": "Irkut Corporation", "country": "RUS"},
    {"name": "MiG Corporation", "country": "RUS"},
    {"name": "Tupolev PJSC", "country": "RUS"},
    {"name": "Admiralty Shipyards", "country": "RUS"},
    {"name": "Zvezdochka Shipyard", "country": "RUS"},
    {"name": "Baltic Shipyard", "country": "RUS"},
]


def resolve_entity(client: Sayari, name: str, country: str) -> dict:
    """
    Resolves a single entity name + country to a Sayari entity ID.

    Uses the Sayari resolution endpoint, which ranks candidates by match confidence
    and returns the best match. We take the top result (index 0) and capture the
    entity ID, matched label, match strength, and type for use downstream.

    Returns a dict with resolved=True on success, or resolved=False with an error
    message if no match is found or the API call fails.
    """
    try:
        response = client.resolution.resolution(name=name, country=country)
        if not response.data:
            return _unresolved(name, country, "no_match")

        match = response.data[0]
        return {
            "input_name": name,
            "input_country": country,
            "entity_id": match.entity_id,
            "matched_name": match.label,
            "match_strength": str(match.match_strength) if match.match_strength else None,
            "score": match.score,
            "entity_type": str(match.type) if match.type else None,
            "resolved": True,
            "error": None,
        }
    except Exception as e:
        return _unresolved(name, country, str(e))


def _unresolved(name: str, country: str, error: str) -> dict:
    """Returns a consistently structured failure record so the analytics layer
    can count and report unresolved entities without special-casing None values."""
    return {
        "input_name": name,
        "input_country": country,
        "entity_id": None,
        "matched_name": None,
        "match_strength": None,
        "score": None,
        "entity_type": None,
        "resolved": False,
        "error": error,
    }


def resolve_all(client: Sayari) -> list[dict]:
    """
    Iterates over every entity in ENTITIES and resolves each one sequentially.

    A 0.2s delay between calls keeps us well within Sayari's rate limits
    (the SDK also handles 429s automatically, but this avoids hitting them at all).
    Progress is printed to stdout so the operator can monitor the run.
    """
    results = []
    for i, entity in enumerate(ENTITIES):
        print(f"  [{i + 1}/{len(ENTITIES)}] {entity['name']}")
        result = resolve_entity(client, entity["name"], entity["country"])
        status = "OK" if result["resolved"] else f"FAILED ({result['error']})"
        print(f"         -> {status}")
        results.append(result)
        time.sleep(0.2)
    return results


def load_or_build_resolved(client: Sayari) -> list[dict]:
    """
    Returns resolved entity data from the local cache if it exists, otherwise
    calls the Sayari API to resolve all entities and writes the results to disk.

    Caching is critical during development — it means the full analytics pipeline
    can be iterated on without consuming API credits or waiting for network calls.
    Delete data/resolved.json to force a fresh resolution run.
    """
    if os.path.exists(RESOLVED_CACHE):
        with open(RESOLVED_CACHE) as f:
            return json.load(f)

    os.makedirs(os.path.dirname(RESOLVED_CACHE), exist_ok=True)
    results = resolve_all(client)

    with open(RESOLVED_CACHE, "w") as f:
        json.dump(results, f, indent=2)

    return results


if __name__ == "__main__":
    from client import get_client

    client = get_client()
    print("Resolving all entities...\n")
    results = load_or_build_resolved(client)

    resolved = [r for r in results if r["resolved"]]
    unresolved = [r for r in results if not r["resolved"]]

    print(f"\nResolution complete: {len(resolved)}/{len(results)} resolved")
    if unresolved:
        print("\nUnresolved entities:")
        for r in unresolved:
            print(f"  - {r['input_name']}: {r['error']}")
