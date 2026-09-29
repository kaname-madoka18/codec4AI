#!/usr/bin/env python3
"""Benchmark loaders on the frozen 3x1000 raw-H.264 dataset."""

from __future__ import annotations

import argparse
import functools
import hashlib
import io
import json
import math
import os
import random
import socket
import statistics
import tempfile
import time
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np
import oss2


DATASETS = ("realsource_world", "robocasa", "table30v2")
PATTERNS = ("continuous", "random10", "gap1", "gap3")
IMPLEMENTATIONS = (
    "pyav_native",
    "decord_native",
    "torchcodec_native",
    "lerobot_v3_torchcodec",
    "jpeg_lance",
    "ours_native",
    "ours_fast_x264",
    "ours_fast_x265",
)
MANIFEST_URI = os.environ.get("DATA_CURATE_MANIFEST_URI", "")
MANIFEST_SHA256 = os.environ.get("EXPECTED_MANIFEST_SHA256", "")
PREPROCESS_ROOT = os.environ.get("DATA_CURATE_PREPROCESS_ROOT", "")
PREPROCESS_SUCCESS_SHA256 = os.environ.get("EXPECTED_PREPROCESS_SUCCESS_SHA256", "")
DEFAULT_OUTPUT_ROOT = os.environ.get("DATA_CURATE_OUTPUT_ROOT", "")
DEFAULT_CREDENTIAL_FILE = os.environ.get("OSS_CREDENTIAL_FILE", "")
DEFAULT_ENDPOINT = os.environ.get("OSS_ENDPOINT", "")
PYAV_DECODE_STRATEGY = "keyframe_seek_intra_periods_v1"
LANCE_PROFILE = {
    "jpeg_lance": "jpeg_lance",
    "ours_fast_x264": "ours_fast_x264",
    "ours_fast_x265": "ours_fast_x265",
}


_INDEX: list[dict[str, Any]] = []
_IMPLEMENTATION = ""
_PATTERN = ""
_SEED = 0
_WINDOW = 10
_RANDOM_SPAN = 0
_SAMPLING_STRIDE = 0
_OSS: "OssStore | None" = None
_LANCE_OPTIONS: dict[str, str] = {}
_LANCE_DATASETS: dict[int, Any] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def log(message: str) -> None:
    print(f"[{now_iso()}] {message}", flush=True)


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not Path(path).is_file():
        return values
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def credentials(path: str) -> tuple[str, str, str]:
    values = load_env_file(path)
    access = os.environ.get("OSS_ACCESS_KEY_ID") or values.get("OSS_ACCESS_KEY_ID", "")
    secret = os.environ.get("OSS_ACCESS_KEY_SECRET") or values.get("OSS_ACCESS_KEY_SECRET", "")
    token = (
        os.environ.get("OSS_SECURITY_TOKEN")
        or os.environ.get("OSS_STS_TOKEN")
        or values.get("OSS_SECURITY_TOKEN")
        or values.get("OSS_STS_TOKEN", "")
    )
    if not access or not secret:
        raise RuntimeError("missing OSS credentials")
    return access, secret, token


def normalize_endpoint(endpoint: str) -> str:
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    return "http://" + endpoint


def split_oss(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "oss" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError(f"invalid OSS URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


class OssStore:
    def __init__(self, credential_file: str, endpoint: str):
        access, secret, token = credentials(credential_file)
        self.auth: oss2.Auth | oss2.StsAuth
        self.auth = oss2.StsAuth(access, secret, token) if token else oss2.Auth(access, secret)
        self.endpoint = normalize_endpoint(endpoint)
        self._buckets: dict[str, oss2.Bucket] = {}

    def bucket(self, name: str) -> oss2.Bucket:
        if name not in self._buckets:
            endpoint = normalize_endpoint(json.loads(os.environ.get("OSS_BUCKET_ENDPOINTS", "{}")).get(name, self.endpoint))
            self._buckets[name] = oss2.Bucket(
                self.auth,
                endpoint,
                name,
                connect_timeout=10,
                enable_crc=True,
            )
        return self._buckets[name]

    def get(self, uri: str) -> bytes:
        bucket_name, key = split_oss(uri)
        error: Exception | None = None
        for attempt in range(3):
            try:
                return self.bucket(bucket_name).get_object(key).read()
            except Exception as exc:  # noqa: BLE001 - OSS retry boundary
                error = exc
                if attempt < 2:
                    time.sleep(0.1 * (2**attempt))
        raise RuntimeError(f"failed to read {uri}: {error}")

    def exists(self, uri: str) -> bool:
        bucket_name, key = split_oss(uri)
        return self.bucket(bucket_name).object_exists(key)

    def put_immutable(self, uri: str, payload: bytes) -> str:
        bucket_name, key = split_oss(uri)
        bucket = self.bucket(bucket_name)
        if bucket.object_exists(key):
            existing = bucket.get_object(key).read()
            if existing != payload:
                raise FileExistsError(f"refusing to overwrite {uri}")
            return sha256(existing)
        bucket.put_object(key, payload, headers={"Content-Type": "application/json"})
        return sha256(payload)


def lance_storage_options(credential_file: str, endpoint: str) -> dict[str, str]:
    access, secret, token = credentials(credential_file)
    result = {
        "allow_http": "true",
        "oss_access_key_id": access,
        "oss_secret_access_key": secret,
        "oss_endpoint": endpoint.removeprefix("http://").removeprefix("https://"),
    }
    if token:
        result["oss_security_token"] = token
    return result


def episode_id(episode: dict[str, Any]) -> str:
    return f"{episode['dataset']}:{episode['source_id']}"


def weighted_partitions(episodes: list[dict[str, Any]], world_size: int = 10) -> list[list[dict[str, Any]]]:
    bins: list[list[dict[str, Any]]] = [[] for _ in range(world_size)]
    loads = [0] * world_size

    def weight(episode: dict[str, Any]) -> int:
        return int(episode["length"]) * sum(
            int(video["width"]) * int(video["height"]) for video in episode["videos"]
        )

    for episode in sorted(episodes, key=lambda item: (-weight(item), int(item["curated_episode_index"]))):
        rank = min(range(world_size), key=lambda item: (loads[item], len(bins[item]), item))
        bins[rank].append(episode)
        loads[rank] += weight(episode)
    for values in bins:
        values.sort(key=lambda item: int(item["curated_episode_index"]))
    return bins


def prepare_index(
    implementation: str,
    store: OssStore,
    credential_file: str,
    endpoint: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not MANIFEST_SHA256 or not PREPROCESS_SUCCESS_SHA256:
        raise ValueError("EXPECTED_MANIFEST_SHA256 and EXPECTED_PREPROCESS_SUCCESS_SHA256 are required")
    manifest_payload = store.get(MANIFEST_URI)
    if sha256(manifest_payload) != MANIFEST_SHA256:
        raise ValueError("manifest SHA mismatch")
    manifest = json.loads(manifest_payload)
    episodes = manifest.get("episodes", [])
    if (
        len(episodes) != 3000
        or Counter(item["dataset"] for item in episodes) != Counter({name: 1000 for name in DATASETS})
        or [int(item["curated_episode_index"]) for item in episodes] != list(range(3000))
    ):
        raise ValueError("invalid frozen manifest")

    success_payload = store.get(f"{PREPROCESS_ROOT}/_SUCCESS.json")
    if sha256(success_payload) != PREPROCESS_SUCCESS_SHA256:
        raise ValueError("preprocess success SHA mismatch")
    success = json.loads(success_payload)
    if success.get("status") != "complete" or success.get("episodes") != 3000:
        raise ValueError("preprocess success contract mismatch")

    index = [
        {
            "id": episode_id(episode),
            "dataset": episode["dataset"],
            "length": int(episode["length"]),
            "videos": episode["videos"],
        }
        for episode in episodes
    ]
    representation: dict[str, Any] = {
        "kind": "native_mp4",
        "manifest_uri": MANIFEST_URI,
        "manifest_sha256": MANIFEST_SHA256,
    }
    if implementation == "lerobot_v3_torchcodec":
        by_id: dict[str, dict[str, Any]] = {}
        manifest_shas: dict[str, str] = {}
        for rank in range(10):
            uri = f"{PREPROCESS_ROOT}/lerobot_v3_video_only/manifests/rank-{rank:05d}.json"
            payload = store.get(uri)
            manifest_shas[uri] = sha256(payload)
            current = json.loads(payload)
            if current.get("status") != "complete" or current.get("rank") != rank:
                raise ValueError(f"invalid LeRobot rank manifest {rank}")
            for entry in current.get("episodes", []):
                identifier = f"{entry['dataset']}:{entry['source_id']}"
                if identifier in by_id:
                    raise ValueError(f"duplicate LeRobot ID: {identifier}")
                by_id[identifier] = entry
        if set(by_id) != {item["id"] for item in index}:
            raise ValueError("LeRobot manifests do not cover frozen IDs")
        for item in index:
            mapped = by_id[item["id"]]
            source_videos = item["videos"]
            output_videos = mapped["videos"]
            if [video["camera"] for video in output_videos] != [video["camera"] for video in source_videos]:
                raise ValueError(f"{item['id']}: LeRobot camera order mismatch")
            item["videos"] = [
                {**output, "fps": float(source["fps"])}
                for output, source in zip(output_videos, source_videos)
            ]
            if int(mapped["length"]) != item["length"]:
                raise ValueError(f"{item['id']}: LeRobot length mismatch")
        representation = {
            "kind": "lerobot_v3_video_only",
            "preprocess_success_sha256": PREPROCESS_SUCCESS_SHA256,
            "rank_manifest_sha256": manifest_shas,
            "codec": "av1",
            "encoder": "LeRobot 0.6.1 v3 RGB default",
        }
    elif implementation in LANCE_PROFILE:
        profile = LANCE_PROFILE[implementation]
        locations: dict[str, tuple[int, int]] = {}
        partitions = weighted_partitions(episodes)
        for rank, partition in enumerate(partitions):
            for row, episode in enumerate(partition):
                locations[episode_id(episode)] = (rank, row)
        if len(locations) != 3000:
            raise ValueError("Lance location map is incomplete")
        for item in index:
            rank, row = locations[item["id"]]
            item["lance_rank"] = rank
            item["lance_row"] = row
            item.pop("videos", None)
        representation = {
            "kind": "lance_embedded_media",
            "profile": profile,
            "preprocess_success_sha256": PREPROCESS_SUCCESS_SHA256,
            "shards": [f"{PREPROCESS_ROOT}/lance/{profile}/rank-{rank:05d}.lance/" for rank in range(10)],
            "selected_quality": success["selected"],
            "lance_storage_options_present": bool(lance_storage_options(credential_file, endpoint)),
        }
    return index, representation


def deterministic_frame_indices(
    identifier: str,
    frame_count: int,
    pattern: str,
    seed: int,
    window: int,
    random_span: int = 0,
    sampling_stride: int = 0,
) -> list[int]:
    if frame_count < 50:
        raise ValueError(f"{identifier}: frame count below curated minimum")
    digest = hashlib.sha256(f"{seed}:{pattern}:{identifier}".encode()).digest()
    value = int.from_bytes(digest[:8], "big")
    if pattern == "random10":
        if random_span < 0:
            raise ValueError("random_span must be nonnegative")
        if not random_span:
            return sorted(random.Random(value).sample(range(frame_count), window))
        candidate_span = min(frame_count, random_span)
        if candidate_span < window:
            raise ValueError(f"{identifier}: random candidate span below output window")
        generator = random.Random(value)
        start = generator.randrange(frame_count - candidate_span + 1)
        return sorted(generator.sample(range(start, start + candidate_span), window))
    stride = sampling_stride or {"continuous": 1, "gap1": 2, "gap3": 4}[pattern]
    span = (window - 1) * stride + 1
    choices = frame_count - span + 1
    if choices <= 0:
        raise ValueError(f"{identifier}: insufficient frames for {pattern}")
    start = value % choices
    return [start + offset * stride for offset in range(window)]


def frame_fingerprint(frames: Any) -> tuple[int, list[int], str]:
    try:
        import torch

        if isinstance(frames, torch.Tensor):
            tensor = frames.detach().cpu()
            shape = [int(value) for value in tensor.shape]
            dtype = str(tensor.dtype)
            flat = tensor.reshape(-1)
            values = bytes([int(flat[0]) & 0xFF, int(flat[-1]) & 0xFF])
            return zlib.crc32(values) or 1, shape, dtype
    except ImportError:
        pass
    array = np.asarray(frames)
    shape = [int(value) for value in array.shape]
    dtype = str(array.dtype)
    flat = array.reshape(-1)
    values = bytes([int(flat[0]) & 0xFF, int(flat[-1]) & 0xFF])
    return zlib.crc32(values) or 1, shape, dtype


def decode_pyav(payload: bytes, indices: list[int]) -> tuple[Any, dict[str, int]]:
    import av
    from bisect import bisect_right

    if not indices or any(type(index) is not int or index < 0 for index in indices):
        raise ValueError("PyAV indices must be nonempty nonnegative integers")

    container = av.open(io.BytesIO(payload), mode="r")
    try:
        if "mp4" not in container.format.name.split(",") or len(container.streams.video) != 1:
            raise ValueError("PyAV indexed seeking requires a single-video-stream MP4")
        stream = container.streams.video[0]
        if stream.codec_context.name not in {"h264", "hevc"}:
            raise ValueError("PyAV sample indexing supports H.264/HEVC MP4")
        stream.codec_context.thread_count = 1
        # Index presentation order without decoding images. Index construction
        # remains inside the timed decode call. Discarded edit-list preroll is
        # retained as a seek anchor, but is not a requested display frame.
        display_pts: list[int] = []
        key_pts: list[int] = []
        for packet in container.demux(stream):
            if not packet.size:
                continue
            if packet.pts is None:
                raise ValueError("PyAV sample has no presentation timestamp")
            if packet.is_keyframe:
                key_pts.append(int(packet.pts))
            if not packet.is_discard:
                display_pts.append(int(packet.pts))
        display_pts.sort()
        key_pts = sorted(set(key_pts))
        if not key_pts or len(display_pts) != len(set(display_pts)):
            raise ValueError("PyAV requires keyframes and unique sample PTS")
        if max(indices) >= len(display_pts):
            raise IndexError("PyAV frame index exceeds the video length")
        groups: dict[int, set[int]] = defaultdict(set)
        for index in indices:
            pts = display_pts[index]
            key_slot = bisect_right(key_pts, pts) - 1
            if key_slot < 0:
                raise ValueError("PyAV target precedes its first usable keyframe")
            groups[key_slot].add(pts)
        selected: dict[int, Any] = {}
        output_frames = 0
        for key_slot, targets in sorted(groups.items()):
            # Jump directly between requested intra periods. FFmpeg performs
            # normal forward decoding, including B-frame reordering/EOF flush.
            container.seek(key_pts[key_slot], stream=stream, backward=True, any_frame=False)
            remaining = targets.copy()
            for frame in container.decode(stream):
                output_frames += 1
                if frame.pts in remaining:
                    selected[frame.pts] = frame.to_ndarray(format="rgb24")
                    remaining.remove(frame.pts)
                if not remaining:
                    break
                if frame.pts is not None and frame.pts > max(targets):
                    break
            if remaining:
                raise ValueError(f"PyAV seek did not produce timestamps {sorted(remaining)}")
        return np.stack([selected[display_pts[index]] for index in indices]), {"frames_output": output_frames}
    finally:
        container.close()


def decode_decord(payload: bytes, indices: list[int]) -> tuple[Any, dict[str, int]]:
    import decord

    reader = decord.VideoReader(io.BytesIO(payload), ctx=decord.cpu(0), num_threads=1)
    if len(reader) <= max(indices):
        raise ValueError(f"Decord frame count {len(reader)} below target {max(indices)}")
    frames = reader.get_batch(indices).asnumpy()
    return frames, {"frames_output": len(indices)}


def decode_torchcodec(
    payload: bytes,
    indices: list[int],
    *,
    approximate: bool,
    fps: float,
) -> tuple[Any, dict[str, int]]:
    from torchcodec.decoders import VideoDecoder

    decoder = VideoDecoder(
        payload,
        dimension_order="NCHW" if approximate else "NHWC",
        num_ffmpeg_threads=1,
        device="cpu",
        seek_mode="approximate" if approximate else "exact",
    )
    selected_indices = indices
    if approximate:
        average_fps = float(decoder.metadata.average_fps)
        timestamps = [index / fps for index in indices]
        selected_indices = [round(timestamp * average_fps) for timestamp in timestamps]
    batch = decoder.get_frames_at(indices=selected_indices)
    return batch.data, {"frames_output": len(selected_indices)}


def decode_video_loader(payload: bytes, indices: list[int]) -> tuple[Any, dict[str, int]]:
    import video_loader

    frames, info = video_loader.decode(payload, indices, return_info=True)
    return frames, {"frames_output": int(info["frames_output"])}


def init_worker(
    index_path: str,
    implementation: str,
    pattern: str,
    seed: int,
    window: int,
    random_span: int,
    sampling_stride: int,
    credential_file: str,
    endpoint: str,
    _worker_id: int,
) -> None:
    global _INDEX, _IMPLEMENTATION, _PATTERN, _SEED, _WINDOW, _RANDOM_SPAN, _SAMPLING_STRIDE, _OSS, _LANCE_OPTIONS, _LANCE_DATASETS
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    _INDEX = json.loads(Path(index_path).read_text(encoding="utf-8"))
    _IMPLEMENTATION = implementation
    _PATTERN = pattern
    _SEED = seed
    _WINDOW = window
    _RANDOM_SPAN = random_span
    _SAMPLING_STRIDE = sampling_stride
    _OSS = OssStore(credential_file, endpoint)
    _LANCE_OPTIONS = lance_storage_options(credential_file, endpoint)
    _LANCE_DATASETS = {}
    try:
        import torch

        torch.set_num_threads(1)
    except ImportError:
        pass


def lance_row(entry: dict[str, Any]) -> tuple[dict[str, Any], int]:
    import lance

    rank = int(entry["lance_rank"])
    if rank not in _LANCE_DATASETS:
        profile = LANCE_PROFILE[_IMPLEMENTATION]
        uri = f"{PREPROCESS_ROOT}/lance/{profile}/rank-{rank:05d}.lance/"
        _LANCE_DATASETS[rank] = lance.dataset(uri, storage_options=_LANCE_OPTIONS)
    table = _LANCE_DATASETS[rank].take([int(entry["lance_row"])])
    row = table.to_pylist()[0]
    if row["id"] != entry["id"]:
        raise ValueError(f"Lance ID mismatch: {row['id']} != {entry['id']}")
    return row, int(table.nbytes)


def process_episode(curated_index: int) -> dict[str, Any]:
    entry = _INDEX[curated_index]
    identifier = entry["id"]
    frame_indices = deterministic_frame_indices(
        identifier,
        int(entry["length"]),
        _PATTERN,
        _SEED,
        _WINDOW,
        _RANDOM_SPAN,
        _SAMPLING_STRIDE,
    )
    load_started = time.perf_counter()
    row_bytes = 0
    media_bytes = 0
    payloads: list[tuple[bytes, float]] = []
    jpeg_selected: list[bytes] = []
    cameras = 0
    if _IMPLEMENTATION in LANCE_PROFILE:
        row, row_bytes = lance_row(entry)
        metadata = json.loads(row["metadata"])
        contents = row["image_content"]
        media_bytes = sum(len(item["content"]) for item in contents)
        if _IMPLEMENTATION == "jpeg_lance":
            encoding = metadata["image_encoding"]
            camera_order = list(encoding["camera_order"])
            cameras = len(camera_order)
            if len(contents) != int(entry["length"]) * cameras:
                raise ValueError(f"{identifier}: JPEG content count mismatch")
            for frame_index in frame_indices:
                for camera_index in range(cameras):
                    jpeg_selected.append(contents[frame_index * cameras + camera_index]["content"])
        else:
            encoding = metadata["video_encoding"]
            camera_order = list(encoding["camera_order"])
            cameras = len(camera_order)
            if len(contents) != cameras:
                raise ValueError(f"{identifier}: video content count mismatch")
            for item, camera in zip(contents, camera_order):
                payloads.append((item["content"], float(metadata["sample_fps"])))
    else:
        assert _OSS is not None
        videos = entry["videos"]
        cameras = len(videos)
        for video in videos:
            payload = _OSS.get(video["uri"])
            if len(payload) != int(video["bytes"]):
                raise ValueError(f"{identifier}: object size mismatch")
            payloads.append((payload, float(video.get("fps", 0) or 0)))
            media_bytes += len(payload)
        row_bytes = media_bytes
    load_seconds = time.perf_counter() - load_started

    decode_started = time.perf_counter()
    checksum = 0
    selected_frames = 0
    output_frames = 0
    output_shapes: list[list[int]] = []
    output_dtypes: list[str] = []
    if _IMPLEMENTATION == "jpeg_lance":
        from PIL import Image

        for payload in jpeg_selected:
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
                rgb = image.convert("RGB")
                shape = [rgb.height, rgb.width, 3]
                first = rgb.getpixel((0, 0))
                last = rgb.getpixel((rgb.width - 1, rgb.height - 1))
                checksum = zlib.crc32(bytes((*first, *last)), checksum)
                output_shapes.append(shape)
                output_dtypes.append("uint8")
                selected_frames += 1
                output_frames += 1
    else:
        for payload, fps in payloads:
            if _IMPLEMENTATION == "pyav_native":
                frames, info = decode_pyav(payload, frame_indices)
            elif _IMPLEMENTATION == "decord_native":
                frames, info = decode_decord(payload, frame_indices)
            elif _IMPLEMENTATION == "torchcodec_native":
                frames, info = decode_torchcodec(payload, frame_indices, approximate=False, fps=fps)
            elif _IMPLEMENTATION == "lerobot_v3_torchcodec":
                if fps <= 0:
                    raise ValueError(f"{identifier}: invalid LeRobot fps")
                frames, info = decode_torchcodec(payload, frame_indices, approximate=True, fps=fps)
            elif _IMPLEMENTATION in ("ours_native", "ours_fast_x264", "ours_fast_x265"):
                frames, info = decode_video_loader(payload, frame_indices)
            else:
                raise ValueError(f"unsupported implementation {_IMPLEMENTATION}")
            current, shape, dtype = frame_fingerprint(frames)
            checksum = zlib.crc32(current.to_bytes(4, "little"), checksum)
            output_shapes.append(shape)
            output_dtypes.append(dtype)
            selected_frames += len(frame_indices)
            output_frames += int(info["frames_output"])
    decode_seconds = time.perf_counter() - decode_started
    if selected_frames != cameras * _WINDOW or not checksum:
        raise ValueError(f"{identifier}: decoded output contract mismatch")
    return {
        "id": identifier,
        "dataset": entry["dataset"],
        "curated_episode_index": curated_index,
        "load_seconds": load_seconds,
        "decode_seconds": decode_seconds,
        "row_bytes": row_bytes,
        "media_bytes": media_bytes,
        "selected_frames": selected_frames,
        "frames_output": output_frames,
        "cameras": cameras,
        "checksum": checksum,
        "output_shapes": output_shapes,
        "output_dtypes": output_dtypes,
    }


class CuratedIndexDataset:
    def __init__(self, indices: list[int]):
        self.indices = indices

    def __len__(self) -> int:
        return len(self.indices)

    def __getitem__(self, position: int) -> dict[str, Any]:
        return process_episode(self.indices[position])


def identity_collate(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return batch


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def stats(values: list[float]) -> dict[str, float]:
    return {
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p95": percentile(values, 0.95),
        "min": min(values),
        "max": max(values),
    }


def build_batch_sequence(index: list[dict[str, Any]], seed: int, warmup_steps: int, steps: int, batch_size: int) -> tuple[list[list[int]], list[list[int]]]:
    def distribute(total: int) -> dict[str, int]:
        base, remainder = divmod(total, len(DATASETS))
        return {
            dataset: base + int(offset < remainder)
            for offset, dataset in enumerate(DATASETS)
        }

    warmup_batches_by_dataset = distribute(warmup_steps)
    measured_batches_by_dataset = distribute(steps)
    warmup_batches: list[list[int]] = []
    measured_batches: list[list[int]] = []
    for dataset_offset, dataset in enumerate(DATASETS):
        candidates = [position for position, item in enumerate(index) if item["dataset"] == dataset]
        warmup_count = warmup_batches_by_dataset[dataset] * batch_size
        measured_count = measured_batches_by_dataset[dataset] * batch_size
        required = warmup_count + measured_count
        draws: list[int] = []
        cycle = 0
        while len(draws) < required:
            current = list(candidates)
            random.Random(seed + dataset_offset + cycle * 100_003).shuffle(current)
            draws.extend(current)
            cycle += 1
        draws = draws[:required]
        warmup = draws[:warmup_count]
        measured = draws[warmup_count:]
        warmup_batches.extend([warmup[offset : offset + batch_size] for offset in range(0, len(warmup), batch_size)])
        measured_batches.extend([measured[offset : offset + batch_size] for offset in range(0, len(measured), batch_size)])
    random.Random(seed + 10_000).shuffle(warmup_batches)
    random.Random(seed + 20_000).shuffle(measured_batches)
    return warmup_batches, measured_batches


def cgroup_cpu_usage_seconds() -> tuple[str, float]:
    candidates = (Path("/sys/fs/cgroup/cpu.stat"), Path("/sys/fs/cgroup/cpuacct/cpuacct.usage"))
    if candidates[0].is_file():
        values = {}
        for line in candidates[0].read_text(encoding="utf-8").splitlines():
            key, value = line.split()
            values[key] = int(value)
        if "usage_usec" not in values:
            raise RuntimeError("cgroup v2 cpu.stat lacks usage_usec")
        return str(candidates[0]), values["usage_usec"] / 1_000_000.0
    if candidates[1].is_file():
        return str(candidates[1]), int(candidates[1].read_text().strip()) / 1_000_000_000.0
    raise RuntimeError("container cgroup CPU accounting is unavailable")


def effective_sampling_stride(args: argparse.Namespace) -> int | None:
    if args.sampling_mode == "random10":
        return None
    return args.sampling_stride or {"continuous": 1, "gap1": 2, "gap3": 4}[args.sampling_mode]


def validate_existing(report: dict[str, Any], args: argparse.Namespace) -> None:
    if args.implementation == "pyav_native" and report.get("pyav_decode_strategy") != PYAV_DECODE_STRATEGY:
        raise ValueError("existing PyAV result uses a different decoding strategy; use a new output root")
    expected = {
        "status": "complete",
        "implementation": args.implementation,
        "sampling_mode": args.sampling_mode,
        "manifest_sha256": MANIFEST_SHA256,
        "preprocess_success_sha256": PREPROCESS_SUCCESS_SHA256,
    }
    if args.protocol == "sensitivity":
        expected.update({"protocol": args.protocol, "config_id": args.config_id})
    for key, value in expected.items():
        if report.get(key) != value:
            raise ValueError(f"existing result {key} mismatch")
    config = report.get("config", {})
    expected_config = {
        "workers": args.workers,
        "batch_size": args.batch_size,
        "warmup_steps": args.warmup_steps,
        "steps": args.steps,
        "seed": args.seed,
        "window": args.window,
        "random_candidate_span": args.random_span if args.sampling_mode == "random10" else None,
        "random_candidate_span_short_episode": (
            "use all available frames when frame_count is below random_candidate_span"
            if args.sampling_mode == "random10" and args.random_span
            else None
        ),
    }
    if args.protocol == "sensitivity":
        expected_config["cpu_limit"] = args.cpu_limit
        expected_config["sampling_stride"] = effective_sampling_stride(args)
    for key, value in expected_config.items():
        if config.get(key) != value:
            raise ValueError(f"existing result config.{key} mismatch")
    artifacts = report.get("artifacts", {})
    for key, env_name in (
        ("script_sha256", "EXPECTED_SCRIPT_SHA256"),
        ("runner_sha256", "EXPECTED_RUNNER_SHA256"),
        ("wheel_sha256", "EXPECTED_WHEEL_SHA256"),
    ):
        expected_value = os.environ.get(env_name)
        if expected_value and artifacts.get(key) != expected_value:
            raise ValueError(f"existing result artifacts.{key} mismatch")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--implementation", choices=IMPLEMENTATIONS, required=True)
    parser.add_argument("--sampling-mode", choices=PATTERNS, required=True)
    parser.add_argument("--protocol", choices=("formal", "sensitivity"), default="formal")
    parser.add_argument("--config-id", default="")
    parser.add_argument("--credential-file", default=DEFAULT_CREDENTIAL_FILE)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--cpu-limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--warmup-steps", type=int, default=10)
    parser.add_argument("--steps", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260729)
    parser.add_argument("--window", type=int, default=10)
    parser.add_argument(
        "--random-span",
        type=int,
        default=0,
        help="For random10, choose the output frames inside one contiguous span; 0 means the full episode.",
    )
    parser.add_argument(
        "--sampling-stride",
        type=int,
        default=0,
        help="Override the deterministic non-random stride; 0 keeps the sampling-mode default.",
    )
    parser.add_argument("--prefetch-factor", type=int, default=1)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--resume-existing", action="store_true")
    args = parser.parse_args()
    if min(args.workers, args.batch_size, args.steps, args.window, args.prefetch_factor) <= 0:
        raise ValueError("benchmark counts must be positive")
    if args.warmup_steps < 0:
        raise ValueError("warmup steps must be nonnegative")
    if args.random_span < 0 or (args.random_span and args.random_span < args.window):
        raise ValueError("random-span must be zero or at least window")
    if args.sampling_stride < 0:
        raise ValueError("sampling-stride must be nonnegative")
    if args.sampling_mode == "random10" and args.sampling_stride:
        raise ValueError("sampling-stride is not applicable to random10")
    if args.protocol == "formal":
        if args.config_id or args.sampling_stride or args.cpu_limit:
            raise ValueError("formal protocol does not accept sensitivity overrides")
        if args.steps != 500 or args.warmup_steps != 10 or args.batch_size != 8 or args.workers != 32:
            raise ValueError("formal protocol is frozen at W32 / B8 / warmup10 / measured500")
        output_uri = f"{args.output_root.rstrip('/')}/results/{args.implementation}/{args.sampling_mode}.json"
    else:
        if args.implementation not in IMPLEMENTATIONS:
            raise ValueError("sensitivity protocol only supports main-table implementations")
        if args.cpu_limit not in {8, 16, 32}:
            raise ValueError("sensitivity protocol requires cpu-limit 8, 16, or 32")
        if not args.config_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in args.config_id):
            raise ValueError("sensitivity protocol requires a lowercase filesystem-safe config-id")
        if args.steps != 100 or args.warmup_steps != 10 or args.prefetch_factor != 1:
            raise ValueError("sensitivity protocol is frozen at warmup10 / measured100 / prefetch1")
        output_uri = f"{args.output_root.rstrip('/')}/results/{args.config_id}/{args.implementation}.json"
    store = OssStore(args.credential_file, args.endpoint)
    if store.exists(output_uri):
        existing_payload = store.get(output_uri)
        existing = json.loads(existing_payload)
        validate_existing(existing, args)
        if args.resume_existing:
            log(f"reused existing result uri={output_uri} sha256={sha256(existing_payload)}")
            return
        raise FileExistsError(f"result already exists: {output_uri}")

    index, representation = prepare_index(args.implementation, store, args.credential_file, args.endpoint)
    warmup_batches, measured_batches = build_batch_sequence(index, args.seed, args.warmup_steps, args.steps, args.batch_size)
    flattened = [value for batch in warmup_batches + measured_batches for value in batch]
    measured_batch_datasets = [index[batch[0]]["dataset"] for batch in measured_batches]
    if any(any(index[value]["dataset"] != index[batch[0]]["dataset"] for value in batch) for batch in measured_batches):
        raise ValueError("measured batch mixes datasets")
    expected_measured_batches = {
        dataset: args.steps // len(DATASETS) + int(offset < args.steps % len(DATASETS))
        for offset, dataset in enumerate(DATASETS)
    }
    if Counter(measured_batch_datasets) != Counter(expected_measured_batches):
        raise ValueError("measured batch distribution mismatch")

    with tempfile.TemporaryDirectory(prefix="data-curate-loader-index-") as temporary:
        index_path = Path(temporary) / "index.json"
        index_path.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        import torch
        from torch.utils.data import DataLoader

        worker_init = functools.partial(
            init_worker,
            str(index_path),
            args.implementation,
            args.sampling_mode,
            args.seed,
            args.window,
            args.random_span,
            args.sampling_stride,
            args.credential_file,
            args.endpoint,
        )
        loader = DataLoader(
            CuratedIndexDataset(flattened),
            batch_size=args.batch_size,
            shuffle=False,
            num_workers=args.workers,
            persistent_workers=True,
            prefetch_factor=args.prefetch_factor,
            multiprocessing_context="spawn",
            collate_fn=identity_collate,
            worker_init_fn=worker_init,
            pin_memory=False,
        )
        iterator = iter(loader)
        for step in range(args.warmup_steps):
            batch = next(iterator)
            if len(batch) != args.batch_size:
                raise ValueError("short warmup batch")
            log(f"warmup={step + 1}/{args.warmup_steps}")

        cgroup_path, cpu_before = cgroup_cpu_usage_seconds()
        measured_start_utc = now_iso()
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
                log(f"step={step + 1}/{args.steps} mean_wait={statistics.fmean(step_seconds):.6f}s")
        benchmark_elapsed = time.perf_counter() - benchmark_started
        measured_end_utc = now_iso()
        _, cpu_after = cgroup_cpu_usage_seconds()
        del iterator
        del loader

    if len(results) != args.steps * args.batch_size or len(step_seconds) != args.steps:
        raise ValueError("measured result cardinality mismatch")
    if any(value <= 0 or not math.isfinite(value) for value in step_seconds):
        raise ValueError("invalid raw step time")
    dataset_results: dict[str, Any] = {}
    for dataset in DATASETS:
        current = [item for item in results if item["dataset"] == dataset]
        expected_steps = expected_measured_batches[dataset]
        if len(current) != expected_steps * args.batch_size or len(step_by_dataset[dataset]) != expected_steps:
            raise ValueError(f"{dataset}: measured distribution mismatch")
        dataset_results[dataset] = {
            "episodes": len(current),
            "steps": len(step_by_dataset[dataset]),
            "step_seconds": stats(step_by_dataset[dataset]),
            "step_seconds_raw": step_by_dataset[dataset],
            "media_bytes": sum(int(item["media_bytes"]) for item in current),
            "media_mib_per_episode": sum(int(item["media_bytes"]) for item in current) / len(current) / 2**20,
        }
    macro_mean = statistics.fmean(dataset_results[name]["step_seconds"]["mean"] for name in DATASETS)
    macro_p95 = statistics.fmean(dataset_results[name]["step_seconds"]["p95"] for name in DATASETS)
    macro_media = statistics.fmean(dataset_results[name]["media_mib_per_episode"] for name in DATASETS)
    cpu_seconds = cpu_after - cpu_before
    average_cpu_cores = cpu_seconds / benchmark_elapsed
    if cpu_seconds <= 0 or not math.isfinite(average_cpu_cores) or average_cpu_cores <= 0:
        raise ValueError("invalid cgroup CPU measurement")
    checksum_xor = functools.reduce(lambda left, right: left ^ int(right["checksum"]), results, 0)
    if not checksum_xor:
        checksum_xor = functools.reduce(lambda value, item: zlib.crc32(item["id"].encode(), value), results, 1)
    report = {
        "pyav_decode_strategy": PYAV_DECODE_STRATEGY if args.implementation == "pyav_native" else None,
        "schema_version": 1,
        "status": "complete",
        "created_at_utc": now_iso(),
        "implementation": args.implementation,
        "sampling_mode": args.sampling_mode,
        "protocol": args.protocol,
        "config_id": args.config_id or None,
        "output_uri": output_uri,
        "manifest_uri": MANIFEST_URI,
        "manifest_sha256": MANIFEST_SHA256,
        "preprocess_root": PREPROCESS_ROOT,
        "preprocess_success_sha256": PREPROCESS_SUCCESS_SHA256,
        "representation": representation,
        "config": {
            "workers": args.workers,
            "cpu_limit": args.cpu_limit or None,
            "batch_size": args.batch_size,
            "warmup_steps": args.warmup_steps,
            "steps": args.steps,
            "prefetch_factor": args.prefetch_factor,
            "persistent_workers": True,
            "multiprocessing_context": "spawn",
            "seed": args.seed,
            "window": args.window,
            "sampling_stride": effective_sampling_stride(args),
            "random_candidate_span": args.random_span if args.sampling_mode == "random10" else None,
            "random_candidate_span_short_episode": (
                "use all available frames when frame_count is below random_candidate_span"
                if args.sampling_mode == "random10" and args.random_span
                else None
            ),
            "measured_episodes": args.steps * args.batch_size,
            "measured_batches_per_dataset": expected_measured_batches,
            "episode_sampling": "deterministic shuffled full-dataset cycles; repeat only after one complete cycle",
            "full_media_materialization": True,
            "decoded_image_full_scan": False,
            "backend_threads": 1,
            "proxy_disabled": True,
        },
        "artifacts": {
            "script_sha256": os.environ.get("EXPECTED_SCRIPT_SHA256"),
            "runner_sha256": os.environ.get("EXPECTED_RUNNER_SHA256"),
            "wheel_sha256": os.environ.get("EXPECTED_WHEEL_SHA256"),
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
            "episodes": len(results),
            "step_seconds": stats(step_seconds),
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
        },
    }
    payload = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    uploaded_sha = store.put_immutable(output_uri, payload)
    log(
        f"BENCHMARK_COMPLETE implementation={args.implementation} pattern={args.sampling_mode} "
        f"macro_mean_ms={macro_mean * 1000:.3f} macro_p95_ms={macro_p95 * 1000:.3f} "
        f"cpu_cores={average_cpu_cores:.3f} media_mib_ep={macro_media:.3f} "
        f"uri={output_uri} sha256={uploaded_sha}"
    )


if __name__ == "__main__":
    main()
