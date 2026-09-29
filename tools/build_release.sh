#!/usr/bin/env bash
set -euo pipefail

package_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${PYTHON:-python3}"
build_root="${BUILD_ROOT:-$(mktemp -d /tmp/codec4ai-build-XXXXXX)}"
mkdir -p "${build_root}"
build_root="$(cd "${build_root}" && pwd)"
source_cache="${SOURCE_CACHE:-${build_root}/downloads}"
version="$("${python}" -c 'import ast,sys; t=ast.parse(open(sys.argv[1]).read()); print(next(ast.literal_eval(n.value) for n in t.body if isinstance(n, ast.Assign) and any(isinstance(x, ast.Name) and x.id == "__version__" for x in n.targets)))' "${package_dir}/src/video_loader/__init__.py")"
output_dir="${OUTPUT_DIR:-${package_dir}/releases/${version}}"
mkdir -p "${output_dir}"
output_dir="$(cd "${output_dir}" && pwd)"
command -v patchelf >/dev/null || { echo 'patchelf is required' >&2; exit 1; }
"${python}" -c 'import build, setuptools, pybind11, auditwheel, twine'

"${python}" "${package_dir}/tools/build_native_deps.py" \
  --work-dir "${build_root}" --cache-dir "${source_cache}" --jobs "${BUILD_JOBS:-4}"
prefix="$(cat "${build_root}/native-prefix.txt")"
source_copy="$(mktemp -d "${build_root}/package-source-XXXXXX")"
"${python}" - "${package_dir}" "${source_copy}" <<'PY'
from pathlib import Path
import shutil, sys
root, dest = map(Path, sys.argv[1:])
for name in ('README.md', 'LICENSE', 'NOTICE', 'MANIFEST.in', 'pyproject.toml', 'setup.py'):
    shutil.copy2(root / name, dest / name)
for name in ('src', 'tests', 'tools', 'docs', 'licenses', 'examples', 'analysis', 'benchmarks'):
    shutil.copytree(root / name, dest / name,
                   ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.so', '*.egg-info', '.pytest_cache'))
PY
raw_dir="$(mktemp -d "${build_root}/raw-dist-XXXXXX")"
PKG_CONFIG_PATH="${prefix}/lib/pkgconfig" LD_LIBRARY_PATH="${prefix}/lib" \
  "${python}" -m build --no-isolation --outdir "${raw_dir}" "${source_copy}"
shopt -s nullglob
raw_wheels=("${raw_dir}"/*.whl)
sdists=("${raw_dir}"/*.tar.gz)
[[ ${#raw_wheels[@]} -eq 1 && ${#sdists[@]} -eq 1 ]]
repair_args=()
if [[ -n "${AUDITWHEEL_PLAT:-}" ]]; then
  repair_args+=(--plat "${AUDITWHEEL_PLAT}")
fi
repaired_dir="$(mktemp -d "${build_root}/repaired-XXXXXX")"
LD_LIBRARY_PATH="${prefix}/lib" "${python}" -m auditwheel repair \
  "${repair_args[@]}" --wheel-dir "${repaired_dir}" "${raw_wheels[0]}"
repaired_wheels=("${repaired_dir}"/*.whl)
[[ ${#repaired_wheels[@]} -eq 1 ]]
"${python}" -m twine check "${repaired_wheels[0]}" "${sdists[0]}"
cp "${repaired_wheels[0]}" "${sdists[0]}" "${output_dir}/"
"${python}" "${package_dir}/tools/release_manifest.py" \
  --output-dir "${output_dir}" --source-cache "${source_cache}" \
  --sdist "${sdists[0]}" --wheel "${repaired_wheels[0]}"
"${python}" -m auditwheel show "${repaired_wheels[0]}"
printf 'Release artifacts: %s\n' "${output_dir}"
