# video-loader

[English](API.md) | 简体中文

面向 Linux CPU 的工具包，用于从 H.264、HEVC 和 AV1 MP4 文件中提取指定显示帧，
以及使用 libx264 或 libx265 对 RGB NumPy 视频编码。

## 安装

在 glibc 2.31 或更新版本的系统上安装本地构建的 CPython 3.12 wheel：

```bash
python -m pip install video_loader-0.3.0-cp312-cp312-manylinux_2_31_x86_64.whl
```

wheel 包含通过私有 RPATH 加载的最小 FFmpeg 8.1.2 运行库、x264 和 x265，
不需要安装系统 FFmpeg，也不需要设置 `LD_LIBRARY_PATH`。

开发时仍可从源码构建，需要 C++17 编译器、`pkg-config`、pybind11，以及
libavformat、libavcodec、libavutil 和 libswscale 开发包。FFmpeg 必须启用 libx264
和 libx265 编码器。包含原生库的 wheel 构建流程由 `tools/build_release.sh` 提供；
完整依赖和命令见[构建教程](BUILDING_cn.md)。

## 快速开始

```python
import video_loader

info = video_loader.inspect("episode.mp4")
# info["codec"], info["width"], info["height"], info["frame_count"], ...

frames, metrics = video_loader.decode(
    "episode.mp4", [17, 2, 17], return_info=True
)
# frames：uint8 RGB，形状为 [3, H, W, 3]
# 保留请求顺序和重复索引。

payload = video_loader.transcode(
    frames, fps=30, mode="fast265", crf=18
)
# payload：包含 H.265/HEVC MP4 的字节数据
```

所有读取 API 都支持文件系统路径（`str` 或 `os.PathLike`）以及内存中的 MP4 数据
（`bytes`、`bytearray` 或 `memoryview`）。

## API

### `inspect`

```python
video_loader.inspect(source) -> dict
```

检查第一个视频轨道，不返回解码后的像素。结果包含：

| 字段 | 含义 |
| --- | --- |
| `container` | 容器名称，当前为 `"mp4"`。 |
| `codec` | 视频编解码标准：`"h264"`、`"hevc"` 或 `"av1"`。 |
| `width`, `height` | 显示帧的像素尺寸。 |
| `sample_count` | 压缩视频样本/数据包数量。 |
| `frame_count` | 显示帧数量。 |
| `fps` | 标称帧率。 |
| `duration_seconds` | 视频轨道时长，单位为秒。 |
| `closure_policy` | `decode` 将使用的依赖策略。 |
| `reference_graph_available` | 私有 H.264/HEVC 参考图是否可用。 |
| `reference_edges` | 发现的直接图像参考边数量。 |
| `has_valid_sample_offsets` | 所有 MP4 样本偏移通过验证后为 `True`。 |

内存数据示例：

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

`indices` 是非空的可迭代对象，包含从零开始的显示帧索引。索引可以乱序或重复：
内部只解码一次每个不同的帧，再按请求顺序恢复输出。返回数组的数据类型为 `uint8`，
通道顺序为 RGB，形状为 `[len(indices), height, width, 3]`。

设置 `return_info=True` 后，第二个返回值报告实际完成的工作：

| 字段 | 含义 |
| --- | --- |
| `codec`, `width`, `height`, `total_frames` | 基本视频流信息。 |
| `requested_frames` | 请求的索引数量，包含重复索引。 |
| `unique_requested_frames` | 不同请求索引的数量。 |
| `packets_sent` | 提交给解码器的压缩数据包数量。 |
| `state_only_packets` | 仅用于推进编解码状态的数据包数量。 |
| `frames_reconstructed` | 完成像素重建的图像数量。 |
| `frames_output` | 解码器输出的不同重建图像数量。 |
| `compressed_bytes` | 所有已提交压缩数据包的总字节数。 |
| `reconstructed_bytes` | 属于像素重建数据包的压缩字节数。 |
| `index_seconds`, `decode_seconds` | 索引/依赖分析耗时与像素解码阶段耗时。 |
| `reference_graph_available`, `reference_edges` | 参考图是否可用及其边数。 |
| `reference_graph_segment_begin`, `reference_graph_segment_end` | 覆盖目标帧的单个参考图区段在解码顺序中的起止位置，包含两端；不可用时为 `-1`。 |
| `reference_graph_packets` | 为该参考图实际解析的数据包数量，不包含可丢弃的先行图像。 |
| `reference_graph_attempts` | 找到有效区段前尝试的候选随机访问锚点数量。 |
| `closure_groups` | 合并后的依赖执行范围数量。 |
| `closure_mode` | 本次调用实际使用的策略。 |

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

将 RGB NumPy 数组编码为 H.264 或 H.265/HEVC yuv420p MP4。`frames` 必须采用
`uint8` 数据类型，形状为 `[T, H, W, 3]`，各维度为正数，宽和高均为偶数。
`fps` 必须为有限数值且在 `(0, 1000]` 范围内；`crf` 必须为有限数值且在 `[0, 51]`
范围内。CRF 越低，通常画质越高、文件越大。

未提供 `output` 时，以 `bytes` 返回完整 MP4。若传入路径，则按需创建父目录，
原子替换目标文件，并返回对应的绝对路径 `pathlib.Path`：

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

支持以下五种模式：

| 模式 | 编解码配置 | 熵编码 | GOP/参考结构 |
| --- | --- | --- | --- |
| `base264` | H.264 Main，x264 medium | CABAC | 固定 GOP 32，3 个 B 帧，启用 B-pyramid。 |
| `base265` | HEVC Main，x265 medium | CABAC | 固定 GOP 32，3 个 B 帧，启用 B-pyramid。 |
| `fast264` | H.264 Main，x264 medium | CABAC | GOP 8，7 个非参考 B 帧，禁用 B-pyramid。 |
| `fast265` | HEVC Main，x265 medium | CABAC | GOP 8，7 个非参考 B 帧，禁用 B-pyramid。 |
| `ufast264` | H.264 Main，x264 medium | CAVLC | GOP/参考结构与 `fast264` 相同。 |

默认模式为 `base264`。0.3 版本不接受旧的 `base` 和 `fastRA` 名称。

### `Video`

```python
with video_loader.Video(source) as video:
    info = video.info
    frames = video.decode([0, 10, 20])
```

`Video` 保存不可变的路径或字节数据源，通过 `video.info` 提供与 `inspect` 相同的
信息，并通过 `video.decode` 提供相同的参数和返回值。`close()` 当前不执行任何操作。
在 0.3 版本中，每次调用 `info` 或 `decode` 仍会重新构建原生样本索引；此封装尚不提供
解码或索引缓存。

### 异常

原生实现中的错误会转换为以下公开异常层级：

```text
VideoLoaderError (RuntimeError)
├── InvalidMP4Error
├── UnsupportedVideoError
├── DecodeError
└── EncodeError
```

向 `transcode` 传入无效的 `mode` 会触发 `ValueError`；传入不支持的 `source`
Python 类型会触发 `TypeError`。

## 依赖感知解码

解码指标中的 `closure_mode` 报告依赖选择策略。对于 H.264/HEVC，加载器首先选择
一个覆盖全部请求显示帧的有效区段：从最近的前置随机访问锚点开始，到解码顺序中的
最后一个目标结束。显示时间位于该锚点之前的 Open-GOP 先行图像可以丢弃，不会提交。
如果候选区段仍存在不可用的依赖，则将起点移动到更早的锚点。随后，随附 FFmpeg
仅在验证通过的区段内解析 slice 头，维护编解码器的 DPB/RPS 状态，不对块进行熵解码
或重建像素，并导出每个压缩帧的活动参考列表。加载器为每个目标沿这些列表进行
广度优先遍历，得到像素重建闭包。

在像素解码阶段，按解码顺序提交同一已验证目标覆盖区段内的数据包，直到最后一个
选中的依赖。闭包之外的数据包以 `state_only` 模式运行：更新 POC、MMCO、RPS 和
DPB 状态，但跳过熵解码、逆变换、运动补偿和环路滤波。重叠的执行范围会合并，
因此每次解码调用中，每个压缩数据包最多提交一次，每个目标或参考图像最多重建一次。
这一统一的 `reference_graph_bfs` 路径不依赖编码器采用 base、fast 还是 ufast 配置。

解码指标区分 `packets_sent`、`state_only_packets`、`frames_reconstructed`、
`frames_output`、`compressed_bytes` 和 `reconstructed_bytes`。在稀疏路径中，
满足 `packets_sent == state_only_packets + frames_reconstructed`。

使用未打补丁的系统 FFmpeg 进行开发源码构建时，无法获取该私有参考图。此时会在解码
前选择一个保守的连续闭包（`contiguous_no_reference_graph`），不会对部分解码过的
闭包进行重试。

AV1 当前使用完整的数据包集合，因为标准 FFmpeg 不暴露研究构建使用的自定义 `av1f`
元数据。该实现优先保证正确性，不声称所选 AV1 数据包集合在数学上最小。

有效的 MP4 文件本身包含样本位置表。本版本拒绝缺失或越界的样本偏移，不尝试通过
推测性重新封装来修复。

随附 FFmpeg 配置启用了采用 GPL 许可的 libx264 和 libx265。任何重新分发 wheel 的
使用者都应审查并履行相应的 FFmpeg/x264/x265 许可义务。
