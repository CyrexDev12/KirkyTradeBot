"""Read-only gold paper desk API at /api/gold."""
from ..updown.api import make_router
from . import SETTINGS, data_directory

router = make_router("/api/gold", data_directory, SETTINGS, "Polymarket gold Up/Down paper trading")
