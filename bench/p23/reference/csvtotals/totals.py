import csv
import json
totals = {}
with open("sales.csv", newline="") as f:
    for row in csv.DictReader(f):
        if not row.get("region"):
            continue
        totals[row["region"]] = totals.get(row["region"], 0.0) + float(row["amount"].replace(",", ""))
json.dump({k: round(v, 2) for k, v in totals.items()}, open("totals.json", "w"), sort_keys=True)
