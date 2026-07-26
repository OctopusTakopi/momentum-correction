"""Path configuration for the momentum-correction subproject."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.environ.get("MOMENTUM_CORRECTION_DATA_DIR", ROOT / "data")
)
RESULTS_DIR = Path(
    os.environ.get("MOMENTUM_CORRECTION_RESULTS_DIR", ROOT / "results")
)
RAW_DIR = DATA_DIR / "raw"
PARQUET_DIR = DATA_DIR / "parquet"
FIGURES_DIR = RESULTS_DIR / "figures"


def ensure_directories() -> None:
    for path in (DATA_DIR, RAW_DIR, PARQUET_DIR, RESULTS_DIR, FIGURES_DIR):
        path.mkdir(parents=True, exist_ok=True)
