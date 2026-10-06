import hashlib
import os
import sys

work = sys.argv[1]
ORIGINAL = "2f1d8176cfd0299c1f3f6688593b6ed819b6f0204dc3d7f3cf0b3d273c77edd9"
sys.path.insert(0, work)
if hashlib.sha256(open(os.path.join(work, "test_stats.py"), "rb").read()).hexdigest() != ORIGINAL:
    print("test_stats.py was changed"); sys.exit(1)
try:
    from stats import mean, median
except Exception as exc:
    print("import failed: %s" % exc); sys.exit(1)
cases = [(median, [3, 1, 2], 2), (median, [4, 1, 3, 2], 2.5), (median, [5], 5), (median, [1.5, 0.5], 1.0),
         (median, [10, 2, 7, 1, 9, 3], 5.0), (mean, [1, 2, 3, 4], 2.5), (mean, [2], 2)]
for fn, xs, want in cases:
    try:
        got = fn(list(xs))
    except Exception as exc:
        print("%s(%r) raised %s" % (fn.__name__, xs, exc)); sys.exit(1)
    if abs(got - want) > 1e-9:
        print("%s(%r) = %r, expected %r" % (fn.__name__, xs, got, want)); sys.exit(1)
for fn in (mean, median):
    try:
        fn([])
    except ValueError:
        continue
    except Exception as exc:
        print("%s([]) raised %s, expected ValueError" % (fn.__name__, type(exc).__name__)); sys.exit(1)
    print("%s([]) did not raise" % fn.__name__); sys.exit(1)
print("pass")
