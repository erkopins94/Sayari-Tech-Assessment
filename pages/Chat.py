"""
pages/chat.py — AI-powered chat interface for the Sayari entity dataset.

Users ask natural language questions about the 49 sanctioned/high-risk entities.
Claude interprets each question, calls the appropriate tool(s) from tools.py,
and synthesises a plain-English answer from the structured results.

Intentionally separate from app.py (the visual dashboard) — the dashboard
provides pre-built chart views, this page handles ad-hoc conversational queries.
They share the same profiles cache and tools.py, but serve different use cases:
  - Dashboard: "show me everything about sanctions" → visual overview
  - Chat:       "which defense firms in China have >10 sanctions lists?" → specific answer
"""

import json
import os

import anthropic
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

from tools import (
    ToolResult,
    chart_country_map,
    chart_risk_flags,
    chart_risk_severity,
    chart_sanctions_lists,
    chart_sector_breakdown,
    chart_top_entities,
    compare_entities,
    filter_entities,
    get_entity,
    get_summary_stats,
    rank_entities,
)

# Load .env so ANTHROPIC_API_KEY is available — client.py handles this for
# the main dashboard but chat.py runs independently so we load it here too.
load_dotenv()

# ---------------------------------------------------------------------------
# Page configuration
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Sayari AI Analyst",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Profile loading
#
# chat.py lives in pages/ so the path to data/ needs one level up (..).
# Same cache-first pattern as app.py — no API calls during normal use.
# ---------------------------------------------------------------------------

_PROFILES_CACHE = os.path.join(
    os.path.dirname(__file__), "..", "data", "profiles.json"
)


@st.cache_data
def load_profiles() -> list[dict]:
    """Load entity profiles from the local cache, falling back to live fetch."""
    if os.path.exists(_PROFILES_CACHE):
        with open(_PROFILES_CACHE) as f:
            return json.load(f)

    # Cache miss — pull live and write for subsequent runs
    from client import get_client
    from fetcher import load_or_build_profiles
    return load_or_build_profiles(get_client())


# ---------------------------------------------------------------------------
# Anthropic tool definitions
#
# These mirror the functions in tools.py in the JSON schema format the
# Anthropic API requires. The descriptions are what Claude reads to decide
# which tool to call — they must be accurate and unambiguous.
# ---------------------------------------------------------------------------

_TOOL_DEFINITIONS = [
    {
        "name": "get_entity",
        "description": (
            "Look up a single entity by name and return its full profile: "
            "sanctions status, which sanctions lists it appears on, countries it "
            "operates in, risk flags, network degree, sector, and state ownership. "
            "Use this for any question about a specific named entity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Entity name as typed by the user. "
                        "Fuzzy matched — exact spelling not required."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "filter_entities",
        "description": (
            "Filter the dataset by one or more criteria and return matching entities. "
            "All parameters are optional and combined with AND logic. "
            "Use this for questions like 'show me all defense firms', "
            "'which entities operate in China', or 'which entities are on the OFAC SDN list'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {
                    "type": "string",
                    "description": "Sector name, e.g. 'Defense', 'Banking'. Partial match.",
                },
                "country": {
                    "type": "string",
                    "description": "Country name ('Russia') or ISO-3 code ('RUS').",
                },
                "sanctions_list": {
                    "type": "string",
                    "description": "Sanctions list name, e.g. 'OFAC SDN'. Fuzzy matched.",
                },
                "risk_flag": {
                    "type": "string",
                    "description": (
                        "Risk flag label, e.g. 'State Owned Enterprise', 'Export Controls'."
                    ),
                },
                "sanctioned": {
                    "type": "boolean",
                    "description": "True for sanctioned entities only. False for unsanctioned only.",
                },
                "state_owned": {
                    "type": "boolean",
                    "description": "True for state-owned entities only. False for private only.",
                },
            },
        },
    },
    {
        "name": "rank_entities",
        "description": (
            "Rank entities by a numeric metric and return the top or bottom N results. "
            "Use this for questions like 'which entity has the most sanctions lists', "
            "'top 5 by network connections', or 'rank defense firms by country count'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "description": (
                        "Metric to rank by. Accepted values: "
                        "'degree' / 'connections' / 'network degree', "
                        "'sanctions lists' / 'lists', "
                        "'countries' / 'geographic footprint', "
                        "'critical' / 'critical flags', "
                        "'high' / 'high flags', "
                        "'elevated' / 'elevated flags'."
                    ),
                },
                "n": {
                    "type": "integer",
                    "description": "Number of results to return. Defaults to 10.",
                },
                "ascending": {
                    "type": "boolean",
                    "description": "False (default) = highest first. True = lowest first.",
                },
                "sector": {
                    "type": "string",
                    "description": "Optional sector filter applied before ranking.",
                },
            },
            "required": ["metric"],
        },
    },
    {
        "name": "compare_entities",
        "description": (
            "Compare two or more entities side by side across all key metrics. "
            "Use this for questions like 'compare ZTE and Huawei', "
            "'how do Rosneft and Gazprom differ', or 'which is more heavily sanctioned'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of 2–10 entity names to compare. Fuzzy matched.",
                },
            },
            "required": ["names"],
        },
    },
    {
        "name": "get_summary_stats",
        "description": (
            "Return dataset-wide aggregate statistics. Use this for macro questions "
            "like 'give me a summary of the dataset', 'what percentage are sanctioned', "
            "'how many state-owned enterprises are there', or 'what is the total network footprint'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    # --- Chart tools ---
    # These generate Plotly charts rendered inline in the chat UI.
    # Use them whenever the user asks to "show", "plot", "visualise", or "chart" data.
    {
        "name": "chart_sector_breakdown",
        "description": (
            "Generate a donut chart showing the entity count per sector. "
            "Use for any request to visualise or show the sector distribution of the dataset."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "chart_top_entities",
        "description": (
            "Generate a horizontal bar chart ranking the top N entities by a chosen metric. "
            "Use for requests like 'plot the top 10 by network connections', "
            "'show me a chart of defense firms ranked by sanctions lists', or "
            "'visualise which entities have the widest geographic footprint'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "description": (
                        "Metric to rank by. Same aliases as rank_entities: "
                        "'degree' / 'connections', 'sanctions lists' / 'lists', "
                        "'countries' / 'geographic footprint', "
                        "'critical flags', 'high flags', 'elevated flags'."
                    ),
                },
                "n": {
                    "type": "integer",
                    "description": "Number of entities to show. Defaults to 10.",
                },
                "sector": {
                    "type": "string",
                    "description": "Optional sector filter applied before ranking.",
                },
            },
            "required": ["metric"],
        },
    },
    {
        "name": "chart_sanctions_lists",
        "description": (
            "Generate a horizontal bar chart showing how many entities appear on each "
            "sanctions list. Use for requests to visualise sanctions list coverage or "
            "compare the reach of different sanctions programmes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "chart_risk_flags",
        "description": (
            "Generate a horizontal bar chart of risk flag frequency across all entities. "
            "Use for requests to visualise or chart risk flags, risk indicators, or "
            "how common each type of risk is in the dataset."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "chart_risk_severity",
        "description": (
            "Generate a vertical bar chart of aggregate risk flag counts by severity level "
            "(critical, high, elevated, relevant). Use for requests to visualise the overall "
            "risk severity distribution or compare risk levels across the dataset."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "chart_country_map",
        "description": (
            "Generate a choropleth world map showing how many entities are present in each "
            "country. Use for requests to visualise geographic footprint, map entity presence "
            "by country, or show where in the world these entities operate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]

# ---------------------------------------------------------------------------
# System prompt
#
# Tells Claude what data it has access to, how to use the tools, what tone
# to adopt, and — critically — what it cannot answer. A well-defined boundary
# here prevents hallucination on out-of-scope questions.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an AI analyst with access to a curated dataset of 49 high-risk and sanctioned entities sourced from the Sayari knowledge graph. The dataset includes Russian state-owned enterprises, defense firms, and entities from Iran, China, Venezuela, North Korea, Syria, and Belarus.

Always call a tool to retrieve facts before answering. Do not rely on training knowledge for claims about these specific entities — the tools are the authoritative source.

Tool selection guide — data tools (return text answers):
- get_entity        → any question about a specific named entity
- filter_entities   → filtering or searching by sector, country, sanctions list, risk flag, or status
- rank_entities     → ranking, top N, or superlative questions ("most", "highest", "fewest")
- compare_entities  → explicit side-by-side comparison of two or more named entities
- get_summary_stats → macro or aggregate questions about the full dataset

Tool selection guide — chart tools (render interactive charts in the UI):
Use these whenever the user asks to "show", "plot", "visualise", "chart", or "draw" something.
- chart_sector_breakdown → sector distribution donut chart
- chart_top_entities     → horizontal bar chart ranking entities by any metric (requires: metric)
- chart_sanctions_lists  → bar chart of entities per sanctions list
- chart_risk_flags       → bar chart of risk flag frequency
- chart_risk_severity    → bar chart of aggregate risk flags by severity level
- chart_country_map      → choropleth world map of entity presence by country

When you call a chart tool, the chart is rendered directly in the chat interface. In your text response, briefly describe what the chart shows and highlight the key insight — do not reproduce the data in table form, the chart speaks for itself.

Style guidelines:
- Be concise, direct, and analytical — this is a compliance intelligence tool
- Lead with the answer, then support it with the data
- Cite exact numbers, percentages, and rankings from the tool results
- If a question cannot be answered with the available data, say so clearly and explain why

Hard limits — do not speculate on:
- Revenues, financials, or transaction records
- Personnel, leadership, or ownership chains beyond what the tools return
- News, events, or developments after the dataset snapshot was collected
- Entities not present in the dataset"""

# ---------------------------------------------------------------------------
# Anthropic client
#
# @st.cache_resource persists the client across Streamlit reruns so we don't
# reconstruct the HTTP connection wrapper on every message the user sends.
# ---------------------------------------------------------------------------

@st.cache_resource
def _get_anthropic_client() -> anthropic.Anthropic:
    """Return a cached Anthropic client, initialised once per app session."""
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# Tool dispatcher
#
# Maps tool names returned by Claude → actual Python functions in tools.py.
# Uses **inputs unpacking so each function receives exactly the kwargs it
# expects — no translation layer needed as long as the JSON schema matches
# the function signatures in tools.py.
# ---------------------------------------------------------------------------

def _dispatch_tool(name: str, inputs: dict, profiles: list[dict]) -> ToolResult:
    """
    Call the appropriate tool function by name and return a ToolResult.

    Every return path is wrapped in ToolResult so the caller always receives
    the same type and can unconditionally use .api_payload for the Anthropic
    API and .figure for the Streamlit UI.

    Data tools  → ToolResult(api_payload=<result dict>, figure=None)
    Chart tools → ToolResult(api_payload={"status": "chart_rendered", ...},
                             figure=<go.Figure>)
                  OR ToolResult(api_payload=<error dict>, figure=None) on failure
    Unknown     → ToolResult(api_payload=<error dict>, figure=None)
    """
    # --- Data tools (return plain dicts for the LLM) ---
    if name == "get_entity":
        return ToolResult(api_payload=get_entity(inputs["name"], profiles))
    if name == "filter_entities":
        return ToolResult(api_payload=filter_entities(profiles, **inputs))
    if name == "rank_entities":
        return ToolResult(api_payload=rank_entities(profiles, **inputs))
    if name == "compare_entities":
        return ToolResult(api_payload=compare_entities(inputs["names"], profiles))
    if name == "get_summary_stats":
        return ToolResult(api_payload=get_summary_stats(profiles))

    # --- Chart tools (return go.Figure; send Claude a lightweight confirmation) ---
    chart_fn_map = {
        "chart_sector_breakdown": lambda: chart_sector_breakdown(profiles),
        "chart_top_entities":     lambda: chart_top_entities(
            profiles,
            metric=inputs["metric"],
            n=inputs.get("n", 10),
            sector=inputs.get("sector"),
        ),
        "chart_sanctions_lists":  lambda: chart_sanctions_lists(profiles),
        "chart_risk_flags":       lambda: chart_risk_flags(profiles),
        "chart_risk_severity":    lambda: chart_risk_severity(profiles),
        "chart_country_map":      lambda: chart_country_map(profiles),
    }

    if name in chart_fn_map:
        result = chart_fn_map[name]()
        if isinstance(result, go.Figure):
            # Extract the title so Claude can reference it in its response
            chart_title = (
                result.layout.title.text
                if result.layout.title and result.layout.title.text
                else name
            )
            return ToolResult(
                api_payload={"status": "chart_rendered", "chart_title": chart_title},
                figure=result,
            )
        # Chart function returned an error dict
        return ToolResult(api_payload=result)

    return ToolResult(api_payload={"error": True, "message": f"Unknown tool '{name}'."})


# ---------------------------------------------------------------------------
# Claude API call with tool-use loop
#
# Implements the standard Anthropic multi-step tool-use pattern:
#   1. Send messages → Claude responds with tool_use block(s)
#   2. Run the tool(s), collect results
#   3. Append results and loop → Claude responds with final text
# Repeats until stop_reason is "end_turn" (no more tools needed).
#
# api_messages is modified in-place (tool_use + tool_result blocks appended)
# so the full conversation context is preserved in session state across turns.
# ---------------------------------------------------------------------------

def _call_claude(
    api_messages: list[dict],
    profiles: list[dict],
) -> tuple[str, list[str], list[go.Figure]]:
    """
    Send the conversation to Claude and process tool calls until done.

    Args:
        api_messages: Full message history in Anthropic API format. Modified
                      in-place — tool_use and tool_result blocks are appended
                      during the loop so context is preserved for future turns.
        profiles:     Entity profiles passed through to the tool dispatcher.

    Returns:
        (response_text, tools_used, figures) — the final text answer, a list
        of tool names called this turn (shown in the UI as attribution), and a
        list of Plotly Figure objects collected from chart tools (rendered
        inline by the Streamlit UI after the text response).
    """
    client = _get_anthropic_client()
    tools_used: list[str] = []
    figures: list[go.Figure] = []   # figures collected from chart tools this turn

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            tools=_TOOL_DEFINITIONS,
            messages=api_messages,
        )

        # Claude produced a final text answer — we're done
        if response.stop_reason == "end_turn":
            text = next(
                (block.text for block in response.content if block.type == "text"),
                "No response generated.",
            )
            return text, tools_used, figures

        # Claude wants to call one or more tools — run them and loop back
        if response.stop_reason == "tool_use":
            # Append Claude's assistant turn (contains the tool_use blocks)
            api_messages.append({"role": "assistant", "content": response.content})

            # Execute each tool, collect API payloads and any chart figures
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tools_used.append(block.name)
                    tool_result = _dispatch_tool(block.name, block.input, profiles)
                    # Collect the figure for UI rendering (None for data tools)
                    if tool_result.figure is not None:
                        figures.append(tool_result.figure)
                    # Only the JSON-serialisable api_payload goes to the Anthropic API
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     json.dumps(tool_result.api_payload),
                    })

            # Append all results as a single user turn and loop for final answer
            api_messages.append({"role": "user", "content": tool_results})

        else:
            # Unexpected stop reason (e.g. "max_tokens") — exit the loop rather
            # than spinning forever. Surface whatever partial text Claude produced.
            text = next(
                (block.text for block in response.content if block.type == "text"),
                f"Response stopped unexpectedly (reason: {response.stop_reason}).",
            )
            return text, tools_used, figures


# ---------------------------------------------------------------------------
# Main UI
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Render the AI chat interface.

    Two message lists are maintained in session state:
      display_messages — text-only, rendered in the chat UI
      api_messages     — full Anthropic API history including tool_use and
                         tool_result blocks, needed to maintain conversation
                         context across turns
    """
    profiles = load_profiles()

    # --- Header ---
    st.title("🤖 Sayari AI Analyst")
    st.markdown(
        "Ask any question about the 49 sanctioned entities in this dataset. "
        "The AI queries live data — it does not guess."
    )
    st.markdown("---")

    # --- API key guard ---
    # Fail fast with a clear message rather than an obscure Anthropic API error
    if not os.getenv("ANTHROPIC_API_KEY"):
        st.error(
            "**ANTHROPIC_API_KEY not set.** "
            "Add it to your `.env` file and restart the app."
        )
        st.stop()

    # --- Session state initialisation ---
    if "display_messages" not in st.session_state:
        st.session_state.display_messages = []
    if "api_messages" not in st.session_state:
        st.session_state.api_messages = []

    # --- Render full chat history ---
    for msg in st.session_state.display_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            # Render any charts that were generated with this response
            for fig in msg.get("figures", []):
                st.plotly_chart(fig, use_container_width=True)
            # Show which tools backed this answer so the user knows it's data-driven
            if msg["role"] == "assistant" and msg.get("tools_used"):
                labels = ", ".join(f"`{t}`" for t in msg["tools_used"])
                st.caption(f"Tools used: {labels}")

    # --- Suggested questions (shown only when the chat is empty) ---
    if not st.session_state.display_messages:
        st.markdown("##### Try asking:")
        suggestions = [
            "Give me a summary of the dataset",
            "Which entity has the most network connections?",
            "Show me all defense sector entities",
            "Compare Rosneft and Gazprom",
            "Which entities operate in China?",
            "What sanctions lists does ZTE appear on?",
        ]
        col1, col2 = st.columns(2)
        for i, s in enumerate(suggestions):
            (col1 if i % 2 == 0 else col2).markdown(f"- *{s}*")

    # --- Chat input ---
    if prompt := st.chat_input("Ask anything about the dataset…"):

        # Display and record the user message
        with st.chat_message("user"):
            st.markdown(prompt)
        st.session_state.display_messages.append({"role": "user", "content": prompt})
        st.session_state.api_messages.append({"role": "user", "content": prompt})

        # Call Claude, display the response, render any charts, show tool attribution
        with st.chat_message("assistant"):
            with st.spinner("Querying dataset…"):
                response_text, tools_used, figures = _call_claude(
                    st.session_state.api_messages, profiles
                )
            st.markdown(response_text)
            # Render Plotly charts immediately below the text response
            for fig in figures:
                st.plotly_chart(fig, use_container_width=True)
            if tools_used:
                labels = ", ".join(f"`{t}`" for t in tools_used)
                st.caption(f"Tools used: {labels}")

        # Store the assistant response for display history (figures included so
        # they re-render correctly when the chat history is replayed on rerun)
        st.session_state.display_messages.append({
            "role":       "assistant",
            "content":    response_text,
            "tools_used": tools_used,
            "figures":    figures,
        })
        # Append text-only to API history — the tool_use/tool_result blocks
        # were already appended inside _call_claude during the tool loop
        st.session_state.api_messages.append({
            "role":    "assistant",
            "content": response_text,
        })


if __name__ == "__main__":
    main()
