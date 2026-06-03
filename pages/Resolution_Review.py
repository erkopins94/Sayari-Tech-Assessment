"""
pages/Resolution_Review.py — Human-in-the-loop entity resolution triage.

Name resolution is probabilistic: the Sayari endpoint returns a ranked list of
candidates with confidence scores, and resolver.py takes the top one. For strong
matches that's fine, but a weak top match (e.g. "State Development Bank VEB.RF"
matching a *Belarusian* bank at a low score) is a false positive that would
silently poison every downstream analytic and AI answer.

This tab surfaces those low-confidence matches so an analyst can:
  - see the score and confidence band for every entity,
  - inspect the alternative candidates the API returned,
  - spot jurisdiction mismatches (input country vs. matched country),
  - re-map to the correct candidate, or mark the entity unresolved.

Decisions are written back to data/resolved.json (the same cache fetcher.py and
the dashboard read), with an audit trail (reviewed / manual_override) on each
record. No live API calls are made — the candidate lists were captured offline
when resolver.py ran, so this whole workflow runs against the cache.
"""

import json
import os

import pandas as pd
import streamlit as st

from resolver import REVIEW_SCORE_THRESHOLD, apply_review_decision

# ---------------------------------------------------------------------------
# Page configuration
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Resolution Review",
    page_icon="🪪",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# This page lives in pages/ so the path to data/ needs one level up (..).
_RESOLVED_CACHE = os.path.join(
    os.path.dirname(__file__), "..", "data", "resolved.json"
)

# Confidence band → (emoji badge, human label) for consistent display.
_CONFIDENCE_BADGE = {
    "high": ("✅", "High"),
    "low":  ("⚠️", "Low"),
    "none": ("❌", "Unresolved"),
}


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

@st.cache_data
def load_resolved() -> list[dict]:
    """Load the resolved-entities cache from disk (cached across reruns)."""
    if not os.path.exists(_RESOLVED_CACHE):
        return []
    with open(_RESOLVED_CACHE, encoding="utf-8") as f:
        return json.load(f)


def save_resolved(records: list[dict]) -> None:
    """
    Persist the updated records back to data/resolved.json and clear the cache
    so the next rerun reflects the change.

    Written with ensure_ascii=False so non-Latin entity names stay human-readable
    in the file, and utf-8 encoding so the write is safe on Windows.
    """
    with open(_RESOLVED_CACHE, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    load_resolved.clear()


# ---------------------------------------------------------------------------
# Small display helpers
# ---------------------------------------------------------------------------

def _badge(confidence: str) -> str:
    """Return an emoji + label badge string for a confidence band."""
    emoji, label = _CONFIDENCE_BADGE.get(confidence, ("❔", "Unknown"))
    return f"{emoji} {label}"


def _score_text(score) -> str:
    """Format a score for display, tolerating None (unresolved entities)."""
    return f"{score:.1f}" if isinstance(score, (int, float)) else "—"


def _country_mismatch(record: dict) -> bool:
    """
    True if the entity's input country is absent from its top candidate's
    countries — a strong tell that the match jumped jurisdictions.
    """
    candidates = record.get("candidates") or []
    if not candidates:
        return False
    input_country = record.get("input_country")
    top_countries = candidates[0].get("countries") or []
    return bool(input_country) and bool(top_countries) and input_country not in top_countries


def _candidate_label(cand: dict) -> str:
    """One-line description of a candidate for the radio picker."""
    countries = ", ".join(cand.get("countries") or []) or "—"
    return (
        f"{cand.get('matched_name')}  ·  score {_score_text(cand.get('score'))}"
        f"  ·  {countries}  ·  {cand.get('entity_type') or '—'}"
    )


# ---------------------------------------------------------------------------
# Review widget for a single entity
# ---------------------------------------------------------------------------

def render_review_card(record: dict, records: list[dict]) -> None:
    """
    Render one expandable review card with the candidate picker and save button.

    On save, applies the decision via the pure apply_review_decision() helper,
    writes the full list back to disk, and reruns so the queue refreshes.
    """
    name = record["input_name"]
    candidates = record.get("candidates") or []
    mismatch = _country_mismatch(record)

    flag = " · 🌐 country mismatch" if mismatch else ""
    title = (
        f"{_badge(record.get('confidence', 'none'))}  {name}"
        f"  →  {record.get('matched_name') or '(no match)'}"
        f"  ·  score {_score_text(record.get('score'))}{flag}"
    )

    with st.expander(title, expanded=mismatch):
        st.caption(
            f"Input: **{name}** ({record.get('input_country', '—')})  ·  "
            f"Current match strength: {record.get('match_strength') or '—'}"
        )

        if mismatch:
            st.warning(
                f"The top match is in **{', '.join(candidates[0].get('countries') or [])}**, "
                f"but the input entity is from **{record.get('input_country')}**. "
                "Verify this is the same entity before accepting."
            )

        if not candidates:
            st.info(
                "No candidate list cached for this entity. Re-run "
                "`python resolver.py` (delete data/resolved.json first) to "
                "capture candidates for offline re-mapping."
            )
            return

        # Build the picker: every candidate, plus an explicit "unresolved" option.
        # Default selection is the current best match (index 0).
        option_indices = list(range(len(candidates))) + [-1]

        def _format(i: int) -> str:
            if i == -1:
                return "❌ None of these — mark unresolved"
            prefix = "▶ current best  ·  " if i == 0 else ""
            return prefix + _candidate_label(candidates[i])

        choice = st.radio(
            "Select the correct entity:",
            options=option_indices,
            format_func=_format,
            key=f"radio_{name}",
        )

        if st.button("Save decision", key=f"save_{name}", type="primary"):
            chosen = None if choice == -1 else candidates[choice]
            updated = apply_review_decision(record, chosen)

            # Replace the record in-place within the full list, then persist.
            idx = next(i for i, r in enumerate(records) if r["input_name"] == name)
            records[idx] = updated
            save_resolved(records)

            if chosen is None:
                st.warning(f"'{name}' marked unresolved.")
            elif updated["manual_override"]:
                st.success(f"'{name}' re-mapped to '{updated['matched_name']}'.")
            else:
                st.success(f"'{name}' confirmed — current match accepted.")
            st.rerun()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.title("🪪 Resolution Review")
    st.markdown(
        "Name resolution is probabilistic. This tab surfaces **low-confidence "
        "matches** so they can be verified or re-mapped before they flow into the "
        "analytics and AI chat — turning a hidden data-quality risk into an "
        "auditable, human-in-the-loop decision."
    )

    records = load_resolved()
    if not records:
        st.error(
            "No resolved data found. Run `python resolver.py` to build "
            "data/resolved.json first."
        )
        return

    # Old-schema cache (pre-confidence) won't have the 'confidence' key.
    if not any("confidence" in r for r in records):
        st.warning(
            "This resolved.json predates the confidence/candidate upgrade. "
            "Delete data/resolved.json and re-run `python resolver.py` to enable "
            "candidate re-mapping."
        )

    # --- Summary metrics --------------------------------------------------
    total = len(records)
    high = sum(1 for r in records if r.get("confidence") == "high")
    low = sum(1 for r in records if r.get("confidence") == "low")
    unresolved = sum(1 for r in records if not r.get("resolved"))
    reviewed = sum(1 for r in records if r.get("reviewed"))

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total entities", total)
    c2.metric("✅ High confidence", high)
    c3.metric("⚠️ Low confidence", low)
    c4.metric("❌ Unresolved", unresolved)
    c5.metric("Reviewed", reviewed)

    st.caption(
        f"Confidence floor: score ≥ {REVIEW_SCORE_THRESHOLD} is treated as high "
        "confidence. Matches below it, and unresolved names, are queued for review."
    )

    # --- Stale-profile warning -------------------------------------------
    overridden = [r for r in records if r.get("manual_override") and r.get("resolved")]
    if overridden:
        names = ", ".join(r["input_name"] for r in overridden)
        st.info(
            f"**{len(overridden)} entity(ies) were re-mapped:** {names}. "
            "Their cached profiles point at the previous match — delete "
            "data/profiles.json and data/relationships.json, then re-run "
            "`python fetcher.py` and `python rel_fetcher.py` to refresh them."
        )

    st.divider()

    # --- Review queue -----------------------------------------------------
    queue = [r for r in records if r.get("needs_review") and not r.get("reviewed")]

    st.subheader(f"Review queue ({len(queue)})")
    if not queue:
        st.success("Nothing to review — every entity is high confidence or already reviewed.")
    else:
        # Lowest score first: the weakest, riskiest matches surface at the top.
        queue.sort(key=lambda r: (r.get("score") is not None, r.get("score") or 0))
        for record in queue:
            render_review_card(record, records)

    # --- Full resolution table -------------------------------------------
    st.divider()
    st.subheader("All entities")
    table = pd.DataFrame([
        {
            "Input name": r["input_name"],
            "Country": r.get("input_country"),
            "Confidence": _badge(r.get("confidence", "none")),
            "Score": _score_text(r.get("score")),
            "Matched name": r.get("matched_name") or "—",
            "Reviewed": "✓" if r.get("reviewed") else "",
            "Overridden": "✓" if r.get("manual_override") else "",
        }
        for r in records
    ])
    st.dataframe(table, hide_index=True, use_container_width=True)


if __name__ == "__main__":
    main()
