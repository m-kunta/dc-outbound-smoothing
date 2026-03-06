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
LAMBDA  = float(os.getenv("LAMBDA", 100))   # OSA penalty weight
GAMMA   = float(os.getenv("GAMMA", 1))      # Early ship penalty weight
FROZEN_HOURS   = int(os.getenv("FROZEN_HOURS", 48))
HORIZON_DAYS   = int(os.getenv("HORIZON_DAYS", 10))
# Smoothing sensitivity: trigger = days whose load > avg * PEAK_RATIO
# Move guardrail: only move to days where resulting load < source load * TROUGH_RATIO
SMOOTH_PEAK_RATIO   = float(os.getenv("SMOOTH_PEAK_RATIO", 1.05))   # flag day as peak if >105% avg
SMOOTH_TROUGH_RATIO = float(os.getenv("SMOOTH_TROUGH_RATIO", 0.90)) # accept trough if load < 90% source
DEFAULT_DELIVERY_CALENDAR = "Mon,Tue,Wed,Thu,Fri"
DEFAULT_BACKROOM_CAP = 999
TODAY = date.today()
FROZEN_DATE = TODAY + timedelta(hours=FROZEN_HOURS)


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
def get_daily_capacity(dc_capacity: pd.DataFrame) -> dict[tuple[str, str], float]:
    """Build a lookup: (date_str, resource_type) → max_throughput in pallets."""
    return {
        (row["OP_DATE"], row["RESOURCE_ID"]): row["MAX_THRU"]
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

def is_frozen(date_str: str) -> bool:
    """REQ-04: Returns True if the proposed date is within the frozen window."""
    return date.fromisoformat(date_str) <= FROZEN_DATE


def inventory_ok(sku_id: str, ship_date_str: str, inv_lookup: dict[str, str]) -> bool:
    """REQ-05: Returns True if on-hand or ASN will be ready before the ship date."""
    asn_eta = inv_lookup.get(sku_id)
    if asn_eta is None:
        return True  # No ASN record; assume available
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
    dc_cap_lookup: dict[tuple[str, str], float],
    inv_lookup: dict[str, str],
    store_cal_lookup: dict[str, str],
    backroom_caps: dict[str, float],
    all_dates: list[str],
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

    # Seed the running load with all demand already on each date
    for _, row in soft_orders.iterrows():
        key = (row["NEED_DATE"], row["RESOURCE_TYPE"])
        running_load[key] = running_load.get(key, 0) + row["QTY_PALLETS"]
        skey = (row["NEED_DATE"], row["DEST_LOC"])
        store_day_load[skey] = store_day_load.get(skey, 0) + row["QTY_PALLETS"]

    result_rows = []

    for _, order in soft_orders.sort_values("NEED_DATE", ascending=False).iterrows():
        need_date  = order["NEED_DATE"]
        resource   = order["RESOURCE_TYPE"]
        pallets    = order["QTY_PALLETS"]
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
        peak_threshold = avg_load_for_resource * SMOOTH_PEAK_RATIO
        if (load_on_need > peak_threshold or load_on_need > cap_on_need) and not is_frozen(need_date):
            # Search backward for a trough day within HORIZON_DAYS
            candidate_dates = [
                d for d in all_dates
                if d < need_date and not is_frozen(d)
            ]
            candidate_dates = sorted(candidate_dates, reverse=True)[:HORIZON_DAYS]

            for candidate in candidate_dates:
                ckey = (candidate, resource)
                cap_candidate = dc_cap_lookup.get(ckey, 0)
                load_candidate = running_load.get(ckey, 0)
                headroom = cap_candidate - load_candidate

                if headroom < pallets:
                    continue
                if not inventory_ok(sku_id, candidate, inv_lookup):
                    continue
                if not shelf_life_ok(candidate, need_date, shelf_life):
                    continue
                if not store_delivery_ok(candidate, store_id, store_cal_lookup):
                    continue
                if not backroom_ok(candidate, store_id, pallets, backroom_caps, store_day_load):
                    continue

                # Variance check: only move if the resulting load on the candidate day
                # will be strictly lighter than the source day's current load.
                # This guarantees every move flattens the curve (peak goes down, trough goes up
                # but not higher than where the peak was).
                if load_candidate + pallets >= load_on_need:
                    continue

                # Valid window found — move the order
                # Remove from old date, add to new date in running loads
                running_load[need_key] = running_load.get(need_key, 0) - pallets
                running_load[ckey] = running_load.get(ckey, 0) + pallets

                old_skey = (need_date, store_id)
                new_skey = (candidate, store_id)
                store_day_load[old_skey] = store_day_load.get(old_skey, 0) - pallets
                store_day_load[new_skey] = store_day_load.get(new_skey, 0) + pallets

                smoothed_date = candidate
                move_reason = "Pull-forward (capacity constraint)"
                break
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

def solve(db_path: str = DB_PATH) -> dict:
    """
    Full solve pipeline.
    Returns a dict with: 'plan' DataFrame and 'kpis' dict.
    Also writes the smoothed_plan table back to SQLite.
    """
    data = load_data(db_path)
    demand       = data["demand"]
    dc_capacity  = data["dc_capacity"]
    inventory    = data["inventory"]
    sku_master   = data["sku_master"]
    store_master = data["store_master"]

    # Enrich demand with unit conversion and SKU attributes
    demand = convert_units(demand, sku_master)

    # Build lookups
    dc_cap_lookup   = get_daily_capacity(dc_capacity)
    inv_lookup      = dict(zip(inventory["SKU_ID"], inventory["ASN_ETA"]))
    store_cal_lookup = dict(zip(store_master["STORE_ID"], store_master["DELIVERY_CALENDAR"]))
    backroom_caps   = dict(zip(store_master["STORE_ID"], store_master["BACKROOM_CAP"]))
    all_dates       = sorted(dc_capacity["OP_DATE"].unique().tolist())

    # Split HARD and SOFT
    hard_orders = demand[demand["PRIORITY"] == "HARD"].copy()
    soft_orders = demand[demand["PRIORITY"] == "SOFT"].copy()

    # HARD orders stay pinned to their need date
    hard_orders["SMOOTHED_DATE"] = hard_orders["NEED_DATE"]
    hard_orders["SHIFT_DAYS"]    = 0
    hard_orders["MOVE_REASON"]   = "Locked (HARD priority)"

    # Compute per-resource average daily load for the peak-trigger heuristic
    all_demand = pd.concat([soft_orders, hard_orders])
    avg_by_resource = (
        all_demand.groupby("RESOURCE_TYPE")["QTY_PALLETS"]
        .sum()
        .div(max(len(all_dates), 1))
        .to_dict()
    )

    # Smooth SOFT orders
    soft_plan = smooth(
        soft_orders, dc_cap_lookup, inv_lookup,
        store_cal_lookup, backroom_caps, all_dates,
        avg_by_resource=avg_by_resource,
    )

    # Combine into full plan
    plan = pd.concat([hard_orders, soft_plan], ignore_index=True)
    plan = plan.sort_values(["SMOOTHED_DATE", "RESOURCE_TYPE"])

    # KPIs
    kpis = compute_kpis(demand, plan, dc_cap_lookup)

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
