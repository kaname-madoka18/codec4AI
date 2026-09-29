from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Literal, overload

import numpy as np

from . import _native
from ._exceptions import (
    DecodeError,
    EncodeError,
    InvalidMP4Error,
    UnsupportedVideoError,
    VideoLoaderError,
)


Source = str | os.PathLike[str] | bytes | bytearray | memoryview
TranscodeMode = Literal[
    "base264", "base265", "fast264", "fast265", "ufast264"
]


def _normalize_source(source: Source) -> str | bytes:
    if isinstance(source, (str, os.PathLike)):
        return os.fspath(source)
    if isinstance(source, bytes):
        return source
    if isinstance(source, (bytearray, memoryview)):
        return bytes(source)
    raise TypeError("source must be a path-like object or bytes-like MP4 payload")


def _translate_error(exc: RuntimeError) -> VideoLoaderError:
    message = str(exc)
    if message.startswith("invalid mp4:"):
        return InvalidMP4Error(message.removeprefix("invalid mp4:").strip())
    if message.startswith("unsupported:"):
        return UnsupportedVideoError(message.removeprefix("unsupported:").strip())
    if message.startswith("encode:"):
        return EncodeError(message.removeprefix("encode:").strip())
    if message.startswith("decode:"):
        return DecodeError(message.removeprefix("decode:").strip())
    return VideoLoaderError(message)


def inspect(source: Source) -> dict[str, Any]:
    """Inspect the first video track and its dependency-index capabilities."""

    try:
        return dict(_native.inspect(_normalize_source(source)))
    except RuntimeError as exc:
        raise _translate_error(exc) from exc


@overload
def decode(
    source: Source,
    indices: Iterable[int],
    *,
    return_info: Literal[False] = False,
) -> np.ndarray: ...


@overload
def decode(
    source: Source,
    indices: Iterable[int],
    *,
    return_info: Literal[True],
) -> tuple[np.ndarray, dict[str, Any]]: ...


def decode(
    source: Source,
    indices: Iterable[int],
    *,
    return_info: bool = False,
) -> np.ndarray | tuple[np.ndarray, dict[str, Any]]:
    """Decode zero-based display-frame indices into a uint8 RGB NHWC array.

    Indices may be unsorted and repeated. Internally each unique frame is decoded
    once and the result is restored to caller order.
    """

    requested = [int(index) for index in indices]
    try:
        frames, info = _native.decode(_normalize_source(source), requested)
    except RuntimeError as exc:
        raise _translate_error(exc) from exc
    if return_info:
        return frames, dict(info)
    return frames


def transcode(
    frames: np.ndarray,
    *,
    fps: float,
    mode: TranscodeMode = "base264",
    crf: float = 18,
    output: str | os.PathLike[str] | None = None,
) -> bytes | Path:
    """Encode ``[T,H,W,3]`` uint8 RGB frames into an H.264/H.265 MP4.

    When ``output`` is omitted, MP4 bytes are returned. Otherwise the requested
    path is atomically replaced and the resolved :class:`~pathlib.Path` is
    returned.
    """

    array = np.asarray(frames)
    if mode not in {"base264", "base265", "fast264", "fast265", "ufast264"}:
        raise ValueError(
            "mode must be 'base264', 'base265', 'fast264', 'fast265', "
            "or 'ufast264'"
        )

    destination: Path | None
    if output is None:
        destination = None
        descriptor, temporary = tempfile.mkstemp(prefix="video_loader_", suffix=".mp4")
        os.close(descriptor)
        native_path = temporary
    else:
        destination = Path(output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp.mp4", dir=destination.parent
        )
        os.close(descriptor)
        native_path = temporary

    try:
        try:
            _native.transcode(array, float(fps), mode, float(crf), native_path)
        except RuntimeError as exc:
            raise _translate_error(exc) from exc
        if destination is None:
            return Path(native_path).read_bytes()
        os.replace(native_path, destination)
        return destination
    finally:
        try:
            os.unlink(native_path)
        except FileNotFoundError:
            pass


class Video:
    """Convenience wrapper retaining an immutable path/bytes source.

    The native sample index is rebuilt for each call in v0.3. The class keeps the
    public API stable for a future cached native index implementation.
    """

    def __init__(self, source: Source):
        self._source = _normalize_source(source)

    @property
    def info(self) -> dict[str, Any]:
        return inspect(self._source)

    def decode(
        self, indices: Iterable[int], *, return_info: bool = False
    ) -> np.ndarray | tuple[np.ndarray, dict[str, Any]]:
        return decode(self._source, indices, return_info=return_info)

    def close(self) -> None:
        return None

    def __enter__(self) -> "Video":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
