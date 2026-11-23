"""PAPER_ONLY Polymarket US 15-minute crypto Up/Down bot.

Prices each window's Up contract from the settlement index (CF Benchmarks BRTI,
published by Polymarket at 1-second resolution) with Coinbase as a live
cross-check, buys the side that is cheaper than its fair value after fees, and
journals simulated fills and settlements. It never places a real order.
"""
