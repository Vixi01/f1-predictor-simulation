"""Utility and helper functions."""

import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_data_path(*parts):
    """Get absolute path to a file in the data directory."""
    return os.path.join(PROJECT_ROOT, "data", *parts)


def get_output_path(*parts):
    """Get absolute path to a file in the outputs directory."""
    return os.path.join(PROJECT_ROOT, "outputs", *parts)
