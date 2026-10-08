# Dependencies and licensing

English | [Chinese](DEPENDENCIES_cn.md)

## Installation and runtime

| Layer | Dependency | Included in the bundled wheel? |
| --- | --- | --- |
| Python | Python ≥3.10; the wheel ABI must match | No |
| Python packages | `numpy>=1.23` | No; installed by pip |
| Video containers and decoding | FFmpeg 8.1.2: avformat, avcodec, avutil, swscale | Yes |
| H.264 / HEVC encoding | x264 / x265 | Yes |
| AV1 decoding | libaom 3.14.1 | Statically linked into avcodec, not a separate `.so` |
| System ABI | System libraries permitted by manylinux, including glibc, libstdc++, and libgcc | No; provided by the operating system |

The wheel does not require a system FFmpeg installation, but it still has external
dependencies. The installed public API does not require the `ffmpeg` CLI, CUDA,
PyTorch, Decord, Lance, the OSS SDK, or a cluster platform. FFmpeg network protocols
are disabled in the bundled build; inputs must be local paths or in-memory MP4
payloads.

`Video` currently rebuilds the index on each call and does not provide a persistent
decode cache. AV1 still reads and decodes the complete packet set. The H.264/HEVC
reference-graph and state-only paths depend on the FFmpeg patch published in this
repository.

## Building and testing

Build requirements: a C/C++17 toolchain, make, CMake, Git, Perl, patch, tar/xz,
pkg-config, NASM, setuptools, wheel, pybind11, build, auditwheel, patchelf, and
twine. Direct Python build-tool versions are listed in
`tools/build-requirements.txt`; pinned native sources are recorded in
`tools/sources.json`. Building requires access to these public upstream sources
or a cache containing the same sources.

Test requirements: pytest, NumPy, and a full FFmpeg/ffprobe CLI with
H.264/HEVC/AV1 support. The analytic tests use only the Python standard library.
Tests generate small videos automatically and do not require research datasets
or cloud credentials.

The original research scripts in `benchmarks/` additionally depend on Torch, the
selected decoders, OSS, Lance/Arrow, Pillow, frozen datasets, and the corresponding
FFmpeg toolchain. Node experiments also require Squid/proxychains. These are not
mandatory dependencies of the core wheel. Only source logic has been reviewed;
execution in the current environment has not been verified. The scripts are in
[`benchmarks/`](../benchmarks/).

A regular source build using system development libraries depends on those
libraries at runtime. Only wheels processed by `auditwheel repair` and verified
in isolation provide the library bundling described here.

## Licensing

| Component | License / use in this release |
| --- | --- |
| This project and its FFmpeg patch | GPL-3.0-or-later |
| FFmpeg | Upstream includes LGPL/GPL code; this build uses `--enable-gpl --enable-version3` and is GPLv3+ |
| x264 / x265 | GPL-2.0-or-later; combined with the GPLv3+ distribution |
| libaom | BSD-style license and AOM patent license; see `licenses/aom-*` |
| pybind11 | BSD-3-Clause; header templates are compiled into the extension |
| NASM | BSD-2-Clause; build tool, with no executable bundled in the wheel |
| NumPy | BSD-3-Clause; external Python dependency |

`licenses/` preserves the original third-party license texts, and `NOTICE`
identifies the FFmpeg modifications. The corresponding-sources archive contains
the exact upstream sources and the patches and scripts used to produce the
modified version. Code licenses do not replace patent licenses that video
standards may require, or grant redistribution rights for external datasets or
images. The [FFmpeg licensing documentation](https://ffmpeg.org/legal.html)
discusses its GPL components and patent issues. The supplied upstream sources
and original licenses define the specific terms for each component.
