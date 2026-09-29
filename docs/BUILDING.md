# 构建教程

仓库名为 codec4AI，发行包名为 `video-loader`，导入名为 `video_loader`。
当前版本是 **0.3.0**。源码要求 Python **≥3.10**；预编译 wheel 按 CPython ABI 分别构建。
完整构建目前支持 **Linux x86_64 / glibc**。Windows 可在 WSL2 中运行；原生 Windows、
macOS、ARM 和 musl/Alpine 不在此次验证范围内。

## 1. 准备环境

在 Ubuntu/Debian 安装构建工具；只有这一步需要管理员权限：

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config git patch perl \
  xz-utils patchelf python3-venv ffmpeg
```

`ffmpeg`/`ffprobe` 用于集成测试，安装后的 wheel 本身不调用它们。
编译需要 C++17；NASM 由脚本从固定版本源码编译，不依赖机器预装版本。
在仓库根目录创建虚拟环境，以所需 Python ABI 的解释器运行：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r tools/build-requirements.txt
```

也可使用 Python 3.10/3.11 等重新构建对应 ABI；只有实际验证过的版本才应宣称支持。
`tools/build-requirements.txt` 固定直接构建工具版本；Python 的间接依赖和系统工具链
未全部锁定，因此此流程提供可重建的来源和步骤，不承诺跨机器逐字节相同。

## 2. 构建包含原生库的 wheel

```bash
PYTHON="$PWD/.venv/bin/python" \
BUILD_ROOT=/tmp/codec4ai-build \
SOURCE_CACHE=/tmp/codec4ai-downloads \
BUILD_JOBS=4 \
bash tools/build_release.sh
```

默认输出到 `releases/0.3.0/`，可用 `OUTPUT_DIR` 修改。建议将 `BUILD_ROOT` 放在 Linux
文件系统上，尤其是 WSL 环境；避免在挂载的 Windows 盘上编译大量小文件。
同一构建目录一次只运行一个构建。

流程依次执行：

1. 获取 `tools/sources.json` 中的公开源码，检查 SHA256；libaom 使用固定 Git commit。
2. 编译 NASM、标准 x264、标准 x265、仅解码的静态 libaom。
3. 对 FFmpeg 8.1.2 应用仓库内的 reference-graph 补丁，编译最小动态运行库。
4. 在清洁源码副本中生成 sdist，再从 sdist 编译 C++ 扩展 wheel。
5. 使用 `auditwheel repair` 收集原生依赖并设置 wheel 内部 RPATH。
6. 使用 `twine check` 检查元数据，打包对应源码、构建清单和校验和。

| 输入 | 固定版本 | 构建方式 |
| --- | --- | --- |
| FFmpeg | 8.1.2 + 本仓库补丁 | 动态，GPLv3+，禁用网络、CLI、无关编解码器 |
| x264 | `0480cb05fa188d37ae87e8f4fd8f1aea3711f7ee`，ABI 165 | 标准上游，8-bit 4:2:0，动态 |
| x265 | 3.5，ABI 199 | 标准上游，8-bit，动态，关闭 NUMA |
| libaom | 3.14.1，`03087864cf4bea6abb0d28f95cf7843511413d8f` | 静态，AV1 解码 |
| NASM | 2.16.03 | 构建时使用 |

当前五种转码模式只需 3 或 7 个 B 帧，因此使用标准编码器；不需要原实验环境中的
`b31` 编码器或修改过的 AV1 参考结构。FFmpeg 补丁则是稀疏解码功能必需的。

### 平台兼容性

wheel 标签由实际 ELF 符号需求和 `auditwheel` 检查决定。`auditwheel` 不能将新 glibc
符号转换成旧符号，也不能把 CPython 3.12 扩展变成通用 ABI。
需要更低 glibc 基线时，在相应的 manylinux 构建容器中重新编译全部依赖。
可设置 `AUDITWHEEL_PLAT=manylinux_2_28_x86_64` 要求该目标；若构建输入不满足它，
脚本应失败，不能手工重命名 wheel 绕过检查。
[auditwheel 官方说明](https://github.com/pypa/auditwheel)解释了此限制。

## 3. 验证发布产物

```bash
python tools/verify_release.py releases/0.3.0/video_loader-0.3.0-*.whl \
  --junit-xml /tmp/codec4ai-tests.xml
```

工具会建立一次性虚拟环境并安装 wheel 和测试依赖，然后在源码目录外运行：

- 清空 `LD_LIBRARY_PATH`，并在找不到 `ffmpeg` 的 PATH 下验证五种转码模式和参考图解码。
- 与 FFmpeg 完整解码逐像素比较；检查乱序/重复帧、GOP、CAVLC/CABAC、非对齐分辨率、
  状态包计数和按目标范围解析参考图等行为。
- 执行独立的理论图模型测试。

集成比较需要完整的 FFmpeg CLI：H.264/HEVC/AV1 编解码、`testsrc2`、`select` 和
`trace_headers`。测试使用兼容旧版本的 `-vsync 0`，避免依赖较新的 `-fps_mode` 参数；
生成 AV1 样本时显式启用实验编码器，以兼容 FFmpeg 4.2 对 libaom-av1 的标记。
无可用 CLI 时，只能运行无 CLI 的功能示例，不能据此宣称完整测试通过。

## 4. 开发安装和普通源码构建

先完成原生依赖构建，然后读取前缀：

```bash
python tools/build_native_deps.py --work-dir /tmp/codec4ai-build \
  --cache-dir /tmp/codec4ai-downloads --jobs 4
codec4ai_prefix="$(cat /tmp/codec4ai-build/native-prefix.txt)"
PKG_CONFIG_PATH="$codec4ai_prefix/lib/pkgconfig" \
LD_LIBRARY_PATH="$codec4ai_prefix/lib" \
python -m pip install --no-build-isolation -e '.[test]'
LD_LIBRARY_PATH="$codec4ai_prefix/lib" python -m pytest tests -q
```

此类开发安装不执行 `auditwheel repair`，所以运行时仍需要该动态库前缀。
也可通过 `PKG_CONFIG_PATH` 指向兼容的系统 FFmpeg 8.1.2 开发库。未打补丁的 FFmpeg
会使用 `contiguous_no_reference_graph`，相关稀疏路径测试会跳过；不能将其当作完整发行版。
旧版 FFmpeg 的开发 API 不保证兼容当前 C++ 源码。

单独构建 sdist 不需要安装 FFmpeg 开发库：

```bash
python -m build --sdist --no-isolation
```

构建入口延迟到编译扩展时才查询 `pkg-config`，确保元数据和源码分发包可以独立生成。

## 5. 使用对应源码归档重建

解压发行版的 `*-corresponding-sources.tar.gz`，再解压其中的项目 sdist。
把 `SOURCE_CACHE` 指向归档内的 `downloads/`，执行相同构建命令。
这会复用经过 SHA256 校验的原生源码；Python 构建工具仍需预装或从 PyPI 获取。
归档还包括编译扩展所用的 pybind11 源码/头文件和第三方许可。

若网络下载失败可重试；缓存文件校验失败时删除该文件并重新获取，不应修改锁定值来
绕过校验。构建日志保留失败的编译命令，便于排查。
