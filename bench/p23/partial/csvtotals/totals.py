import csv
import json
totals = {}
with open("sales.csv", newline="") as f:
    for row in csv.DictReader(f):
        if not row.get("region"):
            continue
        try:
            amount = float(row["amount"])
        except ValueError:
            continue
        totals[row["region"]] = totals.get(row["region"], 0.0) + amount
json.dump({k: round(v, 2) for k, v in sorted(totals.items())}, open("totals.json", "w"))
