#!/usr/bin/env python3
"""Calibrate JPEG QF and Ours-fast CRFs on the frozen raw-H.264 manifest."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import numpy as np
import oss2
from PIL import Image


DATASETS = ("realsource_world", "robocasa", "table30v2")
QUALITY_GATES_DB = {"jpeg_lance": 0.15, "ours_fast_x264": 0.20, "ours_fast_x265": 0.15}
PSNR_EXACT_CAP_DB = 100.0
LEROBOT_VERSION = "0.6.1"
LEROBOT_WHEEL_SHA256 = "1894516040c65f80a45bd9741f8174aae90ed5d93da0627ab4f1a85fd8d75e90"
LEROBOT_VIDEO_CONFIG_SHA256 = "51dc623e5bab521319b51f730e94eb365a9cb3679f73537c7d797b6f2a77bb56"
LEROBOT_VIDEO_UTILS_SHA256 = "82d0ced8f3f9adc754d4388122b044d36f2feeadda28d4ddad933cead7cc54d1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"[{now_iso()}] {message}", flush=True)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def split_oss(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "oss" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError(f"invalid OSS URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


class Store:
    def __init__(self, credential_file: str, endpoint: str) -> None:
        values = load_env_file(credential_file)
        access = os.environ.get("OSS_ACCESS_KEY_ID") or values.get("OSS_ACCESS_KEY_ID")
        secret = os.environ.get("OSS_ACCESS_KEY_SECRET") or values.get("OSS_ACCESS_KEY_SECRET")
        token = (
            os.environ.get("OSS_SECURITY_TOKEN")
            or os.environ.get("OSS_STS_TOKEN")
            or values.get("OSS_SECURITY_TOKEN")
            or values.get("OSS_STS_TOKEN")
        )
        if not access or not secret:
            raise RuntimeError("missing OSS credentials")
        self.auth = oss2.StsAuth(access, secret, token) if token else oss2.Auth(access, secret)
        self.endpoint = endpoint
        self.local = threading.local()

    def bucket(self, name: str) -> oss2.Bucket:
        buckets = getattr(self.local, "buckets", None)
        if buckets is None:
            buckets = {}
            self.local.buckets = buckets
        if name not in buckets:
            endpoint = json.loads(os.environ.get("OSS_BUCKET_ENDPOINTS", "{}")).get(name, self.endpoint)
            buckets[name] = oss2.Bucket(self.auth, endpoint, name, connect_timeout=30, enable_crc=True)
        return buckets[name]

    def get_bytes(self, uri: str) -> bytes:
        bucket, key = split_oss(uri)
        return self.bucket(bucket).get_object(key).read()

    def download(self, uri: str, destination: Path) -> int:
        bucket_name, key = split_oss(uri)
        bucket = self.bucket(bucket_name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + f".part-{threading.get_ident()}")
        temporary.unlink(missing_ok=True)
        try:
            head = bucket.head_object(key)
            bucket.get_object_to_file(key, str(temporary))
            if temporary.stat().st_size != int(head.content_length):
                raise IOError(f"short download: {uri}")
            os.replace(temporary, destination)
            return destination.stat().st_size
        finally:
            temporary.unlink(missing_ok=True)

    def exists(self, uri: str) -> bool:
        bucket, key = split_oss(uri)
        return self.bucket(bucket).object_exists(key)

    def put_immutable(self, uri: str, payload: bytes) -> str:
        bucket_name, key = split_oss(uri)
        bucket = self.bucket(bucket_name)
        digest = hashlib.sha256(payload).hexdigest()
        if bucket.object_exists(key):
            existing = bucket.get_object(key).read()
            if existing != payload:
                raise FileExistsError(f"refusing to overwrite non-identical object: {uri}")
            return digest
        bucket.put_object(key, payload)
        if int(bucket.head_object(key).content_length) != len(payload):
            raise IOError(f"short upload: {uri}")
        return digest


@dataclass(frozen=True)
class Camera:
    dataset: str
    curated_episode_index: int
    dataset_episode_index: int
    source_id: str
    camera: str
    uri: str
    frames: int
    width: int
    height: int
    fps: float
    expected_bytes: int
    expected_sha256: str
    local_path: str


@dataclass
class Stats:
    frames: int = 0
    squared_error: int = 0
    rgb_samples: int = 0
    encoded_bytes: int = 0
    source_bytes: int = 0
    encode_seconds: float = 0.0

    def add_rgb(self, reference: np.ndarray, distorted: np.ndarray) -> None:
        if reference.dtype != np.uint8 or distorted.dtype != np.uint8 or reference.shape != distorted.shape:
            raise ValueError(f"RGB mismatch: {reference.shape}/{reference.dtype} vs {distorted.shape}/{distorted.dtype}")
        difference = reference.astype(np.int16) - distorted.astype(np.int16)
        self.squared_error += int(np.square(difference, dtype=np.int32).sum(dtype=np.int64))
        self.rgb_samples += int(reference.size)
        self.frames += 1

    def merge(self, other: "Stats") -> None:
        self.frames += other.frames
        self.squared_error += other.squared_error
        self.rgb_samples += other.rgb_samples
        self.encoded_bytes += other.encoded_bytes
        self.source_bytes += other.source_bytes
        self.encode_seconds += other.encode_seconds

    def final(self) -> dict[str, Any]:
        if not self.frames or not self.rgb_samples:
            raise ValueError("empty metric")
        psnr = (
            PSNR_EXACT_CAP_DB
            if self.squared_error == 0
            else 10.0 * math.log10((255.0 * 255.0 * self.rgb_samples) / self.squared_error)
        )
        return {
            "frames": self.frames,
            "sum_squared_error": self.squared_error,
            "rgb_samples": self.rgb_samples,
            "pooled_rgb_psnr_db": psnr,
            "encoded_bytes": self.encoded_bytes,
            "source_bytes": self.source_bytes,
            "encode_seconds": self.encode_seconds,
        }


def read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def decoder(ffmpeg: str, path: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-i",
            path,
            "-map",
            "0:v:0",
            "-an",
            "-pix_fmt",
            "rgb24",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def finish_decoder(process: subprocess.Popen[bytes], label: str) -> None:
    assert process.stdout is not None and process.stderr is not None
    extra = process.stdout.read()
    stderr = process.stderr.read().decode(errors="replace")
    return_code = process.wait()
    if return_code or extra:
        raise RuntimeError(f"{label}: decoder count/exit mismatch rc={return_code} extra={bool(extra)}: {stderr[-2000:]}")


def compare_videos(ffmpeg: str, camera: Camera, encoded: Path, encode_seconds: float) -> Stats:
    source_process = decoder(ffmpeg, camera.local_path)
    target_process = decoder(ffmpeg, str(encoded))
    assert source_process.stdout is not None and target_process.stdout is not None
    frame_bytes = camera.width * camera.height * 3
    stats = Stats(source_bytes=camera.expected_bytes, encoded_bytes=encoded.stat().st_size, encode_seconds=encode_seconds)
    try:
        for frame_index in range(camera.frames):
            reference_raw = read_exact(source_process.stdout, frame_bytes)
            target_raw = read_exact(target_process.stdout, frame_bytes)
            if len(reference_raw) != frame_bytes or len(target_raw) != frame_bytes:
                raise RuntimeError(f"{camera.source_id}/{camera.camera}: short frame {frame_index}")
            reference = np.frombuffer(reference_raw, dtype=np.uint8).reshape(camera.height, camera.width, 3)
            distorted = np.frombuffer(target_raw, dtype=np.uint8).reshape(camera.height, camera.width, 3)
            stats.add_rgb(reference, distorted)
        finish_decoder(source_process, "source")
        finish_decoder(target_process, "target")
        return stats
    except Exception:
        source_process.kill()
        target_process.kill()
        source_process.wait()
        target_process.wait()
        raise


def video_command(ffmpeg: str, camera: Camera, profile: str, value: int, output: Path) -> list[str]:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-threads",
        "1",
        "-i",
        camera.local_path,
        "-map",
        "0:v:0",
        "-an",
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "passthrough",
    ]
    if profile == "lerobot_v3_default":
        command += [
            "-c:v",
            "libsvtav1",
            "-g",
            "2",
            "-crf",
            "30",
            "-preset",
            "12",
            "-svtav1-params",
            "fast-decode=0",
            "-tag:v",
            "av01",
        ]
    elif profile == "ours_fast_x264":
        command += [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            str(value),
            "-g",
            "32",
            "-keyint_min",
            "32",
            "-bf",
            "31",
            "-x264-params",
            "keyint=32:min-keyint=32:scenecut=0:open-gop=1:bframes=31:b-adapt=0:b-pyramid=none:rc-lookahead=40:ref=1:threads=1:aud=1",
            "-tag:v",
            "avc1",
        ]
    elif profile == "ours_fast_x265":
        command += [
            "-c:v",
            "libx265",
            "-preset",
            "medium",
            "-crf",
            str(value),
            "-x265-params",
            "keyint=32:min-keyint=32:scenecut=0:open-gop=1:bframes=31:b-adapt=0:b-pyramid=0:rc-lookahead=40:ref=1:frame-threads=1:pools=8",
            "-tag:v",
            "hvc1",
        ]
    else:
        raise ValueError(profile)
    if profile.startswith("ours_fast") and (camera.frames - 1) % 32:
        command += ["-force_key_frames", f"expr:eq(n,{camera.frames - 1})"]
    command += ["-movflags", "+faststart", str(output)]
    return command


def evaluate_video_camera(
    encoder_ffmpeg: str,
    metric_ffmpeg: str,
    work_dir: Path,
    camera: Camera,
    profile: str,
    value: int,
) -> Stats:
    output = work_dir / f"{profile}-{value}-{hashlib.sha256((camera.uri + camera.camera).encode()).hexdigest()[:20]}.mp4"
    stderr_path = output.with_suffix(".stderr")
    try:
        started = time.monotonic()
        with stderr_path.open("wb") as stderr:
            completed = subprocess.run(
                video_command(encoder_ffmpeg, camera, profile, value, output),
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
        elapsed = time.monotonic() - started
        if completed.returncode:
            error = stderr_path.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(f"{profile}/{value}/{camera.source_id}/{camera.camera}: {error[-4000:]}")
        return compare_videos(metric_ffmpeg, camera, output, elapsed)
    finally:
        output.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)


def evaluate_jpeg_camera(ffmpeg: str, camera: Camera, quality: int) -> Stats:
    process = decoder(ffmpeg, camera.local_path)
    assert process.stdout is not None and process.stderr is not None
    frame_bytes = camera.width * camera.height * 3
    stats = Stats(source_bytes=camera.expected_bytes)
    started = time.monotonic()
    try:
        for frame_index in range(camera.frames):
            raw = read_exact(process.stdout, frame_bytes)
            if len(raw) != frame_bytes:
                raise RuntimeError(f"{camera.source_id}/{camera.camera}: short source frame {frame_index}")
            reference = np.frombuffer(raw, dtype=np.uint8).reshape(camera.height, camera.width, 3)
            encoded = io.BytesIO()
            Image.fromarray(reference, mode="RGB").save(
                encoded,
                format="JPEG",
                quality=quality,
                subsampling=0,
                optimize=False,
            )
            payload = encoded.getvalue()
            with Image.open(io.BytesIO(payload)) as decoded:
                distorted = np.asarray(decoded.convert("RGB"), dtype=np.uint8)
            stats.encoded_bytes += len(payload)
            stats.add_rgb(reference, distorted)
        finish_decoder(process, "jpeg-source")
        stats.encode_seconds = time.monotonic() - started
        return stats
    except Exception:
        process.kill()
        process.wait()
        raise


def summarize(stats_by_dataset: dict[str, Stats]) -> dict[str, Any]:
    if set(stats_by_dataset) != set(DATASETS):
        raise ValueError(f"dataset mismatch: {sorted(stats_by_dataset)}")
    datasets = {dataset: stats_by_dataset[dataset].final() for dataset in DATASETS}
    aggregate = Stats()
    for value in stats_by_dataset.values():
        aggregate.merge(value)
    return {
        "datasets": datasets,
        "macro_dataset_pooled_rgb_psnr_db": sum(value["pooled_rgb_psnr_db"] for value in datasets.values()) / len(DATASETS),
        "micro_global": aggregate.final(),
    }


def load_existing_candidate(
    store: Store,
    uri: str,
    manifest_sha: str,
    profile: str,
    value: int,
    provenance: dict[str, str],
) -> dict[str, Any] | None:
    if not store.exists(uri):
        return None
    existing = json.loads(store.get_bytes(uri))
    if (
        existing.get("status") != "complete"
        or existing.get("manifest_sha256") != manifest_sha
        or existing.get("profile") != profile
        or type(existing.get("value")) is not int
        or existing.get("value") != value
        or existing.get("provenance") != provenance
    ):
        raise ValueError(f"existing candidate contract mismatch: {uri}")
    log(f"reuse {profile}/{value}")
    return existing


def run_candidate(
    args: argparse.Namespace,
    store: Store,
    cameras: list[Camera],
    manifest_sha: str,
    profile: str,
    value: int,
    work_dir: Path,
) -> dict[str, Any]:
    if type(value) is not int:
        raise TypeError(f"integer-only quality value required, got {value!r}")
    uri = f"{args.output_uri.rstrip('/')}/candidates/{profile}_{value}.json"
    encoder_ffmpeg = (
        args.metric_ffmpeg
        if profile == "jpeg_lance"
        else (args.lerobot_ffmpeg if profile == "lerobot_v3_default" else args.ours_ffmpeg)
    )
    provenance = {
        "script_sha256": args.script_sha256,
        "runner_sha256": args.runner_sha256,
        "encoder_ffmpeg_sha256": sha256_file(Path(encoder_ffmpeg)),
        "metric_ffmpeg_sha256": sha256_file(Path(args.metric_ffmpeg)),
    }
    if existing := load_existing_candidate(store, uri, manifest_sha, profile, value, provenance):
        return existing
    seed_uri = f"{args.seed_output_uri.rstrip('/')}/candidates/{profile}_{value}.json"
    if store.exists(seed_uri):
        seed_payload = store.get_bytes(seed_uri)
        seed = json.loads(seed_payload)
        expected_seed_provenance = {
            "script_sha256": args.expected_seed_script_sha256,
            "runner_sha256": args.expected_seed_runner_sha256,
            "encoder_ffmpeg_sha256": provenance["encoder_ffmpeg_sha256"],
            "metric_ffmpeg_sha256": provenance["metric_ffmpeg_sha256"],
        }
        if (
            seed.get("status") != "complete"
            or seed.get("manifest_sha256") != manifest_sha
            or seed.get("profile") != profile
            or type(seed.get("value")) is not int
            or seed.get("value") != value
            or seed.get("provenance") != expected_seed_provenance
            or int(seed.get("camera_count", -1)) != len(cameras)
        ):
            raise ValueError(f"seed candidate contract mismatch: {seed_uri}")
        result = dict(seed)
        result.update(
            {
                "schema_version": 3,
                "created_at": now_iso(),
                "workers": 0,
                "elapsed_seconds": 0.0,
                "provenance": provenance,
                "reused_candidate": {
                    "uri": seed_uri,
                    "sha256": hashlib.sha256(seed_payload).hexdigest(),
                    "original_created_at": seed.get("created_at"),
                    "original_elapsed_seconds": seed.get("elapsed_seconds"),
                    "original_workers": seed.get("workers"),
                    "original_provenance": seed.get("provenance"),
                },
            }
        )
        store.put_immutable(uri, canonical_json(result))
        log(f"seed {profile}/{value} from {seed_uri}")
        return result
    if profile == "jpeg_lance":
        workers = min(args.jpeg_workers, len(cameras))
        operation: Callable[[Camera], Stats] = lambda camera: evaluate_jpeg_camera(args.metric_ffmpeg, camera, value)
    else:
        requested = args.lerobot_workers if profile == "lerobot_v3_default" else (
            args.x264_workers if profile == "ours_fast_x264" else args.x265_workers
        )
        workers = min(requested, len(cameras))
        encoder_ffmpeg = args.lerobot_ffmpeg if profile == "lerobot_v3_default" else args.ours_ffmpeg
        operation = lambda camera: evaluate_video_camera(
            encoder_ffmpeg, args.metric_ffmpeg, work_dir, camera, profile, value
        )
    log(f"evaluate profile={profile} value={value} cameras={len(cameras)} workers={workers}")
    stats_by_dataset = {dataset: Stats() for dataset in DATASETS}
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [(camera, executor.submit(operation, camera)) for camera in cameras]
        for ordinal, (camera, future) in enumerate(futures, 1):
            stats_by_dataset[camera.dataset].merge(future.result())
            if ordinal % 10 == 0 or ordinal == len(futures):
                log(f"{profile}/{value}: cameras={ordinal}/{len(futures)}")
    result = {
        "schema_version": 1,
        "status": "complete",
        "created_at": now_iso(),
        "manifest_sha256": manifest_sha,
        "profile": profile,
        "value": value,
        "value_name": "qf" if profile == "jpeg_lance" else ("fixed" if profile == "lerobot_v3_default" else "crf"),
        "workers": workers,
        "provenance": provenance,
        "camera_count": len(cameras),
        "elapsed_seconds": time.monotonic() - started,
        **summarize(stats_by_dataset),
    }
    store.put_immutable(uri, canonical_json(result))
    return result


def search_integer(
    profile: str,
    lower: int,
    upper: int,
    target: float,
    evaluate: Callable[[int], dict[str, Any]],
) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    results: dict[int, dict[str, Any]] = {}

    def run(value: int) -> dict[str, Any]:
        if value not in results:
            results[value] = evaluate(value)
        return results[value]

    low, high = lower, upper
    run(low)
    run(high)
    while low <= high:
        middle = (low + high) // 2
        psnr = float(run(middle)["macro_dataset_pooled_rgb_psnr_db"])
        if profile == "jpeg_lance":
            if psnr < target:
                low = middle + 1
            else:
                high = middle - 1
        else:
            if psnr > target:
                low = middle + 1
            else:
                high = middle - 1
    neighborhood = set()
    for center in (low, high):
        neighborhood.update(range(max(lower, center - 2), min(upper, center + 2) + 1))
    for value in sorted(neighborhood):
        run(value)
    selected = min(
        results.values(),
        key=lambda item: (
            abs(float(item["macro_dataset_pooled_rgb_psnr_db"]) - target),
            int(item["micro_global"]["encoded_bytes"]),
            int(item["value"]),
        ),
    )
    return selected, results


def build_cameras(manifest: dict[str, Any], work_dir: Path) -> list[Camera]:
    cameras: list[Camera] = []
    episodes = [item for item in manifest["episodes"] if item.get("calibration_1pct")]
    expected_episodes = len(DATASETS) * 10
    if len(episodes) != expected_episodes:
        raise ValueError(f"expected {expected_episodes} calibration episodes, got {len(episodes)}")
    dataset_counts: dict[str, int] = defaultdict(int)
    for episode in episodes:
        dataset = str(episode["dataset"])
        dataset_counts[dataset] += 1
        for camera_index, video in enumerate(episode["videos"]):
            local = work_dir / "source" / f"{int(episode['curated_episode_index']):06d}" / f"{camera_index:02d}.mp4"
            cameras.append(
                Camera(
                    dataset=dataset,
                    curated_episode_index=int(episode["curated_episode_index"]),
                    dataset_episode_index=int(episode["dataset_episode_index"]),
                    source_id=str(episode["source_id"]),
                    camera=str(video["camera"]),
                    uri=str(video["uri"]),
                    frames=int(video["frames"]),
                    width=int(video["width"]),
                    height=int(video["height"]),
                    fps=float(video["fps"]),
                    expected_bytes=int(video["bytes"]),
                    expected_sha256=str(video["sha256"]),
                    local_path=str(local),
                )
            )
    if dataset_counts != {dataset: 10 for dataset in DATASETS}:
        raise ValueError(f"invalid calibration episode distribution: {dataset_counts}")
    return cameras


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--manifest-uri", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--output-uri", required=True)
    parser.add_argument("--seed-output-uri", required=True)
    parser.add_argument("--expected-seed-script-sha256", required=True)
    parser.add_argument("--expected-seed-runner-sha256", required=True)
    parser.add_argument("--lerobot-ffmpeg", required=True)
    parser.add_argument("--ours-ffmpeg", required=True)
    parser.add_argument("--metric-ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--download-workers", type=int, default=24)
    parser.add_argument("--lerobot-workers", type=int, default=1)
    parser.add_argument("--jpeg-workers", type=int, default=12)
    parser.add_argument("--x264-workers", type=int, default=24)
    parser.add_argument("--x265-workers", type=int, default=6)
    parser.add_argument("--script-sha256", required=True)
    parser.add_argument("--runner-sha256", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    split_oss(args.output_uri.rstrip("/") + "/guard")
    split_oss(args.seed_output_uri.rstrip("/") + "/guard")
    if args.output_uri.rstrip("/") == args.seed_output_uri.rstrip("/"):
        raise ValueError("output and immutable seed roots must differ")
    for executable in (args.lerobot_ffmpeg, args.ours_ffmpeg, args.metric_ffmpeg, args.ffprobe):
        if not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise FileNotFoundError(executable)
    store = Store(args.credential_file, args.endpoint)
    manifest_payload = store.get_bytes(args.manifest_uri)
    manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
    if manifest_sha != args.expected_manifest_sha256:
        raise ValueError(f"manifest SHA mismatch: {manifest_sha}")
    manifest = json.loads(manifest_payload)
    if manifest.get("selection", {}).get("total_episodes") != 3000:
        raise ValueError("manifest is not the formal 3000-episode manifest")
    if tuple(manifest.get("selection", {}).get("datasets", ())) != DATASETS:
        raise ValueError("manifest dataset order does not match the approved three-dataset contract")
    work_dir = Path(tempfile.mkdtemp(prefix="data-curate-calibration-"))
    started = time.monotonic()
    try:
        cameras = build_cameras(manifest, work_dir)
        log(f"download calibration cameras={len(cameras)}")
        with ThreadPoolExecutor(max_workers=args.download_workers) as executor:
            futures = [executor.submit(store.download, camera.uri, Path(camera.local_path)) for camera in cameras]
            for camera, future in zip(cameras, futures):
                actual_bytes = future.result()
                if actual_bytes != camera.expected_bytes:
                    raise ValueError(f"source byte mismatch: {camera.uri}")
                if sha256_file(Path(camera.local_path)) != camera.expected_sha256:
                    raise ValueError(f"source SHA mismatch: {camera.uri}")
        log("download complete")

        lerobot = run_candidate(args, store, cameras, manifest_sha, "lerobot_v3_default", 30, work_dir)
        target = float(lerobot["macro_dataset_pooled_rgb_psnr_db"])

        selections: dict[str, Any] = {}
        evaluated: dict[str, Any] = {}
        for profile, lower, upper in (
            ("jpeg_lance", 1, 100),
            ("ours_fast_x264", 0, 51),
            ("ours_fast_x265", 0, 51),
        ):
            evaluator: Callable[[int], dict[str, Any]] = lambda value, current=profile: run_candidate(
                args, store, cameras, manifest_sha, current, value, work_dir
            )
            selected, results = search_integer(profile, lower, upper, target, evaluator)
            if type(selected.get("value")) is not int:
                raise TypeError(f"{profile}: non-integer selected value")
            selected_value = int(selected["value"])
            selections[profile] = {
                "value": selected_value,
                "value_name": selected["value_name"],
                "macro_dataset_pooled_rgb_psnr_db": selected["macro_dataset_pooled_rgb_psnr_db"],
                "absolute_delta_to_lerobot_db": abs(float(selected["macro_dataset_pooled_rgb_psnr_db"]) - target),
                "candidate_uri": (
                    f"{args.output_uri.rstrip('/')}/candidates/{profile}_{selected_value}.json"
                ),
            }
            gate = QUALITY_GATES_DB[profile]
            if selections[profile]["absolute_delta_to_lerobot_db"] > gate:
                raise ValueError(
                    f"{profile}: selected PSNR delta "
                    f"{selections[profile]['absolute_delta_to_lerobot_db']:.6f} dB exceeds {gate:.2f} dB"
                )
            evaluated[profile] = {
                str(value): result["macro_dataset_pooled_rgb_psnr_db"]
                for value, result in sorted(results.items())
            }

        toolchain = {
            "lerobot_ffmpeg_path": args.lerobot_ffmpeg,
            "lerobot_ffmpeg_sha256": sha256_file(Path(args.lerobot_ffmpeg)),
            "lerobot_ffmpeg_version": subprocess.run([args.lerobot_ffmpeg, "-version"], check=True, text=True, capture_output=True).stdout.splitlines()[0],
            "ours_ffmpeg_path": args.ours_ffmpeg,
            "ours_ffmpeg_sha256": sha256_file(Path(args.ours_ffmpeg)),
            "ours_ffmpeg_version": subprocess.run([args.ours_ffmpeg, "-version"], check=True, text=True, capture_output=True).stdout.splitlines()[0],
            "metric_ffmpeg_path": args.metric_ffmpeg,
            "metric_ffmpeg_sha256": sha256_file(Path(args.metric_ffmpeg)),
            "metric_ffmpeg_version": subprocess.run([args.metric_ffmpeg, "-version"], check=True, text=True, capture_output=True).stdout.splitlines()[0],
            "ffprobe_path": args.ffprobe,
            "ffprobe_sha256": sha256_file(Path(args.ffprobe)),
            "script_sha256": args.script_sha256,
            "runner_sha256": args.runner_sha256,
            "numpy_version": np.__version__,
            "pillow_version": Image.__version__,
        }
        summary = {
            "schema_version": 3,
            "status": "complete",
            "created_at": now_iso(),
            "manifest_uri": args.manifest_uri,
            "manifest_sha256": manifest_sha,
            "sampling": "10 equally spaced episode indices per dataset; all cameras and all frames",
            "calibration_episodes": len(DATASETS) * 10,
            "calibration_cameras": len(cameras),
            "lerobot_v3_default": {
                "lerobot_version": LEROBOT_VERSION,
                "lerobot_wheel_sha256": LEROBOT_WHEEL_SHA256,
                "lerobot_video_config_sha256": LEROBOT_VIDEO_CONFIG_SHA256,
                "lerobot_video_utils_sha256": LEROBOT_VIDEO_UTILS_SHA256,
                "execution": "frozen CLI-equivalent FFmpeg command; the LeRobot wheel is provenance, not imported by this job",
                "codec": "libsvtav1",
                "pix_fmt": "yuv420p",
                "gop": 2,
                "crf": 30,
                "preset": 12,
                "fast_decode": 0,
                "macro_dataset_pooled_rgb_psnr_db": target,
                "candidate_uri": f"{args.output_uri.rstrip('/')}/candidates/lerobot_v3_default_30.json",
            },
            "selection_rule": (
                "minimum absolute delta to LeRobot three-dataset macro pooled-RGB PSNR; "
                "JPEG QF and x264/x265 CRF are integer-only; tie by smaller encoded bytes"
            ),
            "quality_gate_db": QUALITY_GATES_DB,
            "selected": selections,
            "evaluated_macro_psnr_db": evaluated,
            "encoding_commands": {
                "lerobot_v3_default": (
                    "ffmpeg -hide_banner -loglevel error -y -threads 1 -i INPUT.mp4 "
                    "-map 0:v:0 -an -pix_fmt yuv420p -fps_mode passthrough "
                    "-c:v libsvtav1 -g 2 -crf 30 -preset 12 "
                    "-svtav1-params fast-decode=0 -tag:v av01 -movflags +faststart OUTPUT.mp4"
                ),
                "jpeg_lance": "Pillow Image.save(format=JPEG, quality=SELECTED_QF, subsampling=0, optimize=False)",
                "ours_fast_x264": (
                    "ffmpeg -hide_banner -loglevel error -y -threads 1 -i INPUT.mp4 "
                    "-map 0:v:0 -an -pix_fmt yuv420p -fps_mode passthrough -c:v libx264 "
                    "-preset medium -crf SELECTED_CRF -g 32 -keyint_min 32 -bf 31 "
                    "-x264-params keyint=32:min-keyint=32:scenecut=0:open-gop=1:bframes=31:"
                    "b-adapt=0:b-pyramid=none:rc-lookahead=40:ref=1:threads=1:aud=1 "
                    "[-force_key_frames expr:eq(n,LAST_FRAME)] -tag:v avc1 -movflags +faststart OUTPUT.mp4"
                ),
                "ours_fast_x265": (
                    "ffmpeg -hide_banner -loglevel error -y -threads 1 -i INPUT.mp4 "
                    "-map 0:v:0 -an -pix_fmt yuv420p -fps_mode passthrough -c:v libx265 "
                    "-preset medium -crf SELECTED_CRF -x265-params keyint=32:min-keyint=32:"
                    "scenecut=0:open-gop=1:bframes=31:b-adapt=0:b-pyramid=0:rc-lookahead=40:"
                    "ref=1:frame-threads=1:pools=8 [-force_key_frames expr:eq(n,LAST_FRAME)] "
                    "-tag:v hvc1 -movflags +faststart OUTPUT.mp4"
                ),
            },
            "toolchain": toolchain,
            "elapsed_seconds": time.monotonic() - started,
        }
        summary_payload = canonical_json(summary)
        summary_uri = f"{args.output_uri.rstrip('/')}/selected_quality.json"
        summary_sha = store.put_immutable(summary_uri, summary_payload)
        success = {
            "status": "complete",
            "completed_at": now_iso(),
            "manifest_sha256": manifest_sha,
            "selected_quality_uri": summary_uri,
            "selected_quality_sha256": summary_sha,
            "selected": selections,
        }
        store.put_immutable(f"{args.output_uri.rstrip('/')}/_SUCCESS.json", canonical_json(success))
        log(json.dumps(success, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        failure = {
            "status": "failed",
            "failed_at": now_iso(),
            "manifest_sha256": manifest_sha,
            "error": repr(exc),
            "traceback": traceback.format_exc(),
        }
        failure_uri = f"{args.output_uri.rstrip('/')}/_FAILED_{int(time.time())}.json"
        store.put_immutable(failure_uri, canonical_json(failure))
        raise
    finally:
        shutil.rmtree(work_dir)


if __name__ == "__main__":
    main()
