# Build guide

English | [Chinese](BUILDING_cn.md)

The repository is named codec4AI, the distribution is `video-loader`, and the
import name is `video_loader`. The current version is **0.3.0**. Source builds
require Python **≥3.10**; prebuilt wheels are built separately for each CPython ABI.
The complete build currently supports **Linux x86_64 / glibc**. Windows users can
run it in WSL2. Native Windows, macOS, ARM, and musl/Alpine have not been validated
for this release.

## 1. Prepare the environment

Install the build tools on Ubuntu/Debian. Only this step requires administrator
privileges:

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config git patch perl \
  xz-utils python3-venv ffmpeg
```

`ffmpeg` and `ffprobe` are used for integration tests; the installed wheel does
not invoke them. Compilation requires C++17. The script builds NASM from pinned
sources instead of using a preinstalled version. Create a virtual environment
at the repository root with the interpreter for the desired Python ABI:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r tools/build-requirements.txt
```

The requirements include `patchelf==0.19.1.0`; Ubuntu 22.04's older system
package does not meet auditwheel's requirement of patchelf 0.14.5 or newer.
The release script puts the selected Python environment's scripts directory
first on PATH and checks the actual patchelf version before compiling.

You can also rebuild for other ABIs, such as Python 3.10 or 3.11. Advertise support
only for versions that have actually been verified. `tools/build-requirements.txt`
pins the direct Python build tools. Transitive Python dependencies and the system
toolchain are not fully pinned, so this process provides the sources and steps
needed to rebuild, without promising identical output bytes across machines.

## 2. Build a wheel with bundled native libraries

```bash
PYTHON="$PWD/.venv/bin/python" \
BUILD_ROOT=/tmp/codec4ai-build \
SOURCE_CACHE=/tmp/codec4ai-downloads \
BUILD_JOBS=4 \
bash tools/build_release.sh
```

Output goes to `releases/0.3.0/` by default; override it with `OUTPUT_DIR`.
Place `BUILD_ROOT` on a Linux filesystem, especially under WSL, to avoid compiling
many small files on a mounted Windows drive. Run only one build at a time in each
build directory.

The pipeline performs these steps in order:

1. Fetch the public sources in `tools/sources.json` and verify SHA256 hashes;
   libaom uses a pinned Git commit.
2. Build NASM, stock x264, stock x265, and a static, decoder-only libaom.
3. Apply the repository's reference-graph patch to FFmpeg 8.1.2 and build a minimal
   shared runtime.
4. Generate an sdist from a clean source copy, then build the C++ extension wheel
   from that sdist.
5. Collect native dependencies and set the wheel's internal RPATH with
   `auditwheel repair`.
6. Check metadata with `twine check` and package the corresponding sources,
   build manifest, and checksums.

| Input | Pinned version | Build configuration |
| --- | --- | --- |
| FFmpeg | 8.1.2 + repository patch | Shared, GPLv3+; networking, CLI, and unrelated codecs disabled |
| x264 | `0480cb05fa188d37ae87e8f4fd8f1aea3711f7ee`, ABI 165 | Stock upstream, 8-bit 4:2:0, shared |
| x265 | 3.5, ABI 199 | Stock upstream, 8-bit, shared, NUMA disabled |
| libaom | 3.14.1, `03087864cf4bea6abb0d28f95cf7843511413d8f` | Static, AV1 decoding |
| NASM | 2.16.03 | Build-time tool |

The five current transcoding modes need only 3 or 7 B frames, so they use stock
encoders. They do not require the original research environment's `b31` encoders
or modified AV1 reference structures. The FFmpeg patch is required for sparse
decoding.

### Platform compatibility

Wheel tags are determined by actual ELF symbol requirements and `auditwheel`
checks. `auditwheel` cannot convert newer glibc symbols into older ones or turn a
CPython 3.12 extension into a universal ABI extension. To target an older glibc
baseline, rebuild all dependencies in the corresponding manylinux build container.
Set `AUDITWHEEL_PLAT=manylinux_2_28_x86_64` to require that target. If the build
inputs do not satisfy it, the script should fail; do not bypass the check by
renaming the wheel. The [auditwheel documentation](https://github.com/pypa/auditwheel)
explains this limitation.

## 3. Verify release artifacts

```bash
python tools/verify_release.py releases/0.3.0/video_loader-0.3.0-*.whl \
  --junit-xml /tmp/codec4ai-tests.xml
```

The tool creates a temporary virtual environment, installs the wheel and test
dependencies, and runs the following checks outside the source directory:

- Verify all five transcoding modes and reference-graph decoding with
  `LD_LIBRARY_PATH` cleared and no `ffmpeg` executable on PATH.
- Compare pixels with full FFmpeg decoding; check unsorted and repeated frame
  indices, GOP structure, CAVLC/CABAC, unaligned resolutions, state-only packet
  counts, and reference-graph parsing limited to the target range.
- Run the independent theoretical graph-model tests.

Integration comparisons require a full FFmpeg CLI with H.264/HEVC/AV1 encoding
and decoding, `testsrc2`, `select`, and `trace_headers`. Tests use the older
`-vsync 0` option rather than depending on the newer `-fps_mode` option. AV1 sample
generation explicitly enables experimental encoders for compatibility with
FFmpeg 4.2's classification of libaom-av1. Without a usable CLI, you can run only
the examples that do not need it; that does not establish that the full tests pass.

## 4. Development installation and regular source builds

Build the native dependencies first, then read their installation prefix:

```bash
python tools/build_native_deps.py --work-dir /tmp/codec4ai-build \
  --cache-dir /tmp/codec4ai-downloads --jobs 4
codec4ai_prefix="$(cat /tmp/codec4ai-build/native-prefix.txt)"
PKG_CONFIG_PATH="$codec4ai_prefix/lib/pkgconfig" \
LD_LIBRARY_PATH="$codec4ai_prefix/lib" \
python -m pip install --no-build-isolation -e '.[test]'
LD_LIBRARY_PATH="$codec4ai_prefix/lib" python -m pytest tests -q
```

This development installation does not run `auditwheel repair`, so the shared
library prefix is still needed at runtime. You can also point `PKG_CONFIG_PATH`
to compatible system FFmpeg 8.1.2 development libraries. Unpatched FFmpeg uses
`contiguous_no_reference_graph`, and the corresponding sparse-path tests are
skipped; this is not equivalent to the complete release. Development APIs from
older FFmpeg versions are not guaranteed to be compatible with the current C++
sources.

Building an sdist alone does not require FFmpeg development libraries:

```bash
python -m build --sdist --no-isolation
```

The build entry point defers `pkg-config` queries until extension compilation,
allowing metadata and source distributions to be generated independently.

## 5. Rebuild from the corresponding-sources archive

Extract the release's `*-corresponding-sources.tar.gz`, then extract the project
sdist inside it. Point `SOURCE_CACHE` to the archive's `downloads/` directory and
run the same build command. This reuses the native sources verified by SHA256;
Python build tools must still be preinstalled or fetched from PyPI. The archive
also contains the pybind11 sources and headers used to compile the extension,
along with third-party licenses.

HTTP source downloads retry up to three times, waiting 2 and 4 seconds between
attempts. Every download and cache hit must pass the pinned SHA256 check. Failed
responses, when available, and JSON metadata (URL, status, content type, size,
expected/actual hashes) are retained under `SOURCE_CACHE/diagnostics/`. Git source
fetches retain their existing behavior. If retries still fail, inspect these files
instead of changing the pinned hash. A corrupt cached archive must be removed and
downloaded again.

GitHub Actions caches verified source archives using a key derived from
`tools/sources.json`. On job failure, it uploads download diagnostics as the
`codec4ai-download-diagnostics` artifact. Partial downloads and diagnostics are
excluded from the source cache. Build logs retain failed compiler commands.

Build-helper regression tests need only the Python standard library:

```bash
python -m unittest discover -s tools/tests -p 'test_*.py' -v
```
