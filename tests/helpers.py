from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np


def ffmpeg_rgb(path: Path, indices: list[int], height: int, width: int) -> np.ndarray:
    ordered = sorted(set(indices))
    expression = "+".join(f"eq(n\\,{index})" for index in ordered)
    completed = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            f"select={expression}",
            "-vsync",
            "0",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    unique = np.frombuffer(completed.stdout, dtype=np.uint8).reshape(
        len(ordered), height, width, 3
    )
    lookup = {index: slot for slot, index in enumerate(ordered)}
    return np.stack([unique[lookup[index]] for index in indices])


def ffprobe(path: Path) -> dict:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_streams",
            "-show_packets",
            "-show_frames",
            "-show_entries",
            (
                "stream=codec_name,profile,codec_tag_string,pix_fmt,"
                "nal_length_size,has_b_frames:"
                "packet=pts,pos,size:"
                "frame=best_effort_timestamp,pict_type,key_frame"
            ),
            "-of",
            "json",
            str(path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    payload = json.loads(completed.stdout)
    combined = payload.get("packets_and_frames")
    if combined is not None:
        payload["packets"] = [
            item for item in combined if item.get("type") == "packet"
        ]
        payload["frames"] = [
            item for item in combined if item.get("type") == "frame"
        ]
    return payload


def avcc_vcl_headers(path: Path, packet: dict, length_size: int) -> list[int]:
    with path.open("rb") as handle:
        handle.seek(int(packet["pos"]))
        payload = handle.read(int(packet["size"]))
    headers: list[int] = []
    cursor = 0
    while cursor < len(payload):
        size = int.from_bytes(payload[cursor : cursor + length_size], "big")
        cursor += length_size
        assert size > 0 and cursor + size <= len(payload)
        header = payload[cursor]
        if header & 0x1F in (1, 5):
            headers.append(header)
        cursor += size
    assert headers
    return headers


def hvcc_vcl_types(path: Path, packet: dict, length_size: int = 4) -> list[int]:
    with path.open("rb") as handle:
        handle.seek(int(packet["pos"]))
        payload = handle.read(int(packet["size"]))
    nal_types: list[int] = []
    cursor = 0
    while cursor < len(payload):
        size = int.from_bytes(payload[cursor : cursor + length_size], "big")
        cursor += length_size
        assert size > 0 and cursor + size <= len(payload)
        nal_type = (payload[cursor] >> 1) & 0x3F
        if nal_type <= 31:
            nal_types.append(nal_type)
        cursor += size
    assert nal_types
    return nal_types
