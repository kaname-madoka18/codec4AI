# video-loader

English | [Chinese](API_cn.md)

Linux CPU package for extracting selected display frames from H.264, HEVC and
AV1 MP4 files and for encoding RGB NumPy videos with libx264 or libx265.

## Install

Install the locally built CPython 3.12 wheel on glibc 2.31 or newer:

```bash
python -m pip install video_loader-0.3.0-cp312-cp312-manylinux_2_31_x86_64.whl
```

The wheel includes a minimal FFmpeg 8.1.2 runtime, x264 and x265 under a
private RPATH. It does not require a system FFmpeg installation or
`LD_LIBRARY_PATH`.

A source build remains available for development. It needs a C++17 compiler,
`pkg-config`, pybind11, and development packages for libavformat, libavcodec,
libavutil and libswscale. FFmpeg must have the libx264 and libx265 encoders
enabled. The bundled-wheel pipeline is `tools/build_release.sh`; see
[the build guide](BUILDING.md) for exact dependencies and commands.

## Quick start

```python
import video_loader

info = video_loader.inspect("episode.mp4")
# info["codec"], info["width"], info["height"], info["frame_count"], ...

frames, metrics = video_loader.decode(
    "episode.mp4", [17, 2, 17], return_info=True
)
# frames: uint8 RGB, shape [3, H, W, 3]
# Output order and repeated indices are preserved.

payload = video_loader.transcode(
    frames, fps=30, mode="fast265", crf=18
)
# payload: bytes containing an H.265/HEVC MP4
```

All reading APIs accept either a filesystem path (`str` or `os.PathLike`) or
an in-memory MP4 payload (`bytes`, `bytearray` or `memoryview`).

## API

### `inspect`

```python
video_loader.inspect(source) -> dict
```

Inspects the first video track without returning decoded pixels. The result
contains:

| Key | Meaning |
| --- | --- |
| `container` | Container name; currently `"mp4"`. |
| `codec` | Video codec: `"h264"`, `"hevc"` or `"av1"`. |
| `width`, `height` | Display-frame dimensions in pixels. |
| `sample_count` | Number of compressed video samples/packets. |
| `frame_count` | Number of display frames. |
| `fps` | Nominal frame rate. |
| `duration_seconds` | Video-track duration in seconds. |
| `closure_policy` | Dependency policy that `decode` will use. |
| `reference_graph_available` | Whether the private H.264/HEVC reference graph is available. |
| `reference_edges` | Number of direct picture-reference edges discovered. |
| `has_valid_sample_offsets` | `True` after all MP4 sample offsets pass validation. |

Example with an in-memory payload:

```python
from pathlib import Path

payload = Path("episode.mp4").read_bytes()
info = video_loader.inspect(payload)
print(info["codec"], info["frame_count"], info["fps"])
```

### `decode`

```python
video_loader.decode(source, indices, *, return_info=False) -> numpy.ndarray
video_loader.decode(source, indices, *, return_info=True) -> (numpy.ndarray, dict)
```

`indices` is a non-empty iterable of zero-based display-frame indices. Indices
may be unsorted or repeated: each unique frame is decoded once internally, then
the output is restored to the requested order. The returned array has dtype
`uint8`, RGB channel order and shape `[len(indices), height, width, 3]`.

With `return_info=True`, the second return value reports the work performed:

| Key | Meaning |
| --- | --- |
| `codec`, `width`, `height`, `total_frames` | Basic stream information. |
| `requested_frames` | Number of requested indices, including duplicates. |
| `unique_requested_frames` | Number of distinct requested indices. |
| `packets_sent` | Compressed packets submitted to the decoder. |
| `state_only_packets` | Packets used only to advance codec state. |
| `frames_reconstructed` | Pictures for which pixels were reconstructed. |
| `frames_output` | Unique reconstructed pictures output by the decoder. |
| `compressed_bytes` | Bytes in all submitted compressed packets. |
| `reconstructed_bytes` | Compressed bytes belonging to pixel-reconstructed packets. |
| `index_seconds`, `decode_seconds` | Index/dependency analysis time and pixel-pass time. |
| `reference_graph_available`, `reference_edges` | Reference-graph availability and size. |
| `reference_graph_segment_begin`, `reference_graph_segment_end` | Inclusive decode-order bounds of the single target-covering graph segment, or `-1` when unavailable. |
| `reference_graph_packets` | Packets actually parsed for that graph; discardable leading pictures are excluded. |
| `reference_graph_attempts` | Candidate random-access anchors tried before finding a valid segment. |
| `closure_groups` | Number of merged dependency execution ranges. |
| `closure_mode` | Policy actually used for this call. |

### `transcode`

```python
video_loader.transcode(
    frames,
    *,
    fps,
    mode="base264",
    crf=18,
    output=None,
) -> bytes | pathlib.Path
```

Encodes an RGB NumPy array to an H.264 or H.265/HEVC yuv420p MP4. `frames` must
have dtype `uint8`, shape `[T, H, W, 3]`, positive dimensions, and even width
and height. `fps` must be finite and in `(0, 1000]`; `crf` must be finite and
in `[0, 51]`. Lower CRF values generally produce higher quality and larger
files.

When `output` is omitted, the complete MP4 is returned as `bytes`. When it is a
path, parent directories are created as needed, the destination is atomically
replaced, and its absolute `pathlib.Path` is returned:

```python
from pathlib import Path

output_path = video_loader.transcode(
    frames,
    fps=30,
    mode="base265",
    crf=20,
    output=Path("outputs/episode.mp4"),
)
```

The five supported modes are:

| Mode | Codec | Entropy coding | GOP/reference structure |
| --- | --- | --- | --- |
| `base264` | H.264 Main, x264 medium | CABAC | Fixed GOP 32, 3 B frames, B-pyramid enabled. |
| `base265` | HEVC Main, x265 medium | CABAC | Fixed GOP 32, 3 B frames, B-pyramid enabled. |
| `fast264` | H.264 Main, x264 medium | CABAC | GOP 8, 7 non-reference B frames, B-pyramid disabled. |
| `fast265` | HEVC Main, x265 medium | CABAC | GOP 8, 7 non-reference B frames, B-pyramid disabled. |
| `ufast264` | H.264 Main, x264 medium | CAVLC | Same GOP/reference structure as `fast264`. |

`base264` is the default. The former `base` and `fastRA` names are not
accepted in version 0.3.

### `Video`

```python
with video_loader.Video(source) as video:
    info = video.info
    frames = video.decode([0, 10, 20])
```

`Video` retains an immutable path or bytes source and exposes the same
`inspect` information through `video.info` and the same arguments/returns
through `video.decode`. `close()` is currently a no-op. In version 0.3 the
native sample index is still rebuilt for every `info` or `decode` call; the
wrapper does not provide decode/index caching yet.

### Exceptions

Native failures are translated to this public hierarchy:

```text
VideoLoaderError (RuntimeError)
├── InvalidMP4Error
├── UnsupportedVideoError
├── DecodeError
└── EncodeError
```

Invalid `mode` values passed to `transcode` raise `ValueError`, while an
unsupported `source` Python type raises `TypeError`.

## Dependency-aware decoding

The dependency selection policy is reported as `closure_mode` in decode
metrics. For H.264/HEVC, the loader first chooses one valid segment that covers
all requested display frames: it starts at the nearest preceding random-access
anchor and ends at the last target in decode order. Open-GOP leading pictures
displayed before that anchor are discardable and are not submitted. If a
candidate still has an unavailable dependency, the start moves to an earlier
anchor. The bundled FFmpeg then parses slice headers only inside the validated
segment, maintains the codec DPB/RPS state without entropy-decoding blocks or
reconstructing pixels, and exports each compressed frame's active reference
lists. For every target, the loader performs a breadth-first traversal over
these lists to obtain the pixel-reconstruction closure.

During the pixel pass, packets in the same validated target-covering segment,
through the last selected dependency, are submitted in decode order. Packets
outside the closure run in `state_only` mode: they update POC, MMCO, RPS and
DPB state but skip entropy decoding, inverse transforms, motion compensation
and loop filtering. Overlapping execution ranges are merged, so a compressed
packet is submitted at most once and a target/reference picture is
reconstructed at most once per decode call. This single `reference_graph_bfs`
path is independent of the encoder's base, fast or ufast profile.

Decode metrics distinguish `packets_sent`, `state_only_packets`,
`frames_reconstructed`, `frames_output`, `compressed_bytes`, and
`reconstructed_bytes`. In the sparse path,
`packets_sent == state_only_packets + frames_reconstructed`.

A development source build against unpatched system FFmpeg cannot obtain this
private reference graph. It selects one conservative contiguous closure before
decoding (`contiguous_no_reference_graph`) and does not retry partially decoded
closures.

AV1 currently uses the complete packet set because stock FFmpeg does not expose
the custom `av1f` metadata used by the research build. This is correctness-first
and is not a claim of a mathematically minimal AV1 packet set.

Valid MP4 files already carry sample location tables. This version rejects
missing or out-of-range sample offsets and does not attempt a speculative remux.

The bundled FFmpeg configuration enables GPL libx264 and libx265. Anyone
redistributing the wheel must review and satisfy the corresponding
FFmpeg/x264/x265 license obligations.
