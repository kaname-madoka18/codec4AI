"""Compute exact expectations for the infinite periodic model in Section 3.3.

The start phase is uniform over ALL intra-period positions, including the
intra anchor. Each request starts with an empty cache. Sequential decodes the
coding-order prefix of each requested intra period, skipping untouched periods
and unused tails. Shared boundary anchors count once per request. We generate
enough periods to cover every target and reference, so no request is clipped
by a finite video boundary. Fractions keep the phase average exact.

Run with Python 3; no third-party packages are required. The default command
writes three CSV files to the requested output directory.
All positive integer periods are supported by recursive integer-midpoint splits;
period one contains only intra anchors, and period zero is undefined.
The LeRobot reference is modeled as LDP with a fixed two-frame I/P GOP.
It seeks to each requested GOP, decodes through that GOP's last target,
and averages its own two start phases, independently of the swept period.
"""

import argparse
import csv
from bisect import bisect_right
from fractions import Fraction
from pathlib import Path


def hierarchical_stream(period: int, intervals: int):
    """Return the recursive midpoint hierarchy for any positive intra period."""
    if period < 1:
        raise ValueError("The intra period must be a positive integer.")
    references = {0: set()}
    coded_order = [0]

    def add_interior(left, right):
        if right - left <= 1:
            return
        midpoint = (left + right) // 2
        references[midpoint] = {left, right}
        coded_order.append(midpoint)
        add_interior(left, midpoint)
        add_interior(midpoint, right)

    for left in range(0, period * intervals, period):
        right = left + period
        references[right] = set()
        coded_order.append(right)
        add_interior(left, right)
    return references, coded_order


def dependency_closure(targets, references):
    closure = set(targets)
    pending = list(targets)
    while pending:
        for reference in references[pending.pop()]:
            if reference not in closure:
                closure.add(reference)
                pending.append(reference)
    return closure


def reconstruction_counts(targets, references, coded_order):
    """Count distinct reconstructed pictures under the three schedules."""
    rank = {frame: index for index, frame in enumerate(coded_order)}
    closure = dependency_closure(targets, references)
    anchors = {frame for frame, refs in references.items() if not refs}
    ordered_anchors = sorted(anchors)

    # An intra target needs only itself. Each interval containing interior
    # targets needs its two anchors and its own coding-order prefix; it must
    # not include interiors from earlier or untouched intervals.
    last_packet_by_interval = {}
    for frame in targets - anchors:
        interval = bisect_right(ordered_anchors, frame) - 1
        last_packet_by_interval[interval] = max(
            last_packet_by_interval.get(interval, -1), rank[frame]
        )
    prefix = targets & anchors
    for frame in coded_order:
        if frame in anchors:
            continue
        interval = bisect_right(ordered_anchors, frame) - 1
        if rank[frame] <= last_packet_by_interval.get(interval, -1):
            prefix.add(frame)
            prefix.update(ordered_anchors[interval:interval + 2])

    # Under DDRA, the only dependencies are the two bounding intra anchors.
    ddra_closure = targets | (closure & anchors)
    assert targets <= ddra_closure <= closure <= prefix
    return len(prefix), len(closure), len(ddra_closure)


def mean_counts(period: int, count: int, stride: int):
    """Exact means over all start phases of an infinite periodic video."""
    if period < 1:
        raise ValueError("The intra period must be a positive integer.")
    if count < 1 or stride < 1:
        raise ValueError("Frame count and stride must be positive integers.")
    max_target = period - 1 + (count - 1) * stride
    intervals = max(1, (max_target + period - 1) // period)
    references, coded_order = hierarchical_stream(period, intervals)
    rank = {frame: index for index, frame in enumerate(coded_order)}
    assert len(rank) == len(coded_order)
    assert all(rank[r] < rank[f] for f, refs in references.items() for r in refs)
    totals = [0, 0, 0]

    for start in range(period):
        targets = {start + offset * stride for offset in range(count)}
        for column, value in enumerate(
            reconstruction_counts(targets, references, coded_order)
        ):
            totals[column] += value

    return tuple(Fraction(total, period) for total in totals)


def lerobot_reconstruction_count(targets):
    """Count the I/P GOP prefixes needed by an ideal LDP, GOP=2 loader."""
    last_target_by_gop = {}
    for target in targets:
        anchor = target - target % 2
        last_target_by_gop[anchor] = max(
            last_target_by_gop.get(anchor, anchor), target
        )
    return sum(last - anchor + 1 for anchor, last in last_target_by_gop.items())


def mean_lerobot_count(count: int, stride: int):
    """Average both phases of the fixed I/P structure, with no cached frames."""
    if count < 1 or stride < 1:
        raise ValueError("Frame count and stride must be positive integers.")
    total = 0
    for phase in range(2):
        targets = {phase + index * stride for index in range(count)}
        total += lerobot_reconstruction_count(targets)
    return Fraction(total, 2)


def sweep_rows(period: int, variable: str):
    """Exact expectations for the requested stride, count, or period sweep."""
    if variable not in ("stride", "frames", "period"):
        raise ValueError("The sweep variable must be 'stride', 'frames', or 'period'.")
    values = range(1, 33) if variable == "period" else range(1, 31)
    for value in values:
        active_period = value if variable == "period" else period
        if variable == "period":
            count, stride = 10, 4
        else:
            count, stride = (10, value) if variable == "stride" else (value, 4)
        means = mean_counts(active_period, count, stride) + (
            mean_lerobot_count(count, stride),
        )
        row = {"x": value, "period": active_period, "frames": count, "stride": stride,
               "lerobot_gop": 2}
        for method, mean in zip(("sequential", "sparse", "ddra", "lerobot"), means):
            row[f"{method}_count"] = f"{float(mean):.10f}"
            row[f"{method}_amp"] = f"{float(mean / count):.10f}"
            row[f"{method}_count_exact"] = str(mean)
        yield row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", type=int, default=32)
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("outputs/analysis"),
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    print("sweep   T_I   K   stride   E[A_seq]   E[A_sparse]   E[A_DDRA]   E[A_LeRobot]")
    for variable in ("stride", "frames", "period"):
        rows = list(sweep_rows(args.period, variable))
        path = args.output_dir / f"theoretical_amplification_{variable}.csv"
        with path.open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        for row in (rows[0], rows[-1]):
            print(
                f"{variable:6} {row['period']:3} {row['frames']:3} {row['stride']:8}   "
                + "   ".join(
                    f"{float(row[f'{method}_amp']):10.6f}"
                    for method in ("sequential", "sparse", "ddra", "lerobot")
                )
            )
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
