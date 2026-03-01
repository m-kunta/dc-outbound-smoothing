# LevelSet — Test Plan

**Project:** DC Outbound Smoothing (LevelSet)  
**Author:** Mohith Kunta  
**Version:** 1.0  
**Date:** 2026-02-28  
**Status:** Ready for Execution

---

## 1. Scope

This plan covers functional, integration, edge-case, negative, and regression testing for all modules:

| Module | File | Responsibility |
|---|---|---|
| Synthetic Data Generator | `data_gen.py` | 30-day dataset across 5 SQLite tables |
| Solver Engine | `solver.py` | Classify, convert, smooth, guardrails, KPIs |
| Real Data Loader | `data_loader.py` | CSV validation, upload ingestion, export |
| Dashboard UI | `app.py` | Streamlit front-end, sidebar controls, charts |
| AI Layer | `llm_providers.py` | Multi-provider LLM integration |

---

## 2. Test Environment

| Item | Value |
|---|---|
| Python | 3.9+ (venv) |
| Database | SQLite (`levelset.db`) |
| Framework | Streamlit |
| Browser | Chrome / Safari (latest) |
| OS | macOS |

---

## 3. Feature Test Cases

### 3.1 Synthetic Data Generation (`data_gen.py`)

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| DG-01 | Generate all 5 tables | Run `python data_gen.py` | `levelset.db` created with tables: `sku_master`, `store_master`, `dc_capacity`, `inventory`, `demand` | P0 |
| DG-02 | Seed reproducibility | Run twice with same seed (42) | Identical row counts and data in all tables | P0 |
| DG-03 | Demand volume distribution | Query demand by weekday | ≥ 55% of order lines fall on Mon/Tue/Wed | P1 |
| DG-04 | HARD/SOFT ratio | Count orders by PRIORITY | ~20% HARD, ~80% SOFT (±5%) | P1 |
| DG-05 | Store count | Query distinct STORE_ID in demand | 8 stores, matching `store_master` | P1 |
| DG-06 | SKU count | Query distinct SKU_ID in demand | 50 SKUs, matching `sku_master` | P1 |
| DG-07 | Resource types present | Distinct RESOURCE_TYPE | Conveyable, NonConveyable, Bulk | P1 |
| DG-08 | DC capacity structure | Query dc_capacity table | 30 days × 3 resource types = 90 rows | P1 |
| DG-09 | Delayed ASNs exist | Count inventory where ASN_ETA > Start Date + 5 days | ~15% of SKUs have delayed ASN | P2 |
| DG-10 | Date range coverage | Min/max NEED_DATE in demand | Spans ≈30 days from today | P1 |
| DG-11 | Column naming convention | All column names | UPPER_SNAKE_CASE consistently | P2 |

---

### 3.2 Solver Engine (`solver.py`)

#### 3.2.1 Core Smoothing

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| SV-01 | Solver runs without error | Call `solve()` after data gen | Returns dict with `plan` DataFrame and `kpis` dict | P0 |
| SV-02 | HARD orders never moved | Compare NEED_DATE vs SMOOTHED_DATE for HARD | All HARD orders: SMOOTHED_DATE == NEED_DATE | P0 |
| SV-03 | SOFT orders pulled forward only | For shifted SOFT orders | SMOOTHED_DATE ≤ NEED_DATE (never pushed later) | P0 |
| SV-04 | CV improvement or hold | Compare cv_before vs cv_after | cv_after ≤ cv_before | P0 |
| SV-05 | OSA target maintained | Read kpis['osa_pct'] | ≥ 98.5% | P0 |
| SV-06 | All orders accounted for | Count plan rows vs demand rows | Equal row counts — no orders created or dropped | P0 |
| SV-07 | Plan written to SQLite | Query `smoothed_plan` table | Table exists with correct schema and row count | P1 |

#### 3.2.2 Unit Conversion

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| SV-08 | Cases to pallets conversion | Verify QTY_PALLETS = QTY_CASES / UOM_CONV | All rows have correct QTY_PALLETS | P0 |
| SV-09 | Missing UOM_CONV | SKU not in sku_master | Handled gracefully (default or error) | P1 |

#### 3.2.3 Guardrails

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| SV-10 | Frozen zone respected (REQ-04) | Set FROZEN_HOURS=48, check orders due within 48h | No orders within frozen window are moved | P0 |
| SV-11 | Inventory readiness (REQ-05) | Orders with delayed ASN | Not pulled forward to dates before ASN arrival | P0 |
| SV-12 | Store delivery calendar (REQ-06) | Orders assigned to non-delivery day | Never — all SMOOTHED_DATEs match store's calendar | P0 |
| SV-13 | Backroom capacity (REQ-06) | Store with small backroom cap | No additional orders assigned that exceed store backroom limit | P1 |
| SV-14 | Shelf-life / MRSL | Short shelf-life SKU | Not pulled forward beyond shelf-life window | P1 |

#### 3.2.4 Capacity

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| SV-15 | Soft orders handled on over-cap days | Filter SOFT orders on days where load > MAX_THRU | Either SMOOTHED_DATE < NEED_DATE or flagged as alert | P0 |
| SV-16 | Capacity alerts generated | Soft orders on over-cap days with no valid window | MOVE_REASON starts with "⚠️" | P0 |
| SV-17 | Alert count in KPIs | kpis['n_alerts'] | Matches count of alert rows in the plan | P1 |

#### 3.2.5 KPI Computation

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| SV-18 | CV before calculation | Manual verify on demand data | CV = std(daily_pallets) / mean(daily_pallets) | P1 |
| SV-19 | CV after calculation | Manual verify on smoothed plan | Correct CV calculation on SMOOTHED_DATE | P1 |
| SV-20 | Orders shifted count | kpis['n_moved'] | Matches actual count of rows where SMOOTHED_DATE ≠ NEED_DATE | P1 |
| SV-21 | Cube utilisation | kpis['cube_util_before'] and _after | Values between 0 and 200 (reasonable range) | P2 |

#### 3.2.6 Solver Parameters

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| SV-22 | Horizon change effect | Run with HORIZON_DAYS=5 vs 14 | Wider horizon → more orders shifted (or equal) | P1 |
| SV-23 | Frozen zone change effect | FROZEN_HOURS=24 vs 96 | Larger frozen zone → fewer orders eligible → fewer shifted | P1 |
| SV-24 | Lambda effect | High λ (500) vs low (1) | High λ → HARD orders remain stable (unchanged) | P2 |
| SV-25 | Gamma effect | High γ (20) vs low (0) | High γ → fewer pull-forwards, smaller date shifts | P2 |

---

### 3.3 Real Data Loader (`data_loader.py`)

#### 3.3.1 Validation

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| DL-01 | Valid CSV accepted | Upload well-formed CSV with all required columns | Returns (df, []) with correct row count | P0 |
| DL-02 | Missing required column | Upload CSV missing SKU_ID column | Error: "Missing required columns: SKU_ID" | P0 |
| DL-03 | Invalid date format | NEED_DATE values like "03/10/2026" | Error: "NEED_DATE must be in YYYY-MM-DD format" | P0 |
| DL-04 | Optional columns defaulted | Upload demand CSV without PRIORITY column | PRIORITY column auto-filled as "SOFT" | P1 |
| DL-05 | Extra columns preserved | CSV has custom columns beyond schema | Extra columns kept in output DataFrame | P2 |
| DL-06 | Column name normalisation | Lowercase headers like "sku_id", " need_date " | Normalised to "SKU_ID", "NEED_DATE" | P0 |
| DL-07 | Empty CSV | Upload CSV with headers only, no rows | Returns (df, []) with 0 rows (valid but empty) | P2 |
| DL-08 | Corrupt / non-CSV file | Upload a .txt or binary file | Error: "Could not parse CSV" | P1 |
| DL-09 | All 5 tables required | Upload only 3 of 5 tables | UI shows "3/5 tables uploaded" info, Load button disabled | P0 |

#### 3.3.2 Template Download

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| DL-10 | Template has correct columns | Download template for each table | CSV header matches TABLE_SCHEMAS required + optional | P1 |
| DL-11 | Template has sample row | Open downloaded CSV | One row of example data present | P2 |

#### 3.3.3 Database Write

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| DL-12 | Write replaces existing tables | Upload real data, then regenerate synthetic | Synthetic data replaces real data cleanly | P1 |
| DL-13 | Concurrent table writes | Upload all 5 and click Load | All 5 tables written to levelset.db in single transaction | P1 |

#### 3.3.4 Export

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| DL-14 | CSV export downloads | Click "Smoothed Plan — CSV" | Browser downloads `levelset_smoothed_plan.csv` | P0 |
| DL-15 | CSV export content | Open downloaded CSV | Headers match plan columns, row count matches, UTF-8 encoded | P0 |
| DL-16 | JSON export downloads | Click "Smoothed Plan — JSON" | Downloads `levelset_smoothed_plan.json` | P0 |
| DL-17 | JSON export structure | Parse downloaded JSON | Valid JSON, records orientation, all fields present | P0 |
| DL-18 | KPI JSON export | Click "KPI Summary — JSON" | Downloads `levelset_kpis.json` with all KPI keys present | P1 |
| DL-19 | Export after re-solve | Change solver params, re-run, export | Exported data reflects the latest solver run | P1 |

---

### 3.4 Dashboard UI (`app.py`)

#### 3.4.1 Page Load

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| UI-01 | App launches | `streamlit run app.py` | Page loads without errors, title visible | P0 |
| UI-02 | Auto-solve on first load | Open fresh session | Solver runs automatically, KPIs and chart render | P0 |
| UI-03 | How-to Guide visible | Check for expander below title | "📖 How to Use LevelSet" expander present, collapsed by default | P1 |

#### 3.4.2 Sidebar Controls

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| UI-04 | Horizon slider works | Move Look-ahead slider | Value updates, re-run produces different results | P0 |
| UI-05 | Frozen zone slider works | Adjust Frozen Zone | Reflected in solver behaviour | P0 |
| UI-06 | Lambda input works | Change λ value | KPI values respond to parameter change | P1 |
| UI-07 | Gamma input works | Change γ value | Fewer/more orders shifted | P1 |
| UI-08 | Run Solver button | Click ▶️ Run Smoothing Solver | Spinner appears, KPIs update, success toast | P0 |
| UI-09 | Reset Synthetic Data button | Click 🔄 Regenerate | New data generated, solver re-runs, different order IDs | P0 |
| UI-10 | Data mode toggle | Switch between Synthetic and Upload | Upload mode shows file uploaders; Synthetic mode hides them | P0 |
| UI-11 | Credits visible | Scroll to sidebar bottom | "Built by Mohith Kunta" with GitHub link | P2 |

#### 3.4.3 KPI Cards

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| UI-12 | All 5 KPI cards rendered | Page fully loaded | Outbound CV, OSA, Shifted, Alerts, Cube Util all visible | P0 |
| UI-13 | CV delta shown | KPI card "Outbound CV" | Shows green/red delta between before/after | P1 |
| UI-14 | OSA card correct | Compare to kpis dict | Percentage matches solver output | P1 |

#### 3.4.4 Charts

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| UI-15 | Before/After bar chart | Scroll to chart section | Red (Before) and green (After) bars visible, grouped by date | P0 |
| UI-16 | Chart updates on re-solve | Click Run Solver | Chart data refreshes | P0 |
| UI-17 | Resource breakdown chart | Below volume chart | Bar chart by resource type visible | P1 |

#### 3.4.5 Schedule Table

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| UI-18 | Table renders | Scroll to schedule section | Sortable, filterable dataframe visible | P0 |
| UI-19 | Green rows for moved orders | Visual inspection | SOFT orders that were moved have green background | P1 |
| UI-20 | Red rows for alerts | Alert orders | Red-tinted background for ⚠️ rows | P1 |
| UI-21 | Filter dropdowns work | Select Priority=HARD only | Table shows only HARD rows | P1 |

---

### 3.5 AI Planner Insight

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| AI-01 | Button visible with key | Set API key in sidebar | "✨ Generate Planner Insight" button appears | P0 |
| AI-02 | Insight generated | Click Generate button | Spinner → insight text rendered in info box | P0 |
| AI-03 | Insight references KPIs | Read generated text | Mentions CV, OSA, shifted count | P1 |
| AI-04 | No key warning | Remove API key | Warning shown: "Enter a key to enable AI features" | P0 |
| AI-05 | Provider switch works | Change from Gemini to OpenAI | Sidebar updates model name; key field re-labels | P1 |
| AI-06 | Ollama no key needed | Select Ollama provider | "No key needed" info shown, no key input field | P2 |
| AI-07 | API key persists across rerun | Enter key, click Run Solver | Key remains filled after page rerun | P1 |
| AI-08 | API key masked | Enter key in sidebar | Characters displayed as dots/bullets (password field) | P0 |

---

### 3.6 Exception Review Panel

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| EX-01 | Section renders | Scroll to Exception Review | "🚨 Exception Review — Planner Triage" header visible | P0 |
| EX-02 | Exception type counts | Check 3 metric cards | 🔴 Capacity Alerts, 🟠 Near-Frozen HARD, 🟡 Days Over-Capacity counts shown | P0 |
| EX-03 | Impact scoring | Check Impact Score column | Higher urgency (closer dates) × larger volume = higher score | P1 |
| EX-04 | Sort order | Default table ordering | Highest Impact Score first, descending | P0 |
| EX-05 | Row colours | Visual inspection | Red tint (🔴), orange tint (🟠), yellow tint (🟡) — readable text | P0 |
| EX-06 | Clean plan message | Run solver with very high capacity (adjust data) | "✅ No exceptions flagged" success message | P1 |
| EX-07 | Action column populated | Each exception row | Specific action text in Action column | P1 |
| EX-08 | Days Out calculation | Verify Days Out values | Correct: (NEED_DATE − today) in days, min 0 | P2 |

---

### 3.7 AI Exception Triage

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| ET-01 | Button visible with exceptions and key | Exceptions present + API key set | "🔍 Triage Exceptions with AI" button visible | P0 |
| ET-02 | Triage output generated | Click Triage button | Spinner → structured brief rendered | P0 |
| ET-03 | Output has 3 urgency buckets | Read triage text | IMMEDIATE, MONITOR, WATCH sections present | P1 |
| ET-04 | Output references top exceptions | Read triage text | Mentions specific ORDER_IDs or SKUs from the exception table | P1 |
| ET-05 | Pallets at risk metric | Bottom of triage output | Contains a headline pallets-at-risk number | P2 |
| ET-06 | No exceptions = no triage | Clean plan (0 exceptions) | Triage section hidden, success message shown instead | P1 |

---

### 3.8 Data Upload (Real Data)

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| RD-01 | Upload mode shows 5 expanders | Switch to Upload Real Data | 5 table expanders in sidebar: demand, dc_capacity, inventory, sku_master, store_master | P0 |
| RD-02 | Upload valid data end-to-end | Upload all 5 CSVs → click Load & Solve | Solver runs on real data, KPIs update, chart changes | P0 |
| RD-03 | Partial upload blocked | Upload 3 of 5 | "3/5 tables uploaded" info, Load button not shown | P0 |
| RD-04 | Validation error blocks solve | Upload demand with bad date format | ❌ error shown, Load button disabled | P0 |
| RD-05 | Can return to synthetic | Switch back to Synthetic, Regenerate | Synthetic data regenerated, solver re-runs | P1 |

---

## 4. Regression Test Cases

These ensure that new features do not break previously working functionality.

| ID | Test Case | Validates | Steps | Expected Result | Priority |
|---|---|---|---|---|---|
| RG-01 | Solver after UI changes | SV-01, SV-02 | Run `python solver.py` from CLI | Solver completes, HARD orders untouched | P0 |
| RG-02 | Data gen after code changes | DG-01 | Run `python data_gen.py` from CLI | DB generated with correct schema | P0 |
| RG-03 | App loads after data_loader added | UI-01 | `streamlit run app.py` | No import errors, page loads | P0 |
| RG-04 | Existing KPI cards after exception panel | UI-12 | Full page load | All 5 KPI cards still render correctly | P0 |
| RG-05 | Chart still renders after panel reorder | UI-15 | Full page load | Before/After chart visible with correct colours | P0 |
| RG-06 | Schedule table after exception panel | UI-18 | Scroll to schedule section | Table renders with colour-coded rows | P0 |
| RG-07 | Sidebar button order correct | UI-08, UI-09 | Inspect sidebar | Order: Settings → AI Provider → Data Source → Run Solver → Reset → Credits | P1 |
| RG-08 | Export buttons after new features | DL-14, DL-16 | Download CSV and JSON | Both download and contain correct data | P0 |
| RG-09 | AI Insight still works after Exception Triage | AI-02 | Set key, click Generate | Insight generated without errors | P0 |
| RG-10 | Solver params still control solver | SV-22 | Change horizon, re-solve | Different result than default | P1 |
| RG-11 | Frozen zone guardrail intact | SV-10 | FROZEN_HOURS=96, check near-term orders | No orders within 96h moved | P0 |
| RG-12 | Reset data clears session | UI-09 | Click Reset, then check KPIs | KPIs reflect fresh data (different from previous) | P1 |
| RG-13 | Page layout order correct | All UI | Full scroll top to bottom | Order: Header → How-to → KPIs → Chart → Resource → Schedule → AI Insight → Exceptions → AI Triage → Export → Footer | P1 |

---

## 5. Edge Cases & Negative Tests

| ID | Test Case | Steps | Expected Result | Priority |
|---|---|---|---|---|
| EC-01 | Empty demand table | Upload demand CSV with headers only, 0 rows | Solver handles gracefully, KPIs show 0s or N/A | P1 |
| EC-02 | Single order in demand | Upload demand with 1 row | Solver completes, plan has 1 row, no crash | P1 |
| EC-03 | All HARD orders | Upload demand where all PRIORITY=HARD | 0 orders shifted, CV unchanged | P1 |
| EC-04 | All SOFT orders | Upload demand where all PRIORITY=SOFT | All eligible orders may be shifted | P2 |
| EC-05 | Zero DC capacity | Set all MAX_THRU=0 | All orders flagged as capacity alerts | P1 |
| EC-06 | Very large dataset | Upload 50,000+ order lines | Solver completes within reasonable time (< 60s), no memory crash | P2 |
| EC-07 | Unicode in store names | Upload store_master with Unicode characters | No encoding errors, table renders correctly | P2 |
| EC-08 | Past dates in demand | NEED_DATE before today | Solver handles without crash, frozen zone applies | P2 |
| EC-09 | Same NEED_DATE for all orders | All orders on one day | Max spike scenario — smoothing pulls orders across horizon | P1 |
| EC-10 | Invalid API key | Enter garbage key, click Generate Insight | LLM call fails gracefully with error message (no crash) | P0 |
| EC-11 | Network timeout on AI call | Disconnect network, click Triage | Timeout handled, error message displayed | P1 |
| EC-12 | DB file deleted mid-session | Delete levelset.db, click Run Solver | Clear error message, not a raw stack trace | P1 |

---

## 6. Test Execution Priority

| Priority | Description | When to Run |
|---|---|---|
| **P0** | Critical path — app must not ship with a P0 failure | Every build / PR |
| **P1** | Important — significant UX or data quality impact | Every release |
| **P2** | Nice to have — minor cosmetic or edge scenarios | Quarterly / ad-hoc |

---

## 7. Test Execution Summary Template

| Run Date | Tester | P0 Pass | P0 Fail | P1 Pass | P1 Fail | P2 Pass | P2 Fail | Notes |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |

---

*LevelSet Test Plan — Mohith Kunta*  
*[github.com/m-kunta](https://github.com/m-kunta)*
