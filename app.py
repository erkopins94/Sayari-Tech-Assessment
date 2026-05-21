"""
app.py — Streamlit dashboard for the Sayari Entity Analytics Report.

This is the presentation layer. It loads profiles from the local cache,
calls analytics.py for computed insights, and renders everything as
interactive Plotly charts inside a tabbed Streamlit layout.

No API calls are made here. All data flows from data/profiles.json.
"""

import json
import os

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from analytics import (
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
# Page configuration — must be the first Streamlit call in the script
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Sayari Entity Analytics",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Visual constants
# Centralising colours and the Plotly template here means every chart
# stays visually consistent without repeating style arguments everywhere.
# ---------------------------------------------------------------------------

CHART_TEMPLATE = "plotly_white"

# Severity colours follow standard risk-traffic-light conventions so the
# charts are immediately readable without a legend explanation
RISK_COLORS = {
    "critical": "#C0392B",
    "high":     "#E67E22",
    "elevated": "#F1C40F",
    "relevant": "#3498DB",
}

# Qualitative palette for sector and sanctions-list charts
SECTOR_PALETTE = px.colors.qualitative.Safe

PROFILES_CACHE = os.path.join(os.path.dirname(__file__), "data", "profiles.json")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_data
def load_profiles() -> list[dict]:
    """
    Loads entity profiles from the local cache if available.
    Falls back to fetching live from the Sayari API if the cache is missing
    (e.g. first run in a fresh deployment). Results are cached by Streamlit
    for the session so the file is only read once per app lifecycle.
    """
    if os.path.exists(PROFILES_CACHE):
        with open(PROFILES_CACHE) as f:
            return json.load(f)

    # Cache miss — pull live data and write it for subsequent runs
    from client import get_client
    from fetcher import load_or_build_profiles
    return load_or_build_profiles(get_client())


# ---------------------------------------------------------------------------
# Shared chart helpers
# ---------------------------------------------------------------------------

def horizontal_bar(data: dict, title: str, color: str = "#2E86AB",
                   x_label: str = "Count", height: int = 400) -> go.Figure:
    """
    Renders a sorted horizontal bar chart from a {label: count} dict.
    Sorted ascending so the largest bar sits at the top when displayed.
    """
    labels = list(data.keys())
    values = list(data.values())

    # Sort ascending so highest value appears at the top of the chart
    pairs = sorted(zip(values, labels))
    values, labels = zip(*pairs) if pairs else ([], [])

    fig = go.Figure(go.Bar(
        x=values,
        y=labels,
        orientation="h",
        marker_color=color,
        hovertemplate="%{y}: %{x}<extra></extra>",
    ))
    fig.update_layout(
        title=title,
        template=CHART_TEMPLATE,
        height=height,
        xaxis_title=x_label,
        yaxis_title=None,
        margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig


# ---------------------------------------------------------------------------
# Tab renderers — one function per tab keeps main() clean and each section
# independently testable / adjustable without touching the others
# ---------------------------------------------------------------------------

def render_overview(profiles: list[dict]) -> None:
    """
    Overview tab: headline KPI cards followed by sector and entity-type breakdowns.
    Designed to give a viewer a full picture of the dataset in under 30 seconds.
    """
    stats = summary_stats(profiles)

    # --- KPI metric cards ---
    st.subheader("At a Glance")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Entities",       stats["total_entities"])
    c2.metric("Sanctioned",           f"{stats['sanctioned_count']} ({stats['sanctioned_pct']}%)")
    c3.metric("State-Owned",          stats["state_owned_count"])
    c4.metric("Export Controlled",    stats["export_controls_count"])
    c5.metric("Countries Reached",    stats["countries_represented"])
    c6.metric("Avg Sanctions Lists",  stats["avg_sanctions_lists"])

    st.markdown("---")

    # --- Sector breakdown and network connections side by side ---
    col_left, col_right = st.columns(2)

    with col_left:
        # Donut chart shows proportional split across sectors at a glance
        sectors = sector_breakdown(profiles)
        fig = px.pie(
            names=list(sectors.keys()),
            values=list(sectors.values()),
            title="Entity Breakdown by Sector",
            hole=0.45,
            color_discrete_sequence=SECTOR_PALETTE,
        )
        fig.update_traces(textposition="inside", textinfo="percent+label")
        fig.update_layout(template=CHART_TEMPLATE, showlegend=False,
                          margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)

    with col_right:
        # Top 10 entities by total network connections — surfaces the most
        # structurally important nodes in the broader risk network
        top = top_entities_by_degree(profiles, n=10)
        df_top = pd.DataFrame(top)
        fig = px.bar(
            df_top,
            x="degree",
            y="name",
            orientation="h",
            title="Top 10 Entities by Network Degree",
            color="sector",
            color_discrete_sequence=SECTOR_PALETTE,
            labels={"degree": "Known Network Connections", "name": ""},
        )
        fig.update_layout(template=CHART_TEMPLATE, showlegend=True,
                          yaxis={"categoryorder": "total ascending"},
                          margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)

    # --- Total network connections callout ---
    total = stats["total_network_connections"]
    st.info(
        f"Combined, these {stats['fetched']} entities have **{total:,} known network connections** "
        f"spanning **{stats['countries_represented']} countries** — "
        f"illustrating the scale of corporate infrastructure underlying this sanctioned network."
    )


def render_sanctions(profiles: list[dict]) -> None:
    """
    Sanctions tab: breaks down which sanctions lists are in play and which
    entities carry the heaviest sanctions burden. Both charts are interactive —
    clicking a sanctions list bar shows which entities appear on it, and
    clicking an entity bar shows which lists that entity is on.
    """
    from analytics import SECTOR_MAP

    # Pre-build two lookups used by both drill-down panels:
    #   list_to_entities  — sanctions list name → profiles on that list
    #   entity_to_lists   — entity name → sanctions lists it appears on
    fetched = [p for p in profiles if p.get("fetched")]
    list_to_entities: dict[str, list[dict]] = {}
    for p in fetched:
        for sl in p["sanctions_lists"]:
            list_to_entities.setdefault(sl, []).append(p)

    # ---------------------------------------------------------------------------
    # Chart 1 — Entities per sanctions list (clickable)
    # ---------------------------------------------------------------------------
    st.subheader("Sanctions Intelligence")
    st.caption("Click any bar to see which entities appear on that sanctions list.")

    sl_data = sanctions_list_breakdown(profiles)
    fig = horizontal_bar(
        sl_data,
        title="Entities per Sanctions List",
        color="#C0392B",
        x_label="Number of Entities",
        height=600,
    )

    # on_select="rerun" causes Streamlit to re-execute the script when a bar is
    # clicked, returning the selected point data in the event object so we can
    # render the drill-down panel below without any extra state management.
    list_event = st.plotly_chart(
        fig,
        use_container_width=True,
        on_select="rerun",
        selection_mode="points",
        key="sanctions_list_chart",
    )

    # --- Drill-down: entities on the selected list ---
    selected_list = None
    if list_event and list_event.selection and list_event.selection.points:
        # For a horizontal bar chart, the y-axis holds the label (list name)
        selected_list = list_event.selection.points[0].get("y")

    if selected_list and selected_list in list_to_entities:
        entities = sorted(list_to_entities[selected_list], key=lambda p: p["input_name"])
        st.success(f"**{selected_list}** — {len(entities)} {'entity' if len(entities) == 1 else 'entities'}")

        rows = []
        for p in entities:
            rows.append({
                "Entity":              p["input_name"],
                "Sector":              SECTOR_MAP.get(p["input_name"], "Other"),
                "Countries":           len(p["countries"]),
                "Total Sanctions Lists": len(p["sanctions_lists"]),
                "Network Degree":      f"{p['degree']:,}",
                "State-Owned":         "✓" if "state_owned" in p["risk_flags"] else "—",
            })

        st.dataframe(
            pd.DataFrame(rows).sort_values("Entity"),
            use_container_width=True,
            hide_index=True,
        )
    else:
        # Shown when no bar has been clicked yet
        st.info("👆 Click a bar above to drill into the entities on that list.")

    st.markdown("---")

    # ---------------------------------------------------------------------------
    # Chart 2 — Sanctions list coverage per entity (clickable)
    # ---------------------------------------------------------------------------
    st.subheader("Sanctions List Coverage per Entity")
    st.caption("How many distinct sanctions lists each entity appears on. Click an entity to see exactly which lists it is on.")

    coverage = sanctions_coverage_per_entity(profiles)
    df_cov = pd.DataFrame(coverage)

    fig2 = px.bar(
        df_cov,
        x="list_count",
        y="name",
        orientation="h",
        color="sector",
        color_discrete_sequence=SECTOR_PALETTE,
        title="Number of Sanctions Lists per Entity",
        labels={"list_count": "Sanctions Lists", "name": ""},
    )
    fig2.update_layout(
        template=CHART_TEMPLATE,
        height=900,
        yaxis={"categoryorder": "total ascending"},
        margin=dict(l=10, r=10, t=40, b=10),
    )

    entity_event = st.plotly_chart(
        fig2,
        use_container_width=True,
        on_select="rerun",
        selection_mode="points",
        key="entity_sanctions_chart",
    )

    # --- Drill-down: lists for the selected entity ---
    selected_entity = None
    if entity_event and entity_event.selection and entity_event.selection.points:
        selected_entity = entity_event.selection.points[0].get("y")

    if selected_entity:
        # Find the matching profile (y label may be truncated, so use startswith match)
        match = next(
            (p for p in fetched if p["input_name"] == selected_entity),
            None,
        )
        if match and match["sanctions_lists"]:
            st.success(f"**{selected_entity}** — appears on {len(match['sanctions_lists'])} sanctions lists")
            for sl in sorted(match["sanctions_lists"]):
                st.markdown(f"- {sl}")
        elif match:
            st.info(f"**{selected_entity}** has no sanctions list entries in this dataset.")
    else:
        st.info("👆 Click an entity bar above to see its specific sanctions list memberships.")


def render_risk(profiles: list[dict]) -> None:
    """
    Risk tab: surfaces the distribution of risk flag types and the aggregate
    severity landscape across the dataset. The risk flag chart is interactive —
    clicking a flag reveals which entities carry it and at what severity level.
    """
    from analytics import RISK_FLAG_LABELS

    # Reverse the label map so we can go from readable label → raw API key
    # when a bar is clicked and we need to filter the profiles
    label_to_key = {v: k for k, v in RISK_FLAG_LABELS.items()}
    fetched = [p for p in profiles if p.get("fetched")]

    st.subheader("Risk Landscape")
    st.caption("Click a risk flag on the left chart to see which entities carry it.")

    col_left, col_right = st.columns(2)

    # Capture the click event inside the column but render the drill-down
    # below the columns at full width so it isn't cramped
    flag_event = None
    with col_left:
        flag_data = risk_flag_frequency(profiles)
        fig = horizontal_bar(
            flag_data,
            title="Risk Flag Frequency Across All Entities",
            color="#E67E22",
            x_label="Number of Entities",
            height=450,
        )
        # on_select="rerun" triggers a Streamlit rerun on click, returning the
        # selected bar's y-value (the readable flag label) in the event object
        flag_event = st.plotly_chart(
            fig,
            use_container_width=True,
            on_select="rerun",
            selection_mode="points",
            key="risk_flag_chart",
        )

    with col_right:
        # Aggregate risk level distribution — total flags at each severity level
        # including network flags, showing overall depth of risk embedding
        levels = risk_level_distribution(profiles)
        fig = go.Figure(go.Bar(
            x=list(levels.keys()),
            y=list(levels.values()),
            marker_color=[RISK_COLORS[k] for k in levels.keys()],
            hovertemplate="%{x}: %{y} flags<extra></extra>",
        ))
        fig.update_layout(
            title="Aggregate Risk Flags by Severity Level",
            template=CHART_TEMPLATE,
            xaxis_title="Severity Level",
            yaxis_title="Total Flag Count",
            height=450,
            margin=dict(l=10, r=10, t=40, b=10),
        )
        st.plotly_chart(fig, use_container_width=True)

    # --- Drill-down panel (full width, below both columns) ---
    selected_label = None
    if flag_event and flag_event.selection and flag_event.selection.points:
        # The y-axis of the horizontal bar holds the readable flag label
        selected_label = flag_event.selection.points[0].get("y")

    if selected_label:
        raw_key = label_to_key.get(selected_label)
        if raw_key:
            # Filter to entities that carry this specific risk flag
            matching = [p for p in fetched if raw_key in p["risk_flags"]]
            matching.sort(key=lambda p: p["input_name"])

            from analytics import SECTOR_MAP
            st.success(f"**{selected_label}** — {len(matching)} {'entity' if len(matching) == 1 else 'entities'}")

            rows = []
            for p in matching:
                rows.append({
                    "Entity":          p["input_name"],
                    "Sector":          SECTOR_MAP.get(p["input_name"], "Other"),
                    "Flag Severity":   p["risk_flags"].get(raw_key, "—"),
                    "Sanctioned":      "✓" if p["sanctioned"] else "—",
                    "State-Owned":     "✓" if "state_owned" in p["risk_flags"] else "—",
                    "Countries":       len(p["countries"]),
                    "Network Degree":  f"{p['degree']:,}",
                })

            st.dataframe(
                pd.DataFrame(rows),
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.info("👆 Click a risk flag above to see which entities carry it.")

    # Callout explaining what network-level risk flags mean in plain terms
    st.warning(
        "**Note on severity counts:** The aggregate flag counts above include both direct flags "
        "(e.g. an entity being sanctioned) and network-level flags (e.g. being owned by a sanctioned entity). "
        "High network-level flag counts indicate deep embeddedness in the risk graph — "
        "making these entities difficult to isolate even when direct sanctions are applied."
    )


def render_geography(profiles: list[dict]) -> None:
    """
    Geography tab: visualises the global footprint of these entities through
    a choropleth world map and a jurisdiction exposure ranking. The map
    immediately conveys that this is not a regionally confined risk — it is
    a globally distributed network.
    """
    st.subheader("Geographic Reach")

    # --- World map choropleth ---
    country_data = country_breakdown(profiles)
    df_map = pd.DataFrame(
        list(country_data.items()), columns=["country_code", "entity_count"]
    )

    fig = px.choropleth(
        df_map,
        locations="country_code",
        locationmode="ISO-3",
        color="entity_count",
        color_continuous_scale="Reds",
        title="Entity Presence by Country",
        labels={"entity_count": "Entities Present"},
    )
    fig.update_layout(
        template=CHART_TEMPLATE,
        height=500,
        margin=dict(l=0, r=0, t=40, b=0),
        coloraxis_colorbar=dict(title="Entities"),
    )
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")

    # --- Jurisdiction exposure bar chart ---
    st.subheader("Entities with the Widest Jurisdictional Footprint")
    st.caption("Entities present in more countries are harder to sanction effectively — corporate structures can shift assets across jurisdictions.")

    exposure = jurisdiction_exposure(profiles)[:20]
    df_exp = pd.DataFrame(exposure)

    fig = px.bar(
        df_exp,
        x="country_count",
        y="name",
        orientation="h",
        color="country_count",
        color_continuous_scale="Reds",
        title="Top 20 Entities by Number of Countries Present",
        labels={"country_count": "Countries Present", "name": ""},
    )
    fig.update_layout(
        template=CHART_TEMPLATE,
        height=600,
        yaxis={"categoryorder": "total ascending"},
        margin=dict(l=10, r=10, t=40, b=10),
        coloraxis_showscale=False,
    )
    st.plotly_chart(fig, use_container_width=True)


def render_entity_table(profiles: list[dict]) -> None:
    """
    Entity Detail tab: a sortable, filterable table giving the full picture
    for every entity in the dataset. Useful for a client who wants to drill
    into a specific entity after seeing the macro charts.
    """
    st.subheader("Entity Detail")
    st.caption("Full dataset — click column headers to sort.")

    from analytics import SECTOR_MAP

    fetched = [p for p in profiles if p.get("fetched")]
    rows = []
    for p in fetched:
        rows.append({
            "Entity":              p["input_name"],
            "Sector":              SECTOR_MAP.get(p["input_name"], "Other"),
            "Type":                p["entity_type"] or "—",
            "Countries":           len(p["countries"]),
            "Sanctioned":          "✓" if p["sanctioned"] else "—",
            "State-Owned":         "✓" if "state_owned" in p["risk_flags"] else "—",
            "Export Controls":     "✓" if "export_controls" in p["risk_flags"] else "—",
            "Sanctions Lists":     len(p["sanctions_lists"]),
            "Network Degree":      p["degree"],
            "Critical Flags":      p["risk_level_counts"].get("critical", 0),
            "High Flags":          p["risk_level_counts"].get("high", 0),
        })

    df = pd.DataFrame(rows).sort_values("Network Degree", ascending=False)
    st.dataframe(df, use_container_width=True, height=700)


# ---------------------------------------------------------------------------
# App entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Main entry point. Sets up the page header, loads data, and delegates
    rendering to the per-tab functions above.
    """
    # --- Header ---
    st.title("🔍 Sayari Entity Analytics Report")
    st.markdown(
        "Macro-level intelligence across **50 high-risk entities** — "
        "sanctions exposure, risk profiles, geographic reach, and network connectivity. "
        "Data sourced via the [Sayari API](https://documentation.sayari.com)."
    )
    st.markdown("---")

    profiles = load_profiles()

    # --- Tabs ---
    tabs = st.tabs([
        "📊 Overview",
        "🚨 Sanctions",
        "⚠️ Risk Profile",
        "🌍 Geography",
        "📋 Entity Detail",
    ])

    with tabs[0]:
        render_overview(profiles)
    with tabs[1]:
        render_sanctions(profiles)
    with tabs[2]:
        render_risk(profiles)
    with tabs[3]:
        render_geography(profiles)
    with tabs[4]:
        render_entity_table(profiles)


if __name__ == "__main__":
    main()
