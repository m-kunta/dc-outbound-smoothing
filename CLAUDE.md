# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Generate synthetic data (creates levelset.db)
python data_gen.py

# Run all tests
pytest test_backend.py -v

# Run a single test
pytest test_backend.py -v -k "test_function_name"

# Launch dashboard
streamlit run app.py
```

## Architecture

LevelSet is a DC (Distribution Center) outbound replenishment smoothing optimizer. It solves a supply chain problem where aggregated store orders create "spiky" demand on the warehouse (heavy Mon–Wed, light Thu–Fri), causing overtime peaks and idle troughs.

### Data Flow

```
data_gen.py → levelset.db (SQLite)
                    ↓
              solver.py (optimization engine)
                    ↓
              app.py (Streamlit dashboard)
```

`data_loader.py` handles real-user CSV uploads as an alternative to synthetic data. `llm_providers.py` provides optional AI-generated planner insights via a multi-provider factory (Gemini, OpenAI, Anthropic, Groq, Ollama).

### Database (levelset.db) — 5 input tables + 1 output

- **demand** — ~2,000 replenishment order lines with `order_type` (HARD/SOFT), `need_date`, volume
- **dc_capacity** — daily resource-type capacity (Conveyable / NonConveyable / Bulk) for 90 days
- **inventory** — 50 SKUs with on-hand qty and ASN ETA dates
- **sku_master** — shelf-life, cube per case, UOM conversions
- **store_master** — 8 stores with delivery day calendars
- **smoothed_plan** — output written by solver after each run

### Solver Logic (solver.py)

1. **classify_orders()** — Labels orders HARD (safety stock breaches, promos) or SOFT (routine fills, inventory builds). HARD orders are never moved.
2. **convert_units()** — Cases → pallets using sku_master cube data.
3. **check_capacity()** — Identifies peak days exceeding `SMOOTH_PEAK_RATIO × avg`.
4. **smooth()** — Greedy shift: moves SOFT orders from peak days into trough days within the `HORIZON_DAYS` look-ahead window. Uses SciPy SLSQP to minimize:
   ```
   Z = Σ(V_t − μ_H)² + λ·(OSA_Penalty) + γ·(EarlyShipPenalty)
   ```
5. **apply_guardrails()** — Enforces frozen zone (48h), inventory readiness (ASN ETA), store delivery calendars, shelf-life, and backroom capacity.
6. **compute_kpis()** — Returns CV (coefficient of variation), OSA %, OT cost, cube utilization.

### Solver Parameters (via .env or Streamlit sidebar)

| Variable | Default | Meaning |
|---|---|---|
| `LAMBDA` | 100 | OSA penalty weight |
| `GAMMA` | 1 | Early-ship penalty weight |
| `FROZEN_HOURS` | 48 | Hours ahead locked from rescheduling |
| `HORIZON_DAYS` | 10 | Look-ahead window for trough search |
| `SMOOTH_PEAK_RATIO` | 1.05 | Trigger smoothing if day > 105% avg |
| `SMOOTH_TROUGH_RATIO` | 0.90 | Accept trough if < 90% source load |

### Key Concepts

- **HARD orders** — Safety stock breaches and promotional orders. Never rescheduled.
- **SOFT orders** — Routine fills and inventory builds. Eligible for smoothing.
- **Resource types** — Conveyable, NonConveyable, Bulk (separate capacity pools per day).
- **Frozen zone** — Orders within `FROZEN_HOURS` of today are locked, even if SOFT.

### Testing

88 test cases are documented in `TEST_PLAN.md` (P0/P1/P2 priority). `test_backend.py` covers: data generation integrity, solver classification logic, guardrail enforcement, KPI computation, and UI component rendering.
