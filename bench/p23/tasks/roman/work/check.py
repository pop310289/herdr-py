"""Visible check for roman: a few values (the hidden grader tests many more)."""
import os
import sys

sys.path.insert(0, os.getcwd())
try:
    from roman import to_roman
except Exception as exc:
    print("FAIL: cannot import to_roman from roman.py: %s" % exc); sys.exit(1)
for n, want in ((1, "I"), (4, "IV"), (9, "IX"), (14, "XIV"), (40, "XL"), (1994, "MCMXCIV")):
    try:
        got = to_roman(n)
    except Exception as exc:
        print("FAIL: to_roman(%d) raised %s" % (n, exc)); sys.exit(1)
    if got != want:
        print("FAIL: to_roman(%d) = %r, expected %r" % (n, got, want)); sys.exit(1)
try:
    to_roman(0)
    print("FAIL: to_roman(0) should raise ValueError"); sys.exit(1)
except ValueError:
    pass
print("PASS")
