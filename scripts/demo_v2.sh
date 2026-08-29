#!/usr/bin/env bash
# scripts/demo_v2.sh — one-command, end-to-end ChronoVault v2 demo.
# Runs against a throwaway directory; never touches anything else.
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

# Resolve to an ABSOLUTE path BEFORE cd'ing into the demo directory --
# a relative $(dirname "$0") breaks the moment the working directory
# changes below. Found by actually running the script, not by
# inspection: `make demo-v2` failed with "No such file or directory"
# on the very first real run.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V="$PY $SCRIPT_DIR/../chronovault.py"
DEMO_DIR="${1:-/tmp/chronovault-demo-run}"

rm -rf "$DEMO_DIR"
mkdir -p "$DEMO_DIR"
cd "$DEMO_DIR"

step() { echo ""; echo "─────────────────────────────────────────"; echo "▶ $1"; echo "─────────────────────────────────────────"; }

step "1/9  vault init"
$V init .

step "2/9  Create a realistic file, first snapshot"
$PY -c "
lines = [f'def function_{i}():\n    return {i}\n' for i in range(150)]
open('app.py', 'w').write(''.join(lines))
"
echo "app config v1" > config.txt
$V snapshot -m "initial version"

step "3/9  Small realistic edit, second snapshot"
$PY -c "
lines = [f'def function_{i}():\n    return {i}\n' for i in range(150)]
lines[42] = 'def function_42():\n    return 42 + 1  # bugfix\n'
open('app.py', 'w').write(''.join(lines))
"
$V snapshot -m "bugfix in function_42"

step "4/9  vault diff — see exactly what changed"
$V diff 1 2

step "5/9  vault log — history of one file across all snapshots"
$V log app.py

step "6/9  vault pack — delta compression + consolidation"
$V pack

step "7/9  vault verify — integrity check on the packed repository"
$V verify

step "8/9  vault benchmark — real, fresh performance measurements"
$V benchmark

step "9/9  vault stress-test — concurrency safety + corruption recovery, proven live"
$V stress-test --processes 10

echo ""
echo "═════════════════════════════════════════"
echo "Demo complete. Repository left at: $DEMO_DIR"
echo "═════════════════════════════════════════"
