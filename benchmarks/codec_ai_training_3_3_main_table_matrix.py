#!/usr/bin/env python3
"""Frozen 3.3 sensitivity matrix using the main-table native-MP4 baseline."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Config:
    config_id: str
    family: str
    value: str
    cpu_limit: int
    workers: int = 32
    batch_size: int = 8
    window: int = 10
    sampling_mode: str = "continuous"
    random_span: int = 0
    sampling_stride: int = 0

    @property
    def effective_stride(self) -> int | None:
        if self.sampling_mode == "random10":
            return None
        return self.sampling_stride or 1

    @property
    def temporal_span(self) -> int | None:
        if self.sampling_mode == "random10":
            return self.random_span or None
        return (self.window - 1) * int(self.effective_stride) + 1


def matrix(cpu_limit: int) -> list[Config]:
    if cpu_limit not in {8, 16, 32}:
        raise ValueError("3.3 CPU limit must be one of 8, 16, 32")
    baseline = Config("baseline", "baseline", "main_table", cpu_limit)
    if cpu_limit != 16:
        return [baseline]

    configs = [
        Config(f"frames{frames:02d}", "frames", str(frames), cpu_limit, window=frames)
        for frames in (2, 4, 6, 8)
    ]
    configs.append(baseline)
    configs.extend(
        Config(f"frames{frames:02d}", "frames", str(frames), cpu_limit, window=frames)
        for frames in (12, 16, 20)
    )
    configs.extend(
        Config(
            f"stride{stride:02d}",
            "stride",
            str(stride),
            cpu_limit,
            sampling_stride=stride,
        )
        for stride in (2, 3, 4, 5)
    )
    configs.extend(
        Config(
            "random_full" if span == 0 else f"random{span:03d}",
            "random_span",
            "full" if span == 0 else str(span),
            cpu_limit,
            sampling_mode="random10",
            random_span=span,
        )
        for span in (32, 64, 100, 0)
    )
    configs.extend(
        Config(f"batch{batch:02d}", "batch_size", str(batch), cpu_limit, batch_size=batch)
        for batch in (2, 4, 12, 16)
    )
    configs.extend(
        Config(f"workers{workers:02d}", "workers", str(workers), cpu_limit, workers=workers)
        for workers in (8, 16, 24, 40, 48, 64)
    )
    if len(configs) != 26 or len({config.config_id for config in configs}) != len(configs):
        raise AssertionError("unexpected 3.3 matrix cardinality")
    return configs


def grouped_matrix(cpu_limit: int, group: str) -> list[Config]:
    configs = matrix(cpu_limit)
    if group == "all":
        return configs
    if cpu_limit != 16:
        if group != "baseline":
            raise ValueError("CPU 8/32 only support the baseline group")
        return configs
    families = {
        "frames": {"baseline", "frames"},
        "access": {"stride", "random_span"},
        "batch": {"batch_size"},
        "workers": {"workers"},
    }
    if group not in families:
        raise ValueError(f"unsupported CPU16 group: {group}")
    return [config for config in configs if config.family in families[group]]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu-limit", type=int, required=True)
    parser.add_argument("--group", default="all")
    args = parser.parse_args()
    for config in grouped_matrix(args.cpu_limit, args.group):
        payload = asdict(config)
        payload.update(effective_stride=config.effective_stride, temporal_span=config.temporal_span)
        print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
