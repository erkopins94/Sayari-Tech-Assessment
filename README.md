# Sayari Entity Analytics Report

A Streamlit dashboard that surfaces macro-level intelligence across 50 high-risk, sanctioned entities using the [Sayari API](https://documentation.sayari.com). Built as a Proof of Concept for the Sayari Forward Deployed Engineer technical assessment.

---

## What It Does

The app resolves a curated list of sanctioned entities against the Sayari knowledge graph, fetches their full risk profiles, and presents the findings in two ways:

### 📊 Analytics Dashboard (`app.py`)

Five interactive tabs for visual exploration:

| Tab | What You'll See |
|---|---|
| **Overview** | KPI cards, sector breakdown, top entities by network degree |
| **Sanctions** | Entities per sanctions list (click a list → see which entities are on it); sanctions list coverage per entity (click an entity → see every list it appears on) |
| **Risk Profile** | Risk flag frequency (click a flag → see which entities carry it); aggregate severity distribution |
| **Geography** | Choropleth world map (click a country → see which entities are present there); top 20 entities by jurisdictional footprint (click an entity → see every country it operates in) |
| **Entity Detail** | Sortable full-dataset table |

### 🤖 AI Analyst (`pages/chat.py`)

A conversational interface powered by Claude. Ask any natural language question about the dataset — the AI calls the appropriate data tool and synthesises a plain-English answer backed by live data. Example questions:

- *"Which defense firms operate in China and have more than 10 sanctions lists?"*
- *"Compare Rosneft and Gazprom across all risk dimensions"*
- *"Which entity has the widest geographic footprint?"*

The AI chat and dashboard share the same data cache and are intentionally separate — the dashboard for visual overviews, the chat for ad-hoc deep-dive queries.

Key findings from the dataset: **93.9% of entities are sanctioned**, spanning **53 countries** with **428,000+ known network connections** across **21 distinct sanctions lists**.

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.12+ |
| Docker + Docker Compose | any recent version (optional) |
| Sayari API credentials | `CLIENT_ID` and `CLIENT_SECRET` |
| Anthropic API key | `ANTHROPIC_API_KEY` (AI chat only) |

> **Note:** The data cache (`data/resolved.json`, `data/profiles.json`) is committed to this repository. You can run the dashboard and tests entirely without API credentials — credentials are only needed if you want to re-fetch live data. The Anthropic API key is only required for the AI chat page.

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
streamlit run app.py
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
**Only required for the AI chat page (`pages/chat.py`). The dashboard works without it.

---

## Running Tests

```bash
python -m pytest tests/ -v
```

**40 tests** covering every analytics function — no API calls, no file I/O. All tests use a fixture factory that builds synthetic profile data so the suite runs in under 1 second.

```
tests/test_analytics.py::TestSummaryStats                    9 tests
tests/test_analytics.py::TestSectorBreakdown                 5 tests
tests/test_analytics.py::TestCountryBreakdown                4 tests
tests/test_analytics.py::TestSanctionsListBreakdown          3 tests
tests/test_analytics.py::TestRiskFlagFrequency               3 tests
tests/test_analytics.py::TestRiskLevelDistribution           3 tests
tests/test_analytics.py::TestTopEntitiesByDegree             5 tests
tests/test_analytics.py::TestJurisdictionExposure            3 tests
tests/test_analytics.py::TestSanctionsCoveragePerEntity      5 tests
```

---

## Project Structure

```
.
├── app.py                  # Streamlit dashboard (5 interactive tabs)
├── analytics.py            # Pure analytics functions over the profiles cache
├── tools.py                # LLM-callable data-access functions (5 tools)
├── fetcher.py              # Fetches full entity profiles from the Sayari API
├── resolver.py             # Resolves entity names to Sayari entity IDs
├── client.py               # Authenticated Sayari SDK client factory
├── pages/
│   └── chat.py             # AI chat interface (Claude + tool use)
├── data/
│   ├── resolved.json       # Cached name → entity ID mappings (49/50 resolved)
│   └── profiles.json       # Cached full entity profiles (49/50 fetched)
├── tests/
│   └── test_analytics.py   # Unit tests for analytics.py
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
  resolver.py  ──── Sayari resolution endpoint ────► data/resolved.json
        │
        ▼
  fetcher.py   ──── Sayari entity_summary + risk ──► data/profiles.json
        │
        ▼
  analytics.py ──── Pure functions over cache ─────► Computed insights
        │                                                      │
        ▼                                                      ▼
    app.py     ──── Streamlit + Plotly ──────────────► Dashboard UI (5 tabs)
                                                               │
  tools.py     ──── LLM-callable data tools ──────────► AI Chat (pages/chat.py)
  (5 tools)         Claude interprets questions,              │
                    calls tools, returns answers   ──────► Natural language UI
```

**Data pipeline runs once.** On every subsequent launch the app loads directly from the local cache — no API calls are made during normal use. Delete either cache file to trigger a fresh fetch.

---

## Re-fetching Live Data

To re-resolve entities from scratch:

```bash
# Delete the cache files
rm data/resolved.json data/profiles.json

# Re-run the pipeline
python resolver.py
python fetcher.py

# Or let the app fetch on first load (requires credentials in .env)
streamlit run app.py
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

---

## Assumptions

- Entity names from the provided list were used as-is where possible. Three required alias corrections: `PDVSA` (for "Venezuelan State-Owned Oil Company (PDVSA)") and `Belarusian Potash Company` (for "Belorusskaya Kaliynaya Companya") resolved successfully; `Belnauchcompositit` had no match in Sayari.
- Sector classifications are manually assigned based on each entity's publicly known business activities, as Sayari does not provide SIC/NAICS codes directly.
- `entity_summary` was used over `get_entity` to keep API usage efficient — it returns all risk, country, and relationship metadata needed for macro analytics without paginating through individual relationship records.
