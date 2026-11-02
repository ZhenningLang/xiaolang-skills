"""Arithmetic-normalized adjusted mutual information with fixed margins."""

from collections import Counter
from math import comb, fsum, log


def partition_labels(groups, idea_ids):
    """Align a strict partition to an explicit universe; never take intersections."""
    if not isinstance(groups, list) or any(not isinstance(group, list) or not group for group in groups):
        raise ValueError("Partition must contain nonempty groups")
    labels = {}
    for index, group in enumerate(groups):
        for item in group:
            if not isinstance(item, str) or item in labels:
                raise ValueError("Partition has duplicate or invalid idea IDs")
            labels[item] = index
    if set(labels) != set(idea_ids) or len(set(idea_ids)) != len(idea_ids):
        raise ValueError("Partition must cover exactly the same idea IDs")
    return [labels[item] for item in idea_ids]


def adjusted_mutual_info(left, right):
    """Return AMI, retaining negative values and handling equivalent degeneracies."""
    if len(left) != len(right):
        raise ValueError("Partitions have different lengths")
    n = len(left)
    if n == 0:
        return 1.0
    a, b, cells = Counter(left), Counter(right), Counter(zip(left, right))
    if len(a) == len(b) == len(cells):
        return 1.0
    if len(a) == 1 or len(b) == 1:
        return 0.0
    entropy_a = -fsum(count / n * log(count / n) for count in a.values())
    entropy_b = -fsum(count / n * log(count / n) for count in b.values())
    mi = fsum(count / n * log(n * count / (a[x] * b[y])) for (x, y), count in cells.items())
    # ceiling: exact hypergeometric sums suit exam-sized partitions; cache margins if profiling warrants it.
    expected = fsum(
        multiplicity_a * multiplicity_b * fsum(
            (comb(size_a, k) * comb(n - size_a, size_b - k) / comb(n, size_b))
            * k / n * log(n * k / (size_a * size_b))
            for k in range(max(1, size_a + size_b - n), min(size_a, size_b) + 1))
        for size_a, multiplicity_a in Counter(a.values()).items()
        for size_b, multiplicity_b in Counter(b.values()).items())
    denominator = (entropy_a + entropy_b) / 2 - expected
    return (mi - expected) / denominator
