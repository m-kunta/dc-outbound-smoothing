"""
LevelSet — Real Data Loader
============================
Validates user-uploaded CSVs, normalises column names, and writes them
into the SQLite database so the solver can run against real data.

Each table has a REQUIRED_COLS set and an optional OPTIONAL_COLS set.
If an optional column is absent, a sensible default is filled in.

Author: Mohith Kunta (https://github.com/m-kunta)
"""

from __future__ import annotations  # enables X | Y unions on Python 3.9

import io
import sqlite3

import pandas as pd

DB_PATH = "levelset.db"

# ── Schema Definitions ────────────────────────────────────────────────────────
# Each entry: (required_cols, optional_cols_with_defaults)

TABLE_SCHEMAS: dict[str, dict] = {
    "demand": {
        "required": {"ORDER_ID", "SKU_ID", "DEST_LOC", "NEED_DATE", "QTY_CASES", "RESOURCE_TYPE"},
        "optional": {"PRIORITY": "SOFT"},
        "description": "Replenishment order lines",
        "sample_row": {
            "ORDER_ID": "ORD00001",
            "SKU_ID": "SKU001",
            "DEST_LOC": "STORE001",
            "NEED_DATE": "2026-03-05",
            "QTY_CASES": 24,
            "RESOURCE_TYPE": "Conveyable",
            "PRIORITY": "SOFT",
        },
    },
    "dc_capacity": {
        "required": {"DC_ID", "RESOURCE_ID", "MAX_THRU", "OP_DATE"},
        "optional": {},
        "description": "Daily DC outbound capacity by resource type",
        "sample_row": {
            "DC_ID": "DC001",
            "RESOURCE_ID": "Conveyable",
            "MAX_THRU": 220,
            "OP_DATE": "2026-03-03",
        },
    },
    "inventory": {
        "required": {"SKU_ID", "ON_HAND_AVAIL"},
        "optional": {"ASN_ETA": "2026-01-01"},  # default = already available
        "description": "Current on-hand inventory and expected ASN arrival",
        "sample_row": {
            "SKU_ID": "SKU001",
            "ON_HAND_AVAIL": 150,
            "ASN_ETA": "2026-03-02",
        },
    },
    "sku_master": {
        "required": {"SKU_ID", "RESOURCE_TYPE"},
        "optional": {
            "CATEGORY": "General",
            "SHELF_LIFE": 30,
            "CASE_CUBE": 1.0,
            "UOM_CONV": 12.0,
        },
        "description": "SKU attributes: resource type, shelf life, cube, UOM conversion",
        "sample_row": {
            "SKU_ID": "SKU001",
            "RESOURCE_TYPE": "Conveyable",
            "CATEGORY": "Grocery",
            "SHELF_LIFE": 60,
            "CASE_CUBE": 0.8,
            "UOM_CONV": 12,
        },
    },
    "store_master": {
        "required": {"STORE_ID", "DELIVERY_CALENDAR"},
        "optional": {"BACKROOM_CAP": 150},
        "description": "Store delivery calendar and backroom pallet capacity",
        "sample_row": {
            "STORE_ID": "STORE001",
            "DELIVERY_CALENDAR": "Mon,Wed,Fri",
            "BACKROOM_CAP": 120,
        },
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Strip whitespace and upper-case all column names."""
    df.columns = [c.strip().upper() for c in df.columns]
    return df


def validate(table: str, df: pd.DataFrame) -> list[str]:
    """
    Return a list of error strings. Empty list = valid.
    Checks required columns are present and NEED_DATE / OP_DATE are ISO-format dates.
    """
    schema = TABLE_SCHEMAS[table]
    errors = []

    missing = schema["required"] - set(df.columns)
    if missing:
        errors.append(f"Missing required columns: {', '.join(sorted(missing))}")

    # Date format checks
    for date_col in ("NEED_DATE", "OP_DATE", "ASN_ETA"):
        if date_col in df.columns:
            try:
                pd.to_datetime(df[date_col], format="%Y-%m-%d")
            except Exception:
                errors.append(f"`{date_col}` must be in YYYY-MM-DD format (e.g. 2026-03-10)")

    return errors


def apply_defaults(table: str, df: pd.DataFrame) -> pd.DataFrame:
    """Fill in any missing optional columns with their defaults."""
    for col, default in TABLE_SCHEMAS[table]["optional"].items():
        if col not in df.columns:
            df[col] = default
    return df


def load_csv(table: str, file_bytes: bytes) -> tuple[pd.DataFrame | None, list[str]]:
    """
    Parse uploaded bytes, validate, apply defaults, and return (df, errors).
    df is None if validation failed.
    """
    try:
        df = pd.read_csv(io.BytesIO(file_bytes))
    except Exception as exc:
        return None, [f"Could not parse CSV: {exc}"]

    df = normalise_columns(df)
    errors = validate(table, df)
    if errors:
        return None, errors

    df = apply_defaults(table, df)
    return df, []


def write_to_db(tables: dict[str, pd.DataFrame], db_path: str = DB_PATH) -> None:
    """Write validated DataFrames into SQLite, replacing existing tables."""
    with sqlite3.connect(db_path) as conn:
        for table, df in tables.items():
            df.to_sql(table, conn, if_exists="replace", index=False)


def get_sample_csv(table: str) -> str:
    """Return a one-row CSV string with the correct column headers for a table."""
    row = TABLE_SCHEMAS[table]["sample_row"]
    df = pd.DataFrame([row])
    return df.to_csv(index=False)


# ── Export Helpers ────────────────────────────────────────────────────────────

def plan_to_csv(plan: pd.DataFrame) -> bytes:
    """Return the smoothed plan as UTF-8 CSV bytes."""
    return plan.to_csv(index=False).encode("utf-8")


def plan_to_json(plan: pd.DataFrame) -> bytes:
    """Return the smoothed plan as UTF-8 JSON bytes (records orientation)."""
    return plan.to_json(orient="records", indent=2, date_format="iso").encode("utf-8")


def kpis_to_json(kpis: dict) -> bytes:
    """Return the KPI dict as formatted JSON bytes."""
    import json
    return json.dumps(kpis, indent=2).encode("utf-8")
