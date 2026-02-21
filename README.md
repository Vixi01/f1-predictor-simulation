# F1 Performance Predictor

A data-driven approach to predicting Formula 1 race outcomes using statistical modeling, machine learning, and Monte Carlo simulation.

---

## Overview

This project analyzes **10 years of Formula 1 data (2014–2024)** across **4,626 race entries**, **59 drivers**, **20 constructors**, and **32 circuits** to build predictive models for race finishing positions. The pipeline moves from raw API data through exploratory analysis, feature engineering, ML modeling, and finally probabilistic race simulation.

**Skills demonstrated:** Python, statistical analysis, machine learning, Monte Carlo simulation, data visualization, feature engineering, API data collection

---

## Project Pipeline

```
┌─────────────────────┐     ┌─────────────────────┐     ┌─────────────────────┐
│  01_data_collection │────▶│      02_eda          │────▶│ 03_feature_engineer │
│   Ergast F1 API     │     │  Trends & Patterns   │     │  21 ML Features     │
└─────────────────────┘     └─────────────────────┘     └──────────┬──────────┘
                                                                    │
                    ┌───────────────────────────────────────────────┘
                    ▼
         ┌─────────────────────┐     ┌─────────────────────┐
         │    04_modeling      │────▶│   05_simulation     │
         │  4 ML Models Tuned  │     │  10,000 Monte Carlo │
         └─────────────────────┘     └─────────────────────┘
```

---

## Stage Breakdown

### Stage 1 — Data Collection (`01_data_collection.ipynb`)

Fetches all race, qualifying, and circuit data from the **Ergast Developer API** across 11 seasons (2014–2024).

| Dataset | Rows | Columns |
|---------|------|---------|
| Race Results | 4,626 | 15 |
| Qualifying Results | 4,610 | 10 |
| Circuits | 78 | 5 |

- Validates completeness and handles missing qualifying times (Q2/Q3 not set for eliminated drivers)
- Saves raw CSVs to `data/raw/` for reproducibility

---

### Stage 2 — Exploratory Data Analysis (`02_eda.ipynb`)

Deep dive into historical patterns across drivers, teams, and circuits.

**Key findings:**

| Metric | Value |
|--------|-------|
| Seasons covered | 2014 – 2024 |
| Unique drivers | 59 |
| Unique constructors | 20 |
| Unique circuits | 32 |
| Overall DNF rate | **17.3%** (801 of 4,626 entries) |
| Qualifying → Race position correlation | **r = 0.632** |
| Pole position win rate | **53.5%** |
| Front row (P1–P2) podium rate | **73.7%** |

**Constructor dominance by season:**

| Season | Dominant Constructor | Wins |
|--------|---------------------|------|
| 2014–2020 | Mercedes | 11–19 wins/season |
| 2021 | Red Bull | 11 |
| 2022 | Red Bull | 17 |
| 2023 | Red Bull | **21** |
| 2024 | Red Bull | 9 |

**Top drivers by average finish position (2014–2024):**

| Driver | Avg Position | Points/Race | Wins | Podiums | DNF Rate |
|--------|-------------|-------------|------|---------|----------|
| ROS | 3.76 | 17.4 | 20 | 46 | 6.8% |
| HAM | 3.99 | 16.4 | 83 | 148 | 6.2% |
| VER | 5.65 | 13.9 | 63 | 112 | 15.3% |
| VET | 7.56 | 9.2 | 14 | 60 | 13.9% |
| LEC | 7.56 | 9.1 | 8 | 43 | 16.1% |

---

### Stage 3 — Feature Engineering (`03_feature_engineering.ipynb`)

Constructs **21 engineered features** across three categories: driver performance, team performance, and track characteristics.

**Driver features (rolling window, per circuit):**

| Feature | Description |
|---------|-------------|
| `driver_rolling_position` | 5-race rolling avg finish |
| `driver_rolling_points` | 5-race rolling points scored |
| `driver_consistency` | Std dev of recent finishes |
| `driver_rolling_dnf_rate` | Recent DNF frequency |
| `driver_avg_position_gain` | Avg grid→finish delta |
| `driver_career_avg_position` | Career-long avg finish |
| `driver_career_wins` | Career win count |
| `driver_circuit_avg_position` | Historical avg at this circuit |

**Team features:**

| Feature | Description |
|---------|-------------|
| `team_rolling_points` | 5-race rolling team points |
| `team_rolling_position` | 5-race rolling team position |
| `team_reliability` | Recent finish completion rate |
| `team_development` | Points trend (improving/declining) |

**Track features (PCA applied):**

| Feature | Description |
|---------|-------------|
| `track_position_std` | Typical position spread |
| `track_dnf_rate` | Historical DNF rate |
| `track_avg_position_gain` | Avg overtaking opportunity |
| `track_top3_conversion_rate` | Pole-to-podium conversion |

**PCA on track features:**

| Component | Variance Explained | Cumulative |
|-----------|--------------------|------------|
| PC1 | 44.6% | 44.6% |
| PC2 | 21.4% | 66.0% |
| PC3 | 20.9% | **86.9%** |

**Top 5 features most correlated with race finishing position:**

| Feature | Pearson r |
|---------|-----------|
| `qualifyingPosition` | 0.632 |
| `team_rolling_best_position` | 0.601 |
| `team_rolling_position` | 0.595 |
| `team_rolling_points` | -0.584 |
| `driver_rolling_points` | -0.573 |

---

### Stage 4 — Predictive Modeling (`04_modeling.ipynb`)

Trains and tunes four models using **3,691 training samples (2014–2022)** and evaluates on **919 test samples (2023–2024)**.

**Train/test split:** Temporal (no data leakage — future seasons never seen during training)

#### Model Performance

| Model | CV MAE | Test MAE | Test RMSE | Test R² | Median AE |
|-------|--------|----------|-----------|---------|-----------|
| Baseline (grid pos) | — | 3.362 | 4.856 | 0.290 | 2.000 |
| **Ridge** ⭐ | 3.463 | **3.098** | **4.012** | **0.515** | 2.542 |
| Lasso | 3.463 | 3.099 | 4.013 | 0.515 | 2.524 |
| Random Forest | 3.523 | 3.191 | 4.103 | 0.493 | 2.628 |
| XGBoost | 3.498 | 3.306 | 4.170 | 0.476 | 2.830 |

> **Best model: Ridge Regression** — 8% MAE improvement over baseline grid position alone.
> R² of 0.515 means the model explains **51.5% of variance** in race finishing positions.

#### XGBoost Feature Importance

| Rank | Feature | Importance Score |
|------|---------|-----------------|
| 1 | `qualifyingPosition` | 0.4385 |
| 2 | `team_rolling_position` | 0.0689 |
| 3 | `team_rolling_points` | 0.0554 |
| 4 | `team_rolling_best_position` | 0.0523 |
| 5 | `driver_rolling_points` | 0.0472 |
| 6 | `driver_career_avg_position` | 0.0381 |
| 7 | `grid` | 0.0360 |
| 8 | `driver_career_wins` | 0.0318 |
| 9 | `driver_consistency` | 0.0214 |
| 10 | `driver_rolling_position` | 0.0212 |

> Qualifying position alone accounts for **43.9%** of predictive signal — confirming that Saturday performance is the single strongest predictor of Sunday results.

---

### Stage 5 — Monte Carlo Simulation (`05_simulation.ipynb`)

Runs **10,000 race simulations** per race to generate probabilistic outcome distributions for all 20 drivers.

**Sample output — 2024 Abu Dhabi GP (Round 24):**

| Driver | Expected Pos | Win % | Podium % | Points % | 90% CI |
|--------|-------------|-------|----------|----------|--------|
| NOR | 5.5 | 18.9% | 45.2% | 86.6% | [1–18] |
| VER | 5.4 | 18.7% | 46.4% | 87.1% | [1–18] |
| SAI | 5.5 | 18.3% | 45.6% | 86.7% | [1–17] |
| PIA | 6.0 | 14.5% | 39.2% | 83.8% | [1–18] |
| RUS | 7.6 | 7.0% | 23.8% | 74.0% | [1–19] |
| LEC | 9.3 | 3.3% | 14.1% | 60.9% | [2–19] |
| HAM | 9.7 | 2.8% | 11.9% | 58.3% | [2–19] |

**Simulation validation (2024 season — 479 driver-race entries):**

| Metric | Value |
|--------|-------|
| Mean Absolute Error | **2.96 positions** |
| Median Absolute Error | **2.50 positions** |
| 90% CI Coverage | **97.9%** (target: 90%) |
| 50% CI Coverage | **72.4%** (target: 50%) |
| Single-race 90% CI hit rate | **95%** (19/20 drivers) |

> The model is slightly conservative (wider CIs than needed), which is appropriate for a probabilistic racing predictor — it's better to be calibrated toward uncertainty than overconfident.

---

## Final Evaluation Summary

| Category | Metric | Value |
|----------|--------|-------|
| **Data** | Race entries analyzed | 4,626 |
| **Data** | Seasons covered | 2014–2024 (11 seasons) |
| **Features** | Engineered features | 21 |
| **Features** | Strongest predictor | Qualifying position (r=0.632) |
| **Model** | Best model | Ridge Regression |
| **Model** | Test MAE | **3.098 positions** |
| **Model** | Test RMSE | **4.012** |
| **Model** | Test R² | **0.515** |
| **Model** | Improvement over baseline | **8% MAE reduction** |
| **Simulation** | Runs per race | 10,000 |
| **Simulation** | Season MAE | **2.96 positions** |
| **Simulation** | 90% CI coverage | **97.9%** |

---

## Tech Stack

| Category | Tools |
|----------|-------|
| Language | Python 3.13 |
| Data manipulation | pandas, NumPy, SciPy |
| Visualization | matplotlib, seaborn |
| Machine learning | scikit-learn (Ridge, Lasso, Random Forest), XGBoost |
| Simulation | NumPy Monte Carlo, custom race engine |
| Data source | [Ergast Developer API](http://ergast.com/mrd/) |
| Environment | Jupyter Notebook, Miniconda |

---

## Quick Start

```bash
# Clone the repository
git clone https://github.com/Vixi01/f1-performance-predictor.git
cd f1-performance-predictor

# Install dependencies
pip install -r requirements.txt

# Launch Jupyter and run notebooks in order (01 → 05)
jupyter notebook notebooks/
```

---

## Project Structure

```
├── data/
│   ├── raw/                # Race results, qualifying, circuits (from API)
│   ├── processed/          # Cleaned and merged datasets
│   └── features/           # Engineered feature CSVs
├── notebooks/              # 5 staged analysis notebooks
├── src/                    # Reusable Python modules
├── outputs/
│   ├── figures/            # All generated charts and plots
│   ├── models/             # Saved model files (.pkl)
│   ├── predictions/        # Historical and simulation predictions
│   └── reports/            # Model comparison tables
└── docs/                   # Methodology and references
```

---

## Author

**Brian** — Data Science & Computational Mathematics

Built as a portfolio project demonstrating analytical and quantitative skills for data analyst roles.
