import sqlite3
import pandas as pd
import numpy as np
from datetime import date, timedelta
import os

from data_gen import generate
from solver import solve
from data_loader import validate, apply_defaults

DB_PATH = "test_levelset.db"

def setup_module():
    os.environ["HORIZON_DAYS"] = "10"
    os.environ["FROZEN_HOURS"] = "48"
    os.environ["LAMBDA"] = "100"
    os.environ["GAMMA"] = "1"
    
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    
    generate(seed=42, db_path=DB_PATH)

def teardown_module():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

def test_dg_01_all_tables_generated():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cursor.fetchall()}
    
    expected = {"sku_master", "store_master", "dc_capacity", "inventory", "demand"}
    assert expected.issubset(tables)

def test_dg_04_hard_soft_ratio():
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql("SELECT PRIORITY, COUNT(*) as count FROM demand GROUP BY PRIORITY", conn)
    
    total = df['count'].sum()
    hard_count_series = df[df['PRIORITY'] == 'HARD']['count']
    if len(hard_count_series) > 0:
        hard_count = hard_count_series.values[0]
    else:
        hard_count = 0
    hard_ratio = hard_count / total
    
    assert 0.15 <= hard_ratio <= 0.25, f"HARD ratio {hard_ratio} outside 15-25% bound"

def test_sv_01_solver_runs_and_kpis():
    result = solve(db_path=DB_PATH)
    plan = result["plan"]
    kpis = result["kpis"]
    
    assert not plan.empty
    assert "cv_before" in kpis
    assert "cv_after" in kpis
    assert kpis["cv_after"] <= kpis["cv_before"]

def test_sv_02_hard_orders_never_moved():
    result = solve(db_path=DB_PATH)
    plan = result["plan"]
    hard_orders = plan[plan["PRIORITY"] == "HARD"]
    
    assert (hard_orders["NEED_DATE"] == hard_orders["SMOOTHED_DATE"]).all()

def test_sv_10_frozen_zone_respected():
    result = solve(db_path=DB_PATH)
    plan = result["plan"]
    moved = plan[plan["NEED_DATE"] != plan["SMOOTHED_DATE"]]
    
    today = date.today()
    frozen_date_str = (today + timedelta(hours=48)).isoformat()
    
    violaters = moved[moved["NEED_DATE"] <= frozen_date_str]
    assert violaters.empty, f"Orders within frozen zone were moved:\n{violaters}"

def test_sv_06_all_orders_accounted_for():
    with sqlite3.connect(DB_PATH) as conn:
        demand = pd.read_sql("SELECT * FROM demand", conn)
    
    result = solve(db_path=DB_PATH)
    plan = result["plan"]
    
    assert len(plan) == len(demand)

def test_dl_02_missing_required_column():
    df = pd.DataFrame({"SKU_ID": ["S1"], "DEST_LOC": ["L1"]})
    df.columns = [c.upper() for c in df.columns]
    errors = validate("demand", df)
    
    assert len(errors) > 0
    assert any("Missing required columns" in e for e in errors)

def test_dl_04_optional_columns_defaulted():
    df = pd.DataFrame({"ORDER_ID": ["O1"], "SKU_ID": ["S1"], "DEST_LOC": ["L1"], 
                       "NEED_DATE": ["2026-03-05"], "QTY_CASES": [10], "RESOURCE_TYPE": ["Bulk"]})
    df = apply_defaults("demand", df)
    
    assert "PRIORITY" in df.columns
    assert df["PRIORITY"].iloc[0] == "SOFT"
