#!/usr/bin/env python3
"""Check wheel-repair prerequisites before compiling native dependencies."""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess


def check_patchelf():
    executable = shutil.which("patchelf")
    if executable is None:
        raise RuntimeError(
            "patchelf >= 0.14.5 is required; install tools/build-requirements.txt "
            "in the build environment"
        )
    reported = subprocess.check_output([executable, "--version"], text=True).strip()
    match = re.fullmatch(r"patchelf\s+(\d+)\.(\d+)(?:\.(\d+))?(?:[.-]\S+)?", reported)
    if match is None or tuple(int(part or 0) for part in match.groups()) < (0, 14, 5):
        raise RuntimeError(
            f"auditwheel requires patchelf >= 0.14.5; found {reported!r} at {executable}. "
            "Install tools/build-requirements.txt and check PATH."
        )
    return Path(executable), reported


if __name__ == "__main__":
    try:
        path, version = check_patchelf()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Wheel repair tool: {path} ({version})", flush=True)
