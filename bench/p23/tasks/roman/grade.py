import sys

sys.path.insert(0, sys.argv[1])
try:
    from roman import to_roman
except Exception as exc:
    print("import failed: %s" % exc); sys.exit(1)


def ref(n):
    out = ""
    for v, s in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= v:
            out += s
            n -= v
    return out


for n in list(range(1, 60)) + [90, 400, 444, 900, 999, 2024, 3888, 3999]:
    try:
        got = to_roman(n)
    except Exception as exc:
        print("to_roman(%d) raised %s" % (n, exc)); sys.exit(1)
    if got != ref(n):
        print("to_roman(%d) = %r, expected %r" % (n, got, ref(n))); sys.exit(1)
for bad in (0, 4000, -1, 2.5, "5", None):
    try:
        to_roman(bad)
    except ValueError:
        continue
    except Exception as exc:
        print("to_roman(%r) raised %s, expected ValueError" % (bad, type(exc).__name__)); sys.exit(1)
    print("to_roman(%r) did not raise" % (bad,)); sys.exit(1)
print("pass")
