#!/usr/bin/env python3
"""Collect corresponding sources, build provenance and release checksums."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

import pybind11

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-cache", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    name = args.sdist.name.removesuffix(".tar.gz")
    source_manifest = json.loads((ROOT / "tools/sources.json").read_text())
    source_bundle = output / f"{name}-corresponding-sources.tar.gz"
    with tempfile.TemporaryDirectory(prefix="codec4ai-sources-") as tmp:
        stage = Path(tmp) / "corresponding-sources"
        (stage / "downloads").mkdir(parents=True)
        for item in source_manifest["sources"]:
            src = args.source_cache / item["filename"]
            if sha256(src) != item["sha256"]:
                raise RuntimeError(f"Source checksum mismatch: {item['name']}")
            shutil.copy2(src, stage / "downloads" / src.name)
        shutil.copy2(args.sdist, stage / args.sdist.name)
        shutil.copy2(ROOT / "tools/sources.json", stage / "sources.json")
        # pybind11 template code is part of the compiled extension.
        shutil.copytree(Path(pybind11.__file__).parent, stage / "pybind11",
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copytree(ROOT / "licenses", stage / "licenses")
        (stage / "README.md").write_text(
            "# Corresponding sources\n\n"
            "This archive accompanies the GPL-3.0-or-later video-loader wheel.\n"
            "It contains the project sdist (including all build scripts and the FFmpeg patch), "
            "the exact upstream codec/assembler source archives, and the pybind11 sources/headers "
            f"used to compile the extension (version {pybind11.__version__}).\n\n"
            "Extract the project sdist, install tools/build-requirements.txt in a Python environment, "
            "then run tools/build_release.sh with SOURCE_CACHE set to this archive's downloads/ "
            "directory. Native source downloads will be read from that cache and verified. "
            "See docs/BUILDING.md for system prerequisites and supported targets. "
            "The build environment may still download Python build tools from PyPI. "
            "Compiler/OS differences can change binary hashes.\n"
        )
        with tarfile.open(source_bundle, "w:gz") as archive:
            archive.add(stage, arcname=stage.name)
    with zipfile.ZipFile(args.wheel) as wheel:
        bundled = [entry for entry in wheel.namelist() if ".libs/" in entry and not entry.endswith("/")]
        wheel_metadata = wheel.read(next(entry for entry in wheel.namelist() if entry.endswith("/WHEEL"))).decode()
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "wheel": args.wheel.name,
        "wheel_sha256": sha256(args.wheel),
        "sdist": args.sdist.name,
        "sdist_sha256": sha256(args.sdist),
        "corresponding_sources": source_bundle.name,
        "corresponding_sources_sha256": sha256(source_bundle),
        "python": sys.version.split()[0],
        "platform": platform.system(),
        "architecture": platform.machine(),
        "libc": platform.libc_ver(),
        "compiler": subprocess.check_output(["c++", "--version"], text=True).splitlines()[0],
        "build_tools": {name: metadata.version(name) for name in
                        ("build", "setuptools", "wheel", "pybind11", "auditwheel", "twine")},
        "native_sources": source_manifest["sources"],
        "ffmpeg_patch_sha256": sha256(ROOT / "tools/patches/ffmpeg-8.1.2-reference-graph.patch"),
        "bundled_libraries": bundled,
        "wheel_metadata": wheel_metadata,
        "publication_status": "local artifacts; not uploaded to a package index",
    }
    (output / "build-manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    artifacts = sorted(p for p in output.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    (output / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in artifacts))
    print(f"Wrote {source_bundle.name} and SHA256SUMS")


if __name__ == "__main__":
    main()
