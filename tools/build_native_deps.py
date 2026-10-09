#!/usr/bin/env python3
"""Build public, pinned codec dependencies. Does not need root or private paths."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def run(command, *, cwd=None, env=None):
    print("+ " + shlex.join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), cwd=cwd, env=env, check=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download_archive(item, partial):
    """Retry HTTP downloads without ever accepting a different source hash."""
    attempts = 3
    for attempt in range(1, attempts + 1):
        partial.unlink(missing_ok=True)
        details = {
            "source": item["name"], "url": item["url"], "attempt": attempt,
            "expected_sha256": item["sha256"],
        }
        try:
            with urllib.request.urlopen(item["url"], timeout=120) as response:
                details.update(status=response.status, final_url=response.geturl(),
                               content_type=response.headers.get("Content-Type"))
                with partial.open("wb") as output:
                    shutil.copyfileobj(response, output)
            if digest(partial) != item["sha256"]:
                raise ValueError(f"SHA256 mismatch for {item['name']}")
            return
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # HTTPError is also an OSError. Preserve its response body when available.
            if isinstance(exc, urllib.error.HTTPError):
                details.update(status=exc.code, final_url=exc.geturl(),
                               content_type=exc.headers.get("Content-Type"))
                try:
                    with exc, partial.open("wb") as output:
                        shutil.copyfileobj(exc, output)
                except (OSError, http.client.HTTPException) as body_error:
                    details["response_read_error"] = str(body_error)
            details["error"] = str(exc)
            diagnostics = partial.parent / "diagnostics"
            diagnostics.mkdir(parents=True, exist_ok=True)
            basename = f"{item['filename']}.attempt-{attempt}"
            if partial.exists():
                details.update(size=partial.stat().st_size, actual_sha256=digest(partial))
                response_path = diagnostics / f"{basename}.response"
                partial.replace(response_path)
                details["response_file"] = response_path.name
            report = diagnostics / f"{basename}.json"
            report.write_text(json.dumps(details, indent=2) + "\n")
            print(f"Download failed ({attempt}/{attempts}): "
                  f"{json.dumps(details)}; diagnostics: {report}", flush=True)
            if attempt == attempts:
                raise RuntimeError(
                    f"Failed to download {item['name']} after {attempts} attempts; "
                    f"see {diagnostics}"
                ) from exc
            time.sleep(2 ** attempt)


def fetch(item, cache):
    archive = cache / item["filename"]
    if archive.is_file():
        if digest(archive) != item["sha256"]:
            raise RuntimeError(f"SHA256 mismatch: {archive}; remove the corrupt cached file")
        return archive
    partial = archive.with_suffix(archive.suffix + ".part")
    try:
        if "repository" in item:
            with tempfile.TemporaryDirectory(prefix="codec4ai-git-") as directory:
                run(["git", "init", "--quiet", directory])
                run(["git", "-C", directory, "remote", "add", "origin", item["repository"]])
                run(["git", "-C", directory, "fetch", "--quiet", "--depth=1", "origin", item["commit"]])
                commit = subprocess.check_output(
                    ["git", "-C", directory, "rev-parse", "FETCH_HEAD"], text=True
                ).strip()
                if commit != item["commit"]:
                    raise RuntimeError("Unexpected source commit")
                # Fixed gzip header makes the cached source archive reproducible.
                with partial.open("wb") as output:
                    with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as zipped:
                        source = subprocess.Popen(
                            ["git", "-C", directory, "archive", "--format=tar",
                             f"--prefix={item['name']}-{item['version']}/", commit],
                            stdout=subprocess.PIPE,
                        )
                        try:
                            shutil.copyfileobj(source.stdout, zipped)
                        finally:
                            source.stdout.close()
                        if source.wait():
                            raise RuntimeError("git archive failed")
        else:
            download_archive(item, partial)
        if digest(partial) != item["sha256"]:
            raise RuntimeError(f"SHA256 mismatch for {item['name']}")
        partial.replace(archive)
        return archive
    finally:
        partial.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        parser.error("The bundled build currently supports Linux x86_64 only")
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    for tool in ("cc", "c++", "make", "cmake", "pkg-config", "patch", "tar", "git", "perl"):
        if not shutil.which(tool):
            parser.error(f"Missing build tool: {tool}")
    work = args.work_dir.resolve()
    cache = args.cache_dir.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    manifest = ROOT / "tools/sources.json"
    patch = ROOT / "tools/patches/ffmpeg-8.1.2-reference-graph.patch"
    stamp = hashlib.sha256(
        manifest.read_bytes() + patch.read_bytes() + Path(__file__).read_bytes()
        + subprocess.check_output([os.environ.get("CC", "cc"), "--version"])
        + json.dumps({k: os.environ.get(k, "") for k in ("CC", "CXX", "CFLAGS", "CXXFLAGS", "LDFLAGS")}, sort_keys=True).encode()
    ).hexdigest()[:16]
    stage = work / "native" / stamp
    prefix = stage / "prefix"
    items = json.loads(manifest.read_text())["sources"]
    archives = {item["name"]: fetch(item, cache) for item in items}
    if not (prefix / ".complete").exists():
        prefix.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PATH"] = f"{prefix}/bin:{env['PATH']}"
        env["PKG_CONFIG_PATH"] = f"{prefix}/lib/pkgconfig"
        env["LD_LIBRARY_PATH"] = f"{prefix}/lib"
        env["CFLAGS"] = f"-O2 -fPIC -ffile-prefix-map={stage}=."
        env["CXXFLAGS"] = env["CFLAGS"]
        for item in items:
            name = item["name"]
            done = stage / f"{name}.done"
            if done.exists():
                continue
            src = stage / "src" / name
            if src.exists():
                shutil.rmtree(src)
            src.mkdir(parents=True)
            run(["tar", "-xf", archives[name], "--strip-components=1", "-C", src])
            common = [f"-DCMAKE_INSTALL_PREFIX={prefix}", "-DCMAKE_INSTALL_LIBDIR=lib",
                      "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_POSITION_INDEPENDENT_CODE=ON"]
            if name == "nasm":
                run(["./configure", f"--prefix={prefix}"], cwd=src, env=env)
                run(["make", "-j", args.jobs], cwd=src, env=env)
                run(["make", "install"], cwd=src, env=env)
            elif name == "x264":
                run(["./configure", f"--prefix={prefix}", "--enable-shared", "--enable-pic",
                     "--disable-cli", "--disable-opencl", "--bit-depth=8", "--chroma-format=420"], cwd=src, env=env)
                run(["make", "-j", args.jobs], cwd=src, env=env)
                run(["make", "install"], cwd=src, env=env)
            elif name in ("x265", "aom"):
                build = src / "codec4ai-build"
                if name == "x265":
                    source = src / "source"
                    flags = ["-DENABLE_SHARED=ON", "-DENABLE_CLI=OFF", "-DENABLE_LIBNUMA=OFF",
                             "-DENABLE_PIC=ON", "-DHIGH_BIT_DEPTH=OFF", "-DENABLE_ASSEMBLY=ON"]
                else:
                    source = src
                    flags = ["-DBUILD_SHARED_LIBS=OFF", "-DENABLE_TESTS=OFF", "-DENABLE_EXAMPLES=OFF",
                             "-DENABLE_TOOLS=OFF", "-DENABLE_DOCS=OFF", "-DCONFIG_AV1_ENCODER=0"]
                run(["cmake", "-S", source, "-B", build, *common, *flags], env=env)
                run(["cmake", "--build", build, "--parallel", args.jobs], env=env)
                run(["cmake", "--install", build], env=env)
            elif name == "ffmpeg":
                run(["patch", "--batch", "-p1", "--input", patch], cwd=src, env=env)
                # Preserve original copyrights and identify every modified source.
                changed = [line[6:] for line in patch.read_text().splitlines() if line.startswith("+++ b/")]
                for relative in changed:
                    path = src / relative
                    path.write_text("/* Modified by codec4AI contributors, 2026-09-29: reference graph/state-only decoding. */\n" + path.read_text())
                run(["./configure", f"--prefix={prefix}", "--disable-everything", "--disable-autodetect",
                     "--disable-programs", "--disable-doc", "--disable-debug", "--disable-network",
                     "--disable-static", "--enable-shared", "--enable-pic", "--disable-avdevice",
                     "--disable-avfilter", "--disable-swresample", "--enable-swscale", "--enable-gpl",
                     "--enable-version3", "--enable-libx264", "--enable-libx265", "--enable-libaom",
                     "--enable-decoder=h264,hevc,libaom_av1", "--enable-encoder=libx264,libx265",
                     "--enable-demuxer=mov", "--enable-muxer=mp4", "--enable-parser=h264,hevc,av1",
                     "--enable-protocol=file", f"--extra-cflags=-I{prefix}/include -ffile-prefix-map={stage}=.",
                     f"--extra-ldflags=-L{prefix}/lib"], cwd=src, env=env)
                run(["make", "-j", args.jobs], cwd=src, env=env)
                run(["make", "install"], cwd=src, env=env)
            else:
                raise RuntimeError(f"Unsupported source: {name}")
            done.write_text(item["sha256"] + "\n")
        (prefix / ".complete").write_text(stamp + "\n")
    (work / "native-prefix.txt").write_text(str(prefix) + "\n")
    print(f"Native dependencies ready: {prefix}", flush=True)


if __name__ == "__main__":
    main()
