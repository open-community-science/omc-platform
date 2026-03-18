"""Shared data loading utilities for Marimo notebooks."""

import gzip
import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
VIZ_DATA_DIR = Path("/app/viz/data")


def load_viz_json(name: str):
    """Load a preprocessed viz JSON file (tries .gz first)."""
    gz = VIZ_DATA_DIR / f"{name}.json.gz"
    plain = VIZ_DATA_DIR / f"{name}.json"
    if gz.exists():
        with gzip.open(gz, "rt") as f:
            return json.load(f)
    if plain.exists():
        with open(plain) as f:
            return json.load(f)
    return None


def load_tsv(path: str, sep="\t"):
    """Load a TSV/CSV file from pipeline results as a DataFrame."""
    import pandas as pd
    fp = DATA_DIR / path
    if not fp.exists():
        return None
    return pd.read_csv(fp, sep=sep)


def fmt_bp(bp: int) -> str:
    """Format base pairs with K/M/G suffix."""
    if bp >= 1e9:
        return f"{bp/1e9:.1f} Gbp"
    if bp >= 1e6:
        return f"{bp/1e6:.1f} Mbp"
    if bp >= 1e3:
        return f"{bp/1e3:.1f} Kbp"
    return f"{bp} bp"
