#!/usr/bin/env python3
"""Run every implementation for the frozen 3.3 matrix on one CPU allocation."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from codec_ai_training_3_3_main_table_matrix import grouped_matrix


IMPLEMENTATIONS = (
    "pyav_native",
    "decord_native",
    "torchcodec_native",
    "ours_native",
    "lerobot_v3_torchcodec",
    "jpeg_lance",
    "ours_fast_x264",
    "ours_fast_x265",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-script", type=Path, required=True)
    parser.add_argument("--cpu-limit", type=int, required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--credential-file", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    commands: list[list[str]] = []
    configs = grouped_matrix(args.cpu_limit, args.group)
    for config in configs:
        for implementation in IMPLEMENTATIONS:
            command = [
                sys.executable,
                str(args.benchmark_script),
                "--protocol",
                "sensitivity",
                "--config-id",
                config.config_id,
                "--implementation",
                implementation,
                "--sampling-mode",
                config.sampling_mode,
                "--credential-file",
                args.credential_file,
                "--endpoint",
                args.endpoint,
                "--output-root",
                args.output_root,
                "--workers",
                str(config.workers),
                "--cpu-limit",
                str(config.cpu_limit),
                "--batch-size",
                str(config.batch_size),
                "--warmup-steps",
                "10",
                "--steps",
                "100",
                "--seed",
                "20260729",
                "--window",
                str(config.window),
                "--random-span",
                str(config.random_span),
                "--sampling-stride",
                str(config.sampling_stride),
                "--prefetch-factor",
                "1",
                "--progress-every",
                "25",
                "--resume-existing",
            ]
            commands.append(command)

    print(
        f"3.3 matrix cpu={args.cpu_limit} group={args.group} configs={len(configs)} "
        f"reports={len(commands)} measured_steps_per_report=100",
        flush=True,
    )
    for index, command in enumerate(commands, 1):
        print(f"[{index}/{len(commands)}] {' '.join(command)}", flush=True)
        if not args.dry_run:
            subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
