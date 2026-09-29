class VideoLoaderError(RuntimeError):
    """Base exception raised by video_loader."""


class InvalidMP4Error(VideoLoaderError):
    """The input is not a usable MP4 payload."""


class UnsupportedVideoError(VideoLoaderError):
    """The input uses a codec or container feature not supported by this build."""


class DecodeError(VideoLoaderError):
    """FFmpeg could not decode the requested frames."""


class EncodeError(VideoLoaderError):
    """FFmpeg/libx264/libx265 could not encode the input frames."""
