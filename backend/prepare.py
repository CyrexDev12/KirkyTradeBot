"""Run `python -m backend.prepare` to cache data, validate, and fit the model."""
import argparse
import json
from .model import train
from .totals import train as train_totals

parser = argparse.ArgumentParser()
parser.add_argument("--refresh", action="store_true", help="Force NFLverse data downloads")
args = parser.parse_args()
print(json.dumps({"moneyline": train(refresh_data=args.refresh), "totals": train_totals()}, indent=2))
