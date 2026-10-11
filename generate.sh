#!/bin/sh
# Lint the repository, then build, assemble and smoke-test the toolset for
# this machine's platform. Result: out/frice-tools-<version>-<platform>.zip
#
# Set PYTHON to choose the interpreter (default: python3, then python).
# Python 3.12 or newer is required.

set -eu
cd "$(dirname "$0")"

if [ -z "${PYTHON:-}" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON=python3
    elif command -v python >/dev/null 2>&1; then
        PYTHON=python
    else
        echo "generate.sh: Python 3.12 or newer is required" >&2
        exit 1
    fi
fi

for step in lint build assemble smoke; do
    echo "== $step"
    "$PYTHON" scripts/tools.py "$step"
done