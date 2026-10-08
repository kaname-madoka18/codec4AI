# Release guide

English | [Chinese](RELEASING_cn.md)

Version: **0.3.0**. The repository is named codec4AI, the distribution is
`video-loader`, and the import name is `video_loader`. This directory produces
local release candidates; it does not automatically publish them to PyPI or
GitHub.

## 1. Build and check

Prepare the environment using the [build guide](BUILDING.md), then run these
commands at the repository root:

```bash
PYTHON=python BUILD_JOBS=4 bash tools/build_release.sh
python tools/verify_release.py releases/0.3.0/video_loader-0.3.0-*.whl
python -m twine check releases/0.3.0/*.whl releases/0.3.0/video_loader-0.3.0.tar.gz
cd releases/0.3.0
sha256sum --check SHA256SUMS
```

Each official release should include at least these files:

| File | Purpose |
| --- | --- |
| `video_loader-0.3.0-<ABI>-<platform>.whl` | Installable package for the corresponding platform |
| `video_loader-0.3.0.tar.gz` | Project sdist, including C++ sources, the FFmpeg patch, build scripts, tests, and documentation |
| `video_loader-0.3.0-corresponding-sources.tar.gz` | Exact codec-library sources, project sdist, pybind11, and licenses |
| `build-manifest.json` | Compiler and tool versions, upstream versions and checksums, bundled libraries, and wheel tags |
| `SHA256SUMS` | Checksums for all artifacts |

FFmpeg is built with x264/x265 and GPL components. Do not upload only the wheel
and remove the corresponding-sources archive. See the
[FFmpeg licensing documentation](https://ffmpeg.org/legal.html) and the original
licenses in the archive. The original authors retain ownership of their source
files; this repository is distributed under the selected GPL-3.0-or-later license.

## 2. GitHub Release

Create an empty repository you control and commit this directory's contents at
its root. Attach binary artifacts to a repository Release instead of committing
them to Git. After confirming the target repository, use this template:

```bash
# Replace OWNER/codec4AI with the actual repository; run at the repository root.
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

GitHub's automatically generated source ZIP does not include downloaded
third-party sources and cannot replace the complete archive listed above.
Identify the corresponding-sources attachment and the validated Python, glibc,
and architecture combinations in the release notes.

## 3. TestPyPI / PyPI

First confirm that you have permission to publish under the target PyPI project
name. Ownership of the name has not been verified as part of this work, and no
upload has been performed. If `video-loader` is unavailable, choose a new
distribution name rather than using an existing project's identity. The import
name and distribution name can be chosen independently.

```bash
# Upload only Python distributions, not the corresponding-sources archive as an sdist.
python -m twine upload --repository testpypi \
  releases/0.3.0/*.whl releases/0.3.0/video_loader-0.3.0.tar.gz
```

First publish the complete corresponding sources in a public Release for the
same version, and add their actual link to the project documentation and release
notes. Before publishing to PyPI, add your repository's `[project.urls]` to
`pyproject.toml`. Then run:

```bash
python -m twine upload \
  releases/0.3.0/*.whl releases/0.3.0/video_loader-0.3.0.tar.gz
```

Provide the publishing token through Twine's interactive prompt or the CI
credential mechanism, rather than storing it in source files. PyPI does not allow
published files with the same name and version to be overwritten. If 0.3.0 has
already been published, update the version in `pyproject.toml` and
`src/video_loader/__init__.py`, then rebuild and verify. See the
[PyPA packaging tutorial](https://packaging.python.org/en/latest/tutorials/packaging-projects/)
for the package-index publishing process.

## 4. CI

`.github/workflows/build.yml` builds and verifies a Python 3.12 wheel on pushes,
pull requests, and manual runs, then uploads an Actions artifact for download.
It does not create a public Release or upload to PyPI. The glibc tag determined
by `auditwheel` in CI may differ from a local build; use the actual artifact's tag.

To support additional Python versions or manylinux platforms, add the
corresponding build environments and run the full verification for each. Source
compatibility declarations alone do not establish broader compatibility for
prebuilt wheels.
