#!/usr/bin/env python3
"""Formal transcoding for the frozen three-dataset raw-H.264 manifest."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import lance
import numpy as np
import pyarrow as pa
from PIL import Image

import calibrate_data_curate_quality as quality


DATASETS = ("realsource_world", "robocasa", "table30v2")
OURS_FFMPEG_SHA256 = "c85f792acc845cb2be1b47157e37a5515306b260ad2df131a7819b7950837683"

CONTENT_STRUCT = pa.struct([pa.field("path", pa.string()), pa.field("content", pa.large_binary())])
CONVERSATION_STRUCT = pa.struct([pa.field("from", pa.string()), pa.field("value", pa.string())])
ROW_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("qa_type", pa.string()),
        pa.field("calib", pa.string()),
        pa.field("local_pose", pa.string()),
        pa.field("metadata", pa.string()),
        pa.field("image_path", pa.list_(pa.string())),
        pa.field("image_content", pa.list_(CONTENT_STRUCT)),
        pa.field("conversations", pa.list_(CONVERSATION_STRUCT)),
        pa.field("future_traj_info", pa.string()),
    ]
)


def safe_camera_name(value: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.-") or "camera"
    suffix = hashlib.sha256(value.encode()).hexdigest()[:8]
    return f"{readable[:80]}-{suffix}"


def storage_options(credential_file: str, endpoint: str) -> dict[str, str]:
    values = quality.load_env_file(credential_file)
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
    result = {
        "allow_http": "true",
        "oss_access_key_id": access,
        "oss_secret_access_key": secret,
        "oss_endpoint": endpoint,
    }
    if token:
        result["oss_security_token"] = token
    return result


def prefix_exists(store: quality.Store, uri: str) -> bool:
    bucket_name, prefix = quality.split_oss(uri.rstrip("/") + "/guard")
    prefix = prefix.rsplit("/", 1)[0].rstrip("/") + "/"
    result = store.bucket(bucket_name).list_objects_v2(prefix=prefix, max_keys=1)
    return bool(result.object_list)


def episode_identifier(episode: dict[str, Any]) -> str:
    return f"{episode['dataset']}:{episode['source_id']}"


def existing_lance_ids(
    store: quality.Store,
    uri: str,
    options: dict[str, str],
    expected_ids: list[str],
    valid_counts: set[int],
) -> list[str]:
    if not prefix_exists(store, uri):
        return []
    dataset = lance.dataset(uri, storage_options=options)
    observed = dataset.to_table(columns=["id"])["id"].to_pylist()
    if len(observed) not in valid_counts:
        raise ValueError(f"{uri}: row count {len(observed)} is not a completed batch boundary")
    if observed != expected_ids[: len(observed)]:
        raise ValueError(f"{uri}: existing rows are not the expected rank prefix")
    return observed


def weighted_partition(episodes: list[dict[str, Any]], world_size: int) -> tuple[list[list[dict[str, Any]]], list[int]]:
    bins: list[list[dict[str, Any]]] = [[] for _ in range(world_size)]
    loads = [0] * world_size

    def weight(episode: dict[str, Any]) -> int:
        return int(episode["length"]) * sum(int(video["width"]) * int(video["height"]) for video in episode["videos"])

    for episode in sorted(episodes, key=lambda item: (-weight(item), int(item["curated_episode_index"]))):
        rank = min(range(world_size), key=lambda index: (loads[index], len(bins[index]), index))
        bins[rank].append(episode)
        loads[rank] += weight(episode)
    for values in bins:
        values.sort(key=lambda item: int(item["curated_episode_index"]))
    return bins, loads


def probe_video(ffprobe: str, path: Path, expected_codec: str, expected_frames: int, width: int, height: int) -> dict[str, Any]:
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,nb_read_frames:format=format_name",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(completed.stdout)
    streams = payload.get("streams", [])
    if len(streams) != 1:
        raise ValueError(f"{path}: expected one video stream")
    stream = streams[0]
    observed = (str(stream["codec_name"]), int(stream["nb_read_frames"]), int(stream["width"]), int(stream["height"]))
    expected = (expected_codec, expected_frames, width, height)
    if observed != expected:
        raise ValueError(f"{path}: probe {observed} != {expected}")
    content = path.read_bytes()
    moov = content.find(b"moov")
    mdat = content.find(b"mdat")
    if len(content) < 12 or content[4:8] != b"ftyp" or moov < 0 or mdat < 0 or moov > mdat:
        raise ValueError(f"{path}: expected faststart MP4")
    return {"codec": expected_codec, "frames": expected_frames, "width": width, "height": height, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def validate_source_video(ffprobe: str, path: Path, video: dict[str, Any]) -> None:
    if quality.sha256_file(path) != str(video["sha256"]):
        raise ValueError(f"source SHA mismatch: {video['uri']}")
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,nb_read_frames",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    streams = json.loads(completed.stdout).get("streams", [])
    if len(streams) != 1:
        raise ValueError(f"source stream count mismatch: {video['uri']}")
    stream = streams[0]
    observed = (
        str(stream["codec_name"]),
        int(stream["nb_read_frames"]),
        int(stream["width"]),
        int(stream["height"]),
    )
    expected = (
        str(video["codec"]),
        int(video["frames"]),
        int(video["width"]),
        int(video["height"]),
    )
    if observed != expected or observed[0] != "h264":
        raise ValueError(f"source probe mismatch for {video['uri']}: {observed} != {expected}")


def encode_video(
    encoder_ffmpeg: str,
    ffprobe: str,
    camera: quality.Camera,
    profile: str,
    value: int,
    output: Path,
    expected_codec: str,
) -> tuple[bytes, dict[str, Any]]:
    stderr_path = output.with_suffix(output.suffix + ".stderr")
    try:
        started = time.monotonic()
        with stderr_path.open("wb") as stderr:
            completed = subprocess.run(
                quality.video_command(encoder_ffmpeg, camera, profile, value, output),
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
        if completed.returncode:
            error = stderr_path.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(f"{profile}/{camera.source_id}/{camera.camera}: {error[-4000:]}")
        metadata = probe_video(ffprobe, output, expected_codec, camera.frames, camera.width, camera.height)
        metadata["encode_seconds"] = time.monotonic() - started
        return output.read_bytes(), metadata
    finally:
        stderr_path.unlink(missing_ok=True)


def encode_jpeg_frames(metric_ffmpeg: str, camera: quality.Camera, qf: int) -> tuple[list[bytes], dict[str, Any]]:
    process = quality.decoder(metric_ffmpeg, camera.local_path)
    assert process.stdout is not None
    frame_bytes = camera.width * camera.height * 3
    encoded_frames: list[bytes] = []
    started = time.monotonic()
    try:
        for frame_index in range(camera.frames):
            raw = quality.read_exact(process.stdout, frame_bytes)
            if len(raw) != frame_bytes:
                raise RuntimeError(f"{camera.source_id}/{camera.camera}: short source frame {frame_index}")
            rgb = np.frombuffer(raw, dtype=np.uint8).reshape(camera.height, camera.width, 3)
            output = io.BytesIO()
            Image.fromarray(rgb, mode="RGB").save(
                output,
                format="JPEG",
                quality=qf,
                subsampling=0,
                optimize=False,
            )
            payload = output.getvalue()
            if not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
                raise ValueError("invalid JPEG output")
            encoded_frames.append(payload)
        quality.finish_decoder(process, "jpeg-source")
    except Exception:
        process.kill()
        process.wait()
        raise
    return encoded_frames, {
        "frames": camera.frames,
        "width": camera.width,
        "height": camera.height,
        "bytes": sum(map(len, encoded_frames)),
        "encode_seconds": time.monotonic() - started,
    }


def base_row(identifier: str, metadata: dict[str, Any], paths: list[str], content: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": identifier,
        "qa_type": "",
        "calib": "",
        "local_pose": "",
        "metadata": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
        "image_path": [json.dumps({"images": paths}, ensure_ascii=False, separators=(",", ":"))],
        "image_content": content,
        "conversations": [],
        "future_traj_info": "",
    }


def process_episode(
    args: argparse.Namespace,
    store: quality.Store,
    episode: dict[str, Any],
    qf: int,
    x264_crf: int,
    x265_crf: int,
    work_root: Path,
) -> dict[str, Any]:
    curated_index = int(episode["curated_episode_index"])
    episode_dir = work_root / f"episode-{curated_index:06d}"
    episode_dir.mkdir(parents=True, exist_ok=True)
    identifier = episode_identifier(episode)
    cameras: list[quality.Camera] = []
    try:
        for camera_index, video in enumerate(episode["videos"]):
            source = episode_dir / f"source-{camera_index:02d}.mp4"
            actual_bytes = store.download(str(video["uri"]), source)
            if actual_bytes != int(video["bytes"]):
                raise ValueError(f"source byte mismatch: {video['uri']}")
            validate_source_video(args.ffprobe, source, video)
            cameras.append(
                quality.Camera(
                    dataset=str(episode["dataset"]),
                    curated_episode_index=curated_index,
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
                    local_path=str(source),
                )
            )

        camera_order = [camera.camera for camera in cameras]
        jpeg_by_camera: list[list[bytes]] = []
        jpeg_stats: dict[str, Any] = {}
        x264_content: list[dict[str, Any]] = []
        x264_stats: dict[str, Any] = {}
        x265_content: list[dict[str, Any]] = []
        x265_stats: dict[str, Any] = {}
        lerobot_videos: list[dict[str, Any]] = []

        for camera_index, camera in enumerate(cameras):
            camera_key = safe_camera_name(camera.camera)

            lerobot_uri = (
                f"{args.output_uri.rstrip('/')}/lerobot_v3_video_only/{episode['dataset']}/videos/"
                f"chunk-{int(episode['dataset_episode_index']) // 1000:03d}/{camera_key}/"
                f"episode_{int(episode['dataset_episode_index']):06d}.mp4"
            )
            lerobot_path = episode_dir / f"lerobot-{camera_index:02d}.mp4"
            if store.exists(lerobot_uri):
                store.download(lerobot_uri, lerobot_path)
                lerobot_stat = probe_video(
                    args.ffprobe,
                    lerobot_path,
                    "av1",
                    camera.frames,
                    camera.width,
                    camera.height,
                )
                lerobot_stat["encode_seconds"] = 0.0
                lerobot_bytes = lerobot_path.read_bytes()
            else:
                lerobot_bytes, lerobot_stat = encode_video(
                    args.lerobot_ffmpeg,
                    args.ffprobe,
                    camera,
                    "lerobot_v3_default",
                    30,
                    lerobot_path,
                    "av1",
                )
            uploaded_sha = store.put_immutable(lerobot_uri, lerobot_bytes)
            if uploaded_sha != lerobot_stat["sha256"]:
                raise ValueError("LeRobot upload SHA mismatch")
            lerobot_videos.append({"camera": camera.camera, "uri": lerobot_uri, **lerobot_stat})
            lerobot_path.unlink(missing_ok=True)

            x264_path = episode_dir / f"x264-{camera_index:02d}.mp4"
            x264_bytes, current_x264 = encode_video(
                args.ours_ffmpeg,
                args.ffprobe,
                camera,
                "ours_fast_x264",
                x264_crf,
                x264_path,
                "h264",
            )
            embedded_x264 = f"embedded://video/{curated_index:06d}/{camera_key}.mp4"
            x264_content.append({"path": embedded_x264, "content": x264_bytes})
            x264_stats[camera.camera] = current_x264
            x264_path.unlink(missing_ok=True)

            x265_path = episode_dir / f"x265-{camera_index:02d}.mp4"
            x265_bytes, current_x265 = encode_video(
                args.ours_ffmpeg,
                args.ffprobe,
                camera,
                "ours_fast_x265",
                x265_crf,
                x265_path,
                "hevc",
            )
            embedded_x265 = f"embedded://video/{curated_index:06d}/{camera_key}.mp4"
            x265_content.append({"path": embedded_x265, "content": x265_bytes})
            x265_stats[camera.camera] = current_x265
            x265_path.unlink(missing_ok=True)

            jpeg_frames, current_jpeg = encode_jpeg_frames(args.metric_ffmpeg, camera, qf)
            jpeg_by_camera.append(jpeg_frames)
            jpeg_stats[camera.camera] = current_jpeg

        common = {
            "format_version": 1,
            "dataset": episode["dataset"],
            "source_id": episode["source_id"],
            "curated_episode_index": curated_index,
            "dataset_episode_index": int(episode["dataset_episode_index"]),
            "action_horizon": int(episode["length"]),
            "sample_fps": float(cameras[0].fps),
            "camera_name_mapping": {camera: camera for camera in camera_order},
            "source_video_uris": [camera.uri for camera in cameras],
            "manifest_uri": args.manifest_uri,
            "manifest_sha256": args.expected_manifest_sha256,
            "quality_selection_uri": args.quality_uri,
            "quality_selection_sha256": args.expected_quality_sha256,
        }

        jpeg_paths: list[str] = []
        jpeg_content: list[dict[str, Any]] = []
        for frame_index in range(int(episode["length"])):
            for camera_index, camera in enumerate(cameras):
                path = f"embedded://image/{curated_index:06d}/{safe_camera_name(camera.camera)}/frame_{frame_index:06d}.jpg"
                jpeg_paths.append(path)
                jpeg_content.append({"path": path, "content": jpeg_by_camera[camera_index][frame_index]})
        jpeg_metadata = dict(common)
        jpeg_metadata["image_encoding"] = {
            "profile": "jpeg_lance",
            "codec": "jpeg",
            "qf": qf,
            "subsampling": "4:4:4",
            "optimize": False,
            "camera_order": camera_order,
            "cameras": jpeg_stats,
        }

        def video_metadata(profile: str, codec: str, crf: int, stats: dict[str, Any]) -> dict[str, Any]:
            metadata = dict(common)
            metadata["video_encoding"] = {
                "profile": profile,
                "container": "mp4",
                "codec": codec,
                "pix_fmt": "yuv420p",
                "preset": "medium",
                "crf": crf,
                "gop": 32,
                "open_gop": True,
                "bframes": 31,
                "b_adapt": 0,
                "b_pyramid": False,
                "scenecut": 0,
                "camera_order": camera_order,
                "cameras": stats,
            }
            return metadata

        result = {
            "jpeg_lance": base_row(identifier, jpeg_metadata, jpeg_paths, jpeg_content),
            "ours_fast_x264": base_row(
                identifier,
                video_metadata("264ra_fast_medium", "h264", x264_crf, x264_stats),
                [item["path"] for item in x264_content],
                x264_content,
            ),
            "ours_fast_x265": base_row(
                identifier,
                video_metadata("265ra_fast_medium", "hevc", x265_crf, x265_stats),
                [item["path"] for item in x265_content],
                x265_content,
            ),
            "lerobot": {
                "dataset": episode["dataset"],
                "source_id": episode["source_id"],
                "curated_episode_index": curated_index,
                "dataset_episode_index": int(episode["dataset_episode_index"]),
                "length": int(episode["length"]),
                "videos": lerobot_videos,
            },
        }
        return result
    finally:
        shutil.rmtree(episode_dir)


def write_lance_batch(
    uri: str,
    rows: list[dict[str, Any]],
    options: dict[str, str],
    first: bool,
) -> None:
    table = pa.Table.from_pylist(rows, schema=ROW_SCHEMA)
    lance.write_dataset(
        table,
        uri,
        mode="create" if first else "append",
        storage_options=options,
        max_rows_per_file=32,
        max_rows_per_group=4,
    )


def validate_batch_marker(
    marker: dict[str, Any],
    *,
    rank: int,
    batch_index: int,
    start: int,
    end: int,
    expected_ids: list[str],
    manifest_sha: str,
    quality_sha: str,
    script_sha: str,
    quality_script_sha: str,
    runner_sha: str,
) -> None:
    expected = {
        "status": "complete",
        "rank": rank,
        "batch_index": batch_index,
        "start": start,
        "end": end,
        "manifest_sha256": manifest_sha,
        "quality_selection_sha256": quality_sha,
        "script_sha256": script_sha,
        "quality_script_sha256": quality_script_sha,
        "runner_sha256": runner_sha,
    }
    for key, value in expected.items():
        if marker.get(key) != value:
            raise ValueError(f"batch marker {rank}/{batch_index}: {key} mismatch")
    if marker.get("ids") != expected_ids[start:end]:
        raise ValueError(f"batch marker {rank}/{batch_index}: id mismatch")


def validate_rank_marker(
    marker: dict[str, Any],
    *,
    rank: int,
    world_size: int,
    episodes: int,
    manifest_sha: str,
    quality_sha: str,
    targets: dict[str, str],
    script_sha: str,
    quality_script_sha: str,
    runner_sha: str,
) -> None:
    expected = {
        "status": "complete",
        "rank": rank,
        "world_size": world_size,
        "episodes": episodes,
        "manifest_sha256": manifest_sha,
        "quality_selection_sha256": quality_sha,
        "lance_uris": targets,
        "script_sha256": script_sha,
        "quality_script_sha256": quality_script_sha,
        "runner_sha256": runner_sha,
    }
    for key, value in expected.items():
        if marker.get(key) != value:
            raise ValueError(f"rank marker {rank}: {key} mismatch")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--manifest-uri", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--quality-uri", required=True)
    parser.add_argument("--expected-quality-sha256", required=True)
    parser.add_argument("--output-uri", required=True)
    parser.add_argument("--rank", type=int, required=True)
    parser.add_argument("--world-size", type=int, required=True)
    parser.add_argument("--cpus-per-node", type=int, required=True)
    parser.add_argument("--episode-workers", type=int, default=2)
    parser.add_argument("--batch-episodes", type=int, default=2)
    parser.add_argument("--barrier-timeout-seconds", type=float, default=86400)
    parser.add_argument("--lerobot-ffmpeg", required=True)
    parser.add_argument("--ours-ffmpeg", required=True)
    parser.add_argument("--metric-ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--script-sha256", required=True)
    parser.add_argument("--quality-script-sha256", required=True)
    parser.add_argument("--runner-sha256", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    quality.split_oss(args.output_uri.rstrip("/") + "/guard")
    expected_world_size = 10
    expected_cpus = 60
    if args.world_size != expected_world_size or not 0 <= args.rank < args.world_size:
        raise ValueError(f"formal conversion requires world size {expected_world_size}")
    if args.cpus_per_node != expected_cpus:
        raise ValueError(f"formal conversion requires {expected_cpus} CPUs per node")
    if hashlib.sha256(Path(args.ours_ffmpeg).read_bytes()).hexdigest() != OURS_FFMPEG_SHA256:
        raise ValueError("Ours FFmpeg SHA mismatch")

    store = quality.Store(args.credential_file, args.endpoint)
    options = storage_options(args.credential_file, args.endpoint)
    manifest_payload = store.get_bytes(args.manifest_uri)
    manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
    if manifest_sha != args.expected_manifest_sha256:
        raise ValueError("manifest SHA mismatch")
    manifest = json.loads(manifest_payload)
    episodes = manifest.get("episodes", [])
    if len(episodes) != 3000 or Counter(item["dataset"] for item in episodes) != Counter({name: 1000 for name in DATASETS}):
        raise ValueError("invalid formal manifest episode distribution")
    expected_distribution = Counter({name: 1000 for name in DATASETS})
    expected_total = sum(expected_distribution.values())

    quality_payload = store.get_bytes(args.quality_uri)
    quality_sha = hashlib.sha256(quality_payload).hexdigest()
    if quality_sha != args.expected_quality_sha256:
        raise ValueError("quality selection SHA mismatch")
    selection = json.loads(quality_payload)
    if selection.get("status") != "complete" or selection.get("manifest_sha256") != manifest_sha:
        raise ValueError("quality selection contract mismatch")
    if selection.get("toolchain", {}).get("script_sha256") != args.quality_script_sha256:
        raise ValueError("quality selection script SHA mismatch")
    lerobot_contract = selection.get("lerobot_v3_default", {})
    if (
        lerobot_contract.get("codec") != "libsvtav1"
        or lerobot_contract.get("gop") != 2
        or lerobot_contract.get("crf") != 30
        or lerobot_contract.get("preset") != 12
    ):
        raise ValueError("LeRobot quality target is not the frozen v3 default")
    selected = selection.get("selected", {})
    expected_value_names = {
        "jpeg_lance": "qf",
        "ours_fast_x264": "crf",
        "ours_fast_x265": "crf",
    }
    quality_gates = {"jpeg_lance": 0.15, "ours_fast_x264": 0.20, "ours_fast_x265": 0.15}
    if selection.get("quality_gate_db") != quality_gates:
        raise ValueError("quality selection gate contract mismatch")
    for profile, value_name in expected_value_names.items():
        if selected.get(profile, {}).get("value_name") != value_name:
            raise ValueError(f"quality selection missing {profile}/{value_name}")
        if float(selected[profile].get("absolute_delta_to_lerobot_db", float("inf"))) > quality_gates[profile]:
            raise ValueError(f"quality selection exceeds PSNR gate for {profile}")
        if type(selected[profile].get("value")) is not int:
            raise TypeError(f"quality selection is not integer-only for {profile}")
    qf = int(selected["jpeg_lance"]["value"])
    x264_crf = int(selected["ours_fast_x264"]["value"])
    x265_crf = int(selected["ours_fast_x265"]["value"])
    if not 1 <= qf <= 100 or not 0 <= x264_crf <= 51 or not 0 <= x265_crf <= 51:
        raise ValueError("quality selection value outside calibrated search bounds")

    partitions, loads = weighted_partition(episodes, args.world_size)
    shard = partitions[args.rank]
    expected_ids = [episode_identifier(episode) for episode in shard]
    batch_ranges = [
        (batch_index, start, min(start + args.batch_episodes, len(shard)))
        for batch_index, start in enumerate(range(0, len(shard), args.batch_episodes))
    ]
    valid_counts = {0, *(end for _, _, end in batch_ranges)}
    targets = {
        profile: f"{args.output_uri.rstrip('/')}/lance/{profile}/rank-{args.rank:05d}.lance/"
        for profile in ("jpeg_lance", "ours_fast_x264", "ours_fast_x265")
    }
    marker_uri = f"{args.output_uri.rstrip('/')}/markers/rank-{args.rank:05d}.json"
    existing_ids = {
        profile: existing_lance_ids(store, target, options, expected_ids, valid_counts)
        for profile, target in targets.items()
    }
    existing_counts = {profile: len(ids) for profile, ids in existing_ids.items()}
    rank_marker: dict[str, Any] | None = None
    if store.exists(marker_uri):
        rank_marker = json.loads(store.get_bytes(marker_uri))
        validate_rank_marker(
            rank_marker,
            rank=args.rank,
            world_size=args.world_size,
            episodes=len(shard),
            manifest_sha=manifest_sha,
            quality_sha=quality_sha,
            targets=targets,
            script_sha=args.script_sha256,
            quality_script_sha=args.quality_script_sha256,
            runner_sha=args.runner_sha256,
        )
        if any(count != len(shard) for count in existing_counts.values()):
            raise ValueError(f"rank {args.rank}: complete marker has incomplete Lance targets {existing_counts}")
        quality.log(f"formal rank={args.rank}: validated existing complete marker")

    work_root: Path | None = None
    try:
        if rank_marker is None:
            work_root = Path(tempfile.mkdtemp(prefix=f"data-curate-formal-rank-{args.rank}-"))
            started = time.monotonic()
            lerobot_entries: list[dict[str, Any]] = []
            profile_bytes = Counter()
            processed_by_dataset = Counter()
            batch_marker_uris: list[str] = []
            with ThreadPoolExecutor(max_workers=args.episode_workers) as executor:
                for batch_index, start, end in batch_ranges:
                    batch = shard[start:end]
                    batch_marker_uri = (
                        f"{args.output_uri.rstrip('/')}/batch_markers/rank-{args.rank:05d}/"
                        f"batch-{batch_index:05d}.json"
                    )
                    batch_marker_uris.append(batch_marker_uri)
                    if store.exists(batch_marker_uri):
                        batch_marker = json.loads(store.get_bytes(batch_marker_uri))
                        validate_batch_marker(
                            batch_marker,
                            rank=args.rank,
                            batch_index=batch_index,
                            start=start,
                            end=end,
                            expected_ids=expected_ids,
                            manifest_sha=manifest_sha,
                            quality_sha=quality_sha,
                            script_sha=args.script_sha256,
                            quality_script_sha=args.quality_script_sha256,
                            runner_sha=args.runner_sha256,
                        )
                        if any(count < end for count in existing_counts.values()):
                            raise ValueError(
                                f"batch marker {args.rank}/{batch_index} exists before all Lance rows: {existing_counts}"
                            )
                        lerobot_entries.extend(batch_marker["lerobot_entries"])
                        profile_bytes.update(batch_marker["profile_payload_bytes"])
                        processed_by_dataset.update(batch_marker["dataset_episodes"])
                        quality.log(f"formal rank={args.rank}: reused episodes={end}/{len(shard)}")
                        continue

                    futures = [
                        executor.submit(
                            process_episode,
                            args,
                            store,
                            episode,
                            qf,
                            x264_crf,
                            x265_crf,
                            work_root,
                        )
                        for episode in batch
                    ]
                    results = [future.result() for future in futures]
                    batch_profile_bytes = Counter()
                    for profile, target in targets.items():
                        rows = [result[profile] for result in results]
                        current = existing_counts[profile]
                        if current == start:
                            write_lance_batch(target, rows, options, first=current == 0)
                            existing_counts[profile] = end
                        elif current < end:
                            raise ValueError(
                                f"{profile}: existing count {current} intersects batch [{start}, {end})"
                            )
                        batch_profile_bytes[profile] += sum(
                            len(item["content"])
                            for row in rows
                            for item in row["image_content"]
                        )
                    batch_lerobot_entries = [result["lerobot"] for result in results]
                    batch_profile_bytes["lerobot_v3_video_only"] += sum(
                        video["bytes"]
                        for result in results
                        for video in result["lerobot"]["videos"]
                    )
                    batch_datasets = Counter(str(episode["dataset"]) for episode in batch)
                    batch_marker = {
                        "schema_version": 1,
                        "status": "complete",
                        "created_at": quality.now_iso(),
                        "rank": args.rank,
                        "batch_index": batch_index,
                        "start": start,
                        "end": end,
                        "ids": expected_ids[start:end],
                        "manifest_sha256": manifest_sha,
                        "quality_selection_sha256": quality_sha,
                        "script_sha256": args.script_sha256,
                        "quality_script_sha256": args.quality_script_sha256,
                        "runner_sha256": args.runner_sha256,
                        "dataset_episodes": dict(sorted(batch_datasets.items())),
                        "profile_payload_bytes": dict(batch_profile_bytes),
                        "lerobot_entries": batch_lerobot_entries,
                    }
                    store.put_immutable(batch_marker_uri, quality.canonical_json(batch_marker))
                    lerobot_entries.extend(batch_lerobot_entries)
                    profile_bytes.update(batch_profile_bytes)
                    processed_by_dataset.update(batch_datasets)
                    quality.log(f"formal rank={args.rank}: encoded episodes={end}/{len(shard)}")

            if any(count != len(shard) for count in existing_counts.values()):
                raise ValueError(f"rank {args.rank}: final Lance counts mismatch {existing_counts}")
            if len(lerobot_entries) != len(shard):
                raise ValueError(f"rank {args.rank}: LeRobot entry count mismatch")

            lerobot_manifest_uri = f"{args.output_uri.rstrip('/')}/lerobot_v3_video_only/manifests/rank-{args.rank:05d}.json"
            if store.exists(lerobot_manifest_uri):
                lerobot_manifest_payload = store.get_bytes(lerobot_manifest_uri)
                lerobot_manifest = json.loads(lerobot_manifest_payload)
                if (
                    lerobot_manifest.get("status") != "complete"
                    or lerobot_manifest.get("rank") != args.rank
                    or lerobot_manifest.get("manifest_sha256") != manifest_sha
                    or [entry["source_id"] for entry in lerobot_manifest.get("episodes", [])]
                    != [entry["source_id"] for entry in lerobot_entries]
                ):
                    raise ValueError(f"rank {args.rank}: existing LeRobot manifest mismatch")
                lerobot_manifest_sha = hashlib.sha256(lerobot_manifest_payload).hexdigest()
            else:
                lerobot_manifest = {
                    "schema_version": 1,
                    "status": "complete",
                    "created_at": quality.now_iso(),
                    "rank": args.rank,
                    "world_size": args.world_size,
                    "manifest_sha256": manifest_sha,
                    "quality_selection_sha256": quality_sha,
                    "video_only": True,
                    "reason_no_single_lerobot_info": "curated source intentionally preserves heterogeneous fps and resolutions",
                    "episodes": lerobot_entries,
                }
                lerobot_manifest_payload = quality.canonical_json(lerobot_manifest)
                lerobot_manifest_sha = store.put_immutable(lerobot_manifest_uri, lerobot_manifest_payload)

            rank_marker = {
                "schema_version": 2,
                "status": "complete",
                "created_at": quality.now_iso(),
                "rank": args.rank,
                "world_size": args.world_size,
                "manifest_sha256": manifest_sha,
                "quality_selection_sha256": quality_sha,
                "script_sha256": args.script_sha256,
                "quality_script_sha256": args.quality_script_sha256,
                "runner_sha256": args.runner_sha256,
                "ours_ffmpeg_sha256": OURS_FFMPEG_SHA256,
                "episodes": len(shard),
                "dataset_episodes": dict(sorted(processed_by_dataset.items())),
                "pixel_work": loads[args.rank],
                "profile_payload_bytes": dict(profile_bytes),
                "lance_uris": targets,
                "batch_markers": batch_marker_uris,
                "lerobot_manifest_uri": lerobot_manifest_uri,
                "lerobot_manifest_sha256": lerobot_manifest_sha,
                "elapsed_seconds": time.monotonic() - started,
            }
            marker_payload = quality.canonical_json(rank_marker)
            marker_sha = store.put_immutable(marker_uri, marker_payload)
            quality.log(f"rank complete rank={args.rank} marker_sha256={marker_sha}")

        if args.rank == 0:
            deadline = time.monotonic() + args.barrier_timeout_seconds
            marker_uris = [f"{args.output_uri.rstrip('/')}/markers/rank-{rank:05d}.json" for rank in range(args.world_size)]
            while time.monotonic() < deadline:
                missing = [uri for uri in marker_uris if not store.exists(uri)]
                if not missing:
                    break
                quality.log(f"rank0 barrier: waiting_for={len(missing)}")
                time.sleep(30)
            else:
                raise TimeoutError("timed out waiting for rank markers")
            markers = [json.loads(store.get_bytes(uri)) for uri in marker_uris]
            if sum(int(item["episodes"]) for item in markers) != expected_total:
                raise ValueError("rank marker episode total mismatch")
            combined_datasets = Counter()
            for rank, item in enumerate(markers):
                rank_targets = {
                    profile: f"{args.output_uri.rstrip('/')}/lance/{profile}/rank-{rank:05d}.lance/"
                    for profile in targets
                }
                validate_rank_marker(
                    item,
                    rank=rank,
                    world_size=args.world_size,
                    episodes=len(partitions[rank]),
                    manifest_sha=manifest_sha,
                    quality_sha=quality_sha,
                    targets=rank_targets,
                    script_sha=args.script_sha256,
                    quality_script_sha=args.quality_script_sha256,
                    runner_sha=args.runner_sha256,
                )
                combined_datasets.update(item["dataset_episodes"])
            if combined_datasets != expected_distribution:
                raise ValueError(f"rank marker dataset mismatch: {combined_datasets}")
            success_uri = f"{args.output_uri.rstrip('/')}/_SUCCESS.json"
            if store.exists(success_uri):
                success_payload = store.get_bytes(success_uri)
                success = json.loads(success_payload)
                if (
                    success.get("status") != "complete"
                    or success.get("manifest_sha256") != manifest_sha
                    or success.get("quality_selection_sha256") != quality_sha
                    or success.get("episodes") != expected_total
                ):
                    raise ValueError("existing global success marker mismatch")
                quality.log(f"formal already complete success_sha256={hashlib.sha256(success_payload).hexdigest()}")
                return
            success = {
                "schema_version": 1,
                "status": "complete",
                "completed_at": quality.now_iso(),
                "manifest_uri": args.manifest_uri,
                "manifest_sha256": manifest_sha,
                "quality_selection_uri": args.quality_uri,
                "quality_selection_sha256": quality_sha,
                "mode": "formal",
                "world_size": args.world_size,
                "nodes": args.world_size,
                "cpus_per_node": args.cpus_per_node,
                "episodes": expected_total,
                "cameras": sum(len(item["videos"]) for item in episodes),
                "dataset_episodes": dict(sorted(combined_datasets.items())),
                "selected": {"jpeg_qf": qf, "x264_crf": x264_crf, "x265_crf": x265_crf},
                "rank_markers": marker_uris,
                "rank_lance_uris": {profile: [marker["lance_uris"][profile] for marker in markers] for profile in targets},
                "lerobot_rank_manifests": [marker["lerobot_manifest_uri"] for marker in markers],
                "profile_payload_bytes": {
                    profile: sum(int(marker["profile_payload_bytes"].get(profile, 0)) for marker in markers)
                    for profile in ("jpeg_lance", "ours_fast_x264", "ours_fast_x265", "lerobot_v3_video_only")
                },
                "artifacts": {
                    "script_sha256": args.script_sha256,
                    "quality_script_sha256": args.quality_script_sha256,
                    "runner_sha256": args.runner_sha256,
                    "ours_ffmpeg_sha256": OURS_FFMPEG_SHA256,
                    "lerobot_ffmpeg_sha256": quality.sha256_file(Path(args.lerobot_ffmpeg)),
                    "metric_ffmpeg_sha256": quality.sha256_file(Path(args.metric_ffmpeg)),
                    "ffprobe_sha256": quality.sha256_file(Path(args.ffprobe)),
                },
            }
            success_payload = quality.canonical_json(success)
            success_sha = store.put_immutable(success_uri, success_payload)
            quality.log(f"formal complete success_sha256={success_sha}")
    except Exception as exc:
        failure = {
            "status": "failed",
            "failed_at": quality.now_iso(),
            "rank": args.rank,
            "error": repr(exc),
            "traceback": traceback.format_exc(),
        }
        store.put_immutable(
            f"{args.output_uri.rstrip('/')}/failures/rank-{args.rank:05d}-{int(time.time())}.json",
            quality.canonical_json(failure),
        )
        raise
    finally:
        if work_root is not None:
            shutil.rmtree(work_root)


if __name__ == "__main__":
    main()
