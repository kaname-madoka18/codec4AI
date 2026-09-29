#!/usr/bin/env python3
"""Install a wheel in a clean venv and validate it outside the source tree."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

SMOKE = r'''
import importlib.metadata, json, shutil
from pathlib import Path
import numpy as np
import video_loader
assert shutil.which("ffmpeg") is None
assert "/site-packages/" in str(Path(video_loader.__file__).resolve())
assert video_loader.__version__ == importlib.metadata.version("video-loader")
results = {}
frames = np.random.default_rng(42).integers(0, 256, (33, 48, 64, 3), dtype=np.uint8)
for mode in ("base264", "base265", "fast264", "fast265", "ufast264"):
    payload = video_loader.transcode(frames, fps=30, mode=mode)
    actual, metrics = video_loader.decode(payload, [17, 2, 17], return_info=True)
    assert actual.shape == (3, 48, 64, 3)
    np.testing.assert_array_equal(actual[0], actual[2])
    assert metrics["reference_graph_available"], metrics
    assert metrics["closure_mode"] == "reference_graph_bfs", metrics
    results[mode] = metrics["closure_mode"]
print(json.dumps({"version": video_loader.__version__, "no_ffmpeg_cli": True,
                  "no_ld_library_path": True, "modes": results}, indent=2))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--junit-xml", type=Path)
    args = parser.parse_args()
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            parser.error(f"{command} is needed for integration comparisons")
    env = os.environ.copy()
    for variable in ("LD_LIBRARY_PATH", "LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME"):
        env.pop(variable, None)
    with tempfile.TemporaryDirectory(prefix="codec4ai-verify-") as directory:
        work = Path(directory)
        subprocess.run([sys.executable, "-m", "venv", str(work / "venv")], check=True, env=env)
        python = work / "venv/bin/python"
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        f"{args.wheel.resolve()}[test]"], check=True, cwd=work, env=env)
        no_cli = dict(env, PATH=str(work / "no-executables"))
        subprocess.run([str(python), "-I", "-c", SMOKE], check=True, cwd=work, env=no_cli)
        command = [str(python), "-I", "-m", "pytest", str(ROOT / "tests"), "-q"]
        if args.junit_xml:
            command.append(f"--junitxml={args.junit_xml.resolve()}")
        subprocess.run(command, check=True, cwd=work, env=env)
        subprocess.run([str(python), "-I", "-m", "unittest", "discover", "-s", str(ROOT / "analysis"),
                        "-p", "test_*.py", "-v"], check=True, cwd=work, env=env)


if __name__ == "__main__":
    main()
