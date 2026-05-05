# LevelSet DC Outbound Smoothing — Quickstart Guide

This guide explains how to install and run the DC Outbound Smoothing prototype. It requires Python 3.9+ to be installed on your machine.

---

## 1. Get the Code & Setup the Environment

We strongly recommend using a virtual environment to keep dependencies clean.

```bash
git clone https://github.com/m-kunta/dc-outbound-smoothing
cd dc-outbound-smoothing

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows use: .venv\Scripts\activate

# Install required packages
pip install -r requirements.txt
```

---

## 2. Enable AI Features (Optional but Recommended)

LevelSet uses GenAI to generate Planner Insights and Triage Exceptions.

### Option A: Enter Key in the Dashboard (Easiest)
Once you launch the app, simply select your provider (Gemini, OpenAI, Anthropic, Groq, or Ollama) from the sidebar dropdown and paste your API key directly into the secure text field.

### Option B: Use an Environment File
If you prefer not to enter your key every time, you can set it via a `.env` file:
1. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```
2. Open `.env` in a text editor and add an API key for your preferred provider.
   ```env
   # Example for Google Gemini
   GEMINI_API_KEY=your_api_key_here
   LLM_PROVIDER=Gemini
   ```

---

## 3. Generate the Sample Database

LevelSet comes with a robust synthetic data generator. This creates a realistic 30-day snapshot of a distribution center's operations, including spiky demand patterns designed specifically to test the smoothing algorithm.

```bash
python data_gen.py
```
*You should see output indicating that 5 tables were created and saved to `levelset.db`.*

---

## 4. Run the Test Suite (Optional but Recommended)

Before launching the dashboard, you can verify that all backend logic is working correctly on your machine:

```bash
pytest test_backend.py -v
```

You should see **57 tests pass**. These cover the solver guardrails, unit conversion math, data loader validation, guardrail boundary conditions, multi-DC rerouting, and edge-case handling. If any tests fail, check that all dependencies in `requirements.txt` were installed correctly.

---

## 5. Run the Dashboard

Launch the Streamlit planning interface:

```bash
streamlit run app.py
```

This will automatically open your default web browser to [http://localhost:8501](http://localhost:8501).

---

## 6. Using the Application

### 1) Explore the Before & After

When the app loads, the solver runs automatically. 
- Scroll down to the **Volume Chart**. You will see grey bars (the original, spiky unconstrained demand) overlaid with coloured bars (the smoothed, constrained plan).
- Check the **KPI Scorecards** to see the actual number of orders shifted and the improvement in the Outbound CV (Coefficient of Variation).

### 2) Adjust the Levers

Use the sidebar on the left to change how the solver behaves:
- **Look-ahead Horizon:** Increase this to give the solver more days to find trough capacity.
- **Frozen Zone:** Increase this to lock down the immediate 2–4 days, simulating a real warehouse environment where waves are already dropping.
- **Penalty Weights (λ and γ):** Adjust these to change how aggressively the solver protects On-Shelf Availability (OSA) versus penalizing early shipping.

*Tip: After making a change, click the **▶️ Run Smoothing Solver** button to re-run the engine and see the new results.*

### 3) Run a What-If Scenario Comparison

Scroll down to the **🔬 What-If Scenario Comparison** section and expand **Configure & Run Comparison**.

- **Scenario A** pre-fills from the current sidebar parameters (your baseline).
- **Scenario B** defaults to a slightly different configuration — widen the horizon, lower λ, or raise γ to see how the solver responds.
- Click **▶️ Run Scenario Comparison** to run both configs and see a side-by-side KPI table plus an overlay volume chart.

*Tip: This is the fastest way to answer "what if I gave the solver two more days?" without touching the main plan.*

### 4) Review Exceptions & AI Triage

Scroll down to the **Exception Review** section. Here you will find any "Capacity Alerts" (orders that could not be smoothed) or days that are still over capacity.
- If you entered an AI API key, click **🔍 Triage Exceptions with AI** to get a structured priority list of what needs immediate attention versus what just needs to be monitored.

---

## 7. Upload Real Data & Exporting

Want to try LevelSet with real data?
1. In the sidebar, change the **Data Source** radio button from *Synthetic Data* to *Upload Real Data*.
2. Expand the upload boxes and download the CSV templates for each table.
3. Replace the template data with your own, adhering to the required column names and formats.
4. Upload all 5 CSVs and click the **Load Data & Run Solver** button.
5. **Export the Plan:** Once your data is processed, you can download the optimized ship schedule as a CSV or JSON file to import into your WMS/OMS.

---

*For full technical details on the objective function, constraints, and data specifications, please refer to [REQUIREMENTS.md](REQUIREMENTS.md).*
