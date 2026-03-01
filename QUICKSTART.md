# LevelSet DC Outbound Smoothing — Quickstart Guide

Welcome to LevelSet! This guide will get you up and running with the DC Outbound Smoothing prototype in just a few minutes, assuming you already have Python installed.

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

1. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```
2. Open `.env` in a text editor and add an API key for your preferred provider (Google Gemini is the default, but you can also use OpenAI, Anthropic, or Groq).
   ```env
   # Example for Google Gemini
   GEMINI_API_KEY=your_api_key_here
   LLM_PROVIDER=gemini
   ```

*Note: You can also enter the API key directly in the Streamlit UI later if you prefer not to use the `.env` file.*

---

## 3. Generate the Sample Database

LevelSet comes with a robust synthetic data generator. This creates a realistic 30-day snapshot of a distribution center's operations, including spiky demand patterns designed specifically to test the smoothing algorithm.

```bash
python data_gen.py
```
*You should see output indicating that 5 tables were created and saved to `levelset.db`.*

---

## 4. Run the Dashboard

Launch the Streamlit planning interface:

```bash
streamlit run app.py
```

This will automatically open your default web browser to [http://localhost:8501](http://localhost:8501).

---

## 5. Using the Application

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

### 3) Review Exceptions & AI Triage

Scroll down to the **Exception Review** section. Here you will find any "Capacity Alerts" (orders that could not be smoothed) or days that are still over capacity.
- If you entered an AI API key, click **🔍 Triage Exceptions with AI** to get a structured priority list of what needs immediate attention versus what just needs to be monitored.

---

## 6. Upload Your Own Data

Want to try LevelSet with real data?
1. In the sidebar, change the **Data Source** radio button from *Synthetic Data* to *Upload Real Data*.
2. Expand the upload boxes and download the CSV templates for each table.
3. Replace the template data with your own, adhering to the required column names and formats.
4. Upload all 5 CSVs and click the **Load Data & Run Solver** button.

---

*For full technical details on the objective function, constraints, and data specifications, please refer to [REQUIREMENTS.md](REQUIREMENTS.md).*
