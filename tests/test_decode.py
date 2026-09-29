from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import video_loader

from .helpers import ffmpeg_rgb


@pytest.mark.parametrize("codec", ["h264", "hevc", "av1"])
def test_decode_matches_full_ffmpeg(
    codec_samples: dict[str, Path], codec: str
) -> None:
    path = codec_samples[codec]
    info = video_loader.inspect(path)
    assert info["codec"] == codec
    assert info["frame_count"] == 17
    assert info["sample_count"] >= info["frame_count"]
    assert info["has_valid_sample_offsets"] is True

    indices = [8, 0, 8, 16, 3]
    actual, metrics = video_loader.decode(path, indices, return_info=True)
    expected = ffmpeg_rgb(path, indices, info["height"], info["width"])
    np.testing.assert_array_equal(actual, expected)
    assert actual.dtype == np.uint8
    assert actual.shape == (len(indices), 48, 64, 3)
    assert metrics["requested_frames"] == 5
    assert metrics["unique_requested_frames"] == 4
    if codec == "av1":
        assert metrics["closure_mode"] == "complete_packets_av1"
    elif metrics["reference_graph_available"]:
        assert metrics["closure_mode"] == "reference_graph_bfs"
        assert metrics["closure_groups"] >= 1
        assert metrics["packets_sent"] == (
            metrics["state_only_packets"]
            + metrics["frames_reconstructed"]
        )
        assert metrics["frames_reconstructed"] == metrics["frames_output"]
    else:
        assert metrics["closure_mode"] == "contiguous_no_reference_graph"


def test_bytes_source_and_video_wrapper(codec_samples: dict[str, Path]) -> None:
    payload = codec_samples["h264"].read_bytes()
    with video_loader.Video(payload) as video:
        assert video.info["codec"] == "h264"
        direct = video_loader.decode(payload, [0, 5])
        wrapped = video.decode([0, 5])
    np.testing.assert_array_equal(direct, wrapped)


def test_decode_validation(codec_samples: dict[str, Path]) -> None:
    path = codec_samples["h264"]
    with pytest.raises(video_loader.DecodeError, match="must not be empty"):
        video_loader.decode(path, [])
    with pytest.raises(video_loader.DecodeError, match="non-negative"):
        video_loader.decode(path, [-1])
    with pytest.raises(video_loader.DecodeError, match="exceeds video length"):
        video_loader.decode(path, [100])
    with pytest.raises(video_loader.InvalidMP4Error):
        video_loader.inspect(b"not an mp4")
