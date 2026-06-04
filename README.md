# Sayari Entity Analytics Report

A Streamlit dashboard that surfaces macro-level intelligence across 50 high-risk, sanctioned entities using the [Sayari API](https://documentation.sayari.com). Built as a Proof of Concept for the Sayari Forward Deployed Engineer technical assessment.

---

## What It Does

The app resolves a curated list of sanctioned entities against the Sayari knowledge graph, fetches their full risk profiles, and presents the findings in two ways:

### 📊 Analytics Dashboard (`Dashboards.py`)

Five interactive tabs for visual exploration:

| Tab | What You'll See |
|---|---|
| **Overview** | KPI cards, sector breakdown, top entities by network degree |
| **Sanctions** | Entities per sanctions list (click a list → see which entities are on it); sanctions list coverage per entity (click an entity → see every list it appears on) |
| **Risk Profile** | Risk flag frequency (click a flag → see which entities carry it); aggregate severity distribution |
| **Geography** | Choropleth world map (click a country → see which entities are present there); top 20 entities by jurisdictional footprint (click an entity → see every country it operates in) |
| **Entity Detail** | Sortable full-dataset table |

### 🤖 AI Analyst (`pages/Chat.py`)

A conversational interface powered by Claude. Ask any natural language question about the dataset — the AI calls the appropriate data tool and synthesises a plain-English answer backed by live data. Example questions:

- *"Which defense firms operate in China and have more than 10 sanctions lists?"*
- *"Compare Rosneft and Gazprom across all risk dimensions"*
- *"Which entity has the widest geographic footprint?"*
- *"Who are Rosneft's top network connections?"*
- *"Which of Rostec's counterparties are sanctioned?"*

The AI chat and dashboard share the same data cache and are intentionally separate — the dashboard for visual overviews, the chat for ad-hoc deep-dive queries.

### 🪪 Resolution Review (`pages/Resolution_Review.py`)

Name resolution is probabilistic — the Sayari endpoint returns a *ranked list* of candidates with confidence scores, and naively taking the top one risks false positives (in our dataset, *"State Development Bank VEB.RF"* (Russia) initially matched a *Belarusian* bank at a low score). Rather than gate matches with the API's `minimum_score_threshold` — which would silently drop weak matches and hide that risk — `resolver.py` keeps every match but captures the **top-5 candidates** and a **confidence band** for each entity. Low-confidence matches are flagged `needs_review`.

This tab is the human-in-the-loop triage:

- A confidence badge (✅ high / ⚠️ low / ❌ unresolved) and score for every entity
- Automatic **jurisdiction-mismatch** warnings (input country vs. matched country)
- A candidate picker to **re-map** a weak match to the correct entity, or mark it unresolved
- Decisions persist back to `resolved.json` with a `reviewed` / `manual_override` audit trail

Key findings from the dataset: **93.9% of entities are sanctioned**, spanning **53 countries** with **428,000+ known network connections** across **21 distinct sanctions lists**.

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.12+ |
| Docker + Docker Compose | any recent version (optional) |
| Sayari API credentials | `CLIENT_ID` and `CLIENT_SECRET` |
| Anthropic API key | `ANTHROPIC_API_KEY` (AI chat only) |

> **Note:** The data cache (`data/resolved.json`, `data/profiles.json`, `data/relationships.json`) is committed to this repository. You can run the dashboard and tests entirely without API credentials — credentials are only needed if you want to re-fetch live data. The Anthropic API key is only required for the AI chat page.

---

## Quickstart — Local Python

**1. Clone the repository and navigate into it**

```bash
git clone <repo-url>
cd sayari-analytics
```

**2. Create and activate a virtual environment**

```bash
python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

**3. Install dependencies**

```bash
pip install -r requirements.txt
```

**4. Set up environment variables**

```bash
cp .env.example .env
```

Open `.env` and fill in your credentials:

```
CLIENT_ID=your_client_id_here
CLIENT_SECRET=your_client_secret_here
ANTHROPIC_API_KEY=your_anthropic_api_key_here
```

`ANTHROPIC_API_KEY` is only needed for the AI chat page — the dashboard works without it.

**5. Run the app**

```bash
streamlit run Dashboards.py
```

Open [http://localhost:8501](http://localhost:8501) in your browser.

---

## Quickstart — Docker

No Python environment needed — Docker handles everything.

```bash
docker compose up --build
```

Open [http://localhost:8501](http://localhost:8501) in your browser.

To stop the app:

```bash
docker compose down
```

> **Credentials in Docker:** The `docker-compose.yml` loads your `.env` file automatically. If you only want to browse the cached data, the app runs without a `.env` file since the profiles cache is bundled in the image.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `CLIENT_ID` | Yes* | Sayari API client ID |
| `CLIENT_SECRET` | Yes* | Sayari API client secret |
| `ANTHROPIC_API_KEY` | Yes** | Anthropic API key for the AI chat page |

*Only required for live data fetching. The dashboard runs fully offline using the committed cache.
**Only required for the AI chat page (`pages/Chat.py`). The dashboard works without it.

---

## Running Tests

```bash
python -m pytest tests/ -v
```

**162 tests** covering every non-UI module — no API calls, no file I/O. All tests use fixture factories that build synthetic profile and relationship data, plus mocked SDK clients for the fetch paths, so the full suite runs in under 10 seconds.

```
tests/test_analytics.py     40 tests   Pure analytics functions over the profiles cache
tests/test_tools.py         64 tests   All 6 AI chat tools + 4 shared resolution helpers
tests/test_fetcher.py       24 tests   Risk flag/level extraction, sanctions parsing, fetch_profile
tests/test_rel_fetcher.py    9 tests   Relationship flattening + fetch_relationships error path
tests/test_resolver.py      25 tests   Candidate capture, confidence banding, review decisions
```

The Sayari API calls in `fetcher.py`, `rel_fetcher.py`, and `resolver.py` are exercised with mocked SDK clients, so no credentials or network access are required. The Streamlit UI layers (`Dashboards.py`, `pages/Chat.py`) and the credentialed client factory (`client.py`) are intentionally excluded, as they require a live session or real API keys.

---

## Project Structure

```
.
├── Dashboards.py           # Streamlit dashboard (5 interactive tabs)
├── analytics.py            # Pure analytics functions over the profiles cache
├── tools.py                # LLM-callable data-access functions (6 tools)
├── fetcher.py              # Fetches full entity profiles from the Sayari API
├── rel_fetcher.py          # Fetches top-50 network connections per entity
├── resolver.py             # Resolves names to entity IDs; captures candidates + confidence
├── client.py               # Authenticated Sayari SDK client factory
├── pages/
│   ├── Chat.py             # AI chat interface (Claude + tool use)
│   └── Resolution_Review.py # Human-in-the-loop triage for low-confidence matches
├── data/
│   ├── resolved.json       # Cached name → entity ID mappings (49/50 resolved)
│   ├── profiles.json       # Cached full entity profiles (49/50 fetched)
│   └── relationships.json  # Cached top-50 network connections per entity
├── tests/
│   ├── test_analytics.py   # Unit tests for analytics.py
│   ├── test_tools.py       # Unit tests for tools.py (AI chat tools + helpers)
│   ├── test_fetcher.py     # Unit tests for fetcher.py
│   ├── test_rel_fetcher.py # Unit tests for rel_fetcher.py
│   └── test_resolver.py    # Unit tests for resolver.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── REPORT.md               # Written assessment report (approach, findings, value)
```

---

## How It Works

```
Entity names (List 1)
        │
        ▼
  resolver.py    ──── Sayari resolution endpoint ────► data/resolved.json
        │
        ▼
  fetcher.py     ──── Sayari entity_summary + risk ──► data/profiles.json
        │
        ▼
  rel_fetcher.py ──── Sayari get_entity (top 50) ────► data/relationships.json
        │
        ▼
  analytics.py   ──── Pure functions over cache ─────► Computed insights
        │                                                      │
        ▼                                                      ▼
    Dashboards.py  ── Streamlit + Plotly ───────────► Dashboard UI (5 tabs)
                                                               │
  tools.py     ──── LLM-callable data tools ──────────► AI Chat (pages/Chat.py)
  (6 tools)         Claude interprets questions,              │
                    calls tools, returns answers   ──────► Natural language UI
```

**Data pipeline runs once.** On every subsequent launch the app loads directly from the local cache — no API calls are made during normal use. Delete either cache file to trigger a fresh fetch.

---

## Re-fetching Live Data

To re-resolve entities from scratch:

```bash
# Delete the cache files
rm data/resolved.json data/profiles.json data/relationships.json

# Re-run the pipeline
python resolver.py
python fetcher.py
python rel_fetcher.py

# Or let the app fetch profiles on first load (requires credentials in .env)
# Note: relationships must be fetched manually via rel_fetcher.py
streamlit run Dashboards.py
```

---

## Entity Dataset

The 50 entities are drawn from a curated list of sanctioned and high-risk organisations sourced from the assessment's provided entity list. They span seven sectors:

| Sector | Count |
|---|---|
| Defense & Aerospace | 20 |
| Banking & Finance | 10 |
| Energy & Oil/Gas | 6 |
| Transport & Industrial | 4 |
| State Trade & Other | 4 |
| Technology | 3 |
| Mining & Resources | 3 |

One entity (`Belnauchcompositit`) returned no match in the Sayari database and is represented as an unresolved stub in the cache.

---

## Dependencies

| Package | Purpose |
|---|---|
| `sayari` | Official Sayari Python SDK |
| `python-dotenv` | Loads credentials from `.env` |
| `streamlit` | Dashboard and chat interface framework |
| `plotly` | Interactive charts |
| `pandas` | Dataframe rendering in the Entity Detail tab |
| `anthropic` | Claude API client for the AI chat page |
| `pycountry` | ISO 3166-1 country name resolution for the AI chat filters |

---

## Assumptions

- Entity names from the provided list were used as-is where possible. Three required alias corrections: `PDVSA` (for "Venezuelan State-Owned Oil Company (PDVSA)") and `Belarusian Potash Company` (for "Belorusskaya Kaliynaya Companya") resolved successfully; `Belnauchcompositit` had no match in Sayari.
- Sector classifications are manually assigned based on each entity's publicly known business activities, as Sayari does not provide SIC/NAICS codes directly.
- `entity_summary` was used over `get_entity` to keep API usage efficient — it returns all risk, country, and relationship metadata needed for macro analytics without paginating through individual relationship records.
