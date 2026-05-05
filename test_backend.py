import sqlite3
import pandas as pd
import numpy as np
from datetime import date, timedelta
import os

from data_gen import generate
from solver import solve, smooth, get_runtime_config, convert_units
from data_loader import (
    TABLE_SCHEMAS,
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


def test_ec_01_empty_demand_handled(tmp_path):
    db_path = str(tmp_path / "empty_demand.db")
    generate(seed=42, db_path=db_path)
    
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM demand")
        
    result = solve(db_path=db_path)
    
    assert len(result["plan"]) == 0
    assert result["kpis"]["n_moved"] == 0


def test_ec_03_all_hard_orders_no_shift(tmp_path):
    db_path = str(tmp_path / "all_hard.db")
    generate(seed=42, db_path=db_path)
    
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE demand SET PRIORITY = 'HARD'")
        
    result = solve(db_path=db_path)
    
    assert result["kpis"]["n_moved"] == 0
    assert result["kpis"]["cv_before"] == result["kpis"]["cv_after"]


def test_sv_21_cube_utilisation_range():
    result = solve(db_path=DB_PATH)
    kpis = result["kpis"]
    
    assert 0 <= kpis["cube_util_before"] <= 200
    assert 0 <= kpis["cube_util_after"] <= 200


def test_dl_08_corrupt_file_rejected():
    corrupt_bytes = b"\x00\x01\x02\x03\x04\x05\xff\xff"
    df, errors = load_csv("demand", corrupt_bytes)

    assert df is None
    assert len(errors) > 0
    assert any("Could not parse CSV" in e for e in errors)


# ── Multi-DC tests ─────────────────────────────────────────────────────────────

def test_dg_05_two_dcs_in_capacity():
    with sqlite3.connect(DB_PATH) as conn:
        dc_ids = pd.read_sql("SELECT DISTINCT DC_ID FROM dc_capacity", conn)["DC_ID"].tolist()
    assert "DC001" in dc_ids
    assert "DC002" in dc_ids


def test_dg_06_demand_has_dc_id_column():
    with sqlite3.connect(DB_PATH) as conn:
        cols = pd.read_sql("SELECT * FROM demand LIMIT 1", conn).columns.tolist()
    assert "DC_ID" in cols


def test_sv_31_smoothed_dc_in_plan():
    result = solve(db_path=DB_PATH)
    assert "SMOOTHED_DC" in result["plan"].columns
    # Every row must have a valid DC assignment
    assert result["plan"]["SMOOTHED_DC"].notna().all()


def test_sv_32_n_rerouted_in_kpis():
    result = solve(db_path=DB_PATH)
    assert "n_rerouted" in result["kpis"]
    assert result["kpis"]["n_rerouted"] >= 0


# ── Guardrail boundary-condition tests ────────────────────────────────────────

def test_guardrail_is_frozen_exact_boundary():
    """Date exactly equal to frozen_date must be treated as frozen (≤ check)."""
    from solver import is_frozen
    today = date.today()
    frozen_dt = today + timedelta(days=2)
    assert is_frozen(frozen_dt.isoformat(), frozen_dt) is True
    assert is_frozen((frozen_dt + timedelta(days=1)).isoformat(), frozen_dt) is False


def test_guardrail_inventory_ok_exact_on_hand_match():
    """on_hand exactly equal to qty_cases should be allowed (>= check)."""
    from solver import inventory_ok
    assert inventory_ok("SKU1", _future_date(5), 10.0, {"SKU1": _future_date(20)}, {"SKU1": 10}) is True


def test_guardrail_inventory_ok_asn_exact_day_match():
    """ASN arriving on the ship date itself should be allowed (>= check)."""
    from solver import inventory_ok
    ship = _future_date(5)
    assert inventory_ok("SKU1", ship, 50.0, {"SKU1": ship}, {"SKU1": 0}) is True


def test_guardrail_shelf_life_ok_very_short_shelf():
    """shelf_life < 4: max(shelf_life // 4, 1) = 1, so only 1 day pull-forward allowed."""
    from solver import shelf_life_ok
    need = _future_date(10)
    one_day_early = _future_date(9)
    two_days_early = _future_date(8)
    # Exactly 1 day early — at the boundary, must be OK
    assert shelf_life_ok(one_day_early, need, shelf_life_days=3) is True
    # 2 days early — exceeds max(3//4, 1)=1, must be blocked
    assert shelf_life_ok(two_days_early, need, shelf_life_days=3) is False


def test_guardrail_shelf_life_ok_exact_boundary():
    """Pull-forward exactly at the allowed limit should pass; one day more should fail."""
    from solver import shelf_life_ok
    need = _future_date(20)
    # shelf_life=40 → max(40//4,1)=10 days allowed
    allowed = _future_date(10)   # 10 days early — at boundary
    blocked  = _future_date(9)   # 11 days early — one over
    assert shelf_life_ok(allowed, need, shelf_life_days=40) is True
    assert shelf_life_ok(blocked, need, shelf_life_days=40) is False


# ── KPI math verification ─────────────────────────────────────────────────────

def test_sv_36_cv_formula_correctness():
    """compute_kpis CV should equal std/mean on the daily pallet series."""
    from solver import compute_kpis
    # Craft a plan with known daily volumes: day A=10, day B=30 → mean=20, std≈14.14, CV≈0.707
    day_a = _future_date(5)
    day_b = _future_date(6)
    demand = pd.DataFrame([
        {"ORDER_ID": "O1", "NEED_DATE": day_a, "QTY_PALLETS": 10, "PRIORITY": "SOFT", "RESOURCE_TYPE": "Bulk"},
        {"ORDER_ID": "O2", "NEED_DATE": day_b, "QTY_PALLETS": 30, "PRIORITY": "SOFT", "RESOURCE_TYPE": "Bulk"},
    ])
    plan = demand.copy()
    plan["SMOOTHED_DATE"] = plan["NEED_DATE"]
    plan["SHIFT_DAYS"] = 0
    plan["MOVE_REASON"] = "No change needed"
    dc_cap = {(day_a, "Bulk"): 500, (day_b, "Bulk"): 500}
    kpis = compute_kpis(demand, plan, dc_cap)

    vols = pd.Series([10.0, 30.0])
    expected_cv = vols.std() / vols.mean()
    assert abs(kpis["cv_before"] - expected_cv) < 0.001


def test_sv_37_osa_zero_when_hard_orders_late():
    """OSA must be 0% when all HARD orders are scheduled after their need date."""
    from solver import compute_kpis
    day_need = _future_date(5)
    day_late  = _future_date(6)   # 1 day after need_date
    demand = pd.DataFrame([
        {"ORDER_ID": "O1", "NEED_DATE": day_need, "QTY_PALLETS": 5, "PRIORITY": "HARD", "RESOURCE_TYPE": "Bulk"},
    ])
    plan = demand.copy()
    plan["SMOOTHED_DATE"] = day_late   # late!
    plan["SHIFT_DAYS"] = -1
    plan["MOVE_REASON"] = "Locked (HARD priority)"
    kpis = compute_kpis(demand, plan, {})
    assert kpis["osa_pct"] == 0.0


# ── Edge case integration tests ────────────────────────────────────────────────

def test_ec_04_all_soft_orders(tmp_path):
    """Solver runs cleanly when all demand is SOFT (no HARD orders)."""
    db = str(tmp_path / "all_soft.db")
    generate(seed=42, db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE demand SET PRIORITY = 'SOFT'")
    result = solve(db_path=db)
    plan = result["plan"]
    assert len(plan[plan["PRIORITY"] == "HARD"]) == 0
    assert result["kpis"]["osa_pct"] == 100.0   # no HARD orders → perfect OSA


def test_ec_05_zero_dc_capacity_all_alerts(tmp_path):
    """When all DC capacity is 0, every SOFT order becomes a capacity alert."""
    db = str(tmp_path / "zero_cap.db")
    generate(seed=42, db_path=db)
    with sqlite3.connect(db) as conn:
        # Synthetic data uses past dates; move all need_dates into the future
        # so orders are outside the frozen zone and smoothing is actually attempted
        conn.execute(f"UPDATE demand SET NEED_DATE = '{_future_date(15)}'")
        conn.execute("UPDATE dc_capacity SET MAX_THRU = 0")
    result = solve(db_path=db, frozen_hours=24)
    kpis = result["kpis"]
    assert kpis["n_alerts"] == kpis["n_soft"]
    assert kpis["n_moved"] == 0


def test_ec_09_all_orders_same_date(tmp_path):
    """Max spike: all SOFT orders share one NEED_DATE. Solver must not crash."""
    db = str(tmp_path / "single_date.db")
    generate(seed=42, db_path=db)
    with sqlite3.connect(db) as conn:
        spike_date = (date.today() + timedelta(days=15)).isoformat()
        conn.execute(f"UPDATE demand SET NEED_DATE = '{spike_date}'")
    result = solve(db_path=db, frozen_hours=24, horizon_days=10)
    assert not result["plan"].empty
    assert result["kpis"]["n_orders_total"] > 0


def test_sv_23_larger_frozen_zone_reduces_movable_orders():
    """Increasing FROZEN_HOURS should lock more orders (fewer moved or equal)."""
    result_narrow = solve(db_path=DB_PATH, frozen_hours=24, horizon_days=10)
    result_wide   = solve(db_path=DB_PATH, frozen_hours=96, horizon_days=10)
    # A wider frozen zone can only reduce or hold the moved count, never increase it
    assert result_wide["kpis"]["n_moved"] <= result_narrow["kpis"]["n_moved"]


def test_sv_34_backward_compat_no_dc_id_column(tmp_path):
    """solve() must handle a legacy DB where demand/dc_capacity have no DC_ID column."""
    db = str(tmp_path / "legacy.db")
    generate(seed=42, db_path=db)
    with sqlite3.connect(db) as conn:
        # Drop DC_ID from both tables to simulate pre-multi-DC database
        conn.execute("CREATE TABLE demand_legacy AS SELECT ORDER_ID,SKU_ID,DEST_LOC,NEED_DATE,PRIORITY,QTY_CASES,RESOURCE_TYPE FROM demand")
        conn.execute("DROP TABLE demand")
        conn.execute("ALTER TABLE demand_legacy RENAME TO demand")
        conn.execute("CREATE TABLE dc_cap_legacy AS SELECT RESOURCE_ID,MAX_THRU,OP_DATE FROM dc_capacity WHERE DC_ID='DC001'")
        conn.execute("DROP TABLE dc_capacity")
        conn.execute("ALTER TABLE dc_cap_legacy RENAME TO dc_capacity")
    result = solve(db_path=db)
    assert not result["plan"].empty
    assert "SMOOTHED_DC" in result["plan"].columns


# ── Cross-DC reroute integration test ─────────────────────────────────────────

def test_sv_33_cross_dc_reroute_fires_when_primary_at_capacity(tmp_path):
    """
    Phase 2 rerouting: a SOFT order assigned to DC001 (zero capacity everywhere)
    must be rerouted to DC002 (ample capacity) after Phase 1 raises a capacity alert.
    """
    db = str(tmp_path / "reroute.db")
    target_date = _future_date(10)
    dates = [_future_date(d) for d in range(2, 15)]

    sku = pd.DataFrame([{
        "SKU_ID": "SKU1", "CATEGORY": "Grocery", "RESOURCE_TYPE": "Bulk",
        "SHELF_LIFE": 30, "CASE_CUBE": 1.0, "UOM_CONV": 10.0,
    }])
    store = pd.DataFrame([{
        "STORE_ID": "STORE1", "BACKROOM_CAP": 999,
        "DELIVERY_CALENDAR": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
    }])
    inv = pd.DataFrame([{"SKU_ID": "SKU1", "ON_HAND_AVAIL": 9999, "ASN_ETA": _future_date(1)}])
    cap_rows = []
    for d in dates:
        cap_rows.append({"DC_ID": "DC001", "RESOURCE_ID": "Bulk", "MAX_THRU": 0,   "OP_DATE": d})
        cap_rows.append({"DC_ID": "DC002", "RESOURCE_ID": "Bulk", "MAX_THRU": 500, "OP_DATE": d})
    dc_cap = pd.DataFrame(cap_rows)
    demand = pd.DataFrame([{
        "ORDER_ID": "ORD001", "SKU_ID": "SKU1", "DEST_LOC": "STORE1",
        "NEED_DATE": target_date, "PRIORITY": "SOFT",
        "QTY_CASES": 10, "RESOURCE_TYPE": "Bulk", "DC_ID": "DC001",
    }])

    with sqlite3.connect(db) as conn:
        sku.to_sql("sku_master", conn, if_exists="replace", index=False)
        store.to_sql("store_master", conn, if_exists="replace", index=False)
        inv.to_sql("inventory", conn, if_exists="replace", index=False)
        dc_cap.to_sql("dc_capacity", conn, if_exists="replace", index=False)
        demand.to_sql("demand", conn, if_exists="replace", index=False)

    result = solve(db_path=db, frozen_hours=24, horizon_days=10)
    kpis = result["kpis"]
    plan  = result["plan"]

    assert kpis["n_rerouted"] == 1, "Expected exactly 1 cross-DC reroute"
    rerouted = plan[plan["MOVE_REASON"].str.startswith("Cross-DC")]
    assert len(rerouted) == 1
    assert rerouted.iloc[0]["SMOOTHED_DC"] == "DC002"
    assert rerouted.iloc[0]["DC_ID"] == "DC001"


# ── Data generation integrity tests ───────────────────────────────────────────

def test_dg_07_all_three_resource_types_in_demand():
    with sqlite3.connect(DB_PATH) as conn:
        resources = pd.read_sql("SELECT DISTINCT RESOURCE_TYPE FROM demand", conn)["RESOURCE_TYPE"].tolist()
    assert set(resources) == {"Conveyable", "NonConveyable", "Bulk"}


def test_dg_08_dc_capacity_row_count():
    """2 DCs × 30 days × 3 resources = 180 rows."""
    with sqlite3.connect(DB_PATH) as conn:
        n = pd.read_sql("SELECT COUNT(*) AS n FROM dc_capacity", conn)["n"].iloc[0]
    assert n == 180


def test_dg_09_delayed_asns_exist():
    """At least some SKUs (≈15%) should have ASN arriving after day 5."""
    from data_gen import START_DATE
    threshold = (START_DATE + timedelta(days=5)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        delayed = pd.read_sql(
            f"SELECT COUNT(*) AS n FROM inventory WHERE ASN_ETA > '{threshold}'", conn
        )["n"].iloc[0]
    assert delayed >= 1, "Expected at least 1 SKU with delayed ASN"


# ── Data loader gap tests ──────────────────────────────────────────────────────

def test_dl_05_extra_columns_preserved():
    """Columns beyond the schema should be kept in the returned DataFrame."""
    csv_bytes = (
        "order_id,sku_id,dest_loc,need_date,qty_cases,resource_type,custom_flag\n"
        "ORD1,SKU1,STORE1,2026-03-05,12,Bulk,YES\n"
    ).encode("utf-8")
    df, errors = load_csv("demand", csv_bytes)
    assert errors == []
    assert "CUSTOM_FLAG" in df.columns
    assert df.iloc[0]["CUSTOM_FLAG"] == "YES"


def test_dl_07_empty_csv_accepted():
    """A CSV with headers only (0 data rows) should be accepted and return empty DataFrame."""
    csv_bytes = "order_id,sku_id,dest_loc,need_date,qty_cases,resource_type\n".encode("utf-8")
    df, errors = load_csv("demand", csv_bytes)
    assert errors == []
    assert df is not None
    assert len(df) == 0


def test_dl_11_template_has_exactly_one_sample_row():
    """get_sample_csv should return a CSV with exactly 1 data row for each table."""
    for table in TABLE_SCHEMAS:
        csv_text = get_sample_csv(table)
        df = pd.read_csv(pd.io.common.StringIO(csv_text))
        assert len(df) == 1, f"Expected 1 sample row for {table}, got {len(df)}"
