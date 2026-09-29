"""Independent analytic checks of the theoretical figure's expectations."""

import unittest
from fractions import Fraction

from decode_amplification import (
    hierarchical_stream,
    lerobot_reconstruction_count,
    mean_counts,
    mean_lerobot_count,
    reconstruction_counts,
    sweep_rows,
)


class DecodeAmplificationTests(unittest.TestCase):
    def test_single_frame_closed_forms_include_anchor_phase(self):
        for period in range(1, 65):
            depth = period.bit_length() - 1
            # Coded ranks of all phase targets are 0 and 2,...,period;
            # the right boundary (rank 1) is outside the start phase set.
            sequential = Fraction(period + 3, 2) - Fraction(1, period)
            # Full levels 1,...,L have 2^(l-1) midpoints; T-2^L
            # remaining nodes are at level L+1. A depth-l closure has l+2
            # pictures. This also covers T=1, where the anchor is the target.
            sparse = depth + 3 - Fraction(2 ** (depth + 1), period)
            ddra = 3 - Fraction(2, period)
            self.assertEqual(
                mean_counts(period, 1, 1), (sequential, sparse, ddra)
            )

    def test_ddra_closed_form_on_its_valid_stride_range(self):
        # For 1 <= d <= T, targets touch consecutive intra periods.
        # The expectation of bounding anchors, including target anchors, is
        # 2 + ((K-1)d-1)/T; K/T of those anchors are themselves requested.
        for variable in ("stride", "frames", "period"):
            for row in sweep_rows(32, variable):
                count, stride, period = row["frames"], row["stride"], row["period"]
                if stride > period:
                    continue
                expected = count + 2 + Fraction(
                    (count - 1) * stride - count - 1, period
                )
                self.assertEqual(Fraction(row["ddra_count_exact"]), expected)

    def test_appending_periods_does_not_change_request_cost(self):
        targets = {31 + index * 30 for index in range(10)}
        short = reconstruction_counts(targets, *hierarchical_stream(32, 10))
        extended = reconstruction_counts(targets, *hierarchical_stream(32, 40))
        self.assertEqual(short, extended)

    def test_counts_on_a_small_hand_derived_graph(self):
        # Coded order: 0,4,2,1,3,8,6,5,7.
        graph = hierarchical_stream(4, 2)
        cases = (
            ({0}, (1, 1, 1)),
            ({2}, (3, 3, 3)),
            ({1, 3}, (5, 5, 4)),
            ({0, 1, 4}, (4, 4, 3)),
            ({1, 5}, (7, 7, 5)),
        )
        for targets, expected in cases:
            self.assertEqual(reconstruction_counts(targets, *graph), expected)

    def test_sequential_skips_untouched_periods_and_unused_tails(self):
        graph = hierarchical_stream(4, 4)
        cases = (
            # A boundary target does not request the preceding B pictures.
            ({4}, (1, 1, 1)),
            ({0, 8, 16}, (3, 3, 3)),
            # The previous interval's unused tail is absent from this prefix.
            ({5}, (4, 4, 3)),
            # Only the prefixes [0,4,2,1] and [12,16,14,13] are decoded.
            ({1, 13}, (8, 8, 6)),
            # Adjacent prefixes share anchor 4, which must count only once.
            ({3, 4, 5}, (8, 7, 5)),
        )
        for targets, expected in cases:
            with self.subTest(targets=targets):
                self.assertEqual(reconstruction_counts(targets, *graph), expected)

    def test_widely_spaced_targets_have_additive_reconstruction_cost(self):
        # At stride 2T, each target's interval has disjoint bounding anchors.
        # No schedule should pay for the untouched interval between targets.
        for period in (1, 2, 3, 4, 8, 13, 32):
            single = mean_counts(period, 1, 1)
            self.assertEqual(
                mean_counts(period, 10, 2 * period),
                tuple(10 * value for value in single),
            )

    def test_bounds_hold_for_every_phase_in_all_three_sweeps(self):
        for variable in ("stride", "frames", "period"):
            for row in sweep_rows(32, variable):
                count, stride, period = row["frames"], row["stride"], row["period"]
                last = period - 1 + (count - 1) * stride
                graph = hierarchical_stream(
                    period, max(1, (last + period - 1) // period)
                )
                for phase in range(period):
                    targets = {phase + index * stride for index in range(count)}
                    sequential, sparse, ddra = reconstruction_counts(targets, *graph)
                    self.assertLessEqual(count, ddra)
                    self.assertLessEqual(ddra, 3 * count)
                    self.assertLessEqual(ddra, sparse)
                    self.assertLessEqual(sparse, sequential)

    def test_all_intra_video_and_zero_period(self):
        self.assertEqual(mean_counts(1, 10, 4), (Fraction(10),) * 3)
        with self.assertRaises(ValueError):
            mean_counts(0, 10, 4)

    def test_odd_period_hand_derived_graph(self):
        # Coded order: 0,3,1,2,6,4,5; odd splits use the lower midpoint.
        graph = hierarchical_stream(3, 2)
        self.assertEqual(graph[1], [0, 3, 1, 2, 6, 4, 5])
        self.assertEqual(reconstruction_counts({2}, *graph), (4, 4, 3))
        self.assertEqual(reconstruction_counts({1, 5}, *graph), (6, 6, 5))

    def test_period_sweep_covers_all_positive_integer_periods(self):
        rows = list(sweep_rows(32, "period"))
        self.assertEqual([row["period"] for row in rows], list(range(1, 33)))
        self.assertTrue(all(row["frames"] == 10 and row["stride"] == 4 for row in rows))

    def test_lerobot_gop_prefixes_and_shared_reconstruction(self):
        cases = (
            ({0}, 1),
            ({1}, 2),
            ({0, 1}, 2),
            ({1, 2}, 3),
            ({1, 3}, 4),
            ({0, 4, 8}, 3),
            ({1, 5, 9}, 6),
        )
        for targets, expected in cases:
            self.assertEqual(lerobot_reconstruction_count(targets), expected)

    def test_lerobot_matches_independent_phase_expectations(self):
        for count in range(1, 31):
            for stride in range(1, 31):
                # Contiguous requests can need only the first P's extra I.
                # With d>=2 no P's immediately preceding I is requested;
                # each target is P with probability one half.
                expected = count + Fraction(1, 2) if stride == 1 else Fraction(3 * count, 2)
                self.assertEqual(mean_lerobot_count(count, stride), expected)

    def test_lerobot_gop_remains_fixed_during_period_sweep(self):
        rows = list(sweep_rows(32, "period"))
        self.assertTrue(all(row["lerobot_gop"] == 2 for row in rows))
        self.assertTrue(
            all(Fraction(row["lerobot_count_exact"]) == 15 for row in rows)
        )
        # Odd swept periods must not bias the fixed two-phase LDP average.
        self.assertEqual(rows[2]["lerobot_amp"], "1.5000000000")


if __name__ == "__main__":
    unittest.main()
