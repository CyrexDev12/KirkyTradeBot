"""PAPER_ONLY gold desk: Polymarket US "Gold Up or Down" 5-minute, 15-minute and
hourly windows, run on the shared Up/Down engine (``backend.updown``) with the
gold settlement rule (single Chainlink XAU/USD price at the window start and end)
and Coinbase PAXG-USD as the cross-check. Its journal lives in ``data/gold``.
"""
import os
from pathlib import Path

from ..updown.strategy import Settings

SETTINGS = Settings.from_env(prefix="GOLD")


def data_directory() -> Path:
    default = Path(__file__).resolve().parents[2] / "data"
    return Path(os.getenv("NFL_DATA_DIR", default)) / "gold"
