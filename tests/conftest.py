from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def run(command: list[str]) -> None:
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.fixture(scope="session")
def codec_samples(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg CLI is required for codec integration fixtures")
    root = tmp_path_factory.mktemp("codec_samples")
    common = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=12",
        "-frames:v",
        "17",
        "-pix_fmt",
        "yuv420p",
        "-an",
    ]
    outputs: dict[str, Path] = {}

    outputs["h264"] = root / "h264.mp4"
    run(
        common
        + [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-movflags",
            "+faststart",
            "-y",
            str(outputs["h264"]),
        ]
    )

    outputs["hevc"] = root / "hevc.mp4"
    run(
        common
        + [
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-x265-params",
            "log-level=error",
            "-movflags",
            "+faststart",
            "-y",
            str(outputs["hevc"]),
        ]
    )

    outputs["av1"] = root / "av1.mp4"
    run(
        common
        + [
            "-c:v",
            "libaom-av1",
            "-strict",
            "experimental",
            "-cpu-used",
            "8",
            "-crf",
            "35",
            "-b:v",
            "0",
            "-row-mt",
            "1",
            "-movflags",
            "+faststart",
            "-y",
            str(outputs["av1"]),
        ]
    )
    return outputs
