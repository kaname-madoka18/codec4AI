# codec4AI / video-loader 0.3.0

This release prepares the existing 0.3.0 API as an independent GPL-3.0-or-later
source repository. Distribution name `video-loader` and import name
`video_loader` are retained.

- Public, pinned x264/x265/libaom/FFmpeg/NASM inputs replace private build prefixes.
- The FFmpeg H.264/HEVC reference-graph and state-only patch is included.
- Wheels include native codec libraries and third-party license texts.
- Source distributions build without querying installed FFmpeg development libraries.
- Full corresponding sources, tool versions and SHA256 checksums accompany the wheel.
- Python metadata now requires ≥3.10, matching the runtime type expressions.
- Synthetic codec integration tests and independent analytic graph tests are included.
- The internal benchmark worker adapter is omitted from the public Python package.

The original 0.3.0 wheel was CPython 3.12 / manylinux_2_38 x86_64. This release
is rebuilt from the public sources; it is not a renamed copy of that wheel.
Encoder binaries and encoded bytes may differ from the internal research build.

Actual artifact tags and source/compiler versions are recorded in
`build-manifest.json` alongside the release files. The public APIs and supported
sampling semantics are unchanged. AV1 remains a full-packet fallback, and
`Video` does not cache indexes. Native Windows, macOS, ARM and musl are not
validated targets.

## Local validation (2026-09-29)

- Artifact: `video_loader-0.3.0-cp312-cp312-manylinux_2_31_x86_64.whl`.
- Built with CPython 3.12.11 on Linux x86_64, glibc 2.31.
- 35 codec integration tests passed; no skips. Tests compare pixels with FFmpeg
  and verify GOP structure, entropy coding and reference-graph accounting.
- 13 independent analytic tests passed.
- All five encoding modes and H.264/HEVC graph decoding passed with no `ffmpeg`
  executable on PATH and no `LD_LIBRARY_PATH`, in a fresh installation outside
  the source tree.
- `auditwheel repair/show` and `twine check` passed. Six codec shared libraries
  are bundled; libaom is statically linked into libavcodec.

The GitHub workflow has been prepared but has not been run on a hosted runner.
Other CPython ABIs and platforms have not been validated in this release.

Publish the wheel together with its project sdist and corresponding-sources
archive. This local preparation does not imply a PyPI or GitHub upload.

## Research scripts in the current source tree

The current checkout additionally contains minimally adapted original scripts in
`benchmarks/`. PyAV now seeks to the preceding keyframe of each requested intra
period and decodes forward, skipping unused intervening periods. The original
data formats and experiment settings, including GOP32/B31 preprocessing, remain.
Only source logic and syntax were reviewed for this migration; execution in the
current environment is not verified. See [the script notes](../benchmarks/README.md).

The existing `releases/0.3.0` files are the earlier frozen snapshot and retain
their hashes. These additional scripts are included in future source builds;
they have not been inserted into the existing archives or installed wheel.
