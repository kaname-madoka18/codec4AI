#!/usr/bin/env bash
# Compatibility entry point for the previous package build script.
set -euo pipefail
exec bash "$(dirname "${BASH_SOURCE[0]}")/build_release.sh" "$@"
