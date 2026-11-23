"""Read-only natural gas desk API at /api/natgas (same routes as /api/wti)."""
from ..wti.api import make_router
from ..wti.commodity import NATGAS

router = make_router(NATGAS)
