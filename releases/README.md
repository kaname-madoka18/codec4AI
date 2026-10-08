# Local release artifacts

English | [Chinese](README_cn.md)

Run `bash tools/build_release.sh` to create a versioned subdirectory containing
the wheel, source distribution, corresponding-sources archive, build manifest
and SHA256 checksums. Binary artifacts are ignored by Git and should be attached
to a release, not committed into the source repository.

See [the release guide](../docs/RELEASING.md). Local files have not automatically
been uploaded to GitHub or PyPI.
