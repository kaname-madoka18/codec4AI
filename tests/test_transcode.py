from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

import video_loader

from .helpers import avcc_vcl_headers, ffmpeg_rgb, ffprobe, hvcc_vcl_types


MODES = ["base264", "base265", "fast264", "fast265", "ufast264"]


def make_frames(count: int = 18, height: int = 48, width: int = 64) -> np.ndarray:
    y, x = np.mgrid[:height, :width]
    frames = np.empty((count, height, width, 3), dtype=np.uint8)
    for index in range(count):
        frames[index, :, :, 0] = (x * 3 + index * 11) % 256
        frames[index, :, :, 1] = (y * 5 + index * 7) % 256
        frames[index, :, :, 2] = ((x + y) * 2 + index * 13) % 256
    return frames


@pytest.mark.parametrize("mode", MODES)
def test_transcode_roundtrip(tmp_path: Path, mode: str) -> None:
    frames = make_frames()
    path = tmp_path / f"{mode}.mp4"
    result = video_loader.transcode(
        frames[:, :, ::-1, :], fps=30, mode=mode, crf=18, output=path
    )
    assert result == path.resolve()
    info = video_loader.inspect(path)
    expected_codec = "hevc" if mode.endswith("265") else "h264"
    assert info["codec"] == expected_codec
    assert info["frame_count"] == len(frames)
    assert info["fps"] == 30
    stream = ffprobe(path)["streams"][0]
    assert stream["codec_name"] == expected_codec
    assert stream["codec_tag_string"] == (
        "hvc1" if expected_codec == "hevc" else "avc1"
    )
    assert stream["profile"] == "Main"
    assert stream["pix_fmt"] == "yuv420p"

    indices = [0, 1, 7, 8, 16, 17]
    actual, metrics = video_loader.decode(path, indices, return_info=True)
    expected = ffmpeg_rgb(path, indices, 48, 64)
    np.testing.assert_array_equal(actual, expected)
    if metrics["reference_graph_available"]:
        assert metrics["closure_mode"] == "reference_graph_bfs"
    else:
        assert metrics["closure_mode"] == "contiguous_no_reference_graph"


@pytest.mark.parametrize(
    ("mode", "width", "height", "frame_count", "seed"),
    [
        ("base264", 226, 304, 17, 20260829),
        ("base265", 178, 88, 36, 20261831),
        ("fast264", 278, 50, 25, 20262832),
        ("fast265", 276, 102, 65, 20263830),
        ("ufast264", 118, 230, 20, 20264838),
    ],
)
def test_non_macroblock_aligned_width_roundtrip(
    tmp_path: Path,
    mode: str,
    width: int,
    height: int,
    frame_count: int,
    seed: int,
) -> None:
    rng = np.random.default_rng(seed)
    frames = rng.integers(
        0, 256, size=(frame_count, height, width, 3), dtype=np.uint8
    )
    path = tmp_path / f"{mode}_{width}x{height}.mp4"
    video_loader.transcode(frames, fps=25, mode=mode, crf=23, output=path)

    middle = frame_count // 2
    indices = [frame_count - 1, 0, middle, 3, middle]
    actual = video_loader.decode(path, indices)
    expected = ffmpeg_rgb(path, indices, height, width)
    assert actual.flags.c_contiguous
    np.testing.assert_array_equal(actual, expected)


def test_transcode_bytes() -> None:
    payload = video_loader.transcode(make_frames(5), fps=12, crf=22)
    assert isinstance(payload, bytes)
    assert payload[4:8] == b"ftyp"
    assert video_loader.inspect(payload)["codec"] == "h264"
    assert video_loader.inspect(payload)["frame_count"] == 5


@pytest.mark.parametrize("mode", ["base264", "base265"])
def test_base_intra_period_is_32(tmp_path: Path, mode: str) -> None:
    path = tmp_path / f"{mode}.mp4"
    video_loader.transcode(
        make_frames(70), fps=30, mode=mode, crf=18, output=path
    )
    frames = ffprobe(path)["frames"]
    assert [
        index for index, frame in enumerate(frames) if frame["pict_type"] == "I"
    ] == [0, 32, 64]


@pytest.mark.parametrize("mode", ["fast264", "fast265", "ufast264"])
def test_fast_bitstream_contract(tmp_path: Path, mode: str) -> None:
    path = tmp_path / f"{mode}.mp4"
    video_loader.transcode(
        make_frames(), fps=30, mode=mode, crf=18, output=path
    )
    probe = ffprobe(path)
    stream = probe["streams"][0]
    expected_codec = "hevc" if mode == "fast265" else "h264"
    expected_tag = "hvc1" if mode == "fast265" else "avc1"
    assert stream["codec_name"] == expected_codec
    assert stream["codec_tag_string"] == expected_tag
    assert stream["profile"] == "Main"
    assert stream["pix_fmt"] == "yuv420p"
    frames = probe["frames"]
    assert [
        index for index, frame in enumerate(frames) if frame["pict_type"] == "I"
    ] == [0, 8, 16, 17]
    assert all(
        frame["pict_type"] == "B"
        for index, frame in enumerate(frames)
        if index not in {0, 8, 16, 17}
    )

    packets_by_pts = {
        int(packet["pts"]): packet for packet in probe["packets"]
    }
    for frame in frames:
        if frame["pict_type"] != "B":
            continue
        packet = packets_by_pts[int(frame["best_effort_timestamp"])]
        if expected_codec == "h264":
            length_size = int(stream["nal_length_size"])
            headers = avcc_vcl_headers(path, packet, length_size)
            assert all(((header >> 5) & 0x03) == 0 for header in headers)
        else:
            # HEVC non-reference VCL NAL unit types are even; reference
            # variants are the adjacent odd values (TRAIL/RADL/RASL_R).
            assert all(nal_type % 2 == 0 for nal_type in hvcc_vcl_types(path, packet))


@pytest.mark.parametrize(
    ("mode", "entropy_coding_mode_flag"),
    [("base264", 1), ("fast264", 1), ("ufast264", 0)],
)
def test_h264_entropy_coding_mode(
    tmp_path: Path, mode: str, entropy_coding_mode_flag: int
) -> None:
    path = tmp_path / f"{mode}.mp4"
    video_loader.transcode(
        make_frames(), fps=30, mode=mode, crf=18, output=path
    )

    traced = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "verbose",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-bsf:v",
            "trace_headers",
            "-f",
            "null",
            "-",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert "entropy_coding_mode_flag" in traced.stderr
    expected = (
        "entropy_coding_mode_flag                                    "
        f"{entropy_coding_mode_flag} = {entropy_coding_mode_flag}"
    )
    assert expected in traced.stderr


@pytest.mark.parametrize("mode", MODES)
def test_reference_graph_bfs_decodes_each_dependency_once(
    tmp_path: Path, mode: str
) -> None:
    path = tmp_path / f"{mode}_sparse.mp4"
    video_loader.transcode(
        make_frames(65), fps=30, mode=mode, crf=18, output=path
    )

    if not video_loader.inspect(path)["reference_graph_available"]:
        pytest.skip("bundled FFmpeg reference-graph callback is unavailable")

    indices = [3, 35, 59]
    actual, metrics = video_loader.decode(path, indices, return_info=True)
    expected = ffmpeg_rgb(path, indices, 48, 64)
    np.testing.assert_array_equal(actual, expected)
    assert metrics["closure_mode"] == "reference_graph_bfs"
    # Every packet is submitted at most once. State-only packets advance the
    # DPB without pixel reconstruction; every target/reference dependency is
    # reconstructed and output exactly once.
    assert metrics["packets_sent"] == (
        metrics["state_only_packets"] + metrics["frames_reconstructed"]
    )
    assert metrics["frames_reconstructed"] == metrics["frames_output"]
    assert metrics["packets_sent"] <= 65
    assert metrics["frames_reconstructed"] <= 65
    if mode in {"fast264", "fast265", "ufast264"}:
        # Three B targets in disjoint GOPs need two I anchors each;
        # intermediate GOP anchors are not dependencies.
        assert metrics["frames_reconstructed"] == 9
        assert metrics["state_only_packets"] > 0


@pytest.mark.parametrize("mode", MODES)
def test_reference_graph_only_parses_target_segment(
    tmp_path: Path, mode: str
) -> None:
    path = tmp_path / f"{mode}_target_segment.mp4"
    video_loader.transcode(
        make_frames(97), fps=30, mode=mode, crf=18, output=path
    )

    if not video_loader.inspect(path)["reference_graph_available"]:
        pytest.skip("bundled FFmpeg reference-graph callback is unavailable")

    indices = [83, 67, 75, 67]
    actual, metrics = video_loader.decode(path, indices, return_info=True)
    expected = ffmpeg_rgb(path, indices, 48, 64)
    np.testing.assert_array_equal(actual, expected)
    assert metrics["closure_mode"] == "reference_graph_bfs"
    assert metrics["reference_graph_segment_begin"] > 0
    assert metrics["reference_graph_segment_end"] < 97
    segment_packets = (
        metrics["reference_graph_segment_end"]
        - metrics["reference_graph_segment_begin"]
        + 1
    )
    assert 0 < metrics["reference_graph_packets"] <= segment_packets
    assert metrics["reference_graph_packets"] < 97
    assert metrics["reference_graph_attempts"] == 1


def test_transcode_validation(tmp_path: Path) -> None:
    frames = make_frames(3)
    with pytest.raises(video_loader.EncodeError, match="dtype must be uint8"):
        video_loader.transcode(frames.astype(np.float32), fps=30)
    with pytest.raises(video_loader.EncodeError, match="shape must be"):
        video_loader.transcode(frames[:, :, :, 0], fps=30)
    with pytest.raises(video_loader.EncodeError, match="even width and height"):
        video_loader.transcode(frames[:, :, :-1], fps=30)
    with pytest.raises(video_loader.EncodeError, match="CRF"):
        video_loader.transcode(frames, fps=30, crf=52)
    with pytest.raises(video_loader.EncodeError, match="fps"):
        video_loader.transcode(frames, fps=0)
    for invalid_mode in ["other", "base", "fastRA"]:
        with pytest.raises(ValueError, match="mode"):
            video_loader.transcode(  # type: ignore[arg-type]
                frames, fps=30, mode=invalid_mode
            )
