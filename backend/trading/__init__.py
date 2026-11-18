"""Paper-trading research subsystem for NFL Fair Value.

Everything in this package is PAPER_ONLY. Market data is read from public
Polymarket US REST routes; orders, fills, exits and settlements are simulated
locally and journaled. No module here submits, cancels or modifies a real
exchange order, and :mod:`backend.trading.venue` refuses any request other
than a GET to an allow-listed public market-data route.
"""
