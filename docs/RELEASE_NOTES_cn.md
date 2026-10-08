# codec4AI / video-loader 0.3.0

[English](RELEASE_NOTES.md) | 简体中文

本次发行将现有的 0.3.0 API 整理为独立的 GPL-3.0-or-later 源码仓库。
分发包名 `video-loader` 和导入名 `video_loader` 保持不变。

- 使用公开且固定版本的 x264/x265/libaom/FFmpeg/NASM 输入，替代私有构建前缀。
- 提供 FFmpeg H.264/HEVC 参考图和仅更新状态解码补丁。
- wheel 包含原生编解码库和第三方许可原文。
- 生成源码分发包时不再查询已安装的 FFmpeg 开发库。
- 随 wheel 提供完整对应源码、工具版本和 SHA256 校验和。
- Python 元数据要求更新为 ≥3.10，与运行时类型表达式保持一致。
- 包含自动生成视频的编解码集成测试，以及独立的理论图模型测试。
- 公开 Python 包不包含内部基准测试 worker 适配器。

原始 0.3.0 wheel 面向 CPython 3.12 / manylinux_2_38 x86_64。本次发行基于公开源码
重新构建，并非将原 wheel 重命名。编码器二进制和编码后的字节数据可能与内部研究构建
不同。

实际产物标签及源码、编译器版本记录在发行文件旁的 `build-manifest.json` 中。
公开 API 和支持的采样语义保持不变。AV1 仍使用完整数据包回退路径，`Video` 不缓存
索引。原生 Windows、macOS、ARM 和 musl 不属于经过验证的目标平台。

## 本地验证（2026-09-29）

- 产物：`video_loader-0.3.0-cp312-cp312-manylinux_2_31_x86_64.whl`。
- 使用 CPython 3.12.11，在 Linux x86_64、glibc 2.31 环境中构建。
- 35 项编解码集成测试全部通过，无跳过。测试逐像素对比 FFmpeg 结果，并验证 GOP
  结构、熵编码和参考图计数。
- 13 项独立理论模型测试通过。
- 在源码目录外全新安装后，PATH 中没有 `ffmpeg` 可执行文件、没有
  `LD_LIBRARY_PATH` 的条件下，五种编码模式及 H.264/HEVC 参考图解码全部通过。
- `auditwheel repair/show` 和 `twine check` 通过。wheel 包含六个编解码共享库；
  libaom 静态链接到 libavcodec。

截至上述本地验证时，GitHub 工作流已经准备，但尚未在托管运行器上执行；本次发行
尚未验证其他 CPython ABI 或平台。

发布 wheel 时，应同时提供项目 sdist 和对应源码归档。本地准备完成不代表已经上传到
PyPI 或 GitHub。

## 当前源码树中的研究脚本

当前工作目录还在 [`benchmarks/`](../benchmarks/) 中包含经过最小适配的原始脚本。
PyAV 现在会定位到每个请求帧内周期之前的关键帧，再向前解码，跳过中间未使用的周期。
原始数据格式和实验设置保持不变，包括 GOP32/B31 预处理。本次迁移只检查了源码逻辑和
语法，没有验证脚本在当前环境中的实际运行。

现有 `releases/0.3.0` 文件是较早的冻结快照，其哈希保持不变。新增脚本会纳入后续源码
构建；它们尚未加入现有归档或已安装的 wheel。
