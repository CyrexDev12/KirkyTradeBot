"""PAPER_ONLY natural gas desk: Polymarket US "Natural Gas Up or Down" 15-minute, hourly and
daily windows, settled on the Chainlink front-month Henry Hub futures mid (4 decimals).
It runs the same energy desk as WTI crude (``backend.wti``): five competing models with their
own $100 paper accounts, the gas news calendar (EIA storage report, NOAA weather outlooks),
shock and contract-switch detection. Settings use the ``NATGAS_*`` prefix; journals live in
``data/natgas``. See docs/OIL.md (energy desks)."""
from ..wti.commodity import NATGAS

SETTINGS = NATGAS.settings()
MODELS = NATGAS.models()
LEAD_MODEL = NATGAS.lead()


def data_directory():
    return NATGAS.directory()
