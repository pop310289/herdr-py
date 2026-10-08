"""A team member that is a program, not a model, for packing_task.md: it reads herdr_py.coop's prompt on stdin, takes
the best answer it was shown (or a 5 x 5 grid plus one small circle when there is none), improves it by penalty gradient steps
and prints a reply in the team's format.

    --member 'a=command:python3 examples/coop/packing_member.py --seed 1'

This is how a rule-guided loop you already have becomes a member: read the prompt, run your loop, print the reply
(SUMMARY, PARENTS and the answer in a fenced block). Standard library only.
"""
import argparse
import json
import math
import random
import re
import sys

SHOWN = re.compile(r"^- (k[0-9a-f]{12}) by \S+: score (\S+): [^\n]*\n(```|~~~~)[^\n]*\n(.*?)\n\3", re.M | re.S)
MARGIN = 1e-12  # stay this far inside every limit, so float rounding never breaks an exact check


def start(n):
    """A valid start: a 5 x 5 grid of radius 0.1 and the rest as small circles in the grid's gaps."""
    circles = [[0.1 + 0.2 * i, 0.1 + 0.2 * j, 0.0] for i in range(5) for j in range(5)][:n]
    circles += [[0.2 + 0.2 * i, 0.2 + 0.2 * j, 0.0] for i in range(4) for j in range(4)][:max(0, n - len(circles))]
    for i in range(len(circles)):  # grow one at a time: 0.1 + 0.2 * 4 is 0.9000000000000001 in floats
        circles[i][2] = room(i, circles)
    return circles


def room(i, circles):
    """The largest radius circle i can have where it is, given the walls and every other circle."""
    x, y, _ = circles[i]
    r = min(x, 1 - x, y, 1 - y)
    for j, (xj, yj, rj) in enumerate(circles):
        if j != i:
            r = min(r, math.hypot(x - xj, y - yj) - rj)
    return r - MARGIN


def valid(circles):
    return all(r > 0 and room(i, circles) + MARGIN >= r for i, (_, _, r) in enumerate(circles))


def fit(circles):
    """Shrink every radius until it fits (one pass is enough: shrinking never breaks a rule that already holds), then
    let every circle grow into whatever room is left."""
    for i in range(len(circles)):
        circles[i][2] = min(circles[i][2], room(i, circles))
    for i in range(len(circles)):
        circles[i][2] = max(circles[i][2], room(i, circles))
    return circles


def improve(circles, steps, rng):
    """Jiggle the centres, then gradient steps on the sum of the radii minus a growing penalty for overlaps and for
    leaving the square; fit() makes the result valid. Keep it only if the sum went up."""
    n = len(circles)
    x = [c[0] + rng.gauss(0, 0.01) for c in circles]
    y = [c[1] + rng.gauss(0, 0.01) for c in circles]
    r = [max(c[2], 0.01) for c in circles]
    for step in range(steps):
        k = 10.0 * (1000.0 ** (step / max(1, steps - 1)))  # the penalty grows from 10 to 10000
        rate = min(2e-3, 1.0 / k)  # smaller steps as the penalty stiffens, or the ascent oscillates
        gx, gy, gr = [0.0] * n, [0.0] * n, [1.0] * n
        for i in range(n):
            for v, sign, axis in ((r[i] - x[i], 1, gx), (x[i] + r[i] - 1, -1, gx), (r[i] - y[i], 1, gy), (y[i] + r[i] - 1, -1, gy)):
                if v > 0:
                    gr[i] -= 2 * k * v
                    axis[i] += sign * 2 * k * v
            for j in range(i + 1, n):
                dx, dy = x[i] - x[j], y[i] - y[j]
                d = math.hypot(dx, dy) or 1e-9
                v = r[i] + r[j] - d
                if v > 0:
                    gr[i] -= 2 * k * v
                    gr[j] -= 2 * k * v
                    push = 2 * k * v / d
                    gx[i] += push * dx
                    gy[i] += push * dy
                    gx[j] -= push * dx
                    gy[j] -= push * dy
        for i in range(n):
            x[i] = min(1 - MARGIN, max(MARGIN, x[i] + rate * gx[i]))
            y[i] = min(1 - MARGIN, max(MARGIN, y[i] + rate * gy[i]))
            r[i] = max(0.0, r[i] + rate * gr[i])
    trial = fit([[x[i], y[i], r[i]] for i in range(n)])
    before, after = sum(c[2] for c in circles), sum(c[2] for c in trial)
    if after > before and valid(trial):
        return trial, after
    return [list(c) for c in circles], before


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--steps", type=int, default=3000)
    ap.add_argument("--n", type=int, default=26)
    a = ap.parse_args(argv)
    prompt = sys.stdin.read()
    shown = SHOWN.search(prompt)  # the brief lists the best answer first
    parent, circles = None, None
    if shown:
        try:
            circles = json.loads(shown.group(4))
            parent = shown.group(1)
        except ValueError:
            circles = None
    if not (isinstance(circles, list) and len(circles) == a.n and valid(circles)):
        parent, circles = None, start(a.n)
    turn = re.search(r"This is (?:round|turn) (\d+) of", prompt)
    rng = random.Random(f"{a.seed}:{turn.group(1) if turn else 0}:{parent}")  # the same input gives the same answer
    before = sum(c[2] for c in circles)
    circles, after = improve(circles, a.steps, rng)
    origin = f"{parent}'s answer" if parent else "a 5 x 5 grid"
    print(f"SUMMARY: penalty ascent (seed {a.seed}, {a.steps} steps) from {origin}: sum of radii {before:.6f} -> {after:.6f}")
    print(f"PARENTS: {parent or 'none'}")
    print("```json")
    print(json.dumps(circles))
    print("```")
    return 0


if __name__ == "__main__":
    sys.exit(main())
