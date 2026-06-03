"""
resolver.py — Resolves entity names to Sayari entity IDs.

Takes the 50 entities in the ENTITIES list and calls the Sayari resolution
endpoint for each one. Rather than blindly accepting the single best match,
we request the top-N candidates, capture each one's score, country, and match
strength, and classify the best match's confidence. Low-confidence matches are
flagged needs_review so a human can adjudicate them in the Resolution Review
tab instead of letting a weak guess silently become "fact" downstream.

Results are written to data/resolved.json and reused on all subsequent runs so
no credits are spent after the first execution.

A note on minimum_score_threshold: the Sayari resolution endpoint accepts a
minimum_score_threshold param that silently drops any match below the cutoff.
We deliberately do NOT gate on it — in a compliance/sanctions context, silently
discarding a weak match hides risk. Instead we keep every match but surface the
weak ones for explicit human review (see REVIEW_SCORE_THRESHOLD below).

Run directly to (re-)build the resolved cache:
    python resolver.py
"""

import json
import os
import time

from sayari.client import Sayari

# Path where resolved entity IDs are cached to avoid re-hitting the API on every run
RESOLVED_CACHE = os.path.join(os.path.dirname(__file__), "data", "resolved.json")

# Number of candidate matches to capture per entity. The best match (index 0)
# becomes the working resolution; the rest are retained so a human reviewer can
# re-map a low-confidence entity to the correct candidate without a live API call.
CANDIDATE_LIMIT = 5

# Widen the search breadth beyond the API default (50) so the candidate pool has
# better recall for the harder-to-match entities. Higher = better candidates at
# the cost of some latency — acceptable for a one-time, 50-entity cache build.
CANDIDATE_POOL_SIZE = 100

# Confidence floor on the resolution response's `score` field. The observed score
# distribution across our 49 entities clusters above ~126, with a clear weak tail
# at 65 / 68 / 80 / 107. A 120 cutoff isolates that tail for human review while
# leaving the confident bulk untouched. Matches at or above this are "high"
# confidence; below it are "low" and flagged needs_review.
REVIEW_SCORE_THRESHOLD = 120


def _strength_str(match_strength) -> str | None:
    """
    Extract the plain string value from a Sayari MatchStrength object.

    MatchStrength is a small model wrapping a single `.value` field (e.g. "weak",
    "strong"). We read .value directly rather than str()-ing the object, which
    would yield the unhelpful repr "value='weak'".
    """
    if match_strength is None:
        return None
    return getattr(match_strength, "value", None) or str(match_strength)


def _classify_confidence(score) -> str:
    """Map a resolution score to a confidence band used for review triage."""
    if score is None:
        return "none"
    return "high" if score >= REVIEW_SCORE_THRESHOLD else "low"


def _candidate(result) -> dict:
    """
    Flatten one Sayari ResolutionResult into a plain dict for the cache.

    We keep `countries` so the review tab can flag jurisdiction mismatches
    (e.g. a Russian input matched to a Belarusian entity) at a glance.
    """
    return {
        "entity_id":      result.entity_id,
        "matched_name":   result.label,
        "score":          result.score,
        "match_strength": _strength_str(result.match_strength),
        "entity_type":    str(result.type) if result.type else None,
        "countries":      list(result.countries) if result.countries else [],
    }


def apply_review_decision(record: dict, chosen: dict | None) -> dict:
    """
    Return an updated copy of a resolved record after a human review decision.

    Pure function (no I/O) so it can be unit-tested and reused by the Resolution
    Review tab. `chosen` is one of record["candidates"], or None to mark the
    entity unresolved (no candidate is correct).

    manual_override is True only when the reviewer changed the match away from
    the originally-resolved entity — confirming the existing top match counts as
    reviewed but not overridden.
    """
    updated = dict(record)
    updated["reviewed"] = True
    updated["needs_review"] = False

    if chosen is None:
        updated.update({
            "entity_id":       None,
            "matched_name":    None,
            "score":           None,
            "match_strength":  None,
            "entity_type":     None,
            "confidence":      "none",
            "resolved":        False,
            "manual_override": True,
        })
    else:
        updated.update({
            "entity_id":       chosen["entity_id"],
            "matched_name":    chosen["matched_name"],
            "score":           chosen["score"],
            "match_strength":  chosen["match_strength"],
            "entity_type":     chosen["entity_type"],
            "confidence":      _classify_confidence(chosen["score"]),
            "resolved":        True,
            "error":           None,
            "manual_override": chosen["entity_id"] != record.get("entity_id"),
        })

    return updated

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

    Uses the Sayari resolution endpoint with limit=CANDIDATE_LIMIT so we get the
    top-N ranked candidates, not just the single best match. The top result
    (index 0) becomes the working resolution; all candidates are retained so a
    human reviewer can re-map a low-confidence match offline. The best match's
    score is classified into a confidence band, and weak matches are flagged
    needs_review.

    Returns a dict with resolved=True on success, or resolved=False with an error
    message if no match is found or the API call fails.
    """
    try:
        response = client.resolution.resolution(
            name=name,
            country=country,
            limit=CANDIDATE_LIMIT,
            candidate_pool_size=CANDIDATE_POOL_SIZE,
        )
        if not response.data:
            return _unresolved(name, country, "no_match")

        candidates = [_candidate(r) for r in response.data[:CANDIDATE_LIMIT]]
        top = candidates[0]
        confidence = _classify_confidence(top["score"])

        return {
            "input_name":      name,
            "input_country":   country,
            "entity_id":       top["entity_id"],
            "matched_name":    top["matched_name"],
            "match_strength":  top["match_strength"],
            "score":           top["score"],
            "entity_type":     top["entity_type"],
            "confidence":      confidence,
            "needs_review":    confidence != "high",
            "reviewed":        False,
            "manual_override": False,
            "candidates":      candidates,
            "resolved":        True,
            "error":           None,
        }
    except Exception as e:
        return _unresolved(name, country, str(e))


def _unresolved(name: str, country: str, error: str) -> dict:
    """Returns a consistently structured failure record so the analytics layer
    can count and report unresolved entities without special-casing None values.

    Unresolved entities are always flagged needs_review — a name we couldn't
    match at all is exactly what a human should look at first."""
    return {
        "input_name":      name,
        "input_country":   country,
        "entity_id":       None,
        "matched_name":    None,
        "match_strength":  None,
        "score":           None,
        "entity_type":     None,
        "confidence":      "none",
        "needs_review":    True,
        "reviewed":        False,
        "manual_override": False,
        "candidates":      [],
        "resolved":        False,
        "error":           error,
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
    import sys

    from client import get_client

    # Matched names can contain non-Latin characters (e.g. Cyrillic). The default
    # Windows console encoding (cp1252) can't print them, so force UTF-8 stdout.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    client = get_client()
    print("Resolving all entities...\n")
    results = load_or_build_resolved(client)

    resolved = [r for r in results if r["resolved"]]
    unresolved = [r for r in results if not r["resolved"]]
    low_conf = [r for r in resolved if r.get("confidence") == "low"]

    print(f"\nResolution complete: {len(resolved)}/{len(results)} resolved")

    if low_conf:
        print(f"\n{len(low_conf)} low-confidence match(es) flagged for review "
              f"(score < {REVIEW_SCORE_THRESHOLD}):")
        for r in sorted(low_conf, key=lambda r: r["score"]):
            print(f"  - {r['input_name']} -> {r['matched_name']} (score {r['score']:.1f})")

    if unresolved:
        print("\nUnresolved entities:")
        for r in unresolved:
            print(f"  - {r['input_name']}: {r['error']}")

    print("\nReview low-confidence matches in the app: Resolution Review tab.")
