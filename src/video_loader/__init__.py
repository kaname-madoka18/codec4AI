"""Dependency-aware MP4 frame extraction and H.264/H.265 transcoding."""

from ._api import Video, decode, inspect, transcode
from ._exceptions import (
    DecodeError,
    EncodeError,
    InvalidMP4Error,
    UnsupportedVideoError,
    VideoLoaderError,
)

__all__ = [
    "DecodeError",
    "EncodeError",
    "InvalidMP4Error",
    "UnsupportedVideoError",
    "Video",
    "VideoLoaderError",
    "decode",
    "inspect",
    "transcode",
]

__version__ = "0.3.0"
