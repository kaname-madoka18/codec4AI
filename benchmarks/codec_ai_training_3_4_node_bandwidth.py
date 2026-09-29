#!/usr/bin/env python3
"""Main-table W32/B8 loader benchmark behind one node aggregate OSS bucket."""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import os
import socket
import statistics
import sys
import tempfile
import time
import zlib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
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


def init_proxy_worker(
    index_path: str,
    implementation: str,
    pattern: str,
    seed: int,
    window: int,
    random_span: int,
    sampling_stride: int,
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
    base._SAMPLING_STRIDE = sampling_stride
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
    result = {"records": 0, "reply_bytes": 0, "cache_hits": 0}
    log_path = Path(path)
    if not log_path.is_file():
        return result
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        try:
            reply_bytes = int(fields[4])
        except ValueError:
            continue
        result["records"] += 1
        result["reply_bytes"] += reply_bytes
        if "HIT" in fields[3].upper():
            result["cache_hits"] += 1
    return result


def artifact_hashes() -> dict[str, str | None]:
    return {
        "adapter_sha256": os.environ.get("EXPECTED_ADAPTER_SHA256"),
        "base_script_sha256": os.environ.get("EXPECTED_BASE_SCRIPT_SHA256"),
        "runner_sha256": os.environ.get("EXPECTED_RUNNER_SHA256"),
        "wheel_sha256": os.environ.get("EXPECTED_WHEEL_SHA256"),
        "proxy_lib_sha256": os.environ.get("EXPECTED_PROXY_LIB_SHA256"),
    }


def validate_existing(report: dict[str, Any], args: argparse.Namespace, output_uri: str) -> None:
    if args.implementation == "pyav_native" and report.get("pyav_decode_strategy") != base.PYAV_DECODE_STRATEGY:
        raise ValueError("existing PyAV result uses a different decoding strategy; use a new output root")
    expected = {
        "status": "complete",
        "protocol": "node_bandwidth_main_table_100step",
        "implementation": args.implementation,
        "sampling_mode": "continuous",
        "manifest_sha256": base.MANIFEST_SHA256,
        "preprocess_success_sha256": base.PREPROCESS_SUCCESS_SHA256,
        "output_uri": output_uri,
    }
    for key, value in expected.items():
        if report.get(key) != value:
            raise ValueError(f"existing report {key} mismatch")
    config = report.get("config", {})
    expected_config = {
        "cpu_limit": 16,
        "workers": 32,
        "batch_size": 8,
        "warmup_steps": 10,
        "steps": 100,
        "prefetch_factor": 1,
        "seed": 20260729,
        "window": 10,
        "sampling_stride": 1,
        "bandwidth_mib_per_second": args.bandwidth_mib,
    }
    for key, value in expected_config.items():
        if config.get(key) != value:
            raise ValueError(f"existing report config.{key} mismatch")
    if report.get("artifacts") != artifact_hashes():
        raise ValueError("existing report artifact hashes mismatch")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--implementation", choices=base.IMPLEMENTATIONS, required=True)
    parser.add_argument("--credential-file", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--proxy-url", required=True)
    parser.add_argument("--proxy-access-log", required=True)
    parser.add_argument("--bandwidth-mib", type=int, required=True)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--cpu-limit", type=int, default=16)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--warmup-steps", type=int, default=10)
    parser.add_argument("--steps", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260729)
    parser.add_argument("--window", type=int, default=10)
    parser.add_argument("--prefetch-factor", type=int, default=1)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--resume-existing", action="store_true")
    args = parser.parse_args()

    if args.bandwidth_mib not in (20, 50, 100, 200, 400, 800):
        raise ValueError("unsupported bandwidth point")
    if (
        args.workers != 32
        or args.cpu_limit != 16
        or args.batch_size != 8
        or args.warmup_steps != 10
        or args.steps != 100
        or args.seed != 20260729
        or args.window != 10
        or args.prefetch_factor != 1
    ):
        raise ValueError("3.4 protocol requires CPU16/W32/B8/F10/warmup10/measured100/prefetch1")

    output_uri = f"{args.output_root.rstrip('/')}/results/{args.implementation}.json"
    store = ProxyOssStore(args.credential_file, args.endpoint, args.proxy_url)
    if store.exists(output_uri):
        payload = store.get(output_uri)
        validate_existing(json.loads(payload), args, output_uri)
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
    warmup_batches, measured_batches = base.build_batch_sequence(
        index,
        args.seed,
        args.warmup_steps,
        args.steps,
        args.batch_size,
    )
    flattened = [value for batch in warmup_batches + measured_batches for value in batch]
    measured_batch_datasets = [index[batch[0]]["dataset"] for batch in measured_batches]
    expected_measured_batches = {
        dataset: args.steps // len(base.DATASETS) + int(offset < args.steps % len(base.DATASETS))
        for offset, dataset in enumerate(base.DATASETS)
    }
    if Counter(measured_batch_datasets) != Counter(expected_measured_batches):
        raise ValueError("measured batch distribution mismatch")

    with tempfile.TemporaryDirectory(prefix="codec-ai-3-4-nodebw-index-") as temporary:
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
            "continuous",
            args.seed,
            args.window,
            0,
            0,
            args.credential_file,
            args.endpoint,
            args.proxy_url,
        )
        loader = DataLoader(
            base.CuratedIndexDataset(flattened),
            batch_size=args.batch_size,
            shuffle=False,
            num_workers=args.workers,
            persistent_workers=True,
            prefetch_factor=args.prefetch_factor,
            multiprocessing_context="spawn",
            collate_fn=base.identity_collate,
            worker_init_fn=worker_init,
            pin_memory=False,
        )
        before_proxy = proxy_log_snapshot(args.proxy_access_log)
        proxy_run_started = time.perf_counter()
        iterator = iter(loader)
        for step in range(args.warmup_steps):
            batch = next(iterator)
            if len(batch) != args.batch_size:
                raise ValueError("short warmup batch")
            base.log(f"warmup={step + 1}/{args.warmup_steps}")

        cgroup_path, cpu_before = base.cgroup_cpu_usage_seconds()
        measured_start_utc = base.now_iso()
        benchmark_started = time.perf_counter()
        results: list[dict[str, Any]] = []
        step_seconds: list[float] = []
        step_by_dataset: dict[str, list[float]] = defaultdict(list)
        for step, dataset in enumerate(measured_batch_datasets):
            step_started = time.perf_counter()
            batch = next(iterator)
            elapsed = time.perf_counter() - step_started
            if len(batch) != args.batch_size or {item["dataset"] for item in batch} != {dataset}:
                raise ValueError(f"step {step}: batch contract mismatch")
            step_seconds.append(elapsed)
            step_by_dataset[dataset].append(elapsed)
            results.extend(batch)
            if (step + 1) % args.progress_every == 0 or step + 1 == args.steps:
                base.log(f"step={step + 1}/{args.steps} mean_wait={statistics.fmean(step_seconds):.6f}s")
        benchmark_elapsed = time.perf_counter() - benchmark_started
        measured_end_utc = base.now_iso()
        _, cpu_after = base.cgroup_cpu_usage_seconds()
        shutdown = getattr(iterator, "_shutdown_workers", None)
        if callable(shutdown):
            shutdown()
        del iterator
        del loader
        proxy_run_elapsed = time.perf_counter() - proxy_run_started
        after_proxy = proxy_log_snapshot(args.proxy_access_log)

    if len(results) != 800 or len(step_seconds) != 100:
        raise ValueError("measured result cardinality mismatch")
    if any(value <= 0 or not math.isfinite(value) for value in step_seconds):
        raise ValueError("invalid raw step time")
    dataset_results: dict[str, Any] = {}
    for dataset in base.DATASETS:
        current = [item for item in results if item["dataset"] == dataset]
        current_steps = step_by_dataset[dataset]
        expected_steps = expected_measured_batches[dataset]
        if len(current) != expected_steps * args.batch_size or len(current_steps) != expected_steps:
            raise ValueError(f"{dataset}: measured distribution mismatch")
        dataset_results[dataset] = {
            "episodes": len(current),
            "steps": len(current_steps),
            "step_seconds": base.stats(current_steps),
            "step_seconds_raw": current_steps,
            "media_bytes": sum(int(item["media_bytes"]) for item in current),
            "media_mib_per_episode": sum(int(item["media_bytes"]) for item in current)
            / len(current)
            / 2**20,
        }

    proxy_records = after_proxy["records"] - before_proxy["records"]
    proxy_reply_bytes = after_proxy["reply_bytes"] - before_proxy["reply_bytes"]
    proxy_cache_hits = after_proxy["cache_hits"] - before_proxy["cache_hits"]
    if proxy_records <= 0 or proxy_reply_bytes <= 0 or proxy_cache_hits:
        raise ValueError("proxy log proves no traffic or contains a cache hit")
    proxy_mib_s = proxy_reply_bytes / 2**20 / proxy_run_elapsed
    maximum_allowed = args.bandwidth_mib * 1.15 + args.bandwidth_mib / proxy_run_elapsed
    if proxy_mib_s > maximum_allowed:
        raise ValueError(
            f"proxy rate exceeded aggregate cap allowance: {proxy_mib_s:.3f} > {maximum_allowed:.3f}"
        )

    macro_mean = statistics.fmean(dataset_results[name]["step_seconds"]["mean"] for name in base.DATASETS)
    macro_p95 = statistics.fmean(dataset_results[name]["step_seconds"]["p95"] for name in base.DATASETS)
    macro_media = statistics.fmean(dataset_results[name]["media_mib_per_episode"] for name in base.DATASETS)
    cpu_seconds = cpu_after - cpu_before
    average_cpu_cores = cpu_seconds / benchmark_elapsed
    if cpu_seconds <= 0 or not math.isfinite(average_cpu_cores) or average_cpu_cores <= 0:
        raise ValueError("invalid cgroup CPU measurement")
    checksum_xor = functools.reduce(lambda left, right: left ^ int(right["checksum"]), results, 0)
    if not checksum_xor:
        checksum_xor = functools.reduce(
            lambda value, item: zlib.crc32(item["id"].encode(), value),
            results,
            1,
        )

    report = {
        "pyav_decode_strategy": base.PYAV_DECODE_STRATEGY if args.implementation == "pyav_native" else None,
        "schema_version": 1,
        "status": "complete",
        "created_at_utc": base.now_iso(),
        "protocol": "node_bandwidth_main_table_100step",
        "implementation": args.implementation,
        "sampling_mode": "continuous",
        "output_uri": output_uri,
        "manifest_uri": base.MANIFEST_URI,
        "manifest_sha256": base.MANIFEST_SHA256,
        "preprocess_root": base.PREPROCESS_ROOT,
        "preprocess_success_sha256": base.PREPROCESS_SUCCESS_SHA256,
        "representation": representation,
        "config": {
            "cpu_limit": 16,
            "workers": 32,
            "batch_size": 8,
            "warmup_steps": 10,
            "steps": 100,
            "prefetch_factor": 1,
            "persistent_workers": True,
            "multiprocessing_context": "spawn",
            "seed": args.seed,
            "window": 10,
            "sampling_mode": "continuous",
            "sampling_stride": 1,
            "measured_episodes": 800,
            "measured_batches_per_dataset": expected_measured_batches,
            "episode_sampling": "same deterministic shuffled main-table sequence",
            "full_media_materialization": True,
            "decoded_image_full_scan": False,
            "backend_threads": 1,
            "bandwidth_definition": "single-node aggregate OSS response traffic through Squid class-1 delay pools",
            "bandwidth_mib_per_second": args.bandwidth_mib,
            "bandwidth_bytes_per_second": args.bandwidth_mib * 2**20,
            "proxy_url": args.proxy_url,
            "proxy_cache": False,
            "proxy_accounting_window": "DataLoader iterator startup through worker shutdown, including prefetch and warmup",
            "new_media_materialization": False,
            "transcoding": False,
        },
        "artifacts": artifact_hashes(),
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
            "episodes": len(results),
            "step_seconds": base.stats(step_seconds),
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
            "dataset_results": dataset_results,
            "proxy_run_elapsed_seconds": proxy_run_elapsed,
            "proxy_completed_records_delta": proxy_records,
            "proxy_reply_bytes_delta": proxy_reply_bytes,
            "proxy_reply_mib_per_second": proxy_mib_s,
            "proxy_cache_hits_delta": proxy_cache_hits,
        },
    }
    payload = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    uploaded_sha = store.put_immutable(output_uri, payload)
    base.log(
        f"NODE_BANDWIDTH_100STEP_COMPLETE implementation={args.implementation} "
        f"bandwidth_mib={args.bandwidth_mib} macro_mean_ms={macro_mean * 1000:.3f} "
        f"macro_p95_ms={macro_p95 * 1000:.3f} proxy_mib_s={proxy_mib_s:.3f} "
        f"uri={output_uri} sha256={uploaded_sha}"
    )


if __name__ == "__main__":
    main()
