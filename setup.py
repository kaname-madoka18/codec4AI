from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from pybind11.setup_helpers import Pybind11Extension, build_ext
from setuptools import find_packages, setup


ROOT = Path(__file__).parent


def pkg_config(option: str) -> list[str]:
    completed = subprocess.run(
        [
            "pkg-config",
            option,
            "libavformat",
            "libavcodec",
            "libavutil",
            "libswscale",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return shlex.split(completed.stdout)


class FFmpegBuildExt(build_ext):
    """Resolve native dependencies only when compiling, not for an sdist."""

    def build_extensions(self) -> None:
        try:
            cflags = pkg_config("--cflags")
            ldflags = pkg_config("--libs")
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                "FFmpeg development libraries were not found. See docs/BUILDING.md "
                "or use tools/build_release.sh to build the pinned dependencies."
            ) from exc
        for extension in self.extensions:
            extension.include_dirs.extend(f[2:] for f in cflags if f.startswith("-I"))
            extension.extra_compile_args.extend(f for f in cflags if not f.startswith("-I"))
            extension.libraries.extend(f[2:] for f in ldflags if f.startswith("-l"))
            extension.library_dirs.extend(f[2:] for f in ldflags if f.startswith("-L"))
            extension.extra_link_args.extend(
                f for f in ldflags if not f.startswith(("-l", "-L"))
            )
        super().build_extensions()

extension = Pybind11Extension(
    "video_loader._native",
    ["src/native/native.cpp"],
    extra_compile_args=["-O3", "-Wall", "-Wextra", f"-ffile-prefix-map={ROOT.resolve()}=."],
    cxx_std=17,
)

setup(
    package_dir={"": "src"},
    packages=find_packages("src"),
    ext_modules=[extension],
    cmdclass={"build_ext": FFmpegBuildExt},
    zip_safe=False,
)
