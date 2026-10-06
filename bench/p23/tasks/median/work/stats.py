def mean(xs):
    return sum(xs) / len(xs)


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2]
