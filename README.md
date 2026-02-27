# DC Outbound Smoothing — LevelSet

> **Constrained Replenishment Planning for Distribution Centers**

**Author:** Mohith Kunta · [github.com/m-kunta](https://github.com/m-kunta)  
**Domain:** Supply Chain Planning / DC Replenishment  
**Status:** In Design

---

## The Problem

DC outbound plans built purely off store need dates tend to spike on two or three days of the week and go quiet on others. The store gets its replenishment on time — but the DC ends up running overtime on peak days and underutilized on trough days. Over a 4-week period, that pattern is expensive and avoidable.

**LevelSet** addresses this by treating DC outbound throughput as a constrained resource and proactively shifting "soft" replenishment orders into available capacity windows — before the need date, not after the wave has already happened.

---

## How It Works

The planning engine classifies each replenishment order as **Hard** (safety stock breach, promo) or **Soft** (routine fill, inventory build). Hard orders are untouched. Soft orders are eligible for smoothing across a 7–10 day look-ahead window, subject to guardrails:

- DC outbound capacity by resource type (Conveyable, Non-Conveyable, Bulk)
- Store delivery calendar and backroom capacity
- Inventory readiness — no pull-forward if the stock isn't there yet
- Shelf-life (MRSL) compliance

If a valid window exists, the order is pulled forward. If not, a capacity alert surfaces for planner review.

See [REQUIREMENTS.md](REQUIREMENTS.md) for the full solver logic, objective function, data feed specs, and KPI targets.

---

## Key KPIs

| Metric | Target |
|---|---|
| Outbound CV (σ/μ) | < 0.15 |
| On-Shelf Availability (OSA) | > 98.5% |
| Overtime Reduction | −12% |
| Cube Utilization | +5% |

---

## Project Structure

```
dc_outbound_smoothing/
├── REQUIREMENTS.md     # Full BRD: solver logic, data specs, KPIs
└── README.md           # This file
```

---

*Mohith Kunta — Supply Chain & AI Portfolio*  
*[github.com/m-kunta](https://github.com/m-kunta)*
