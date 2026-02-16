"""Feature engineering functions for track, driver, and team features."""

import pandas as pd
import numpy as np


def create_track_features(race_df):
    """Create track characteristic features from historical race data.

    Features per circuit:
    - track_position_std: std dev of finishing positions (chaos/predictability)
    - track_dnf_rate: proportion of DNFs at this circuit
    - track_avg_position_gain: avg grid-to-finish position change
    - track_top3_from_top3_rate: how often top-3 qualifiers finish top-3
    - track_races_held: number of seasons this circuit has been used
    """
    is_dnf = ~race_df['status'].str.contains(r'Finished|Lap', regex=True)
    position_gain = race_df['grid'] - race_df['position']

    track_features = race_df.groupby('circuitId').agg(
        track_position_std=('position', 'std'),
        track_avg_finish=('position', 'mean'),
        track_dnf_rate=('status', lambda x: (~x.str.contains(r'Finished|Lap', regex=True)).mean()),
        track_avg_position_gain=('position', lambda x: (race_df.loc[x.index, 'grid'] - x).mean()),
        track_races_held=('season', 'nunique'),
    ).reset_index()

    # Top-3 qualifying converting to top-3 finish rate
    top3_data = race_df[race_df['grid'].between(1, 3)]
    top3_conversion = top3_data.groupby('circuitId').apply(
        lambda x: (x['position'] <= 3).mean(), include_groups=False
    ).reset_index(name='track_top3_conversion_rate')

    track_features = track_features.merge(top3_conversion, on='circuitId', how='left')
    track_features['track_top3_conversion_rate'] = track_features['track_top3_conversion_rate'].fillna(0.5)

    return track_features


def create_driver_features(race_df, qual_df, window=5):
    """Create driver performance features using rolling windows.

    For each driver at each race, computes features based on their
    PREVIOUS races (no data leakage).

    Features:
    - driver_rolling_position: rolling avg finish position (last N races)
    - driver_rolling_points: rolling avg points (last N races)
    - driver_consistency: rolling std of finish position (lower = more consistent)
    - driver_avg_quali_delta: avg qualifying-to-race position gain
    - driver_dnf_rate: rolling DNF rate
    - driver_circuit_avg_position: historical avg at this specific circuit
    """
    # Sort chronologically
    df = race_df.sort_values(['season', 'round']).copy()
    df['is_dnf'] = ~df['status'].str.contains(r'Finished|Lap', regex=True)
    df['position_gain'] = df['grid'] - df['position']

    # Rolling features per driver (shifted to avoid leakage)
    driver_features = []
    for driver_id, group in df.groupby('driverId'):
        g = group.copy()
        g['driver_rolling_position'] = g['position'].shift(1).rolling(window, min_periods=1).mean()
        g['driver_rolling_points'] = g['points'].shift(1).rolling(window, min_periods=1).mean()
        g['driver_consistency'] = g['position'].shift(1).rolling(window, min_periods=2).std()
        g['driver_rolling_dnf_rate'] = g['is_dnf'].shift(1).rolling(window, min_periods=1).mean()
        g['driver_avg_position_gain'] = g['position_gain'].shift(1).rolling(window, min_periods=1).mean()

        # Career stats up to this point
        g['driver_career_races'] = range(1, len(g) + 1)
        g['driver_career_avg_position'] = g['position'].expanding().mean().shift(1)
        g['driver_career_wins'] = (g['position'] == 1).expanding().sum().shift(1)

        driver_features.append(g)

    df = pd.concat(driver_features, ignore_index=True)

    # Driver-circuit historical performance (avg position at this circuit before this race)
    circuit_perf = []
    for (driver_id, circuit_id), group in df.groupby(['driverId', 'circuitId']):
        g = group.copy()
        g['driver_circuit_avg_position'] = g['position'].expanding().mean().shift(1)
        circuit_perf.append(g)

    df = pd.concat(circuit_perf, ignore_index=True)

    # Merge qualifying position
    if qual_df is not None:
        df = df.merge(
            qual_df[['season', 'round', 'driverId', 'qualifyingPosition']],
            on=['season', 'round', 'driverId'],
            how='left'
        )

    feature_cols = [
        'season', 'round', 'driverId', 'driverCode', 'circuitId',
        'constructorId', 'constructorName', 'grid', 'position', 'points',
        'driver_rolling_position', 'driver_rolling_points', 'driver_consistency',
        'driver_rolling_dnf_rate', 'driver_avg_position_gain',
        'driver_career_races', 'driver_career_avg_position', 'driver_career_wins',
        'driver_circuit_avg_position',
    ]
    if 'qualifyingPosition' in df.columns:
        feature_cols.append('qualifyingPosition')

    return df[feature_cols]


def create_team_features(race_df, window=5):
    """Create team/constructor features using rolling windows.

    Features per constructor at each race (no leakage):
    - team_rolling_points: avg points per driver per race (last N races)
    - team_rolling_position: avg finish position (last N races)
    - team_reliability: 1 - rolling DNF rate
    - team_development: slope of recent points trend (positive = improving)
    - team_best_driver_position: best finish among team drivers (rolling)
    """
    df = race_df.sort_values(['season', 'round']).copy()
    df['is_dnf'] = ~df['status'].str.contains(r'Finished|Lap', regex=True)

    # Aggregate to team-race level first
    team_race = df.groupby(['season', 'round', 'constructorId']).agg(
        team_race_points=('points', 'sum'),
        team_race_avg_position=('position', 'mean'),
        team_race_best_position=('position', 'min'),
        team_race_dnf_count=('is_dnf', 'sum'),
        team_race_entries=('is_dnf', 'count'),
    ).reset_index().sort_values(['season', 'round'])

    # Rolling features per constructor
    team_features = []
    for constructor_id, group in team_race.groupby('constructorId'):
        g = group.copy()
        g['team_rolling_points'] = g['team_race_points'].shift(1).rolling(window, min_periods=1).mean()
        g['team_rolling_position'] = g['team_race_avg_position'].shift(1).rolling(window, min_periods=1).mean()
        g['team_rolling_best_position'] = g['team_race_best_position'].shift(1).rolling(window, min_periods=1).mean()
        g['team_reliability'] = 1 - (g['team_race_dnf_count'] / g['team_race_entries']).shift(1).rolling(window, min_periods=1).mean()

        # Development trajectory: slope of points over last N races
        points_shifted = g['team_race_points'].shift(1)
        g['team_development'] = points_shifted.rolling(window, min_periods=3).apply(
            lambda x: np.polyfit(range(len(x)), x, 1)[0] if len(x) >= 3 else 0, raw=False
        )

        team_features.append(g)

    team_df = pd.concat(team_features, ignore_index=True)

    return team_df[['season', 'round', 'constructorId',
                     'team_rolling_points', 'team_rolling_position',
                     'team_rolling_best_position', 'team_reliability',
                     'team_development']]


def build_full_feature_matrix(race_df, qual_df, circuits_df, window=5):
    """Build the complete feature matrix by combining all feature sets.

    Returns a DataFrame ready for modeling with target variable (position).
    """
    # Create individual feature sets
    driver_df = create_driver_features(race_df, qual_df, window=window)
    team_df = create_team_features(race_df, window=window)
    track_df = create_track_features(race_df)

    # Merge everything
    features = driver_df.merge(
        team_df, on=['season', 'round', 'constructorId'], how='left'
    ).merge(
        track_df, on='circuitId', how='left'
    )

    # Fill NaN from first few races (no history yet) with column medians
    feature_cols = [c for c in features.columns if c.startswith(('driver_', 'team_', 'track_'))]
    for col in feature_cols:
        features[col] = features[col].fillna(features[col].median())

    return features
