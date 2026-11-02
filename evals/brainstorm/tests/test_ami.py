"""AMI oracles use exhaustive standard-library permutations, not sklearn."""

from collections import Counter
from itertools import permutations
import math
import unittest

from evals.brainstorm.ami import adjusted_mutual_info, partition_labels


def permutation_oracle(left, right):
    n = len(left)
    def entropy(labels):
        return sum(-count / n * math.log(count / n) for count in Counter(labels).values())
    def information(x, y):
        a, b = Counter(x), Counter(y)
        return sum(count / n * math.log(count * n / (a[i] * b[j]))
                   for (i, j), count in Counter(zip(x, y)).items())
    possibilities = set(permutations(right))
    expected = sum(information(left, other) for other in possibilities) / len(possibilities)
    return (information(left, right) - expected) / ((entropy(left) + entropy(right)) / 2 - expected)


class AmiTests(unittest.TestCase):
    def test_crossed_partition_has_negative_adjustment(self):
        self.assertAlmostEqual(adjusted_mutual_info([0, 0, 1, 1], [0, 1, 0, 1]), -.5, places=10)

    def test_nontrivial_constants_and_independent_permutation_expectation(self):
        cases = [([0, 0, 1, 1, 2, 2], [0, 0, 0, 1, 1, 1], .29879245817089006),
                 ([0, 0, 0, 1, 1, 2], [0, 0, 1, 1, 2, 2], .08372678378671221),
                 ([0, 0, 1, 1, 1], [0, 1, 0, 1, 2], None)]
        for left, right, expected in cases:
            with self.subTest(left=left, right=right):
                actual = adjusted_mutual_info(left, right)
                self.assertAlmostEqual(actual, permutation_oracle(left, right), places=10)
                if expected is not None:
                    self.assertAlmostEqual(actual, expected, places=10)
                self.assertAlmostEqual(actual, adjusted_mutual_info(right, left), places=10)
                self.assertAlmostEqual(actual, adjusted_mutual_info(left[::-1], right[::-1]), places=10)
                self.assertAlmostEqual(actual, adjusted_mutual_info([str(x) for x in left], right), places=10)

    def test_degenerate_equivalent_partitions(self):
        for left, right in (([], []), ([0], [2]), ([0, 0], [2, 2]),
                            ([0, 1, 2], [9, 8, 7]), ([0, 0, 1], [8, 8, 9])):
            self.assertEqual(adjusted_mutual_info(left, right), 1)

    def test_one_group_against_multiple_groups(self):
        for other in ([0, 1, 2, 3], [0, 0, 1, 1]):
            self.assertEqual(adjusted_mutual_info([0] * 4, other), 0)
            self.assertEqual(adjusted_mutual_info(other, [0] * 4), 0)
        with self.assertRaises(ValueError):
            adjusted_mutual_info([0], [])

    def test_strict_id_alignment_rejects_incomplete_partitions(self):
        self.assertEqual(partition_labels([["b"], ["c", "a"]], ["a", "b", "c"]), [1, 0, 1])
        for groups in ([], [["a"]], [["a", "a"], ["b"]], [["a", "b", "x"]], [[], ["a", "b"]], ["ab"]):
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                partition_labels(groups, ["a", "b"])

    def test_larger_partition_is_finite(self):
        self.assertTrue(math.isfinite(adjusted_mutual_info([i // 10 for i in range(500)],
                                                         [i % 13 for i in range(500)])))
