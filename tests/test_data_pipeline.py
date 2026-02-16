"""Unit tests for the data pipeline module."""

import unittest
import pandas as pd
from src.data_pipeline import load_race_results, load_qualifying_results, load_circuits


class TestDataLoading(unittest.TestCase):
    """Test data loading functions after data collection has been run."""

    def test_race_results_shape(self):
        df = load_race_results()
        self.assertGreater(len(df), 0)
        self.assertIn("season", df.columns)
        self.assertIn("driverCode", df.columns)
        self.assertIn("position", df.columns)

    def test_qualifying_results_shape(self):
        df = load_qualifying_results()
        self.assertGreater(len(df), 0)
        self.assertIn("season", df.columns)
        self.assertIn("qualifyingPosition", df.columns)

    def test_circuits_shape(self):
        df = load_circuits()
        self.assertGreater(len(df), 0)
        self.assertIn("circuitId", df.columns)
        self.assertIn("country", df.columns)

    def test_season_range(self):
        df = load_race_results()
        self.assertGreaterEqual(df["season"].min(), 2014)
        self.assertLessEqual(df["season"].max(), 2024)


if __name__ == "__main__":
    unittest.main()
