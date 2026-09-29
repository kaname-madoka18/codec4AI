# 外部依赖与许可

## 安装和运行

| 层级 | 依赖 | 是否包含在 bundled wheel 中 |
| --- | --- | --- |
| Python | Python ≥3.10；wheel ABI 必须匹配 | 否 |
| Python 包 | `numpy>=1.23` | 否，pip 安装 |
| 视频容器/解码 | FFmpeg 8.1.2：avformat、avcodec、avutil、swscale | 是 |
| H.264 / HEVC 编码 | x264 / x265 | 是 |
| AV1 解码 | libaom 3.14.1 | 静态链接进 avcodec，不是独立 `.so` |
| 系统 ABI | glibc、libstdc++、libgcc 等 manylinux 允许的系统库 | 否，操作系统提供 |

因此“无需安装系统 FFmpeg”成立，但“没有外部依赖”不成立。
安装后的公开 API 不需要 `ffmpeg` CLI、CUDA、PyTorch、Decord、Lance、OSS SDK 或集群平台。
FFmpeg 网络协议在 bundled 构建中禁用；输入应为本地路径或内存 MP4。

`Video` 当前每次调用都会重建索引，不提供持久解码缓存。AV1 仍读取和解码完整包集合；
H.264/HEVC 的参考图和 state-only 路径依赖随仓库公开的 FFmpeg 补丁。

## 构建与测试

构建：C/C++17 工具链、make、CMake、Git、Perl、patch、tar/xz、pkg-config、NASM，
以及 setuptools、wheel、pybind11、build、auditwheel、patchelf、twine。
直接 Python 构建工具版本见 `tools/build-requirements.txt`；原生源码完整锁定见
`tools/sources.json`。构建需要访问这些公开上游，或提供包含相同源码的缓存。

测试：pytest、NumPy，以及包含 H.264/HEVC/AV1 支持的完整 FFmpeg/ffprobe CLI。
理论测试只依赖 Python 标准库。测试自动生成小型视频，不需要研究数据或云凭据。

`benchmarks/` 保留原研究脚本，另依赖 Torch、所选解码器、OSS、Lance/Arrow、Pillow、
冻结数据及相应 FFmpeg 工具链；节点实验还需 Squid/proxychains。它们不属于核心 wheel
的强制依赖。本轮只核查源码逻辑，没有验证当前环境可运行，详见[脚本说明](../benchmarks/README.md)。

普通源码构建使用系统开发库时，运行期会依赖那些系统库；只有经过 `auditwheel repair`
并完成隔离验证的 wheel 才具有这里描述的库打包行为。

## 许可

| 组件 | 许可 / 本发行使用方式 |
| --- | --- |
| 本项目及 FFmpeg 补丁 | GPL-3.0-or-later |
| FFmpeg | 上游含 LGPL/GPL；本构建 `--enable-gpl --enable-version3`，采用 GPLv3+ |
| x264 / x265 | GPL-2.0-or-later；与 GPLv3+ 发行方案组合 |
| libaom | BSD 类许可及 AOM 专利许可；见 `licenses/aom-*` |
| pybind11 | BSD-3-Clause；头文件模板编译进扩展 |
| NASM | BSD-2-Clause；构建工具，不随 wheel 提供可执行文件 |
| NumPy | BSD-3-Clause；外部 Python 依赖 |

`licenses/` 保留第三方原文，`NOTICE` 标明 FFmpeg 修改。对应源码归档包含准确上游源码，
以及生成修改版的补丁和脚本。代码许可证不替代视频标准可能涉及的专利许可，
也不赋予外部数据集或图片的再分发权。
[FFmpeg 官方许可说明](https://ffmpeg.org/legal.html)说明了其 GPL 组件和专利问题；
各组件具体条款以随附的上游源码和原始许可证为准。
