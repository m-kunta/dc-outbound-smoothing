"""
LevelSet — Synthetic Data Generator
=====================================
Generates a realistic 30-day replenishment planning dataset across 5 SQLite tables.
Volume is intentionally spiky (60% Mon/Tue/Wed) to demonstrate the smoothing effect.

Author: Mohith Kunta (https://github.com/m-kunta)
"""

import random
import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd

# ── Constants ─────────────────────────────────────────────────────────────────
SEED = 42
DB_PATH = "levelset.db"
N_SKUS = 50
N_STORES = 8
N_DAYS = 30
START_DATE = date(2026, 3, 1)

RESOURCES = ["Conveyable", "NonConveyable", "Bulk"]
CATEGORIES = ["Grocery", "Health & Beauty", "Dairy", "Frozen", "Household"]

# Stores deliberately skewed: 5 stores on Mon/Wed/Fri, 3 on Tue/Thu
STORE_DELIVERY_CALENDARS = {
    "STORE001": "Mon,Wed,Fri",
    "STORE002": "Mon,Wed,Fri",
    "STORE003": "Mon,Wed,Fri",
    "STORE004": "Mon,Wed,Fri",
    "STORE005": "Mon,Wed,Fri",
    "STORE006": "Tue,Thu",
    "STORE007": "Tue,Thu",
    "STORE008": "Tue,Thu",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def date_range(n: int) -> list[date]:
    return [START_DATE + timedelta(days=i) for i in range(n)]


def weekday_name(d: date) -> str:
    return d.strftime("%a")  # Mon, Tue, Wed...


def is_valid_delivery_day(d: date, calendar: str) -> bool:
    return weekday_name(d) in calendar.split(",")


# ── Table Builders ────────────────────────────────────────────────────────────

def build_sku_master(rng: random.Random, np_rng: np.random.Generator) -> pd.DataFrame:
    """50 SKUs with physical and freshness attributes."""
    rows = []
    for i in range(1, N_SKUS + 1):
        category = rng.choice(CATEGORIES)
        resource = (
            "Bulk" if category in ("Grocery", "Household")
            else "NonConveyable" if category in ("Dairy", "Frozen")
            else "Conveyable"
        )
        shelf_life = (
            int(np_rng.integers(3, 10)) if category in ("Dairy", "Frozen")
            else int(np_rng.integers(30, 91))
        )
        rows.append({
            "SKU_ID": f"SKU{i:03d}",
            "CATEGORY": category,
            "RESOURCE_TYPE": resource,
            "SHELF_LIFE": shelf_life,          # days
            "CASE_CUBE": round(float(np_rng.uniform(0.3, 2.5)), 2),  # cubic meters per case
            "UOM_CONV": round(float(np_rng.uniform(6, 24)), 0),       # cases per pallet
        })
    return pd.DataFrame(rows)


def build_store_master() -> pd.DataFrame:
    """8 stores with backroom capacity and valid delivery days."""
    rows = []
    for store_id, calendar in STORE_DELIVERY_CALENDARS.items():
        rows.append({
            "STORE_ID": store_id,
            "BACKROOM_CAP": random.randint(80, 200),  # pallets per delivery day
            "DELIVERY_CALENDAR": calendar,
        })
    return pd.DataFrame(rows)


def build_dc_capacity(np_rng: np.random.Generator) -> pd.DataFrame:
    """Daily capacity ceiling per resource type for 30 operating days."""
    rows = []
    # Tighter caps to simulate a realistic DC at ~75-85% utilisation on average
    base_caps = {"Conveyable": 220, "NonConveyable": 120, "Bulk": 180}  # pallets/day
    for d in date_range(N_DAYS):
        for resource, base in base_caps.items():
            # No capacity on Sundays; slightly lower on Saturdays
            if weekday_name(d) == "Sun":
                cap = 0
            elif weekday_name(d) == "Sat":
                cap = int(base * 0.6)
            else:
                cap = int(base * float(np_rng.uniform(0.92, 1.08)))
            rows.append({
                "DC_ID": "DC001",
                "RESOURCE_ID": resource,
                "MAX_THRU": cap,
                "OP_DATE": d.isoformat(),
            })
    return pd.DataFrame(rows)


def build_inventory(rng: random.Random, np_rng: np.random.Generator, sku_master: pd.DataFrame) -> pd.DataFrame:
    """One record per SKU: current on-hand and expected ASN arrival."""
    rows = []
    for _, sku in sku_master.iterrows():
        on_hand = int(np_rng.integers(0, 500))
        # ~15% of SKUs have a delayed ASN (arrives after day 5) — tests REQ-05
        if rng.random() < 0.15:
            asn_eta = (START_DATE + timedelta(days=int(np_rng.integers(6, 12)))).isoformat()
        else:
            asn_eta = (START_DATE + timedelta(days=int(np_rng.integers(0, 4)))).isoformat()
        rows.append({
            "SKU_ID": sku["SKU_ID"],
            "ON_HAND_AVAIL": on_hand,
            "ASN_ETA": asn_eta,
        })
    return pd.DataFrame(rows)


def build_demand(
    rng: random.Random,
    np_rng: np.random.Generator,
    sku_master: pd.DataFrame,
    store_master: pd.DataFrame,
) -> pd.DataFrame:
    """
    ~2,000 replenishment order lines.
    Volume intentionally spiky: 60% of need_dates fall Mon/Tue/Wed.
    20% of orders are HARD (below safety stock or promo), 80% are SOFT.
    """
    rows = []
    order_id = 1
    all_dates = date_range(N_DAYS)
    peak_days = [d for d in all_dates if weekday_name(d) in ("Mon", "Tue", "Wed")]
    trough_days = [d for d in all_dates if weekday_name(d) not in ("Mon", "Tue", "Wed", "Sun")]

    stores = store_master["STORE_ID"].tolist()

    for _, sku in sku_master.iterrows():
        for store_id in stores:
            # Each SKU/store pair gets 3–8 order lines across the horizon
            n_orders = int(np_rng.integers(3, 9))
            for _ in range(n_orders):
                # 60% chance the need_date lands on a peak day
                if rng.random() < 0.60:
                    need_date = rng.choice(peak_days)
                else:
                    need_date = rng.choice(trough_days)

                # Snap need_date to a valid delivery day for this store
                store_cal = store_master.loc[store_master["STORE_ID"] == store_id, "DELIVERY_CALENDAR"].iloc[0]
                candidate = need_date
                for offset in range(7):
                    if is_valid_delivery_day(candidate, store_cal):
                        need_date = candidate
                        break
                    candidate = candidate + timedelta(days=1)

                priority = "HARD" if rng.random() < 0.20 else "SOFT"
                qty_cases = int(np_rng.integers(5, 60))

                rows.append({
                    "ORDER_ID": f"ORD{order_id:05d}",
                    "SKU_ID": sku["SKU_ID"],
                    "DEST_LOC": store_id,
                    "NEED_DATE": need_date.isoformat(),
                    "PRIORITY": priority,
                    "QTY_CASES": qty_cases,
                    "RESOURCE_TYPE": sku["RESOURCE_TYPE"],
                })
                order_id += 1

    return pd.DataFrame(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def generate(seed: int = SEED, db_path: str = DB_PATH) -> None:
    """Generate all synthetic tables and write to SQLite."""
    random.seed(seed)
    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)

    print(f"🔧 Generating synthetic data (seed={seed})...")

    sku_master = build_sku_master(rng, np_rng)
    store_master = build_store_master()
    dc_capacity = build_dc_capacity(np_rng)
    inventory = build_inventory(rng, np_rng, sku_master)
    demand = build_demand(rng, np_rng, sku_master, store_master)

    with sqlite3.connect(db_path) as conn:
        sku_master.to_sql("sku_master", conn, if_exists="replace", index=False)
        store_master.to_sql("store_master", conn, if_exists="replace", index=False)
        dc_capacity.to_sql("dc_capacity", conn, if_exists="replace", index=False)
        inventory.to_sql("inventory", conn, if_exists="replace", index=False)
        demand.to_sql("demand", conn, if_exists="replace", index=False)

    print(f"✅ Generated {len(demand):,} demand order lines across {N_SKUS} SKUs and {N_STORES} stores.")
    print(f"   SKU Master:    {len(sku_master)} rows")
    print(f"   Store Master:  {len(store_master)} rows")
    print(f"   DC Capacity:   {len(dc_capacity)} rows ({N_DAYS} days × {len(RESOURCES)} resources)")
    print(f"   Inventory:     {len(inventory)} rows")
    print(f"   Demand:        {len(demand):,} rows  (HARD: {(demand['PRIORITY']=='HARD').sum()}, SOFT: {(demand['PRIORITY']=='SOFT').sum()})")
    print(f"💾 Saved to {db_path}")


if __name__ == "__main__":
    generate()
