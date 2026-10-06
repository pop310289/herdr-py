"""Visible check for csvtotals: totals.json exists, is JSON, has sorted keys and every region (values are not checked)."""
import json
import os
import sys

if not os.path.exists("totals.json"):
    print("FAIL: totals.json does not exist (run totals.py)"); sys.exit(1)
try:
    data = json.load(open("totals.json"))
except ValueError as exc:
    print("FAIL: totals.json is not valid JSON: %s" % exc); sys.exit(1)
if not isinstance(data, dict):
    print("FAIL: totals.json must be a JSON object"); sys.exit(1)
if set(data) != {"North", "South", "East", "West"}:
    print("FAIL: regions should be North, South, East, West; got %s" % sorted(data)); sys.exit(1)
if list(data) != sorted(data):
    print("FAIL: keys are not sorted"); sys.exit(1)
print("PASS")
