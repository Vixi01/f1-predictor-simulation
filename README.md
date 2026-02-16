# F1 Performance Predictor

A data-driven approach to predicting Formula 1 race outcomes using statistical modeling, machine learning, and Monte Carlo simulation.

## About

This project analyzes 10 years of F1 data (2014–2024) to build predictive models for race finishing positions. It combines exploratory data analysis, feature engineering from track and driver characteristics, ensemble ML models, and probabilistic simulation to generate race predictions with confidence intervals.

**Skills demonstrated:** Python, statistical analysis, machine learning, Monte Carlo methods, data visualization, feature engineering, API data collection

## Project Pipeline

| Stage | Notebook | Description |
|-------|----------|-------------|
| 1 | `01_data_collection.ipynb` | Fetch race, qualifying, and circuit data from the Ergast F1 API |
| 2 | `02_eda.ipynb` | Explore distributions, trends, and correlations in the data |
| 3 | `03_feature_engineering.ipynb` | Engineer track, driver, and team performance features with PCA |
| 4 | `04_modeling.ipynb` | Train and evaluate Ridge, Random Forest, and XGBoost models |
| 5 | `05_simulation.ipynb` | Monte Carlo race simulation with probability distributions |

## Tech Stack

- **Languages:** Python 3.10+
- **Data:** pandas, NumPy, SciPy
- **Visualization:** matplotlib, seaborn
- **ML:** scikit-learn, XGBoost
- **Data Source:** [Ergast Developer API](http://ergast.com/mrd/)

## Quick Start

```bash
# Clone the repository
git clone https://github.com/yourusername/f1-performance-predictor.git
cd f1-performance-predictor

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Launch Jupyter and run notebooks in order
jupyter notebook notebooks/
```

## Project Structure

```
├── data/               # Raw, processed, and engineered feature datasets
├── notebooks/          # Staged analysis notebooks (01–05)
├── src/                # Reusable Python modules
├── tests/              # Unit tests
├── outputs/            # Figures, saved models, reports, predictions
└── docs/               # Methodology, results, references
```

## Key Findings

*Results will be documented as the project progresses through each stage.*

## Author

Brian — Data Science & Computational Mathematics

Built as a portfolio project demonstrating analytical and quantitative skills for data analyst roles.
