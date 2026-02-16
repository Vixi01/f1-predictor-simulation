"""Monte Carlo simulation for F1 race outcome prediction."""

import numpy as np
import pandas as pd
from scipy import stats


def monte_carlo_race(model, X_race, driver_codes, n_simulations=10000,
                     noise_std=4.0, dnf_probability=0.05, random_state=42):
    """Run Monte Carlo simulation for a single race.

    For each simulation:
    1. Get base predictions from the model
    2. Add Gaussian noise (representing race-day uncertainty)
    3. Randomly apply DNFs based on historical probability
    4. Re-rank drivers to get simulated finishing order

    Parameters
    ----------
    model : fitted model with .predict()
    X_race : array of shape (n_drivers, n_features) for one race
    driver_codes : list of driver codes matching X_race rows
    n_simulations : number of Monte Carlo iterations
    noise_std : std dev of Gaussian noise added to predictions
    dnf_probability : probability of a DNF per driver per simulation
    random_state : for reproducibility

    Returns
    -------
    sim_positions : array of shape (n_simulations, n_drivers) with simulated finishing positions
    """
    rng = np.random.default_rng(random_state)
    n_drivers = X_race.shape[0]

    base_pred = model.predict(X_race)

    sim_positions = np.zeros((n_simulations, n_drivers), dtype=int)

    for i in range(n_simulations):
        # Add noise to predictions
        noisy_pred = base_pred + rng.normal(0, noise_std, n_drivers)

        # Apply random DNFs (push to back of grid)
        dnf_mask = rng.random(n_drivers) < dnf_probability
        noisy_pred[dnf_mask] += 50  # ensure DNFs rank last

        # Rank to get positions (1-indexed)
        order = np.argsort(noisy_pred)
        positions = np.empty(n_drivers, dtype=int)
        positions[order] = np.arange(1, n_drivers + 1)
        sim_positions[i] = positions

    return sim_positions


def generate_probabilities(sim_positions, driver_codes):
    """Calculate outcome probabilities from simulation results.

    Returns a DataFrame with columns: driver, win_prob, podium_prob,
    points_prob (top 10), expected_position, position_std, p5, p25, p50, p75, p95.
    """
    n_simulations, n_drivers = sim_positions.shape

    results = []
    for j, driver in enumerate(driver_codes):
        positions = sim_positions[:, j]
        results.append({
            'driver': driver,
            'win_prob': (positions == 1).mean(),
            'podium_prob': (positions <= 3).mean(),
            'points_prob': (positions <= 10).mean(),
            'expected_position': positions.mean(),
            'position_std': positions.std(),
            'p5': np.percentile(positions, 5),
            'p25': np.percentile(positions, 25),
            'p50': np.percentile(positions, 50),
            'p75': np.percentile(positions, 75),
            'p95': np.percentile(positions, 95),
        })

    return pd.DataFrame(results).sort_values('expected_position')


def build_position_matrix(sim_positions, driver_codes):
    """Build a matrix of P(driver finishes in position p).

    Returns a DataFrame of shape (n_drivers, max_position) with probabilities.
    """
    n_simulations, n_drivers = sim_positions.shape

    matrix = np.zeros((n_drivers, n_drivers))
    for j in range(n_drivers):
        for pos in range(1, n_drivers + 1):
            matrix[j, pos - 1] = (sim_positions[:, j] == pos).mean()

    return pd.DataFrame(
        matrix,
        index=driver_codes,
        columns=[f'P{i}' for i in range(1, n_drivers + 1)]
    )


def sensitivity_analysis(model, X_race, driver_codes, feature_names,
                         feature_idx, vary_range=(-2, 2), n_steps=20,
                         n_simulations=5000, noise_std=4.0):
    """Vary a single feature and measure impact on a driver's expected position.

    Parameters
    ----------
    feature_idx : index of the feature to vary in X_race
    vary_range : (min_delta, max_delta) standard deviations to vary

    Returns
    -------
    DataFrame with columns: feature_value, driver, expected_position
    """
    feature_std = X_race[:, feature_idx].std()
    if feature_std == 0:
        feature_std = 1.0

    deltas = np.linspace(vary_range[0] * feature_std, vary_range[1] * feature_std, n_steps)
    results = []

    for delta in deltas:
        X_modified = X_race.copy()
        X_modified[:, feature_idx] += delta

        sim_pos = monte_carlo_race(model, X_modified, driver_codes,
                                   n_simulations=n_simulations,
                                   noise_std=noise_std)
        probs = generate_probabilities(sim_pos, driver_codes)

        for _, row in probs.iterrows():
            results.append({
                'feature_delta': delta,
                'feature_value_norm': delta / feature_std,
                'driver': row['driver'],
                'expected_position': row['expected_position'],
            })

    return pd.DataFrame(results)
