# 📦 LevelSet — DC Outbound Smoothing

> **Constrained Replenishment Planning for Distribution Centers**

**Author:** Mohith Kunta · [github.com/m-kunta](https://github.com/m-kunta)  
**Domain:** Supply Chain Planning / DC Replenishment  
**Stack:** Python · Streamlit · SQLite · Pandas · Altair · Google Gemini (optional)  
**Status:** ✅ Built & Running

---

## The Problem

DC outbound plans built purely off store need dates spike on two or three days and go quiet on others. Stores get replenishment on time — but the DC runs overtime on peak days and sits idle on trough days. Over a 4-week period, that pattern is expensive and entirely avoidable.

**LevelSet** treats DC outbound throughput as a constrained resource and proactively shifts "soft" replenishment orders into available capacity windows — before the wave happens, not after.

---

## Algorithm Overview

LevelSet employs a greedy heuristic approach designed specifically for high-volume operational environments where exact optimization (MIP/LP) is computationally prohibitive on a daily run-cycle.

### 1. Objective Function Context

While executed as a fast heuristic, the solver conceptually approximates the minimization of the daily variance $Z$:

$$Z = \sum_{t} (V_t - \mu_H)^2 + \lambda(\text{OSA\_Penalty}) + \gamma(\text{EarlyShipPenalty})$$

- **$V_t$**: Outbound volume on day $t$.
- **$\mu_H$**: Mean daily volume across the horizon.
- **$\lambda$ (OSA Penalty)**: Weight applied to missed or delayed HARD orders (enforces service integrity).
- **$\gamma$ (Early Ship Penalty)**: Weight applied to the number of days a SOFT order is pulled forward (minimizes unnecessary store backroom bloat).

### 2. Execution Flow

1. **Classification**: Orders are categorized as `HARD` (safety stock breach, promo launch—immune to movement) or `SOFT` (routine fill, inventory build—eligible for load-leveling). 
2. **Peak Detection**: The algorithm identifies days where total load exceeds either absolute DC capacity or a dynamic threshold (e.g., $1.2 \times \mu_H$). Smoothing logic is only invoked for operations crossing these limits to prevent over-optimization of naturally flat days.
3. **Trough Search (Backward Window)**: For each `SOFT` order on a peak day, the solver scans backward within the configured `HORIZON_DAYS`. It evaluates candidate days against four strict guardrails:
    - **Resource Capacity**: Outbound throughput limits by specific zone (Conveyable, Non-Conveyable, Bulk).
    - **Store Calendar**: Store receiving schedule matrix.
    - **Inventory Readiness**: No phantom picking. Product must physically be on-hand, or ASN ETA must precede the candidate ship date.
    - **Shelf-Life (MRSL)**: Re-routing must not push product outside its freshness window.
4. **Variance Validation**: A move is only committed if pulling the volume forward strictly reduces the volume delta between the peak (source) and trough (destination) day.
5. **Exception Generation**: Volume that cannot find a valid trough window surfaces as a Capacity Alert for manual override.

See [REQUIREMENTS.md](REQUIREMENTS.md) for data feed specifications and KPI targets.

---

## Features

### 🔧 Core Solver

- Greedy smoothing engine with configurable horizon (5–14 days) and frozen zone (24–96h)
- Tunable λ (OSA penalty) and γ (early-ship penalty) weights via sidebar sliders
- Unit conversion: cases → pallets → capacity comparison
- Before/after CV, OSA, cube utilisation KPIs

### 📊 Dashboard

![LevelSet Dashboard](assets/dashboard.png)

- **KPI Scorecards** — Outbound CV, OSA (HARD orders), shifted orders, alerts, cube utilisation
- **Before/After Bar Chart** — Red (unconstrained) vs. green (smoothed) daily volume
- **Smoothed Ship Schedule** — Filterable table with colour-coded rows (green = moved, red = alert)
- **Reset Synthetic Data** — Regenerate the database and re-run the solver in one click

### 📁 Real Data Upload & Export

To test LevelSet with your own network volume rather than the synthetic generator:
- **Bring Your Own Data (BYOD):** Toggle the Data Source in the sidebar to "Upload Real Data"
- **CSV Templates:** Upload the 5 core data feeds (Constraints, Inventory, SKU Master, Store Calendars, Demand/Orders) matching the expected schema.
- **Export Capabilities:** Once the solver completes, download the optimized ship schedule as CSV or JSON for ingestion into local systems.

---

### 🤖 AI Planner Insight

![AI Planner Insight](assets/planner_insight.png)

**Location:** Dashboard → between Schedule Table and Exception Review

An automated briefing of the generated plan. After running the solver, click **✨ Generate Planner Insight** to receive a structured "Situation → Actions" summary that covers:

- What the solver achieved (CV change, OSA, orders shifted)
- Risks from any unresolved capacity alerts
- 2–3 specific recommended actions (adjust λ, expand horizon, manual review)

**Prompt design:** The LLM receives structured solver KPIs and is instructed to produce an operations-team brief — not an executive summary. Output is direct and actionable.

**To enable:** Choose your preferred AI provider directly from the sidebar dropdown (Gemini, OpenAI, Anthropic, Groq, Ollama) and paste your API key. Alternatively, set it in your `.env` file (copy `.env.example` as a template).

---

### 🚨 AI Exception Triage

![AI Exception Triage](assets/exception_triage.png)

**Location:** Dashboard → Exception Review section (below AI Planner Insight)

Exception-based review capability that automatically classifies, scores, and ranks plan exceptions so planners can focus on the highest-impact items first. Consists of two layers:

#### Automated Exception Classification

Each exception is typed and scored by business impact:

| Type | Colour | What it flags | Impact Formula |
|---|---|---|---|
| 🔴 Capacity Alert | Red | SOFT orders with no valid trough window | `pallets × urgency × 1` |
| 🟠 Near-Frozen HARD | Orange | HARD orders shipping in ≤72h — confirmation window | `pallets × (4−days) × 3` |
| 🟡 Day Over-Capacity | Yellow | Days still above DC ceiling after smoothing | `overflow_pallets × urgency` |

Urgency decays linearly as the need date moves further out (`urgency = max(10 − days_out, 1)`), so imminent exceptions always rank above distant ones of equal volume.

#### AI Triage Brief

Click **🔍 Triage Exceptions with AI** to send the top 10 exceptions (ranked by Impact Score) to the AI. The output is structured as:

- **IMMEDIATE** — act today (specific action, owner, deadline)
- **MONITOR** — act this week
- **WATCH** — low risk, keep an eye
- Systemic pattern callouts (e.g. "Bulk resource consistently over-capacity")
- Headline: total pallets at risk if no action is taken

This feature requires an API key configured in `.env` (same as Planner Insight above).

---

## Key KPIs

| Metric | Target |
|---|---|
| Outbound CV (σ/μ) | < 0.15 |
| On-Shelf Availability (OSA) | ≥ 98.5% |
| Overtime Reduction | −12% |
| Cube Utilization | +5% |

---

## Quick Start

```bash
git clone https://github.com/m-kunta/dc-outbound-smoothing
cd dc-outbound-smoothing
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Optional: configure AI providers
cp .env.example .env
# Edit .env and add your API key

# Generate synthetic data
python data_gen.py

# Launch dashboard
streamlit run app.py
```

Open [http://localhost:8501](http://localhost:8501) in your browser.

---

## Project Structure

```
dc_outbound_smoothing/
├── app.py              # Streamlit dashboard (solver controls, charts, AI panels)
├── solver.py           # Smoothing engine: classify → convert → check → smooth → KPIs
├── data_gen.py         # Synthetic 30-day dataset generator (5 SQLite tables)
├── llm_providers.py    # Multi-provider AI client (Gemini, OpenAI, Anthropic, Groq, Ollama)
├── levelset.db         # Generated SQLite database (git-ignored)
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variable template
├── REQUIREMENTS.md     # Full BRD: solver logic, data specs, objective function
└── README.md           # This file
```

---

## Project Status

**The LevelSet prototype is fully complete and operational.**

The backend solver, synthetic data generation, and multi-provider AI integrations have been thoroughly tested (`test_backend.py`). All mathematical guardrails (capacity, shelf-life, backroom space, inventory tracking) perform as designed, effectively smoothing outbound CV by ~20% without violating constraints. The Streamlit frontend and UI workflows have been validated.

---

*Mohith Kunta — Supply Chain & AI Portfolio*  
*[github.com/m-kunta](https://github.com/m-kunta)*
