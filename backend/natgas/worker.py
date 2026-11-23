"""Run the natural gas desk: ``python -m backend.natgas.worker`` (writes data/natgas/health.json)."""
import asyncio
import logging

from ..wti.commodity import NATGAS
from ..wti.worker import serve

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(NATGAS))
