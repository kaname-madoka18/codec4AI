# 发布教程

版本：**0.3.0**。仓库名 codec4AI，分发包名 `video-loader`，导入名 `video_loader`。
本目录生成本地发行候选产物；不会自动向 PyPI 或 GitHub 发布。

## 1. 构建和检查

按 [构建教程](BUILDING.md) 准备环境后，在仓库根目录执行：

```bash
PYTHON=python BUILD_JOBS=4 bash tools/build_release.sh
python tools/verify_release.py releases/0.3.0/video_loader-0.3.0-*.whl
python -m twine check releases/0.3.0/*.whl releases/0.3.0/video_loader-0.3.0.tar.gz
cd releases/0.3.0
sha256sum --check SHA256SUMS
```

每个正式发行版至少附带：

| 文件 | 作用 |
| --- | --- |
| `video_loader-0.3.0-<ABI>-<platform>.whl` | 对应平台的可安装包 |
| `video_loader-0.3.0.tar.gz` | 项目 sdist，含 C++、FFmpeg 补丁、构建脚本、测试和文档 |
| `video_loader-0.3.0-corresponding-sources.tar.gz` | 编解码库的确切源码、项目 sdist、pybind11 和许可 |
| `build-manifest.json` | 编译器、工具版本、上游版本/校验值、打包库和 wheel 标签 |
| `SHA256SUMS` | 所有产物的校验和 |

FFmpeg 启用了 x264/x265 和 GPL 组件，不能只上传 wheel 并删除对应源码归档。
参见 [FFmpeg 许可说明](https://ffmpeg.org/legal.html)及归档中的原始许可。
源文件中的权利归属仍由原作者保留；本仓库以已选定的 GPL-3.0-or-later 发行。

## 2. GitHub Release

创建由你控制的空仓库，将本目录内容提交到仓库根目录。二进制产物不提交到 Git；
使用仓库 Release 附件承载。确认目标仓库后可使用以下模板：

```bash
# 将 OWNER/codec4AI 替换为实际仓库；在仓库根目录执行。
gh release create v0.3.0 \
  --repo OWNER/codec4AI \
  --title 'codec4AI / video-loader 0.3.0' \
  --notes-file docs/RELEASE_NOTES.md \
  releases/0.3.0/*.whl \
  releases/0.3.0/video_loader-0.3.0.tar.gz \
  releases/0.3.0/video_loader-0.3.0-corresponding-sources.tar.gz \
  releases/0.3.0/build-manifest.json \
  releases/0.3.0/SHA256SUMS
```

GitHub 自动生成的仓库源码 ZIP 不包含下载的第三方源码，不能替代上表中的完整归档。
发布说明中标出对应源码附件和当前已验证的 Python/glibc/架构范围。

## 3. TestPyPI / PyPI

先确认你对目标 PyPI 项目名称有发布权限；此工作尚未验证名称归属，也没有上传。
如果 `video-loader` 名称不可用，需要先确定新的分发名称，不能冒用已有项目。
导入名与分发名可以分别决定。

```bash
# 仅上传 Python 分发产物，不把 corresponding-sources 当作 sdist 上传。
python -m twine upload --repository testpypi \
  releases/0.3.0/*.whl releases/0.3.0/video_loader-0.3.0.tar.gz
```

先将完整对应源码发布到同版本的公开 Release，并把其真实链接写入项目和发行说明；
在真正发布 PyPI 前，为 `pyproject.toml` 补上自己仓库的 `[project.urls]`。
然后执行：

```bash
python -m twine upload \
  releases/0.3.0/*.whl releases/0.3.0/video_loader-0.3.0.tar.gz
```

通过 Twine 交互输入或 CI 的凭据机制提供发布 token，不将其写进源码。
PyPI 不允许覆盖已发布的同名版本文件；若 0.3.0 已经发布，应更新
`pyproject.toml` 和 `src/video_loader/__init__.py` 中的版本后重新构建、验证。
具体索引发布流程见 [PyPA 官方教程](https://packaging.python.org/en/latest/tutorials/packaging-projects/)。

## 4. CI

`.github/workflows/build.yml` 在代码推送、PR 和手动触发时构建并验证 Python 3.12 wheel，
上传 Actions artifact 供下载。它不会创建公开 Release，也不会上传 PyPI。
CI 中 `auditwheel` 得出的 glibc 标签可能与本机构建不同，必须以实际产物为准。

要扩展 Python 版本或 manylinux 平台，请添加对应构建环境并分别执行完整验证。
不能仅根据源码声明扩大预编译 wheel 的兼容范围。
