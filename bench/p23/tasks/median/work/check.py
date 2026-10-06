"""Visible check for median: runs the unit tests (it does not test empty lists)."""
import subprocess
import sys

p = subprocess.run([sys.executable, "-m", "unittest", "test_stats"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
print(p.stdout[-1500:])
print("PASS" if p.returncode == 0 else "FAIL: python3 -m unittest test_stats did not pass")
sys.exit(p.returncode)
