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
import streamlit as st
from dotenv import load_dotenv

from tools import (
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

Tool selection guide:
- get_entity        → any question about a specific named entity
- filter_entities   → filtering or searching by sector, country, sanctions list, risk flag, or status
- rank_entities     → ranking, top N, or superlative questions ("most", "highest", "fewest")
- compare_entities  → explicit side-by-side comparison of two or more named entities
- get_summary_stats → macro or aggregate questions about the full dataset

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

def _dispatch_tool(name: str, inputs: dict, profiles: list[dict]) -> dict:
    """
    Call the appropriate tool function by name and return its result.

    Returns an error dict (rather than raising) for unknown tool names so the
    LLM can relay the issue gracefully without crashing the app.
    """
    if name == "get_entity":
        return get_entity(inputs["name"], profiles)
    if name == "filter_entities":
        return filter_entities(profiles, **inputs)
    if name == "rank_entities":
        return rank_entities(profiles, **inputs)
    if name == "compare_entities":
        return compare_entities(inputs["names"], profiles)
    if name == "get_summary_stats":
        return get_summary_stats(profiles)

    return {"error": True, "message": f"Unknown tool '{name}'."}


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
) -> tuple[str, list[str]]:
    """
    Send the conversation to Claude and process tool calls until done.

    Args:
        api_messages: Full message history in Anthropic API format. Modified
                      in-place — tool_use and tool_result blocks are appended
                      during the loop so context is preserved for future turns.
        profiles:     Entity profiles passed through to the tool dispatcher.

    Returns:
        (response_text, tools_used) — the final text answer and a list of
        tool names called this turn, shown in the UI as attribution.
    """
    client = _get_anthropic_client()
    tools_used: list[str] = []

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
            return text, tools_used

        # Claude wants to call one or more tools — run them and loop back
        if response.stop_reason == "tool_use":
            # Append Claude's assistant turn (contains the tool_use blocks)
            api_messages.append({"role": "assistant", "content": response.content})

            # Execute each tool and collect results
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tools_used.append(block.name)
                    result = _dispatch_tool(block.name, block.input, profiles)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     json.dumps(result),
                    })

            # Append all results as a single user turn and loop for final answer
            api_messages.append({"role": "user", "content": tool_results})


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

        # Call Claude, display the response, show tool attribution
        with st.chat_message("assistant"):
            with st.spinner("Querying dataset…"):
                response_text, tools_used = _call_claude(
                    st.session_state.api_messages, profiles
                )
            st.markdown(response_text)
            if tools_used:
                labels = ", ".join(f"`{t}`" for t in tools_used)
                st.caption(f"Tools used: {labels}")

        # Store the assistant response for display history
        st.session_state.display_messages.append({
            "role":       "assistant",
            "content":    response_text,
            "tools_used": tools_used,
        })
        # Append text-only to API history — the tool_use/tool_result blocks
        # were already appended inside _call_claude during the tool loop
        st.session_state.api_messages.append({
            "role":    "assistant",
            "content": response_text,
        })


if __name__ == "__main__":
    main()
