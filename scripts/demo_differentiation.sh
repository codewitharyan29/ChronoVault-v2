#!/usr/bin/env bash
# scripts/demo_differentiation.sh
#
# A 15-20 second visual demo of the actual measured comparison already
# documented in README.md and proven in compare_delta_base_selection.py.
# Computes FRESH numbers on every run -- nothing here is pre-baked.
# Found by a real Windows user running Git Bash: standard Windows
# Python installers add `python` to PATH, not `python3` -- a
# hardcoded `python3` here failed even inside Git Bash, which
# otherwise runs these shell scripts fine. Detect what's actually
# available rather than assuming a Unix-style PATH.
if command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    PY=python
fi
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

$PY "$SCRIPT_DIR/../scripts/_differentiation_table.py"
