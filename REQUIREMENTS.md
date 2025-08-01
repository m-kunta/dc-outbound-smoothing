# LevelSet — Business Requirements

**Project:** DC Outbound Smoothing (Planning Layer)  
**Version:** 1.4  
**Domain:** Supply Chain Planning / Replenishment  
**Author:** Mohith Kunta · [github.com/m-kunta](https://github.com/m-kunta)

---

## What Problem Are We Solving?

Most DC outbound plans are built order-by-order, where each store's need date drives the ship date directly. That works fine in isolation. The problem is that when you aggregate across hundreds of stores and thousands of SKUs, you end up with wave-shaped volume — two or three massive shipping days followed by near-empty days. That's a labor problem. It costs overtime on peak days and idle time on trough days, and it doesn't actually improve service levels at the store.

**LevelSet** fixes this by treating DC outbound throughput as what it actually is: a finite, manageable resource. Rather than letting unconstrained demand dictate the schedule, the system identifies which orders are "soft" replenishment — inventory builds and routine fills — and shifts them into available capacity on lighter days before the need date arrives.

The core goal: flatter daily ship volume, same (or better) store service levels, and less unplanned overtime.

---

## Scope

### In Scope
- **Demand Shaping** — Shifting "Soft Need" orders into trough days within a 7–10 day look-ahead window
- **Constraint Modeling** — DC capacity defined by resource type (Conveyable, Non-Conveyable, Bulk)
- **Guardrail Checks** — MRSL / shelf-life validation, store backroom capacity, inventory readiness
- **Planning Output** — A smoothed ship schedule handed off to WMS/OMS for execution

### Out of Scope
- **WMS Execution** — Wave releasing, pick-path optimization, and labor scheduling stay in the WMS
- **Transportation Routing** — Carrier selection and final-mile routing, unless cube/weight limits are a constraint

---

## Solver Logic

This is the decision sequence the engine runs for each replenishment requirement:

```
1. Requirement enters with a NEED_DATE and a PRIORITY flag
2. Classify the order:
   - HARD: Below safety stock, promo launch → lock to NEED_DATE, no smoothing
   - SOFT: Routine replenishment, inventory build → eligible for smoothing
3. Check DC outbound capacity on NEED_DATE
   - If capacity available → schedule as-is
   - If capacity full → trigger smoothing
4. Scan 7-day trough window (backward from NEED_DATE)
   - Validate each candidate day:
     a. DC capacity available?
     b. Inventory on-hand or ASN arriving before proposed ship date?
     c. Store backroom has space?
     d. MRSL / shelf-life window still met?
5. If a valid day found → Pull-Forward the ship date
   If no valid window → Raise Capacity Alert for planner review
```

---

## Functional Requirements

### Resource & Constraint Modeling

**REQ-01 — Multi-Level Resource Mapping**  
The solver must handle constraints at two levels: the total DC node (aggregate throughput cap) and individual work zones (e.g., Cold-Chain, Ambient, Dry Grocery). Zone-level constraints are designed to prevent cold-chain from bleeding into ambient labor windows.

**REQ-02 — Unit Conversion Engine**  
Demand arrives in units or cases. DC capacity is tracked in pallets, cube, or labor hours depending on the resource. The solver must convert automatically using the SKU/UOM master — planners shouldn't have to do this manually.

---

### Optimization Logic

**REQ-03 — Objective Function**

The solver minimizes:

$$Z = \sum (V_t - \mu_H)^2 + \lambda(\text{OSA\_Penalty}) + \gamma(\text{EarlyShipPenalty})$$

- **$V_t$** = volume on day $t$; **$\mu_H$** = historical average horizon volume
- **$\lambda$** (Lambda) — penalizes decisions that put OSA at risk; tune up for service-critical categories
- **$\gamma$** (Gamma) — penalizes unnecessary early shipping to prevent excess store backroom buildup

Tuning note: Lambda and Gamma are configurable per category or DC. Default starting values are provided in the parameter master.

**REQ-04 — Frozen Zone**  
No ship dates within the next 24–48 hours (configurable) may be modified. Warehouse operations in that window are already in motion. Changes at that stage create more disruption than they prevent.

---

### Guardrails & Compliance

**REQ-05 — Inventory Readiness (Anti-Phantom)**  
The system will not pull forward an order to a date where the required inventory hasn't received yet. If the ASN ETA is later than the proposed smoothed ship date, the candidate day is disqualified. This prevents phantom pick scenarios.

**REQ-06 — Store Receipt Alignment**  
Smoothed ship dates must land on a store's valid delivery days. Backroom capacity limits must also be respected — if a store is already at receiving capacity for a given day, do not route additional volume there regardless of DC capacity.

---

## Data Feed Specifications

| Feed | Key Fields | Purpose |
|---|---|---|
| **Demand Feed** | `SKU_ID`, `DEST_LOC`, `NEED_DATE`, `PRIORITY` | Raw replenishment requirement — one row per order line |
| **DC Capacity** | `DC_ID`, `RESOURCE_ID`, `MAX_THRU`, `OP_DATE` | Daily capacity ceiling per resource type ("pipe diameter") |
| **Inventory Readiness** | `ON_HAND_AVAIL`, `ASN_ETA`, `SKU_ID` | Used by REQ-05 to validate pull-forward eligibility |
| **SKU Master** | `SHELF_LIFE`, `CASE_CUBE`, `UOM_CONV` | Physical and freshness attributes for constraint checks |
| **Store Master** | `BACKROOM_CAP`, `DELIVERY_CALENDAR` | Destination-side receiving constraints |

---

## Success Metrics (KPIs)

| Metric | Calculation | Target |
|---|---|---|
| **Outbound CV** | $\frac{\sigma}{\mu}$ of daily outbound volume per DC | $< 0.15$ |
| **On-Shelf Availability (OSA)** | % of items purchasable at the shelf | $> 98.5\%$ |
| **OT Reduction** | Planned vs. actual overtime hours | $-12\%$ |
| **Cube Utilization** | Average trailer fill rate | $+5\%$ |

CV (Coefficient of Variation) is the primary operational health metric. A CV below 0.15 means the DC is running a genuinely flat plan — not just "smoother on average" while still spiking on Tuesdays.

---

*Part of the [DC Outbound Smoothing](https://github.com/m-kunta) project — Mohith Kunta*
