"""Judge for packing_task.md: N circles in the unit square, checked exactly; the score is the sum of the radii.

    python3 packing_judge.py ANSWER_FILE [--n 26]

Prints one JSON line {"status", "score", "detail"} and exits 0 (herdr_py.coop --judge-mode json). An answer that
breaks a rule is "invalid": the judge itself worked. Every check uses exact fractions of the numbers as written, so
there is no tolerance to exploit. Standard library only.
"""
import argparse
import json
import math
import sys
from fractions import Fraction


def judge(text, n=26):
    try:
        circles = json.loads(text)
    except ValueError as exc:
        return "invalid", None, f"not JSON: {exc}"
    if not isinstance(circles, list) or len(circles) != n:
        return "invalid", None, f"give a list of {n} [x, y, r]"
    exact = []
    for i, c in enumerate(circles):
        if not (isinstance(c, list) and len(c) == 3 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in c)):
            return "invalid", None, f"circle {i}: [x, y, r] with three finite numbers"
        x, y, r = (Fraction(v) for v in c)
        if r <= 0:
            return "invalid", None, f"circle {i}: the radius must be positive"
        if x - r < 0 or x + r > 1 or y - r < 0 or y + r > 1:
            return "invalid", None, f"circle {i} is not inside the square"
        exact.append((x, y, r))
    for i in range(n):
        xi, yi, ri = exact[i]
        for j in range(i + 1, n):
            xj, yj, rj = exact[j]
            if (xi - xj) ** 2 + (yi - yj) ** 2 < (ri + rj) ** 2:
                return "invalid", None, f"circles {i} and {j} overlap"
    score = float(sum(r for _, _, r in exact))
    return "valid", score, f"{n} circles inside the square, none overlapping; sum of radii {score:.6f}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("answer")
    ap.add_argument("--n", type=int, default=26)
    a = ap.parse_args(argv)
    with open(a.answer, encoding="utf-8", errors="replace") as handle:
        status, score, detail = judge(handle.read(), a.n)
    print(json.dumps({"status": status, "score": score, "detail": detail}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
