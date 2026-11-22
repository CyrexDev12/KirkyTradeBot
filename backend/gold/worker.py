"""Run the gold paper desk: ``python -m backend.gold.worker`` (writes data/gold/health.json)."""
import asyncio
import logging

from ..updown.worker import serve
from . import SETTINGS, data_directory

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(data_directory(), SETTINGS, "Gold"))
