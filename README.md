# 📦 LevelSet — DC Outbound Smoothing

> **Constrained Replenishment Planning for Distribution Centers**

**Author:** Mohith Kunta · [github.com/m-kunta](https://github.com/m-kunta)  
**Domain:** Supply Chain Planning / DC Replenishment  
**Stack:** Python · Streamlit · SQLite · Pandas · Altair · Google Gemini (optional)  
**Status:** ✅ Built & Running

---

## The Business Problem

### What's Happening Today

Every week, your Distribution Center faces the same frustrating pattern. On Monday through Wednesday, the dock is chaos — trucks backed up, crew working overtime, waves being released late. By Thursday and Friday? Dead quiet. The warehouse team either burns out or sits idle, depending on the day.

This isn't a planning failure — it's a structural one. Here's why:

**Your store orders arrive with "need dates" embedded in them.** Every store says "I need this by Tuesday." Every replenishment analyst builds a ship schedule purely around those dates. When 80% of your 200 stores all have Monday or Tuesday as their primary delivery day, you get a demand spike — not because anyone planned it that way, but because store receiving schedules naturally cluster.

The math is simple: 200 stores, mostly receiving on Mon/Wed/Fri → 60% of your outbound volume lands on those three days. The remaining four days carry the scraps.

In financial terms, this costs you in three ways:
1. **Labor overtime** — Paying premium wages to handle compressed volume on peak days
2. **Equipment underutilization** — Forklifts, trailers, and dock doors sitting idle 40% of the week
3. **Service risk** — Rushing creates errors. Mis-picks, mis-ships, damaged product

The cruel irony? Your service levels aren't any better for it. Stores get their product on time — but only because you're throwing overtime at the problem. The customer experience is identical whether you run flat or spiky. The cost difference is entirely on your side.

### Who Feels This Pain

| Persona | What They Experience |
|---|---|
| **DC Operations Manager** | "Every Monday I wonder if we'll finish by close. Last week we hit 12 hours overtime. Thursday I'm sending people home early." |
| **Replenishment Analyst** | "I build the plan store by store. I know it's spiky, but the system doesn't give me another lever to pull." |
| **Finance** | "We're $45K over labor budget this month because of overtime. But our service scores are the same as last quarter." |
| **Warehouse Crew** | "Mondays are a grind. We bust ass three days, then twiddle thumbs. It's exhausting." |

### Why Current Tools Don't Help

Most Warehouse Management Systems (WMS) and Order Management Systems (OMS) handle order execution well — they know how to release waves, optimize pick routes, and track inventory. But they're **demand-following**, not **demand-shaping**. They take store need dates as gospel and build the schedule accordingly. There's no concept of "this order can move 2 days earlier if it helps flatten the load."

Some organizations try to solve this with:
- **Manual load-leveling** — Analysts tweak dates in Excel. Time-consuming, error-prone, and hard to scale.
- **Strict delivery calendars** — Force stores to receive evenly. Effective but requires store cooperation that's rarely available.
- **MIP/LP optimization solvers** — Theoretically elegant but computationally too slow for daily operational runs with 2,000+ orders.

### LevelSet's Approach

**LevelSet** treats DC outbound throughput as what it actually is: a constrained, manageable resource. Rather than letting unconstrained demand dictate the schedule, the system:

1. **Classifies each order** — Distinguishes between "HARD" orders that absolutely must ship on the need date (safety stock breach, promo launches) and "SOFT" orders that are flexible (routine replenishment, inventory builds)

2. **Identifies real peak days** — Trigger smoothing only when load exceeds either absolute DC capacity or a dynamic threshold (e.g., 105% of average daily volume)

3. **Finds valid troughs** — For each soft order on a peak day, scans backward within a configurable window (default 10 days) to find:
   - Available DC capacity
   - Inventory that's actually on-hand or arriving before the new ship date (no phantom picking)
   - Store delivery day alignment
   - Backroom space available at the destination
   - Shelf-life constraints still met

4. **Commits moves that flatten** — Only moves an order if the destination day genuinely has less load than the source day

### Real-World Use Case: Midwest Grocery Co-op

**Scenario:** FreshMart Co-op operates a 300,000 sq ft regional DC serving 180 grocery stores across the Midwest. Their outbound volume averages 2,400 pallets/day but regularly spikes to 3,600 on Mondays (40% above average).

**Current state:**
- Monday crew: 45 associates, 12 hours overtime
- Tuesday crew: 42 associates, 10 hours overtime
- Wednesday–Friday crew: 28 associates, no overtime
- Monthly labor overspend: $38,000
- Service score (OSA): 97.2%

**With LevelSet engaged:**
- Lambda (service penalty weight): 100 — protect HARD orders at all costs
- Gamma (early-ship weight): 1 — minimize unnecessary pulling
- Horizon: 10 days
- Frozen zone: 48 hours

**Results after first run:**
- Soft orders identified: 1,840 of 2,100 total (88%)
- Orders successfully shifted: 1,247 (68% of soft orders)
- Peak day volume reduced: Monday from 3,600 → 2,780 pallets
- New outbound CV: 0.14 (was 0.38)
- OSA maintained: 98.1%
- Estimated monthly overtime savings: $18,000

**What changed operationally:**
- Monday still busy, but manageable with standard crew
- Tuesday volume up 22% — the "empty" day now carries moved Monday orders
- No store service impact — all HARD orders satisfied on original need dates
- Three capacity alerts raised — orders that couldn't find valid windows flagged for manual review

### User-Facing Benefits

| Benefit | Impact |
|---|---|
| **Predictable labor loads** | No more Monday crunch / Thursday dead zones. Crew schedules become stable. |
| **Reduced overtime** | Typically 8–15% reduction in OT hours from first optimization cycle. |
| **Maintained service levels** | HARD orders protected — OSA impact typically < 0.5%. |
| **Visible capacity gaps** | Alerts highlight orders that genuinely cannot move — planners review, not search. |
| **Configurable sensitivity** | Tune λ (service penalty) and γ (early-ship penalty) to match your operational risk tolerance. |
| **Faster planning cycles** | The solver runs in seconds, not hours — iterate on parameters freely. |

### How It Fits Into Your Workflow

```
[Store Demand] ──► [WMS/OMS Order Release] ──► [LevelSet (Smoothing)] ──► [Optimized Ship Schedule]
                                                                   │
                                                            [Capacity Alerts]
                                                                  │
                                                           [Planner Review Loop]
                                                                  │
                                                            [Export to WMS]
```

LevelSet sits between your WMS/OMS order release layer and the final ship schedule. Run it daily after demand is loaded but before waves are released. Export the smoothed plan back to your WMS for execution.

If you're using a corporate WMS (Blue Yonder, Manhattan, HighJump, etc.), the export flow is:
1. Load today's demand feed into LevelSet
2. Run the solver with your parameters
3. Download the smoothed schedule as CSV
4. Ingest the new ship dates back into your WMS via API or manual upload

---

## Technical Problem (for implementers)

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
