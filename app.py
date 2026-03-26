"""
LevelSet — DC Outbound Smoothing Dashboard
===========================================
Streamlit UI: run the solver, visualise before/after outbound volume,
KPI scorecards, schedule table, and AI planner insight.

Author: Mohith Kunta (https://github.com/m-kunta)
"""

import os

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from solver import solve, load_data, DB_PATH
from data_loader import (
    TABLE_SCHEMAS, load_csv, write_to_db, get_sample_csv,
    plan_to_csv, plan_to_json, kpis_to_json
)

load_dotenv()

# ── Page Config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="LevelSet — DC Outbound Smoothing",
    page_icon="📦",
    layout="wide",
)

# ── LLM Provider Setup ────────────────────────────────────────────────────────
try:
    from llm_providers import get_llm_response, PROVIDER_DEFAULTS, AUTH_ERROR_PREFIX
    AI_AVAILABLE = True
except ImportError:
    AI_AVAILABLE = False
    AUTH_ERROR_PREFIX = "__AUTH_ERROR__"

# ── Header ────────────────────────────────────────────────────────────────────
st.title("📦 LevelSet — DC Outbound Smoothing")
st.markdown(
    "Constrained replenishment planning that **level-loads** store orders across the outbound "
    "horizon — fewer overtime spikes, same store service levels."
)

with st.expander("📖 How to Use LevelSet — Step-by-Step Guide", expanded=False):
    st.markdown("""
**LevelSet** takes your store replenishment orders and shifts eligible orders into lighter days
before the need date — flattening the outbound wave without compromising store service levels.

---

**Step 1 — Understand Your Data**

The solver works with synthetic data (generated automatically on first run). It simulates:
- **2,100+ replenishment order lines** across 50 SKUs, 8 stores, and 3 resource types (Conveyable, Non-Conveyable, Bulk)
- **Intentionally spiky demand** — 60% of volume lands Mon/Tue/Wed, mimicking real-world wave patterns
- **20% HARD orders** (safety stock breaches, promo launches) that are never moved
- **80% SOFT orders** (routine fills, inventory builds) that are eligible for smoothing

---

**Step 2 — Configure the Solver (Sidebar)**

| Setting | What it controls |
|---|---|
| **Look-ahead Horizon** | How many days back from the need date the solver searches for trough capacity. Wider = more flexibility, but orders arrive earlier. |
| **Frozen Zone** | Orders shipping within this window (e.g. next 48h) are locked — the warehouse is already in motion. |
| **λ OSA Penalty** | How aggressively to protect HARD orders. Set higher if service levels are the top priority. |
| **γ Early Ship Penalty** | How much to penalise pulling orders forward unnecessarily. Set higher if store backroom space is tight. |

---

**Step 3 — Run the Solver**

Click **▶️ Run Smoothing Solver** in the sidebar. The engine will:
1. Classify orders as HARD or SOFT
2. Convert case quantities → pallets for capacity comparison
3. Check DC outbound capacity on each need date
4. For over-capacity days, search backward for a trough day that passes all guardrails:
   - DC has headroom for this resource type
   - Inventory is on-hand or ASN arrives before the proposed ship date
   - Store has this day on their delivery calendar
   - Moving the order doesn't bust the store's backroom capacity
   - Shelf-life (MRSL) window is still met
5. Pull-forward eligible orders, or flag a **Capacity Alert** if no window exists

---

**Step 4 — Read the Results**

- **Outbound CV** — Coefficient of Variation of daily pallet volume. *Lower = flatter plan.* Target: < 0.15
- **OSA — HARD Orders** — % of HARD orders scheduled on or before their need date. Must stay ≥ 98.5%
- **Orders Shifted** — Count of SOFT orders successfully pulled forward
- **Capacity Alerts** — Orders with no valid window; these need manual planner review
- **Before/After Chart** — Red bars = unconstrained plan (spiky). Green bars = smoothed plan (flatter)
- **Schedule Table** — Green rows = orders that were moved. Filter by Priority, Resource, or moved-only to investigate

---

**Step 5 — Generate an AI Planner Insight** *(optional)*

Set your `GEMINI_API_KEY` in a `.env` file in this project folder (copy `.env.example` as a template),
then click **✨ Generate Planner Insight** at the bottom of the page. The LLM will summarise what
the solver achieved, flag any risks from the capacity alerts, and recommend specific next actions
(e.g. adjust λ, expand the horizon, or manually review specific SKU/store combinations).
""")

# ── Sidebar ───────────────────────────────────────────────────────────────────
st.sidebar.header("⚙️ Solver Settings")

horizon = st.sidebar.slider(
    "Look-ahead Horizon (days)", min_value=5, max_value=14, value=10, step=1,
    help="How many days back from the need date the solver will search for trough capacity."
)
frozen_hours = st.sidebar.slider(
    "Frozen Zone (hours)", min_value=24, max_value=96, value=48, step=24,
    help="Ship dates within this window are locked — warehouse is already in motion."
)
lambda_val = st.sidebar.number_input(
    "λ OSA Penalty Weight", min_value=1, max_value=500, value=100, step=10,
    help="Higher = more protection for HARD orders and OSA targets."
)
gamma_val = st.sidebar.number_input(
    "γ Early Ship Penalty", min_value=0, max_value=20, value=1, step=1,
    help="Higher = solver pulls forward less aggressively."
)

# Write params to environment for solver to pick up
os.environ["HORIZON_DAYS"] = str(horizon)
os.environ["FROZEN_HOURS"] = str(frozen_hours)
os.environ["LAMBDA"] = str(lambda_val)
os.environ["GAMMA"] = str(gamma_val)

st.sidebar.markdown("---")

# ── AI Provider Selector ──────────────────────────────────────────────────────
st.sidebar.subheader("🤖 AI Insight Provider")

if AI_AVAILABLE:
    provider_names = list(PROVIDER_DEFAULTS.keys())
    env_default = os.getenv("LLM_PROVIDER", "Gemini")
    default_idx = provider_names.index(env_default) if env_default in provider_names else 0
    selected_provider = st.sidebar.selectbox("Provider", provider_names, index=default_idx)
    default_model = PROVIDER_DEFAULTS[selected_provider]["model"]
    selected_model = st.sidebar.text_input("Model", value=default_model,
                                           help="Override the default model name if needed.")
    key_env = PROVIDER_DEFAULTS[selected_provider]["key_env"]

    if key_env:
        # Persist the entered key in session state so it survives reruns
        ss_key = f"api_key_{selected_provider}"
        existing = st.session_state.get(ss_key, os.getenv(key_env, ""))
        entered_key = st.sidebar.text_input(
            f"🔑 {selected_provider} API Key",
            value=existing,
            type="password",
            placeholder=f"Paste your {selected_provider} API key here…",
            help=f"Stored in memory only — not written to disk. Alternatively set `{key_env}` in your `.env` file.",
        )
        if entered_key:
            st.session_state[ss_key] = entered_key
            os.environ[key_env] = entered_key   # inject so llm_providers picks it up
            st.sidebar.info(f"🔑 {selected_provider} key entered — validity confirmed on first use")
        elif os.getenv(key_env):
            st.sidebar.info(f"🔑 `{key_env}` loaded from `.env` — validity confirmed on first use")
        else:
            st.sidebar.warning(f"⚠️ Enter a {selected_provider} API key above to enable AI features")
    else:
        st.sidebar.info("ℹ️ Ollama runs locally — no key needed")
else:
    selected_provider, selected_model = "Gemini", "gemini-2.5-flash"
    st.sidebar.info("Install `google-genai` (or another provider) to enable AI features.")


st.sidebar.markdown("---")

# ── Data Mode ─────────────────────────────────────────────────────────
st.sidebar.subheader("🗂️ Data Source")
data_mode = st.sidebar.radio(
    "Mode",
    ["Synthetic Data", "Upload Real Data"],
    help="Synthetic: auto-generated 30-day dataset. Real: upload your own CSVs."
)

if data_mode == "Upload Real Data":
    st.sidebar.markdown("Upload one CSV per table. Required columns are listed in the expander below.")

    uploaded = {}   # table_name -> validated DataFrame
    upload_errors = {}

    for table, schema in TABLE_SCHEMAS.items():
        with st.sidebar.expander(f"📄 {table}", expanded=False):
            req = sorted(schema["required"])
            opt = sorted(schema["optional"].keys())
            st.caption(schema["description"])
            st.caption(f"**Required:** {', '.join(req)}")
            if opt:
                st.caption(f"**Optional (defaults applied if missing):** {', '.join(opt)}")

            # Download sample CSV template
            sample_bytes = get_sample_csv(table).encode("utf-8")
            st.download_button(
                label="⬇️ Download Template",
                data=sample_bytes,
                file_name=f"{table}_template.csv",
                mime="text/csv",
                use_container_width=True,
            )

            # Upload field
            file = st.file_uploader(f"Upload {table}.csv", type="csv", key=f"upload_{table}")
            if file is not None:
                df, errors = load_csv(table, file.read())
                if errors:
                    upload_errors[table] = errors
                    for e in errors:
                        st.error(f"❌ {e}")
                else:
                    uploaded[table] = df
                    st.success(f"✅ {len(df)} rows loaded")

    # Load button — only active when all 5 tables are uploaded
    n_uploaded = len(uploaded)
    n_total = len(TABLE_SCHEMAS)
    if upload_errors:
        st.sidebar.error(f"❌ Fix {len(upload_errors)} table(s) with errors before loading.")
    elif n_uploaded < n_total:
        st.sidebar.info(f"⏳ {n_uploaded}/{n_total} tables uploaded — upload all to proceed.")
    else:
        if st.sidebar.button("🚀 Load Real Data & Solve", type="primary", use_container_width=True):
            with st.spinner("Writing data to database and running solver..."):
                write_to_db(uploaded)
                result = solve(
                    DB_PATH,
                    horizon_days=horizon,
                    frozen_hours=frozen_hours,
                    lambda_val=lambda_val,
                    gamma_val=gamma_val,
                )
            st.session_state["result"] = result
            st.session_state["data_mode"] = "real"
            st.success("✅ Real data loaded and solver complete!")
            st.rerun()

# ── Run Solver ────────────────────────────────────────────────────────────────
if st.sidebar.button("▶️ Run Smoothing Solver", type="primary", use_container_width=True):
    with st.spinner("Running LevelSet solver..."):
        result = solve(
            DB_PATH,
            horizon_days=horizon,
            frozen_hours=frozen_hours,
            lambda_val=lambda_val,
            gamma_val=gamma_val,
        )
    st.session_state["result"] = result
    st.success("✅ Solver complete!")

if st.sidebar.button("🔄 Regenerate Synthetic Data", use_container_width=True,
                     help="Rebuilds the SQLite database with fresh synthetic demand and re-runs the solver."):
    from data_gen import generate
    with st.spinner("Regenerating synthetic data..."):
        generate()
    # Clear cached result so solver re-runs on next render with fresh data
    st.session_state.pop("result", None)
    st.rerun()


st.sidebar.markdown("---")
st.sidebar.markdown(
    """
    <div style='text-align:center;color:#888;font-size:0.8rem;padding:0.5rem 0'>
        Built by <strong>Mohith Kunta</strong><br>
        <a href='https://github.com/m-kunta' target='_blank' style='color:#a78bfa;text-decoration:none'>
            🔗 github.com/m-kunta
        </a>
    </div>
    """,
    unsafe_allow_html=True,
)

# Load previous result or run on first load
if "result" not in st.session_state:
    with st.spinner("Running LevelSet solver..."):
        st.session_state["result"] = solve(
            DB_PATH,
            horizon_days=horizon,
            frozen_hours=frozen_hours,
            lambda_val=lambda_val,
            gamma_val=gamma_val,
        )

result = st.session_state["result"]
plan   = result["plan"]
kpis   = result["kpis"]

# ── KPI Scorecards ────────────────────────────────────────────────────────────
st.subheader("📊 KPI Summary")
c1, c2, c3, c4, c5 = st.columns(5)

c1.metric(
    "Outbound CV", f"{kpis['cv_after']:.3f}",
    delta=f"{kpis['cv_after'] - kpis['cv_before']:.3f}",
    delta_color="inverse",
    help="Coefficient of Variation of daily outbound pallets. Lower = flatter plan. Target < 0.15."
)
c2.metric(
    "OSA — HARD Orders", f"{kpis['osa_pct']:.1f}%",
    delta=None,
    help="% of HARD (safety-stock / promo) orders scheduled on or before their need date. Target ≥ 98.5%"
)
c3.metric(
    "Orders Shifted", f"{kpis['n_moved']}",
    delta=f"{kpis['n_soft']} eligible",
    delta_color="off",
    help="Number of SOFT orders that were pull-forwarded into trough days."
)
c4.metric(
    "Capacity Alerts", f"{kpis['n_alerts']}",
    delta=None,
    help="SOFT orders with no valid window found — require planner review."
)
c5.metric(
    "Cube Utilisation", f"{kpis['cube_util_after']:.1f}%",
    delta=f"{kpis['cube_util_after'] - kpis['cube_util_before']:.1f}%",
    delta_color="normal",
    help="Average trailer fill rate vs. DC capacity."
)

st.markdown("---")

# ── Before / After Volume Chart ───────────────────────────────────────────────
st.subheader("📅 Daily Outbound Volume — Before vs. After Smoothing")

before_agg = (
    plan.groupby("NEED_DATE")["QTY_PALLETS"].sum()
    .reset_index().rename(columns={"NEED_DATE": "Date", "QTY_PALLETS": "Pallets"})
)
before_agg["State"] = "Before (Unconstrained)"

after_agg = (
    plan.groupby("SMOOTHED_DATE")["QTY_PALLETS"].sum()
    .reset_index().rename(columns={"SMOOTHED_DATE": "Date", "QTY_PALLETS": "Pallets"})
)
after_agg["State"] = "After (Smoothed)"

chart_df = pd.concat([before_agg, after_agg])

chart = (
    alt.Chart(chart_df)
    .mark_bar(opacity=0.85)
    .encode(
        x=alt.X("Date:T", title="Operating Date", axis=alt.Axis(format="%b %d")),
        y=alt.Y("Pallets:Q", title="Total Pallets"),
        color=alt.Color(
            "State:N",
            scale=alt.Scale(
                domain=["Before (Unconstrained)", "After (Smoothed)"],
                range=["#ef4444", "#22c55e"],
            ),
            legend=alt.Legend(title="Plan"),
        ),
        xOffset="State:N",
        tooltip=["Date:T", "State:N", alt.Tooltip("Pallets:Q", format=".0f")],
    )
    .properties(height=350)
)
st.altair_chart(chart, use_container_width=True)

st.markdown("---")

# ── Resource Breakdown ────────────────────────────────────────────────────────
col_l, col_r = st.columns(2)

with col_l:
    st.subheader("🏷️ Demand by Resource Type")
    res_before = plan.groupby("RESOURCE_TYPE")["QTY_PALLETS"].sum().reset_index()
    res_before.columns = ["Resource", "Pallets"]
    st.bar_chart(res_before.set_index("Resource"))

with col_r:
    st.subheader("🚦 Orders by Move Status")
    status_counts = plan["MOVE_REASON"].value_counts().reset_index()
    status_counts.columns = ["Status", "Count"]
    st.dataframe(status_counts, width="stretch", hide_index=True)

st.markdown("---")

# ── Schedule Table ────────────────────────────────────────────────────────────
st.subheader("📋 Smoothed Ship Schedule")

# Filters
filter_col1, filter_col2, filter_col3 = st.columns(3)
with filter_col1:
    priority_filter = st.selectbox("Priority", ["All", "HARD", "SOFT"])
with filter_col2:
    resource_filter = st.selectbox("Resource", ["All"] + sorted(plan["RESOURCE_TYPE"].unique()))
with filter_col3:
    moved_only = st.checkbox("Show moved orders only", value=False)

display_df = plan.copy()
if priority_filter != "All":
    display_df = display_df[display_df["PRIORITY"] == priority_filter]
if resource_filter != "All":
    display_df = display_df[display_df["RESOURCE_TYPE"] == resource_filter]
if moved_only:
    display_df = display_df[display_df["SHIFT_DAYS"] > 0]

display_cols = ["ORDER_ID", "SKU_ID", "DEST_LOC", "PRIORITY", "RESOURCE_TYPE",
                "QTY_CASES", "QTY_PALLETS", "NEED_DATE", "SMOOTHED_DATE", "SHIFT_DAYS", "MOVE_REASON"]
display_df = display_df[display_cols].sort_values("SMOOTHED_DATE")


def _row_color(row: pd.Series) -> list[str]:
    if row["SHIFT_DAYS"] > 0:
        return ["background-color: #14532d22"] * len(row)
    elif "⚠️" in str(row.get("MOVE_REASON", "")):
        return ["background-color: #7f1d1d22"] * len(row)
    return [""] * len(row)


styled = display_df.style.apply(_row_color, axis=1)
st.dataframe(styled, width="stretch", hide_index=True, height=350)

st.markdown("---")

# ── AI Planner Insight ────────────────────────────────────────────────────────
st.subheader("🤖 AI Planner Insight")

if not AI_AVAILABLE:
    st.info("Install `google-genai` (or another provider package) and set an API key to enable AI insights.")
else:
    if st.button("✨ Generate Planner Insight", type="secondary"):
        prompt = f"""You are a DC Operations Planning Analyst reviewing a constrained replenishment plan.

SOLVER RESULTS:
- Before CV: {kpis['cv_before']} | After CV: {kpis['cv_after']}
- OSA (HARD orders on time): {kpis['osa_pct']}%
- SOFT orders shifted: {kpis['n_moved']} of {kpis['n_soft']}
- Capacity alerts (no window found): {kpis['n_alerts']}
- DC cube utilisation: {kpis['cube_util_before']}% → {kpis['cube_util_after']}%
- Horizon: {horizon} days | Frozen zone: {frozen_hours}h | λ={lambda_val} γ={gamma_val}

Write a professional 150-word "Situation → Actions" briefing for the planning team.
Cover: what the solver achieved, any risks from the {kpis['n_alerts']} alerts, and 2-3 specific recommended actions (e.g., adjust λ, expand horizon, review alerts manually).
Be direct and practical — avoid generic advice."""

        with st.spinner(f"Generating insight via {selected_provider}..."):
            insight = get_llm_response(prompt, selected_provider, selected_model)
        if insight.startswith(AUTH_ERROR_PREFIX):
            st.warning(
                f"🔑 **API Key Invalid or Revoked** — {insight.removeprefix(AUTH_ERROR_PREFIX)}\n\n"
                "Update your key in the sidebar or `.env` file, then try again."
            )
        else:
            st.info(f"**Planner Insight** *(via {selected_provider}/{selected_model})*\n\n{insight}")

st.markdown("---")

# ── Exception Review Panel ────────────────────────────────────────────────────
st.subheader("🚨 Exception Review — Planner Triage")
st.caption(
    "Exceptions are automatically classified and ranked by estimated business impact. "
    "Focus your review time on the highest-impact items first."
)

# ── Build exception records ───────────────────────────────────────────────────
from datetime import date as _date

exceptions = []

# Type 1: Capacity Alerts — no valid window was found for SOFT orders
alert_orders = plan[plan["MOVE_REASON"].str.startswith("⚠️", na=False)].copy()
for _, row in alert_orders.iterrows():
    days_until = max(
        (_date.fromisoformat(row["NEED_DATE"]) - _date.today()).days, 0
    )
    urgency = max(10 - days_until, 1)  # closer = higher urgency
    impact = round(float(row["QTY_PALLETS"]) * urgency * (2 if row["PRIORITY"] == "HARD" else 1), 1)
    exceptions.append({
        "Type": "🔴 Capacity Alert",
        "ORDER_ID": row["ORDER_ID"],
        "SKU_ID": row["SKU_ID"],
        "Store": row["DEST_LOC"],
        "Priority": row["PRIORITY"],
        "Resource": row["RESOURCE_TYPE"],
        "Pallets": row["QTY_PALLETS"],
        "Need Date": row["NEED_DATE"],
        "Days Out": days_until,
        "Impact Score": impact,
        "Action": "Manual re-schedule or capacity expansion required",
    })

# Type 2: Near-frozen HARD orders — HARD orders due within 72h
near_frozen = plan[
    (plan["PRIORITY"] == "HARD") &
    (plan["SMOOTHED_DATE"].apply(
        lambda d: (_date.fromisoformat(d) - _date.today()).days <= 3
    ))
].copy()
for _, row in near_frozen.iterrows():
    days_until = max((_date.fromisoformat(row["SMOOTHED_DATE"]) - _date.today()).days, 0)
    impact = round(float(row["QTY_PALLETS"]) * (4 - days_until) * 3, 1)
    exceptions.append({
        "Type": "🟠 Near-Frozen HARD",
        "ORDER_ID": row["ORDER_ID"],
        "SKU_ID": row["SKU_ID"],
        "Store": row["DEST_LOC"],
        "Priority": "HARD",
        "Resource": row["RESOURCE_TYPE"],
        "Pallets": row["QTY_PALLETS"],
        "Need Date": row["SMOOTHED_DATE"],
        "Days Out": days_until,
        "Impact Score": impact,
        "Action": "Confirm inventory readiness and DC labour — no further changes possible",
    })

# Type 3: Overloaded days — days where total pallets still exceed capacity post-smoothing
from solver import get_daily_capacity, load_data as _load_data
_data = _load_data()
dc_cap = get_daily_capacity(_data["dc_capacity"])
after_day_load = (
    plan.groupby(["SMOOTHED_DATE", "RESOURCE_TYPE"])["QTY_PALLETS"].sum()
)
for (d, res), pallets in after_day_load.items():
    cap = dc_cap.get((d, res), 0)
    if cap > 0 and pallets > cap:
        overflow = round(pallets - cap, 1)
        days_until = max((_date.fromisoformat(d) - _date.today()).days, 0)
        impact = round(overflow * max(10 - days_until, 1), 1)
        exceptions.append({
            "Type": "🟡 Day Over-Capacity",
            "ORDER_ID": "—",
            "SKU_ID": "—",
            "Store": "ALL",
            "Priority": "—",
            "Resource": res,
            "Pallets": overflow,
            "Need Date": d,
            "Days Out": days_until,
            "Impact Score": impact,
            "Action": f"Day {d} [{res}]: {overflow:.0f} pallets over cap. Add shift or defer low-priority SOFT orders.",
        })

# ── Render Exception Table ────────────────────────────────────────────────────
if exceptions:
    exc_df = (
        pd.DataFrame(exceptions)
        .sort_values("Impact Score", ascending=False)
        .reset_index(drop=True)
    )
    exc_df.index = exc_df.index + 1  # 1-based rank

    # Summary counts
    col_a, col_b, col_c = st.columns(3)
    col_a.metric("🔴 Capacity Alerts", (exc_df["Type"] == "🔴 Capacity Alert").sum())
    col_b.metric("🟠 Near-Frozen HARD", (exc_df["Type"] == "🟠 Near-Frozen HARD").sum())
    col_c.metric("🟡 Days Over-Capacity", (exc_df["Type"] == "🟡 Day Over-Capacity").sum())

    st.markdown("**Ranked by Impact Score** *(volume × urgency × priority weight)*")

    def _exc_color(row):
        t = str(row.get("Type", ""))
        if "🔴" in t:
            return ["background-color: rgba(239,68,68,0.15)"] * len(row)
        elif "🟠" in t:
            return ["background-color: rgba(249,115,22,0.15)"] * len(row)
        elif "🟡" in t:
            return ["background-color: rgba(234,179,8,0.12)"] * len(row)
        return [""] * len(row)

    display_exc = exc_df[["Type", "ORDER_ID", "SKU_ID", "Store", "Priority",
                           "Resource", "Pallets", "Need Date", "Days Out",
                           "Impact Score", "Action"]]
    st.dataframe(display_exc.style.apply(_exc_color, axis=1),
                 width="stretch", height=300)

    # ── AI Exception Triage ───────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("🤖 AI Exception Triage")
    st.caption("The AI will review your exceptions and produce a prioritised action brief ranked by business impact.")

    if not AI_AVAILABLE:
        st.info("Set an API key in `.env` to enable AI exception triage.")
    else:
        if st.button("🔍 Triage Exceptions with AI", type="primary"):
            top_exc = exc_df.head(10)
            exc_rows = "\n".join(
                f"  {i}. [{row['Type']}] {row['ORDER_ID']} | SKU {row['SKU_ID']} | "
                f"Store {row['Store']} | {row['Pallets']} pallets | Need: {row['Need Date']} "
                f"({row['Days Out']}d out) | Impact Score: {row['Impact Score']} | "
                f"Suggested Action: {row['Action']}"
                for i, row in top_exc.iterrows()
            )
            triage_prompt = f"""You are a DC Supply Chain Planning Manager conducting an exception review.

PLAN CONTEXT:
- Total orders: {kpis['n_orders_total']} | SOFT shifted: {kpis['n_moved']} | Alerts: {kpis['n_alerts']}
- CV Before: {kpis['cv_before']} → After: {kpis['cv_after']} | OSA: {kpis['osa_pct']}%

TOP EXCEPTIONS (ranked by impact score):
{exc_rows}

Write a prioritised planner action brief with these rules:
1. Group exceptions into 3 urgency buckets: IMMEDIATE (act today), MONITOR (act this week), WATCH (low risk)
2. For each exception, give ONE specific action (who does what, by when)
3. Call out any systemic patterns (e.g. "Store X has recurring backroom issues", "Frozen resource type is consistently over-cap")
4. End with a single headline metric: estimated pallets at risk if no action taken
Keep it under 250 words. Be direct — this is for an operations team, not an executive deck."""

            with st.spinner(f"Triaging exceptions via {selected_provider}..."):
                triage = get_llm_response(triage_prompt, selected_provider, selected_model)
            if triage.startswith(AUTH_ERROR_PREFIX):
                st.warning(
                    f"🔑 **API Key Invalid or Revoked** — {triage.removeprefix(AUTH_ERROR_PREFIX)}\n\n"
                    "Update your key in the sidebar or `.env` file, then try again."
                )
            else:
                st.success("**🔍 AI Exception Triage Brief**")
                st.markdown(triage)

else:
    st.success("✅ No exceptions flagged — the plan is clean. All SOFT orders were successfully smoothed and all HARD orders are protected.")




# ── Export Panel ──────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("⬇️ Export Results")
st.caption("Download the smoothed plan and KPIs for use in upstream systems, audits, or further analysis.")

exp_cols = st.columns(3)

with exp_cols[0]:
    st.download_button(
        label="📄 Smoothed Plan — CSV",
        data=plan_to_csv(plan),
        file_name="levelset_smoothed_plan.csv",
        mime="text/csv",
        use_container_width=True,
        help="Full order-level smoothed plan in flat CSV format. Ready for Excel, Power BI, or S&OP tools.",
    )

with exp_cols[1]:
    st.download_button(
        label="🗂️ Smoothed Plan — JSON",
        data=plan_to_json(plan),
        file_name="levelset_smoothed_plan.json",
        mime="application/json",
        use_container_width=True,
        help="Records-oriented JSON — ideal for REST API ingestion, WMS feeds, or BlueYonder / Kinaxis integration.",
    )

with exp_cols[2]:
    st.download_button(
        label="📊 KPI Summary — JSON",
        data=kpis_to_json(kpis),
        file_name="levelset_kpis.json",
        mime="application/json",
        use_container_width=True,
        help="Key metrics snapshot: CV before/after, OSA, shift counts, cube utilisation, and alerts.",
    )

# ── Footer ────────────────────────────────────────────────────────────────────

st.markdown("""
<div style='text-align:center;margin-top:3rem;padding:1.5rem;
            border-top:1px solid #333;color:#888;font-size:0.85rem;line-height:1.8'>
    📦 <strong>LevelSet — DC Outbound Smoothing</strong> &nbsp;|&nbsp; Constrained Replenishment Planning<br>
    Built by <strong>Mohith Kunta</strong> &nbsp;—&nbsp;
    <a href='https://github.com/m-kunta' target='_blank' style='color:#a78bfa;text-decoration:none'>
        github.com/m-kunta
    </a>
    &nbsp;|&nbsp; MIT License
</div>
""", unsafe_allow_html=True)
