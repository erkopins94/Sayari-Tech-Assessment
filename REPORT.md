# Sayari FDE Assessment — Analytics Report

**Scenario:** Scenario 2 — Analytics Report
**Dataset:** 50 high-risk / sanctioned entities (List 1)
**Author:** Eric Kopins

---

## Approach

The goal was to demonstrate the value of Sayari's data through a macro-level analytics report — the kind of PoC you might build for a compliance, risk, or national security client who wants to understand what a set of entities actually looks like inside Sayari's knowledge graph before committing to a full deployment.

I built a four-stage pipeline:

1. **Resolution** — Entity names from the provided list were resolved to canonical Sayari entity IDs using the resolution endpoint, which ranks candidates by match confidence and returns the best fit.
2. **Fetching** — For each resolved entity, `entity_summary` was called to retrieve the full risk profile: sanctions status, country footprint, risk flags by severity, sanctions list memberships, and relationship counts.
3. **Analytics** — A layer of pure functions transforms the cached profiles into macro-level insights: country breakdowns, sanctions list coverage, risk flag frequency, sector classification, network degree rankings, and jurisdiction exposure.
4. **Presentation** — A Streamlit dashboard with Plotly charts surfaces these insights interactively across five views, deployed to Streamlit Community Cloud for zero-install access.

All API results are cached locally after the first run. This means the dashboard and tests run entirely offline — important for a client demo where you can't depend on network availability.

---

## Key Findings

The data tells a striking story. These 50 entities are not simply names on a list — they represent a deeply interconnected, globally distributed risk network.

**Sanctions exposure is near-total.** 46 of 49 resolvable entities (93.9%) are directly sanctioned. The three exceptions still carry significant risk flags including export controls and state ownership. This is not a dataset with pockets of risk — it is almost uniformly high-risk from top to bottom.

**International sanctions coordination is broad.** Across the dataset, entities appear on 21 distinct sanctions lists from jurisdictions including the USA, EU, UK, Switzerland, Canada, Australia, Japan, New Zealand, Ukraine, and the UN Security Council. The average entity appears on **8.4 separate lists**. United Aircraft Corporation appears on 16 lists — meaning that even if one jurisdiction lifts its sanctions, 15 others remain. This level of coordinated multi-jurisdictional targeting is a direct signal of severity.

**The network footprint is massive.** Combined, these 49 entities have **428,230 known network connections** spanning 53 countries. Russian Railways alone has 114,371 connections across 12 countries. ZTE Corporation has 93,742 connections across 11. These are not isolated companies — they are nodes in vast corporate structures that extend well beyond their home jurisdictions. For a client conducting due diligence, the exposure doesn't end at the entity name; it extends to every counterparty in that network.

**Defense dominates, but the risk is sector-wide.** 20 of 50 entities (40%) are defense and aerospace firms — weapons manufacturers, shipyards, aircraft producers. But the risk is not confined to defense. The 10 banking and finance entities represent financial system access points for sanctioned activity, and the 6 energy firms represent leverage over critical infrastructure. Sayari's data surfaces this cross-sector complexity in a single query.

**State ownership is the common thread.** 34 of 49 entities (69%) are flagged as state-owned enterprises. This means sanctions against these entities are effectively sanctions against the Russian, Belarusian, Iranian, Venezuelan, North Korean, Syrian, and Myanmar states — with all the geopolitical complexity that entails.

---

## Assumptions

- **Entity names.** Three entities required alias corrections to resolve successfully: `PDVSA` (listed as "Venezuelan State-Owned Oil Company (PDVSA)"), `Belarusian Potash Company` (listed as "Belorusskaya Kaliynaya Companya"), and `Belnauchcompositit` which returned no match in Sayari and is represented as an unresolved stub.
- **Sector classification.** Sayari does not provide SIC or NAICS industry codes, so all 50 entities were manually classified into seven sectors based on publicly known business activities.
- **`entity_summary` over `get_entity`.** The summary endpoint returns all the risk, country, and relationship metadata needed for macro analytics without paginating through individual relationship records, making it significantly more credit-efficient for a 50-entity batch.
- **Direct vs. network risk flags.** The analytics layer separates direct risk flags (an entity being sanctioned) from network-level flags (being owned by a sanctioned entity). Both are captured, but they are reported distinctly to avoid conflating an entity's own exposure with its neighborhood's.

---

## Challenges

**Name resolution ambiguity.** The resolution endpoint returns a best-match candidate, but "best match" is not always obvious for entities with transliterated names, common abbreviations, or multiple registered variants. Two of the three failures were caused by name format mismatches rather than data absence — resolved by testing alternate aliases. The third (`Belnauchcompositit`) appears to be genuinely absent from Sayari's database, which is itself a meaningful data point worth surfacing to a client.

**Rich but deeply nested response structures.** The `entity_summary` response returns risk data as a dict of Pydantic model instances, each with a `level`, `value`, and `metadata` field. Extracting the right signal (e.g. distinguishing a direct sanctions flag from a network-proximity flag) required careful exploration of the schema before writing the fetcher. The probe-first, write-second approach taken here prevented several false assumptions about which fields would be populated.

---

## The Value of Sayari Data

The central "Aha moment" this report is designed to create is this: **a compliance team looking at a list of 50 entity names sees 50 rows in a spreadsheet. Sayari turns those 50 rows into a network of 428,000 relationships spanning 53 countries and 21 sanctions lists.**

That shift — from a flat list to a risk graph — is what Sayari uniquely enables. A client cannot get this from OFAC's SDN list alone, or from a single-jurisdiction registry. The value is in the aggregation: Sayari has already done the work of linking corporate registries, sanctions lists, trade data, and ownership networks across hundreds of jurisdictions into a single queryable graph.

For a forward-deployed use case, this PoC demonstrates that Sayari can power a compliance dashboard in hours, not weeks — with data that is immediately richer and more actionable than anything a client could assemble themselves.

---

## Given More Time

- **Relationship network visualisation.** A force-directed graph (using `pyvis` or `networkx`) showing ownership links between entities would powerfully illustrate how interconnected the dataset is — some entities share shareholders or officers, creating hidden exposure pathways.
- **Temporal risk analysis.** Sayari's data includes sanctions dates. Plotting when entities were added to lists over time would reveal the escalation arc of international enforcement.
- **Counterparty exposure.** Using `traversal()` or `shortest_path()`, it would be possible to show a client which of *their own* known counterparties are one or two hops away from these sanctioned entities — turning the report from an abstract intelligence brief into a direct business risk assessment.
- **Live search.** Adding a search bar to the dashboard that resolves arbitrary entity names in real-time would let a client use the app interactively during a meeting, rather than just browsing a pre-built report.
