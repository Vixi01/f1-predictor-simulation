"""Data pipeline utilities for fetching and loading F1 data."""

import os
import time
import pandas as pd
import requests

BASE_URL = "https://api.jolpi.ca/ergast/f1"
RAW_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw")

REQUEST_DELAY = 1.5  # seconds between API calls to avoid rate limiting
MAX_RETRIES = 3


def _api_get(url):
    """Make a GET request with retry logic for rate limiting."""
    for attempt in range(MAX_RETRIES):
        response = requests.get(url, timeout=30)
        if response.status_code == 429:
            wait = 2 ** (attempt + 1)
            print(f"  Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        response.raise_for_status()
        return response.json()
    response.raise_for_status()


def fetch_race_results(start_year=2014, end_year=2024):
    """Fetch race results from Ergast API for the given year range.

    Returns a DataFrame with columns: season, round, circuitId, driverCode,
    givenName, familyName, constructorName, grid, position, points, status, laps.
    """
    all_results = []

    for year in range(start_year, end_year + 1):
        offset = 0
        while True:
            url = f"{BASE_URL}/{year}/results.json?limit=100&offset={offset}"
            data = _api_get(url)

            races = data["MRData"]["RaceTable"]["Races"]
            if not races:
                break

            for race in races:
                for result in race["Results"]:
                    all_results.append({
                        "season": int(race["season"]),
                        "round": int(race["round"]),
                        "raceName": race["raceName"],
                        "circuitId": race["Circuit"]["circuitId"],
                        "driverCode": result["Driver"].get("code", "N/A"),
                        "givenName": result["Driver"]["givenName"],
                        "familyName": result["Driver"]["familyName"],
                        "driverId": result["Driver"]["driverId"],
                        "constructorName": result["Constructor"]["name"],
                        "constructorId": result["Constructor"]["constructorId"],
                        "grid": int(result["grid"]),
                        "position": int(result["position"]) if result["position"].isdigit() else None,
                        "points": float(result["points"]),
                        "status": result["status"],
                        "laps": int(result["laps"]),
                    })

            offset += 100
            if offset >= int(data["MRData"]["total"]):
                break
            time.sleep(REQUEST_DELAY)

        print(f"Fetched {year} race results")

    return pd.DataFrame(all_results)


def fetch_qualifying_results(start_year=2014, end_year=2024):
    """Fetch qualifying results from Ergast API for the given year range.

    Returns a DataFrame with columns: season, round, circuitId, driverCode,
    constructorName, qualifyingPosition, Q1, Q2, Q3.
    """
    all_results = []

    for year in range(start_year, end_year + 1):
        offset = 0
        while True:
            url = f"{BASE_URL}/{year}/qualifying.json?limit=100&offset={offset}"
            data = _api_get(url)

            races = data["MRData"]["RaceTable"]["Races"]
            if not races:
                break

            for race in races:
                for result in race["QualifyingResults"]:
                    all_results.append({
                        "season": int(race["season"]),
                        "round": int(race["round"]),
                        "circuitId": race["Circuit"]["circuitId"],
                        "driverCode": result["Driver"].get("code", "N/A"),
                        "driverId": result["Driver"]["driverId"],
                        "constructorName": result["Constructor"]["name"],
                        "qualifyingPosition": int(result["position"]),
                        "Q1": result.get("Q1", None),
                        "Q2": result.get("Q2", None),
                        "Q3": result.get("Q3", None),
                    })

            offset += 100
            if offset >= int(data["MRData"]["total"]):
                break
            time.sleep(REQUEST_DELAY)

        print(f"Fetched {year} qualifying results")

    return pd.DataFrame(all_results)


def fetch_circuits():
    """Fetch all circuit information from Ergast API.

    Returns a DataFrame with columns: circuitId, circuitName, country, lat, lng.
    """
    url = f"{BASE_URL}/circuits.json?limit=100"
    data = _api_get(url)

    circuits = []
    for circuit in data["MRData"]["CircuitTable"]["Circuits"]:
        circuits.append({
            "circuitId": circuit["circuitId"],
            "circuitName": circuit["circuitName"],
            "country": circuit["Location"]["country"],
            "lat": float(circuit["Location"]["lat"]),
            "lng": float(circuit["Location"]["long"]),
        })

    return pd.DataFrame(circuits)


def save_raw_data(race_df, qualifying_df, circuits_df):
    """Save raw DataFrames to CSV in the data/raw/ directory."""
    os.makedirs(RAW_DATA_DIR, exist_ok=True)
    race_df.to_csv(os.path.join(RAW_DATA_DIR, "race_results.csv"), index=False)
    qualifying_df.to_csv(os.path.join(RAW_DATA_DIR, "qualifying_results.csv"), index=False)
    circuits_df.to_csv(os.path.join(RAW_DATA_DIR, "circuits.csv"), index=False)
    print(f"Saved raw data to {RAW_DATA_DIR}")


def load_race_results():
    """Load race results from saved CSV."""
    return pd.read_csv(os.path.join(RAW_DATA_DIR, "race_results.csv"))


def load_qualifying_results():
    """Load qualifying results from saved CSV."""
    return pd.read_csv(os.path.join(RAW_DATA_DIR, "qualifying_results.csv"))


def load_circuits():
    """Load circuit information from saved CSV."""
    return pd.read_csv(os.path.join(RAW_DATA_DIR, "circuits.csv"))


def get_data_summary():
    """Print an overview of all available datasets."""
    for name, loader in [("Race Results", load_race_results),
                         ("Qualifying", load_qualifying_results),
                         ("Circuits", load_circuits)]:
        try:
            df = loader()
            print(f"\n{name}: {df.shape[0]} rows, {df.shape[1]} columns")
            print(f"  Columns: {list(df.columns)}")
        except FileNotFoundError:
            print(f"\n{name}: NOT FOUND — run data collection first")
