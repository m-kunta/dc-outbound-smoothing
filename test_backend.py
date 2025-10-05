import sqlite3
import pandas as pd
import numpy as np
from datetime import date, timedelta
import os

from data_gen import generate
from solver import solve, smooth, get_runtime_config, convert_units
from data_loader import (
    validate,
    apply_defaults,
    normalise_columns,
    load_csv,
    write_to_db,
    get_sample_csv,
    plan_to_csv,
    plan_to_json,
    kpis_to_json,
)

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

def test_dg_02_seed_reproducibility(tmp_path):
    db1 = str(tmp_path / "db1.db")
    db2 = str(tmp_path / "db2.db")
    
    generate(seed=42, db_path=db1)
    generate(seed=42, db_path=db2)
    
    with sqlite3.connect(db1) as conn1, sqlite3.connect(db2) as conn2:
        df1 = pd.read_sql("SELECT * FROM demand ORDER BY ORDER_ID", conn1)
        df2 = pd.read_sql("SELECT * FROM demand ORDER BY ORDER_ID", conn2)
        
    pd.testing.assert_frame_equal(df1, df2)

def test_dg_03_demand_wave_pattern():
    with sqlite3.connect(DB_PATH) as conn:
        demand = pd.read_sql("SELECT NEED_DATE FROM demand", conn)
        
    demand['NEED_DATE'] = pd.to_datetime(demand['NEED_DATE'])
    demand['weekday'] = demand['NEED_DATE'].dt.dayofweek
    
    # 0=Mon, 1=Tue, 2=Wed. The plan says ~60% of volume should be Mon-Wed.
    wave_volume = demand['weekday'].isin([0, 1, 2]).sum()
    total_volume = len(demand)
    
    assert wave_volume / total_volume >= 0.55, "Mon/Tue/Wed demand did not hit 55% threshold"

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


def _future_date(days_out):
    return (date.today() + timedelta(days=days_out)).isoformat()


def _make_order(*, need_date, qty_pallets, qty_cases, resource="Bulk", priority="SOFT",
                sku_id="SKU1", dest_loc="STORE1", shelf_life=30):
    return {
        "ORDER_ID": f"{priority}_{need_date}_{qty_cases}",
        "SKU_ID": sku_id,
        "DEST_LOC": dest_loc,
        "NEED_DATE": need_date,
        "PRIORITY": priority,
        "QTY_CASES": qty_cases,
        "QTY_PALLETS": qty_pallets,
        "RESOURCE_TYPE": resource,
        "SHELF_LIFE": shelf_life,
    }


def test_sv_11_inventory_on_hand_allows_move_before_asn():
    need_date = _future_date(10)
    candidate = _future_date(8)
    soft_orders = pd.DataFrame([
        _make_order(need_date=need_date, qty_pallets=2, qty_cases=20, sku_id="SKU1"),
        _make_order(need_date=need_date, qty_pallets=4, qty_cases=40, sku_id="SKU2"),
    ])
    hard_orders = pd.DataFrame(columns=soft_orders.columns)
    dc_cap_lookup = {
        (need_date, "Bulk"): 3,
        (candidate, "Bulk"): 10,
    }

    result = smooth(
        soft_orders,
        hard_orders,
        dc_cap_lookup,
        {"SKU1": _future_date(20)},
        {"SKU1": 100},
        {"STORE1": "Mon,Tue,Wed,Thu,Fri,Sat,Sun"},
        {"STORE1": 999},
        [candidate, need_date],
        config=get_runtime_config(lambda_val=0, gamma_val=0, frozen_hours=24, horizon_days=5),
        avg_by_resource={"Bulk": 1.0},
    )

    moved_row = result[result["SKU_ID"] == "SKU1"].iloc[0]
    assert moved_row["SMOOTHED_DATE"] == candidate
    assert moved_row["SHIFT_DAYS"] == 2


def test_sv_capacity_checks_include_hard_load_on_candidate_day():
    need_date = _future_date(10)
    candidate = _future_date(8)
    soft_orders = pd.DataFrame([
        _make_order(need_date=need_date, qty_pallets=3, qty_cases=30, sku_id="SKU1")
    ])
    hard_orders = pd.DataFrame([
        _make_order(need_date=candidate, qty_pallets=8, qty_cases=80, priority="HARD", sku_id="SKU9")
    ])
    dc_cap_lookup = {
        (need_date, "Bulk"): 2,
        (candidate, "Bulk"): 10,
    }

    result = smooth(
        soft_orders,
        hard_orders,
        dc_cap_lookup,
        {"SKU1": _future_date(1)},
        {"SKU1": 0},
        {"STORE1": "Mon,Tue,Wed,Thu,Fri,Sat,Sun"},
        {"STORE1": 999},
        [candidate, need_date],
        config=get_runtime_config(lambda_val=0, gamma_val=0, frozen_hours=24, horizon_days=5),
        avg_by_resource={"Bulk": 1.0},
    )

    assert result.iloc[0]["SMOOTHED_DATE"] == need_date
    assert result.iloc[0]["MOVE_REASON"].startswith("⚠️")


def test_sv_22_horizon_parameter_changes_candidate_search():
    need_date = _future_date(10)
    near_1 = _future_date(9)
    near_2 = _future_date(8)
    far_valid = _future_date(7)
    soft_orders = pd.DataFrame([
        _make_order(need_date=need_date, qty_pallets=2, qty_cases=20, sku_id="SKU1"),
        _make_order(need_date=need_date, qty_pallets=4, qty_cases=40, sku_id="SKU2"),
    ])
    hard_orders = pd.DataFrame(columns=soft_orders.columns)
    dc_cap_lookup = {
        (need_date, "Bulk"): 3,
        (near_1, "Bulk"): 1,
        (near_2, "Bulk"): 1,
        (far_valid, "Bulk"): 10,
    }

    short_horizon = smooth(
        soft_orders,
        hard_orders,
        dc_cap_lookup,
        {"SKU1": _future_date(1)},
        {"SKU1": 0},
        {"STORE1": "Mon,Tue,Wed,Thu,Fri,Sat,Sun"},
        {"STORE1": 999},
        [far_valid, near_2, near_1, need_date],
        config=get_runtime_config(lambda_val=0, gamma_val=0, frozen_hours=24, horizon_days=2),
        avg_by_resource={"Bulk": 1.0},
    )
    long_horizon = smooth(
        soft_orders,
        hard_orders,
        dc_cap_lookup,
        {"SKU1": _future_date(1)},
        {"SKU1": 0},
        {"STORE1": "Mon,Tue,Wed,Thu,Fri,Sat,Sun"},
        {"STORE1": 999},
        [far_valid, near_2, near_1, need_date],
        config=get_runtime_config(lambda_val=0, gamma_val=0, frozen_hours=24, horizon_days=3),
        avg_by_resource={"Bulk": 1.0},
    )

    assert short_horizon[short_horizon["SKU_ID"] == "SKU1"].iloc[0]["SMOOTHED_DATE"] == need_date
    assert long_horizon[long_horizon["SKU_ID"] == "SKU1"].iloc[0]["SMOOTHED_DATE"] == far_valid


def test_sv_24_lambda_penalizes_moves_into_hard_heavy_days():
    need_date = _future_date(10)
    candidate = _future_date(9)
    soft_orders = pd.DataFrame([
        _make_order(need_date=need_date, qty_pallets=2, qty_cases=20, sku_id="SKU1"),
        _make_order(need_date=need_date, qty_pallets=8, qty_cases=80, sku_id="SKU2"),
    ])
    hard_orders = pd.DataFrame([
        _make_order(need_date=candidate, qty_pallets=5, qty_cases=50, priority="HARD", sku_id="SKU9")
    ])
    dc_cap_lookup = {
        (need_date, "Bulk"): 2,
        (candidate, "Bulk"): 20,
    }
    shared_args = (
        soft_orders,
        hard_orders,
        dc_cap_lookup,
        {"SKU1": _future_date(1)},
        {"SKU1": 0},
        {"STORE1": "Mon,Tue,Wed,Thu,Fri,Sat,Sun"},
        {"STORE1": 999},
        [candidate, need_date],
    )

    low_lambda = smooth(
        *shared_args,
        config=get_runtime_config(lambda_val=0, gamma_val=0, frozen_hours=24, horizon_days=5),
        avg_by_resource={"Bulk": 1.0},
    )
    high_lambda = smooth(
        *shared_args,
        config=get_runtime_config(lambda_val=2000, gamma_val=0, frozen_hours=24, horizon_days=5),
        avg_by_resource={"Bulk": 1.0},
    )

    assert low_lambda[low_lambda["SKU_ID"] == "SKU1"].iloc[0]["SMOOTHED_DATE"] == candidate
    assert high_lambda[high_lambda["SKU_ID"] == "SKU1"].iloc[0]["SMOOTHED_DATE"] == need_date


def test_sv_25_gamma_penalizes_large_pull_forwards():
    need_date = _future_date(10)
    far_candidate = _future_date(7)
    soft_orders = pd.DataFrame([
        _make_order(need_date=need_date, qty_pallets=2, qty_cases=20, sku_id="SKU1"),
        _make_order(need_date=need_date, qty_pallets=4, qty_cases=40, sku_id="SKU2"),
    ])
    hard_orders = pd.DataFrame(columns=soft_orders.columns)
    dc_cap_lookup = {
        (need_date, "Bulk"): 2,
        (far_candidate, "Bulk"): 10,
    }
    shared_args = (
        soft_orders,
        hard_orders,
        dc_cap_lookup,
        {"SKU1": _future_date(1)},
        {"SKU1": 0},
        {"STORE1": "Mon,Tue,Wed,Thu,Fri,Sat,Sun"},
        {"STORE1": 999},
        [far_candidate, need_date],
    )

    low_gamma = smooth(
        *shared_args,
        config=get_runtime_config(lambda_val=0, gamma_val=0, frozen_hours=24, horizon_days=5),
        avg_by_resource={"Bulk": 1.0},
    )
    high_gamma = smooth(
        *shared_args,
        config=get_runtime_config(lambda_val=0, gamma_val=5, frozen_hours=24, horizon_days=5),
        avg_by_resource={"Bulk": 1.0},
    )

    assert low_gamma[low_gamma["SKU_ID"] == "SKU1"].iloc[0]["SMOOTHED_DATE"] == far_candidate
    assert high_gamma[high_gamma["SKU_ID"] == "SKU1"].iloc[0]["SMOOTHED_DATE"] == need_date


def test_sv_03_soft_orders_pulled_forward_only():
    result = solve(db_path=DB_PATH)
    soft_orders = result["plan"][result["plan"]["PRIORITY"] == "SOFT"]

    assert (soft_orders["SMOOTHED_DATE"] <= soft_orders["NEED_DATE"]).all()


def test_sv_12_store_calendar_blocks_invalid_delivery_day():
    need_date = _future_date(10)
    candidate = _future_date(8)
    soft_orders = pd.DataFrame([
        _make_order(need_date=need_date, qty_pallets=2, qty_cases=20, sku_id="SKU1"),
        _make_order(need_date=need_date, qty_pallets=4, qty_cases=40, sku_id="SKU2"),
    ])
    hard_orders = pd.DataFrame(columns=soft_orders.columns)

    result = smooth(
        soft_orders,
        hard_orders,
        {(need_date, "Bulk"): 3, (candidate, "Bulk"): 10},
        {"SKU1": _future_date(1)},
        {"SKU1": 0},
        {"STORE1": "Mon"},
        {"STORE1": 999},
        [candidate, need_date],
        config=get_runtime_config(lambda_val=0, gamma_val=0, frozen_hours=24, horizon_days=5),
        avg_by_resource={"Bulk": 1.0},
    )

    assert result[result["SKU_ID"] == "SKU1"].iloc[0]["SMOOTHED_DATE"] == need_date


def test_sv_13_backroom_capacity_blocks_move():
    need_date = _future_date(10)
    candidate = _future_date(8)
    soft_orders = pd.DataFrame([
        _make_order(need_date=need_date, qty_pallets=3, qty_cases=30, sku_id="SKU1"),
        _make_order(need_date=candidate, qty_pallets=8, qty_cases=80, sku_id="SKU9"),
        _make_order(need_date=need_date, qty_pallets=6, qty_cases=60, sku_id="SKU2"),
    ])
    hard_orders = pd.DataFrame(columns=soft_orders.columns)

    result = smooth(
        soft_orders,
        hard_orders,
        {(need_date, "Bulk"): 8, (candidate, "Bulk"): 20},
        {"SKU1": _future_date(1)},
        {"SKU1": 0},
        {"STORE1": "Mon,Tue,Wed,Thu,Fri,Sat,Sun"},
        {"STORE1": 10},
        [candidate, need_date],
        config=get_runtime_config(lambda_val=0, gamma_val=0, frozen_hours=24, horizon_days=5),
        avg_by_resource={"Bulk": 1.0},
    )

    assert result[result["SKU_ID"] == "SKU1"].iloc[0]["SMOOTHED_DATE"] == need_date


def test_sv_14_shelf_life_blocks_excessive_pull_forward():
    need_date = _future_date(10)
    candidate = _future_date(5)
    soft_orders = pd.DataFrame([
        _make_order(need_date=need_date, qty_pallets=2, qty_cases=20, sku_id="SKU1", shelf_life=8),
        _make_order(need_date=need_date, qty_pallets=6, qty_cases=60, sku_id="SKU2"),
    ])
    hard_orders = pd.DataFrame(columns=soft_orders.columns)

    result = smooth(
        soft_orders,
        hard_orders,
        {(need_date, "Bulk"): 4, (candidate, "Bulk"): 20},
        {"SKU1": _future_date(1)},
        {"SKU1": 0},
        {"STORE1": "Mon,Tue,Wed,Thu,Fri,Sat,Sun"},
        {"STORE1": 999},
        [candidate, need_date],
        config=get_runtime_config(lambda_val=0, gamma_val=0, frozen_hours=24, horizon_days=7),
        avg_by_resource={"Bulk": 1.0},
    )

    assert result[result["SKU_ID"] == "SKU1"].iloc[0]["SMOOTHED_DATE"] == need_date


def test_sv_17_alert_count_matches_plan_rows():
    result = solve(db_path=DB_PATH)
    plan = result["plan"]
    kpis = result["kpis"]

    alert_rows = plan[
        (plan["PRIORITY"] == "SOFT") &
        (plan["MOVE_REASON"].str.startswith("⚠️", na=False))
    ]
    assert kpis["n_alerts"] == len(alert_rows)


def test_sv_20_shifted_count_matches_plan_rows():
    result = solve(db_path=DB_PATH)
    plan = result["plan"]
    kpis = result["kpis"]

    shifted_rows = plan[
        (plan["PRIORITY"] == "SOFT") &
        (plan["SHIFT_DAYS"] > 0)
    ]
    assert kpis["n_moved"] == len(shifted_rows)


def test_sv_07_plan_written_to_sqlite():
    result = solve(db_path=DB_PATH)
    plan = result["plan"]

    with sqlite3.connect(DB_PATH) as conn:
        smoothed = pd.read_sql("SELECT * FROM smoothed_plan", conn)

    assert len(smoothed) == len(plan)
    assert {"ORDER_ID", "SMOOTHED_DATE", "MOVE_REASON"}.issubset(smoothed.columns)


def test_dl_01_valid_csv_accepted():
    csv_bytes = (
        "order_id,sku_id,dest_loc,need_date,qty_cases,resource_type\n"
        "ORD1,SKU1,STORE1,2026-03-05,12,Bulk\n"
    ).encode("utf-8")
    df, errors = load_csv("demand", csv_bytes)

    assert errors == []
    assert df is not None
    assert list(df.columns) == ["ORDER_ID", "SKU_ID", "DEST_LOC", "NEED_DATE", "QTY_CASES", "RESOURCE_TYPE", "PRIORITY"]
    assert df.iloc[0]["PRIORITY"] == "SOFT"


def test_dl_03_invalid_date_format_rejected():
    df = pd.DataFrame({
        "ORDER_ID": ["O1"],
        "SKU_ID": ["S1"],
        "DEST_LOC": ["L1"],
        "NEED_DATE": ["03/05/2026"],
        "QTY_CASES": [10],
        "RESOURCE_TYPE": ["Bulk"],
    })
    errors = validate("demand", df)

    assert any("YYYY-MM-DD" in e for e in errors)


def test_dl_06_column_name_normalisation():
    df = pd.DataFrame(columns=[" sku_id ", " need_date ", "qty_cases"])
    normalised = normalise_columns(df)

    assert list(normalised.columns) == ["SKU_ID", "NEED_DATE", "QTY_CASES"]


def test_dl_10_template_has_expected_columns():
    csv_text = get_sample_csv("store_master")
    df = pd.read_csv(pd.io.common.StringIO(csv_text))

    assert set(df.columns) == {"STORE_ID", "DELIVERY_CALENDAR", "BACKROOM_CAP"}
    assert len(df) == 1


def test_dl_12_write_to_db_replaces_existing_tables(tmp_path):
    db_path = tmp_path / "loader_test.db"
    first_tables = {
        "demand": pd.DataFrame([{"ORDER_ID": "A1"}]),
    }
    second_tables = {
        "demand": pd.DataFrame([{"ORDER_ID": "B1"}, {"ORDER_ID": "B2"}]),
    }

    write_to_db(first_tables, db_path=str(db_path))
    write_to_db(second_tables, db_path=str(db_path))

    with sqlite3.connect(db_path) as conn:
        rows = pd.read_sql("SELECT * FROM demand", conn)

    assert list(rows["ORDER_ID"]) == ["B1", "B2"]


def test_dl_14_and_dl_16_export_helpers_return_expected_payloads():
    plan = pd.DataFrame([
        {"ORDER_ID": "O1", "SMOOTHED_DATE": "2026-03-05", "QTY_PALLETS": 3}
    ])
    kpis = {"cv_before": 0.5, "cv_after": 0.3}

    csv_bytes = plan_to_csv(plan)
    json_bytes = plan_to_json(plan)
    kpi_bytes = kpis_to_json(kpis)

    assert b"ORDER_ID,SMOOTHED_DATE,QTY_PALLETS" in csv_bytes
    assert b'"ORDER_ID":"O1"' in json_bytes or b'"ORDER_ID": "O1"' in json_bytes
    assert b'"cv_before": 0.5' in kpi_bytes


def test_sv_05_osa_target_maintained():
    result = solve(db_path=DB_PATH)
    assert result["kpis"]["osa_pct"] >= 98.5


def test_sv_08_unit_conversion():
    demand = pd.DataFrame([
        {"ORDER_ID": "O1", "SKU_ID": "SKU1", "QTY_CASES": 10},
        {"ORDER_ID": "O2", "SKU_ID": "SKU1", "QTY_CASES": 12},
        {"ORDER_ID": "O3", "SKU_ID": "SKU2", "QTY_CASES": 5},
    ])
    sku_master = pd.DataFrame([
        {"SKU_ID": "SKU1", "UOM_CONV": 10, "SHELF_LIFE": 30, "CASE_CUBE": 1.5},
        {"SKU_ID": "SKU2", "UOM_CONV": 2, "SHELF_LIFE": 30, "CASE_CUBE": 1.0},
    ])
    
    result = convert_units(demand, sku_master)
    
    assert result.loc[result["ORDER_ID"] == "O1", "QTY_PALLETS"].iloc[0] == 1.0
    assert result.loc[result["ORDER_ID"] == "O2", "QTY_PALLETS"].iloc[0] == 2.0  # ceil(12/10)
    assert result.loc[result["ORDER_ID"] == "O3", "QTY_PALLETS"].iloc[0] == 3.0  # ceil(5/2)
