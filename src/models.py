"""ML model classes for F1 position prediction."""

import numpy as np
import pandas as pd
import joblib
from sklearn.linear_model import Ridge, Lasso
from sklearn.ensemble import RandomForestRegressor
from xgboost import XGBRegressor
from sklearn.model_selection import cross_val_score, GridSearchCV
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score


FEATURE_COLS = [
    'grid', 'qualifyingPosition',
    'driver_rolling_position', 'driver_rolling_points', 'driver_consistency',
    'driver_rolling_dnf_rate', 'driver_avg_position_gain',
    'driver_career_races', 'driver_career_avg_position', 'driver_career_wins',
    'driver_circuit_avg_position',
    'team_rolling_points', 'team_rolling_position', 'team_rolling_best_position',
    'team_reliability', 'team_development',
    'track_position_std', 'track_avg_finish', 'track_dnf_rate',
    'track_avg_position_gain', 'track_top3_conversion_rate',
]

TARGET = 'position'


def prepare_data(features_df):
    """Prepare X and y from the full feature matrix.

    Drops rows with NaN in feature columns and returns clean arrays.
    """
    df = features_df.dropna(subset=FEATURE_COLS + [TARGET]).copy()
    X = df[FEATURE_COLS].values
    y = df[TARGET].values
    return X, y, df


def temporal_train_test_split(features_df, test_seasons=None):
    """Split data temporally: train on earlier seasons, test on later ones.

    Default: train on 2014–2022, test on 2023–2024.
    """
    if test_seasons is None:
        test_seasons = [2023, 2024]

    df = features_df.dropna(subset=FEATURE_COLS + [TARGET]).copy()
    train = df[~df['season'].isin(test_seasons)]
    test = df[df['season'].isin(test_seasons)]

    X_train = train[FEATURE_COLS].values
    y_train = train[TARGET].values
    X_test = test[FEATURE_COLS].values
    y_test = test[TARGET].values

    return X_train, X_test, y_train, y_test, train, test


class BaselineModel:
    """Baseline: predict finishing position = grid position."""

    def fit(self, X, y):
        return self

    def predict(self, X):
        # grid is the first column in FEATURE_COLS
        return X[:, 0].copy()


def build_models():
    """Return a dict of model name -> (model, param_grid) for tuning."""
    return {
        'Ridge': (
            Ridge(),
            {'alpha': [0.1, 1.0, 10.0, 100.0]}
        ),
        'Lasso': (
            Lasso(max_iter=5000),
            {'alpha': [0.01, 0.1, 1.0, 10.0]}
        ),
        'RandomForest': (
            RandomForestRegressor(random_state=42, n_jobs=-1),
            {
                'n_estimators': [100, 200],
                'max_depth': [8, 12, 16],
                'min_samples_leaf': [5, 10],
            }
        ),
        'XGBoost': (
            XGBRegressor(random_state=42, n_jobs=-1, verbosity=0),
            {
                'n_estimators': [100, 200],
                'max_depth': [4, 6, 8],
                'learning_rate': [0.05, 0.1],
                'subsample': [0.8],
            }
        ),
    }


def tune_and_evaluate(model, param_grid, X_train, y_train, X_test, y_test, cv=5):
    """Run GridSearchCV and evaluate on the test set.

    Returns: best_model, cv_results_dict, test_metrics_dict
    """
    grid = GridSearchCV(model, param_grid, cv=cv, scoring='neg_mean_absolute_error',
                        n_jobs=-1, refit=True)
    grid.fit(X_train, y_train)

    best = grid.best_estimator_
    y_pred = best.predict(X_test)

    metrics = evaluate_model(y_test, y_pred)
    cv_mae = -grid.best_score_

    return best, {
        'best_params': grid.best_params_,
        'cv_mae': cv_mae,
    }, metrics


def evaluate_model(y_true, y_pred):
    """Compute regression metrics."""
    return {
        'MAE': mean_absolute_error(y_true, y_pred),
        'RMSE': np.sqrt(mean_squared_error(y_true, y_pred)),
        'R2': r2_score(y_true, y_pred),
        'Median_AE': np.median(np.abs(y_true - y_pred)),
    }


def save_model(model, path):
    """Save a trained model to disk."""
    joblib.dump(model, path)


def load_model(path):
    """Load a trained model from disk."""
    return joblib.load(path)
