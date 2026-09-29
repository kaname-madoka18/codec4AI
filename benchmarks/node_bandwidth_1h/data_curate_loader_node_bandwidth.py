#!/usr/bin/env python3
"""Time-bounded W32 loader benchmark through one node-shared OSS proxy.

The decoder, sampling and frozen-dataset implementation is imported
from ../data_curate_loader_benchmark.py. This adapter
only changes the transport boundary and the measured stopping condition.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import os
import random
import socket
import statistics
import sys
import tempfile
import time
import zlib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
BENCHMARK_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BENCHMARK_DIR))
import data_curate_loader_benchmark as base  # noqa: E402


class ProxyOssStore(base.OssStore):
    def __init__(self, credential_file: str, endpoint: str, proxy_url: str):
        super().__init__(credential_file, endpoint)
        self.proxy_url = proxy_url

    def bucket(self, name: str):
        if name not in self._buckets:
            endpoint = base.normalize_endpoint(json.loads(os.environ.get("OSS_BUCKET_ENDPOINTS", "{}")).get(name, self.endpoint))
            self._buckets[name] = base.oss2.Bucket(
                self.auth,
                endpoint,
                name,
                connect_timeout=10,
                enable_crc=True,
                proxies={"http": self.proxy_url, "https": self.proxy_url},
            )
        return self._buckets[name]


class InfiniteBalancedBatchSampler:
    """Yield deterministic single-dataset B8 batches forever.

    Every three yielded batches contain exactly one batch from each frozen
    dataset.  Within each dataset, a new deterministic shuffle is started only
    after all 1,000 episodes from the preceding cycle have been consumed.
    """

    def __init__(self, index: list[dict[str, Any]], batch_size: int, seed: int):
        self.batch_size = batch_size
        self.seed = seed
        self.candidates = {
            dataset: [position for position, item in enumerate(index) if item["dataset"] == dataset]
            for dataset in base.DATASETS
        }
        if any(len(values) != 1000 for values in self.candidates.values()):
            raise ValueError("frozen index must contain 1,000 episodes per dataset")

    def __iter__(self) -> Iterator[list[int]]:
        buffers = {dataset: [] for dataset in base.DATASETS}
        cycles = {dataset: 0 for dataset in base.DATASETS}
        dataset_offsets = {dataset: offset for offset, dataset in enumerate(base.DATASETS)}
        round_index = 0
        while True:
            order = list(base.DATASETS)
            random.Random(self.seed + 20_000 + round_index * 1_000_003).shuffle(order)
            round_index += 1
            for dataset in order:
                while len(buffers[dataset]) < self.batch_size:
                    current = list(self.candidates[dataset])
                    random.Random(
                        self.seed
                        + dataset_offsets[dataset]
                        + cycles[dataset] * 100_003
                    ).shuffle(current)
                    buffers[dataset].extend(current)
                    cycles[dataset] += 1
                batch = buffers[dataset][: self.batch_size]
                del buffers[dataset][: self.batch_size]
                yield batch


def init_proxy_worker(
    index_path: str,
    implementation: str,
    pattern: str,
    seed: int,
    window: int,
    random_span: int,
    credential_file: str,
    endpoint: str,
    proxy_url: str,
    _worker_id: int,
) -> None:
    base._INDEX = json.loads(Path(index_path).read_text(encoding="utf-8"))
    base._IMPLEMENTATION = implementation
    base._PATTERN = pattern
    base._SEED = seed
    base._WINDOW = window
    base._RANDOM_SPAN = random_span
    base._OSS = ProxyOssStore(credential_file, endpoint, proxy_url)
    base._LANCE_OPTIONS = base.lance_storage_options(credential_file, endpoint)
    base._LANCE_DATASETS = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ[key] = proxy_url
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    import torch

    torch.set_num_threads(1)


def proxy_log_snapshot(path: str) -> dict[str, int]:
    result = {"records": 0, "reply_bytes": 0}
    log_path = Path(path)
    if not log_path.is_file():
        return result
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        try:
            result["reply_bytes"] += int(fields[4])
        except ValueError:
            continue
        result["records"] += 1
    return result


def report_stats(values: list[float]) -> dict[str, float]:
    return base.stats(values)


def validate_existing(report: dict[str, Any], args: argparse.Namespace) -> None:
    if args.implementation == "pyav_native" and report.get("pyav_decode_strategy") != base.PYAV_DECODE_STRATEGY:
        raise ValueError("existing PyAV result uses a different decoding strategy; use a new output root")
    expected = {
        "status": "complete",
        "implementation": args.implementation,
        "sampling_mode": args.sampling_mode,
        "manifest_sha256": base.MANIFEST_SHA256,
        "preprocess_success_sha256": base.PREPROCESS_SUCCESS_SHA256,
    }
    for key, value in expected.items():
        if report.get(key) != value:
            raise ValueError(f"existing report {key} mismatch")
    config = report.get("config", {})
    for key, value in {
        "workers": args.workers,
        "batch_size": args.batch_size,
        "warmup_steps": args.warmup_steps,
        "duration_seconds": args.duration_seconds,
        "bandwidth_mib_per_second": args.bandwidth_mib,
        "stopping_rule": "after each complete step, stop when measured elapsed >= duration_seconds",
    }.items():
        if config.get(key) != value:
            raise ValueError(f"existing report config.{key} mismatch")
    for key, env_name in (
        ("script_sha256", "EXPECTED_SCRIPT_SHA256"),
        ("base_script_sha256", "EXPECTED_BASE_SCRIPT_SHA256"),
        ("runner_sha256", "EXPECTED_RUNNER_SHA256"),
        ("wheel_sha256", "EXPECTED_WHEEL_SHA256"),
        ("proxy_lib_sha256", "EXPECTED_PROXY_LIB_SHA256"),
    ):
        expected_value = os.environ.get(env_name)
        if expected_value and report.get("artifacts", {}).get(key) != expected_value:
            raise ValueError(f"existing report artifacts.{key} mismatch")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--implementation", choices=base.IMPLEMENTATIONS, required=True)
    parser.add_argument("--sampling-mode", choices=base.PATTERNS, default="continuous")
    parser.add_argument("--credential-file", default=base.DEFAULT_CREDENTIAL_FILE)
    parser.add_argument("--endpoint", default=base.DEFAULT_ENDPOINT)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--proxy-url", required=True)
    parser.add_argument("--proxy-access-log", required=True)
    parser.add_argument("--bandwidth-mib", type=int, required=True)
    parser.add_argument("--duration-seconds", type=float, default=3600.0)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--warmup-steps", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260729)
    parser.add_argument("--window", type=int, default=10)
    parser.add_argument("--random-span", type=int, default=0)
    parser.add_argument("--prefetch-factor", type=int, default=1)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--resume-existing", action="store_true")
    args = parser.parse_args()

    if args.bandwidth_mib not in (1, 5, 10, 50, 100, 200, 400, 800, 1600, 3200):
        raise ValueError("unsupported bandwidth point")
    if args.workers != 32 or args.batch_size != 8 or args.warmup_steps != 10:
        raise ValueError("Main Table alignment requires W32 / B8 / warmup10")
    if args.sampling_mode != "continuous" or args.window != 10 or args.random_span != 0:
        raise ValueError("bandwidth protocol is frozen at continuous / 10 frames per camera")
    if args.duration_seconds <= 0 or args.prefetch_factor != 1:
        raise ValueError("invalid duration or prefetch factor")

    output_uri = (
        f"{args.output_root.rstrip('/')}/results/{args.implementation}/{args.sampling_mode}.json"
    )
    store = ProxyOssStore(args.credential_file, args.endpoint, args.proxy_url)
    if store.exists(output_uri):
        payload = store.get(output_uri)
        validate_existing(json.loads(payload), args)
        if args.resume_existing:
            base.log(f"reused existing result uri={output_uri} sha256={base.sha256(payload)}")
            return
        raise FileExistsError(f"result already exists: {output_uri}")

    index, representation = base.prepare_index(
        args.implementation,
        store,
        args.credential_file,
        args.endpoint,
    )
    before_proxy = proxy_log_snapshot(args.proxy_access_log)

    with tempfile.TemporaryDirectory(prefix="node-bandwidth-loader-index-") as temporary:
        index_path = Path(temporary) / "index.json"
        index_path.write_text(
            json.dumps(index, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        import torch
        from torch.utils.data import DataLoader

        worker_init = functools.partial(
            init_proxy_worker,
            str(index_path),
            args.implementation,
            args.sampling_mode,
            args.seed,
            args.window,
            args.random_span,
            args.credential_file,
            args.endpoint,
            args.proxy_url,
        )
        loader = DataLoader(
            base.CuratedIndexDataset(list(range(len(index)))),
            batch_sampler=InfiniteBalancedBatchSampler(index, args.batch_size, args.seed),
            num_workers=args.workers,
            persistent_workers=True,
            prefetch_factor=args.prefetch_factor,
            multiprocessing_context="spawn",
            collate_fn=base.identity_collate,
            worker_init_fn=worker_init,
            pin_memory=False,
        )
        iterator = iter(loader)
        for step in range(args.warmup_steps):
            batch = next(iterator)
            if len(batch) != args.batch_size or len({item["dataset"] for item in batch}) != 1:
                raise ValueError("warmup batch contract mismatch")
            base.log(f"warmup={step + 1}/{args.warmup_steps}")

        cgroup_path, cpu_before = base.cgroup_cpu_usage_seconds()
        measured_start_utc = base.now_iso()
        benchmark_started = time.perf_counter()
        results: list[dict[str, Any]] = []
        step_seconds: list[float] = []
        step_by_dataset: dict[str, list[float]] = defaultdict(list)
        while True:
            step_started = time.perf_counter()
            batch = next(iterator)
            elapsed = time.perf_counter() - step_started
            datasets = {item["dataset"] for item in batch}
            if len(batch) != args.batch_size or len(datasets) != 1:
                raise ValueError("measured batch contract mismatch")
            dataset = next(iter(datasets))
            step_seconds.append(elapsed)
            step_by_dataset[dataset].append(elapsed)
            results.extend(batch)
            measured_elapsed = time.perf_counter() - benchmark_started
            step_count = len(step_seconds)
            if step_count % args.progress_every == 0 or measured_elapsed >= args.duration_seconds:
                base.log(
                    f"step={step_count} measured_elapsed={measured_elapsed:.3f}s "
                    f"mean_wait={statistics.fmean(step_seconds):.6f}s"
                )
            if measured_elapsed >= args.duration_seconds:
                break

        benchmark_elapsed = time.perf_counter() - benchmark_started
        measured_end_utc = base.now_iso()
        _, cpu_after = base.cgroup_cpu_usage_seconds()
        shutdown = getattr(iterator, "_shutdown_workers", None)
        if callable(shutdown):
            shutdown()
        del iterator
        del loader

    time.sleep(1.0)
    after_proxy = proxy_log_snapshot(args.proxy_access_log)
    if not step_seconds or len(results) != len(step_seconds) * args.batch_size:
        raise ValueError("measured result cardinality mismatch")
    if any(value <= 0 or not math.isfinite(value) for value in step_seconds):
        raise ValueError("invalid raw step time")
    if benchmark_elapsed < args.duration_seconds:
        raise ValueError("duration stopping rule terminated early")

    dataset_results: dict[str, Any] = {}
    for dataset in base.DATASETS:
        current = [item for item in results if item["dataset"] == dataset]
        current_steps = step_by_dataset[dataset]
        if not current_steps or len(current) != len(current_steps) * args.batch_size:
            raise ValueError(f"{dataset}: missing or malformed measured steps")
        dataset_results[dataset] = {
            "episodes": len(current),
            "steps": len(current_steps),
            "step_seconds": report_stats(current_steps),
            "step_seconds_raw": current_steps,
            "media_bytes": sum(int(item["media_bytes"]) for item in current),
            "media_mib_per_episode": sum(int(item["media_bytes"]) for item in current)
            / len(current)
            / 2**20,
        }

    macro_mean = statistics.fmean(
        dataset_results[name]["step_seconds"]["mean"] for name in base.DATASETS
    )
    macro_p95 = statistics.fmean(
        dataset_results[name]["step_seconds"]["p95"] for name in base.DATASETS
    )
    macro_media = statistics.fmean(
        dataset_results[name]["media_mib_per_episode"] for name in base.DATASETS
    )
    cpu_seconds = cpu_after - cpu_before
    average_cpu_cores = cpu_seconds / benchmark_elapsed
    if cpu_seconds <= 0 or not math.isfinite(average_cpu_cores) or average_cpu_cores <= 0:
        raise ValueError("invalid cgroup CPU measurement")
    checksum_xor = functools.reduce(lambda left, right: left ^ int(right["checksum"]), results, 0)
    if not checksum_xor:
        checksum_xor = functools.reduce(
            lambda value, item: zlib.crc32(item["id"].encode(), value), results, 1
        )
    proxy_records = after_proxy["records"] - before_proxy["records"]
    proxy_reply_bytes = after_proxy["reply_bytes"] - before_proxy["reply_bytes"]
    report = {
        "pyav_decode_strategy": base.PYAV_DECODE_STRATEGY if args.implementation == "pyav_native" else None,
        "schema_version": 2,
        "status": "complete",
        "created_at_utc": base.now_iso(),
        "implementation": args.implementation,
        "sampling_mode": args.sampling_mode,
        "manifest_uri": base.MANIFEST_URI,
        "manifest_sha256": base.MANIFEST_SHA256,
        "preprocess_root": base.PREPROCESS_ROOT,
        "preprocess_success_sha256": base.PREPROCESS_SUCCESS_SHA256,
        "representation": representation,
        "config": {
            "workers": args.workers,
            "batch_size": args.batch_size,
            "warmup_steps": args.warmup_steps,
            "duration_seconds": args.duration_seconds,
            "stopping_rule": "after each complete step, stop when measured elapsed >= duration_seconds",
            "steps": len(step_seconds),
            "prefetch_factor": args.prefetch_factor,
            "persistent_workers": True,
            "multiprocessing_context": "spawn",
            "seed": args.seed,
            "window": args.window,
            "episode_sampling": "deterministic balanced infinite full-dataset cycles",
            "bandwidth_definition": "node aggregate OSS response traffic through one Squid class-1 delay pool",
            "bandwidth_mib_per_second": args.bandwidth_mib,
            "bandwidth_bytes_per_second": args.bandwidth_mib * 2**20,
            "proxy_url": args.proxy_url,
            "proxy_cache": False,
            "lance_direct_fragment_io": args.implementation in base.LANCE_PROFILE,
            "new_media_materialization": False,
            "decoded_image_full_scan": False,
            "backend_threads": 1,
        },
        "artifacts": {
            "script_sha256": os.environ.get("EXPECTED_SCRIPT_SHA256"),
            "base_script_sha256": os.environ.get("EXPECTED_BASE_SCRIPT_SHA256"),
            "runner_sha256": os.environ.get("EXPECTED_RUNNER_SHA256"),
            "wheel_sha256": os.environ.get("EXPECTED_WHEEL_SHA256"),
            "proxy_lib_sha256": os.environ.get("EXPECTED_PROXY_LIB_SHA256"),
        },
        "environment": {
            "hostname": socket.gethostname(),
            "python": os.sys.version,
            "torch": str(torch.__version__),
            "cpu_count": os.cpu_count(),
            "fuyao_job_id": os.environ.get("FUYAO_JOB_ID"),
            "fuyao_job_name": os.environ.get("FUYAO_JOB_NAME"),
        },
        "measured_start_utc": measured_start_utc,
        "measured_end_utc": measured_end_utc,
        "result": {
            "elapsed_seconds": benchmark_elapsed,
            "duration_overshoot_seconds": benchmark_elapsed - args.duration_seconds,
            "steps": len(step_seconds),
            "episodes": len(results),
            "step_seconds": report_stats(step_seconds),
            "step_seconds_raw": step_seconds,
            "macro_step_seconds": {"mean": macro_mean, "p95": macro_p95},
            "macro_media_mib_per_episode": macro_media,
            "average_cpu_cores": average_cpu_cores,
            "cgroup_cpu_usage_seconds": cpu_seconds,
            "cgroup_cpu_path": cgroup_path,
            "row_bytes": sum(int(item["row_bytes"]) for item in results),
            "media_bytes": sum(int(item["media_bytes"]) for item in results),
            "selected_frames": sum(int(item["selected_frames"]) for item in results),
            "frames_output": sum(int(item["frames_output"]) for item in results),
            "checksum_xor": hex(checksum_xor),
            "proxy_completed_records_delta": proxy_records,
            "proxy_reply_bytes_delta": proxy_reply_bytes,
            "proxy_reply_mib_per_second": proxy_reply_bytes / 2**20 / benchmark_elapsed,
            "dataset_results": dataset_results,
        },
    }
    payload = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    uploaded_sha = store.put_immutable(output_uri, payload)
    base.log(
        f"NODE_BANDWIDTH_BENCHMARK_COMPLETE implementation={args.implementation} "
        f"bandwidth_mib={args.bandwidth_mib} steps={len(step_seconds)} "
        f"elapsed={benchmark_elapsed:.3f}s macro_mean_ms={macro_mean * 1000:.3f} "
        f"macro_p95_ms={macro_p95 * 1000:.3f} cpu_cores={average_cpu_cores:.3f} "
        f"proxy_reply_mib_s={proxy_reply_bytes / 2**20 / benchmark_elapsed:.3f} "
        f"uri={output_uri} sha256={uploaded_sha}"
    )


if __name__ == "__main__":
    main()
