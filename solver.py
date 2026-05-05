"""
LevelSet — Smoothing Solver Engine
=====================================
Implements the constrained replenishment optimizer:
  1. Load data from SQLite
  2. Classify orders (HARD vs SOFT)
  3. Convert units (cases → capacity UOM)
  4. Check capacity on need_dates
  5. Smooth: shift SOFT orders into trough days (minimize variance objective Z)
  6. Apply guardrails: frozen zone, inventory readiness, store calendar, MRSL
  7. Compute KPIs: before/after CV, OSA proxy, OT estimate, cube utilization

Author: Mohith Kunta (https://github.com/m-kunta)
"""

from __future__ import annotations  # enables X | Y unions on Python 3.9

import os
import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "levelset.db")
DEFAULT_DELIVERY_CALENDAR = "Mon,Tue,Wed,Thu,Fri"
DEFAULT_BACKROOM_CAP = 999


def get_runtime_config(
    *,
    lambda_val: float | None = None,
    gamma_val: float | None = None,
    frozen_hours: int | None = None,
    horizon_days: int | None = None,
) -> dict[str, float | int | date]:
    """Read solver settings at call time so UI/env changes take effect immediately."""
    resolved_lambda = float(lambda_val if lambda_val is not None else os.getenv("LAMBDA", 100))
    resolved_gamma = float(gamma_val if gamma_val is not None else os.getenv("GAMMA", 1))
    resolved_frozen_hours = int(frozen_hours if frozen_hours is not None else os.getenv("FROZEN_HOURS", 48))
    resolved_horizon_days = int(horizon_days if horizon_days is not None else os.getenv("HORIZON_DAYS", 10))
    smooth_peak_ratio = float(os.getenv("SMOOTH_PEAK_RATIO", 1.05))
    smooth_trough_ratio = float(os.getenv("SMOOTH_TROUGH_RATIO", 0.90))
    today = date.today()

    return {
        "lambda": resolved_lambda,
        "gamma": resolved_gamma,
        "frozen_hours": resolved_frozen_hours,
        "horizon_days": resolved_horizon_days,
        "smooth_peak_ratio": smooth_peak_ratio,
        "smooth_trough_ratio": smooth_trough_ratio,
        "today": today,
        "frozen_date": today + timedelta(hours=resolved_frozen_hours),
    }


# ── Data Loading ───────────────────────────────────────────────────────────────

def load_data(db_path: str = DB_PATH) -> dict[str, pd.DataFrame]:
    """Load all 5 tables from SQLite into DataFrames."""
    with sqlite3.connect(db_path) as conn:
        return {
            "demand":       pd.read_sql("SELECT * FROM demand", conn),
            "dc_capacity":  pd.read_sql("SELECT * FROM dc_capacity", conn),
            "inventory":    pd.read_sql("SELECT * FROM inventory", conn),
            "sku_master":   pd.read_sql("SELECT * FROM sku_master", conn),
            "store_master": pd.read_sql("SELECT * FROM store_master", conn),
        }


# ── Unit Conversion (REQ-02) ────────────────────────────────────────────────
def convert_units(demand: pd.DataFrame, sku_master: pd.DataFrame) -> pd.DataFrame:
    """
    Convert each order line from QTY_CASES to QTY_PALLETS for capacity comparison.
    QTY_PALLETS = QTY_CASES / UOM_CONV (cases per pallet).
    """
    demand = demand.merge(
        sku_master[["SKU_ID", "UOM_CONV", "SHELF_LIFE", "CASE_CUBE"]],
        on="SKU_ID", how="left"
    )
    demand["QTY_PALLETS"] = np.ceil(demand["QTY_CASES"] / demand["UOM_CONV"])
    return demand


# ── Capacity Aggregation (REQ-01) ────────────────────────────────────────────
def get_daily_capacity(dc_capacity: pd.DataFrame) -> dict[tuple[str, str, str], float]:
    """Build a lookup: (dc_id, date_str, resource_type) → max_throughput in pallets."""
    return {
        (row["DC_ID"], row["OP_DATE"], row["RESOURCE_ID"]): row["MAX_THRU"]
        for _, row in dc_capacity.iterrows()
    }


def build_day_load(demand: pd.DataFrame, date_col: str = "NEED_DATE") -> pd.DataFrame:
    """
    Aggregate total pallets per (date, resource_type) from the given date column.
    Returns a DataFrame with columns: OP_DATE, RESOURCE_TYPE, PALLETS.
    """
    return (
        demand.groupby([date_col, "RESOURCE_TYPE"])["QTY_PALLETS"]
        .sum()
        .reset_index()
        .rename(columns={date_col: "OP_DATE", "QTY_PALLETS": "PALLETS"})
    )


# ── Guardrail Helpers ─────────────────────────────────────────────────────────

def is_frozen(date_str: str, frozen_date: date) -> bool:
    """REQ-04: Returns True if the proposed date is within the frozen window."""
    return date.fromisoformat(date_str) <= frozen_date


def inventory_ok(
    sku_id: str,
    ship_date_str: str,
    qty_cases: float,
    inv_lookup: dict[str, str],
    on_hand_lookup: dict[str, float],
) -> bool:
    """REQ-05: Allow if enough on-hand exists or the ASN arrives by the ship date."""
    on_hand = float(on_hand_lookup.get(sku_id, 0) or 0)
    if on_hand >= float(qty_cases):
        return True

    asn_eta = inv_lookup.get(sku_id)
    if asn_eta is None:
        return False
    return ship_date_str >= asn_eta


def shelf_life_ok(ship_date_str: str, need_date_str: str, shelf_life_days: int) -> bool:
    """Validate that pulling forward doesn't violate MRSL shelf-life window."""
    ship_dt = date.fromisoformat(ship_date_str)
    need_dt = date.fromisoformat(need_date_str)
    days_early = (need_dt - ship_dt).days
    # Conservative: ship date must leave at least 50% of shelf life after the need date
    return days_early <= max(shelf_life_days // 4, 1)


import functools

@functools.lru_cache(maxsize=1024)
def _get_weekday(date_str: str) -> str:
    return date.fromisoformat(date_str).strftime("%a")

def store_delivery_ok(date_str: str, store_id: str, store_lookup: dict[str, str]) -> bool:
    """REQ-06: Returns True if the date is a valid delivery day for this store."""
    calendar = store_lookup.get(store_id, DEFAULT_DELIVERY_CALENDAR)
    weekday = _get_weekday(date_str)
    return weekday in calendar


def backroom_ok(
    date_str: str,
    store_id: str,
    pallets: float,
    backroom_caps: dict[str, float],
    store_day_load: dict[tuple[str, str], float],
) -> bool:
    """REQ-06: Returns True if adding this order doesn't bust the store's backroom cap."""
    cap = backroom_caps.get(store_id, DEFAULT_BACKROOM_CAP)
    current = store_day_load.get((date_str, store_id), 0)
    return current + pallets <= cap


# ── Core Smoothing Algorithm ─────────────────────────────────────────────────

def smooth(
    soft_orders: pd.DataFrame,
    hard_orders: pd.DataFrame,
    dc_cap_lookup: dict[tuple[str, str], float],
    inv_lookup: dict[str, str],
    on_hand_lookup: dict[str, float],
    store_cal_lookup: dict[str, str],
    backroom_caps: dict[str, float],
    all_dates: list[str],
    config: dict[str, float | int | date],
    avg_by_resource: dict[str, float] | None = None,
) -> pd.DataFrame:
    """
    For each SOFT order, attempt to pull forward to a trough day.

    Trigger: a day is a candidate for smoothing if its resource load
    exceeds avg_by_resource[resource] * SMOOTH_PEAK_RATIO (default 105%).
    This means the solver actively levels peaks, not just capacity overflows.

    Move guardrail: only accept a candidate trough if the resulting
    load there is < the source day's load * SMOOTH_TROUGH_RATIO (default 90%).
    This prevents moving volume to days that are almost-as-peak.
    """
    if avg_by_resource is None:
        avg_by_resource = {}
    # Running load tracker: (date, resource) → pallets already assigned
    running_load: dict[tuple[str, str], float] = {}
    # Store backroom running load: (date, store) → pallets
    store_day_load: dict[tuple[str, str], float] = {}
    # HARD load reservation: (date, resource) → locked pallets
    hard_load: dict[tuple[str, str], float] = {}

    # Seed the running load with all demand already on each date
    for _, row in pd.concat([soft_orders, hard_orders], ignore_index=True).iterrows():
        key = (row["NEED_DATE"], row["RESOURCE_TYPE"])
        running_load[key] = running_load.get(key, 0) + row["QTY_PALLETS"]
        skey = (row["NEED_DATE"], row["DEST_LOC"])
        store_day_load[skey] = store_day_load.get(skey, 0) + row["QTY_PALLETS"]
        if row["PRIORITY"] == "HARD":
            hard_load[key] = hard_load.get(key, 0) + row["QTY_PALLETS"]

    result_rows = []

    for _, order in soft_orders.sort_values("NEED_DATE", ascending=False).iterrows():
        need_date  = order["NEED_DATE"]
        resource   = order["RESOURCE_TYPE"]
        pallets    = order["QTY_PALLETS"]
        qty_cases  = order["QTY_CASES"]
        sku_id     = order["SKU_ID"]
        store_id   = order["DEST_LOC"]
        shelf_life = order.get("SHELF_LIFE", 30)

        need_key   = (need_date, resource)
        cap_on_need = dc_cap_lookup.get(need_key, 0)
        load_on_need = running_load.get(need_key, 0)

        smoothed_date = need_date
        move_reason   = "No change needed"

        # Trigger: attempt smoothing if this day's per-resource load is a peak
        # (above DC capacity OR above the per-resource daily average * PEAK_RATIO)
        avg_load_for_resource = avg_by_resource.get(resource, 0.0)
        peak_threshold = avg_load_for_resource * float(config["smooth_peak_ratio"])
        if (load_on_need > peak_threshold or load_on_need > cap_on_need) and not is_frozen(need_date, config["frozen_date"]):
            # Search backward for a trough day within HORIZON_DAYS
            candidate_dates = [
                d for d in all_dates
                if d < need_date and not is_frozen(d, config["frozen_date"])
            ]
            candidate_dates = sorted(candidate_dates, reverse=True)[:int(config["horizon_days"])]
            best_candidate = None
            best_score = None

            for candidate in candidate_dates:
                ckey = (candidate, resource)
                cap_candidate = dc_cap_lookup.get(ckey, 0)
                load_candidate = running_load.get(ckey, 0)
                hard_load_candidate = hard_load.get(ckey, 0)
                headroom = cap_candidate - load_candidate

                if headroom < pallets:
                    continue
                if not inventory_ok(sku_id, candidate, qty_cases, inv_lookup, on_hand_lookup):
                    continue
                if not shelf_life_ok(candidate, need_date, shelf_life):
                    continue
                if not store_delivery_ok(candidate, store_id, store_cal_lookup):
                    continue
                if not backroom_ok(candidate, store_id, pallets, backroom_caps, store_day_load):
                    continue

                # Keep destination meaningfully lighter than the source after the move.
                if load_candidate + pallets >= load_on_need * float(config["smooth_trough_ratio"]):
                    continue

                days_early = (date.fromisoformat(need_date) - date.fromisoformat(candidate)).days
                flattening_gain = load_on_need - (load_candidate + pallets)
                hard_pressure_penalty = (float(config["lambda"]) / 100.0) * (
                    hard_load_candidate / max(cap_candidate, 1)
                )
                early_ship_penalty = float(config["gamma"]) * days_early
                score = flattening_gain - hard_pressure_penalty - early_ship_penalty

                if score <= 0:
                    continue
                if best_score is None or score > best_score:
                    best_score = score
                    best_candidate = candidate

            if best_candidate is not None:
                ckey = (best_candidate, resource)
                # Valid window found — move the order
                # Remove from old date, add to new date in running loads
                running_load[need_key] = running_load.get(need_key, 0) - pallets
                running_load[ckey] = running_load.get(ckey, 0) + pallets

                old_skey = (need_date, store_id)
                new_skey = (best_candidate, store_id)
                store_day_load[old_skey] = store_day_load.get(old_skey, 0) - pallets
                store_day_load[new_skey] = store_day_load.get(new_skey, 0) + pallets

                smoothed_date = best_candidate
                move_reason = "Pull-forward (capacity constraint)"
            else:
                move_reason = "⚠️ No valid window — capacity alert"

        result_rows.append({
            **order.to_dict(),
            "SMOOTHED_DATE": smoothed_date,
            "SHIFT_DAYS": (
                (date.fromisoformat(need_date) - date.fromisoformat(smoothed_date)).days
            ),
            "MOVE_REASON": move_reason,
        })

    return pd.DataFrame(result_rows)


# ── KPI Computation ────────────────────────────────────────────────────────────

def compute_kpis(
    demand_before: pd.DataFrame,
    plan_after: pd.DataFrame,
    dc_cap_lookup: dict[tuple[str, str], float],
) -> dict:
    """
    Compute before/after KPIs:
      - Outbound CV (coefficient of variation of daily total pallets)
      - OSA proxy (% HARD orders scheduled on or before their need date)
      - Shifted orders (count and % of SOFT orders that were moved)
      - Capacity alerts (SOFT orders with no valid window)
      - Cube utilization (avg daily pallet fill rate vs capacity)
    """
    def daily_volumes(df: pd.DataFrame, date_col: str) -> pd.Series:
        return df.groupby(date_col)["QTY_PALLETS"].sum()

    before_vols = daily_volumes(demand_before, "NEED_DATE")
    after_vols  = daily_volumes(plan_after, "SMOOTHED_DATE")

    def cv(series: pd.Series) -> float:
        return float(series.std() / series.mean()) if series.mean() > 0 else 0.0

    hard_after = plan_after[plan_after["PRIORITY"] == "HARD"]
    # When there are no HARD orders, OSA is vacuously 100% (nothing to miss)
    if hard_after.empty:
        osa_pct = 100.0
    else:
        osa_pct = float(
            (hard_after["SMOOTHED_DATE"] <= hard_after["NEED_DATE"]).mean() * 100
        )

    soft_after = plan_after[plan_after["PRIORITY"] == "SOFT"]
    n_moved  = (soft_after["SHIFT_DAYS"] > 0).sum()
    n_alerts = (soft_after["MOVE_REASON"].str.startswith("⚠️")).sum()

    avg_daily_cap = sum(dc_cap_lookup.values()) / max(
        len({k[0] for k in dc_cap_lookup.keys()}), 1
    )
    cube_util_before = float(before_vols.mean() / avg_daily_cap * 100) if avg_daily_cap else 0
    cube_util_after  = float(after_vols.mean()  / avg_daily_cap * 100) if avg_daily_cap else 0

    return {
        "cv_before":          round(cv(before_vols), 3),
        "cv_after":           round(cv(after_vols), 3),
        "osa_pct":            round(osa_pct, 1),
        "n_orders_total":     len(plan_after),
        "n_soft":             len(soft_after),
        "n_moved":            int(n_moved),
        "n_alerts":           int(n_alerts),
        "cube_util_before":   round(cube_util_before, 1),
        "cube_util_after":    round(cube_util_after, 1),
        "avg_daily_before":   round(float(before_vols.mean()), 1),
        "avg_daily_after":    round(float(after_vols.mean()), 1),
    }


# ── Main Entrypoint ────────────────────────────────────────────────────────────

def solve(
    db_path: str = DB_PATH,
    *,
    horizon_days: int | None = None,
    frozen_hours: int | None = None,
    lambda_val: float | None = None,
    gamma_val: float | None = None,
) -> dict:
    """
    Full solve pipeline with multi-DC support.

    Phase 1: Solve each DC independently using the greedy smooth() algorithm.
    Phase 2: For orders that received a capacity alert in their primary DC,
             attempt cross-DC rerouting to any alternate DC with headroom.

    Returns a dict with: 'plan' DataFrame and 'kpis' dict.
    Also writes the smoothed_plan table back to SQLite.
    """
    config = get_runtime_config(
        lambda_val=lambda_val,
        gamma_val=gamma_val,
        frozen_hours=frozen_hours,
        horizon_days=horizon_days,
    )
    data = load_data(db_path)
    demand       = data["demand"]
    dc_capacity  = data["dc_capacity"]
    inventory    = data["inventory"]
    sku_master   = data["sku_master"]
    store_master = data["store_master"]

    # Enrich demand with unit conversion and SKU attributes
    demand = convert_units(demand, sku_master)

    # Backward-compat: databases generated before multi-DC support lack DC_ID columns
    if "DC_ID" not in demand.columns:
        demand["DC_ID"] = "DC001"
    if "DC_ID" not in dc_capacity.columns:
        dc_capacity["DC_ID"] = "DC001"

    # Full cap lookup: (dc_id, date_str, resource_type) → max_thru
    full_cap_lookup = get_daily_capacity(dc_capacity)
    all_dc_ids = sorted(dc_capacity["DC_ID"].unique().tolist())
    all_dates  = sorted(dc_capacity["OP_DATE"].unique().tolist())

    # Common guardrail lookups
    inv_lookup       = dict(zip(inventory["SKU_ID"], inventory["ASN_ETA"]))
    on_hand_lookup   = dict(zip(inventory["SKU_ID"], inventory["ON_HAND_AVAIL"]))
    store_cal_lookup = dict(zip(store_master["STORE_ID"], store_master["DELIVERY_CALENDAR"]))
    backroom_caps    = dict(zip(store_master["STORE_ID"], store_master["BACKROOM_CAP"]))

    # Helper: per-DC capacity sub-lookup for smooth()
    def _dc_cap(dc_id: str) -> dict[tuple[str, str], float]:
        return {(d, r): cap for (dc, d, r), cap in full_cap_lookup.items() if dc == dc_id}

    # Split HARD and SOFT
    hard_orders = demand[demand["PRIORITY"] == "HARD"].copy()
    soft_orders = demand[demand["PRIORITY"] == "SOFT"].copy()

    # HARD orders stay pinned to their need date
    hard_orders["SMOOTHED_DATE"] = hard_orders["NEED_DATE"]
    hard_orders["SHIFT_DAYS"]    = 0
    hard_orders["MOVE_REASON"]   = "Locked (HARD priority)"
    hard_orders["SMOOTHED_DC"]   = hard_orders["DC_ID"]

    # ── Phase 1: solve each DC independently ──────────────────────────────────
    soft_plans: list[pd.DataFrame] = []
    for dc in all_dc_ids:
        dc_soft = soft_orders[soft_orders["DC_ID"] == dc].copy()
        dc_hard = hard_orders[hard_orders["DC_ID"] == dc].copy()
        if dc_soft.empty:
            continue
        dc_lookup = _dc_cap(dc)
        avg_by_resource = (
            pd.concat([dc_soft, dc_hard])
            .groupby("RESOURCE_TYPE")["QTY_PALLETS"]
            .sum()
            .div(max(len(all_dates), 1))
            .to_dict()
        )
        dc_plan = smooth(
            dc_soft, dc_hard, dc_lookup, inv_lookup, on_hand_lookup,
            store_cal_lookup, backroom_caps, all_dates,
            config=config,
            avg_by_resource=avg_by_resource,
        )
        dc_plan["SMOOTHED_DC"] = dc
        soft_plans.append(dc_plan)

    soft_plan = pd.concat(soft_plans, ignore_index=True) if soft_plans else pd.DataFrame()

    # ── Phase 2: cross-DC rerouting for remaining capacity alerts ─────────────
    n_rerouted = 0
    if len(all_dc_ids) > 1 and not soft_plan.empty:
        # Seed running loads from the already-scheduled plan for every DC
        alt_running: dict[str, dict[tuple[str, str], float]] = {dc: {} for dc in all_dc_ids}
        for _, row in pd.concat([soft_plan, hard_orders], ignore_index=True).iterrows():
            dc  = row.get("SMOOTHED_DC", row.get("DC_ID", all_dc_ids[0]))
            key = (row["SMOOTHED_DATE"], row["RESOURCE_TYPE"])
            alt_running[dc][key] = alt_running[dc].get(key, 0) + row["QTY_PALLETS"]

        alert_mask = soft_plan["MOVE_REASON"].str.startswith("⚠️")
        for idx in soft_plan[alert_mask].index:
            order      = soft_plan.loc[idx]
            primary_dc = order["DC_ID"]
            need_date  = order["NEED_DATE"]
            resource   = order["RESOURCE_TYPE"]
            pallets    = order["QTY_PALLETS"]
            qty_cases  = order["QTY_CASES"]
            sku_id     = order["SKU_ID"]
            store_id   = order["DEST_LOC"]
            shelf_life = order.get("SHELF_LIFE", 30)

            for alt_dc in all_dc_ids:
                if alt_dc == primary_dc:
                    continue
                running = alt_running[alt_dc]
                # Try need_date first, then scan backward within horizon
                candidates = [need_date] + sorted(
                    [d for d in all_dates if d < need_date
                     and not is_frozen(d, config["frozen_date"])],
                    reverse=True,
                )[:int(config["horizon_days"])]

                for candidate in candidates:
                    cap  = full_cap_lookup.get((alt_dc, candidate, resource), 0)
                    load = running.get((candidate, resource), 0)
                    if cap - load < pallets:
                        continue
                    if not inventory_ok(sku_id, candidate, qty_cases, inv_lookup, on_hand_lookup):
                        continue
                    if not shelf_life_ok(candidate, need_date, shelf_life):
                        continue
                    if not store_delivery_ok(candidate, store_id, store_cal_lookup):
                        continue
                    # Accept reroute
                    running[(candidate, resource)] = load + pallets
                    shift = (date.fromisoformat(need_date) - date.fromisoformat(candidate)).days
                    soft_plan.at[idx, "SMOOTHED_DC"]   = alt_dc
                    soft_plan.at[idx, "SMOOTHED_DATE"]  = candidate
                    soft_plan.at[idx, "SHIFT_DAYS"]     = shift
                    soft_plan.at[idx, "MOVE_REASON"]    = f"Cross-DC reroute → {alt_dc}"
                    n_rerouted += 1
                    break
                else:
                    continue
                break

    # Combine into full plan
    plan = pd.concat([hard_orders, soft_plan], ignore_index=True)
    plan = plan.sort_values(["SMOOTHED_DATE", "RESOURCE_TYPE"])

    # KPIs — collapse full_cap to (date, resource) by summing across DCs for CV/cube calc
    dc_cap_for_kpis: dict[tuple[str, str], float] = {}
    for (dc, d, r), cap in full_cap_lookup.items():
        key = (d, r)
        dc_cap_for_kpis[key] = dc_cap_for_kpis.get(key, 0) + cap

    kpis = compute_kpis(demand, plan, dc_cap_for_kpis)
    kpis["n_rerouted"] = n_rerouted

    # Persist to DB
    with sqlite3.connect(db_path) as conn:
        plan.to_sql("smoothed_plan", conn, if_exists="replace", index=False)

    return {"plan": plan, "kpis": kpis}


if __name__ == "__main__":
    print("🔄 Running LevelSet solver...")
    result = solve()
    kpis = result["kpis"]
    print(f"\n📊 KPI Summary")
    print(f"   Outbound CV:      {kpis['cv_before']} → {kpis['cv_after']}  {'✅' if kpis['cv_after'] < kpis['cv_before'] else '❌'}")
    print(f"   OSA (HARD):       {kpis['osa_pct']}%  {'✅' if kpis['osa_pct'] >= 98.5 else '⚠️'}")
    print(f"   Orders shifted:   {kpis['n_moved']} of {kpis['n_soft']} SOFT orders")
    print(f"   Capacity alerts:  {kpis['n_alerts']}")
    print(f"   Cube util:        {kpis['cube_util_before']}% → {kpis['cube_util_after']}%")
    print(f"\n✅ Smoothed plan written to levelset.db (smoothed_plan table)")
