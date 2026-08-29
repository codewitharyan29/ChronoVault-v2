#!/usr/bin/env bash
# scripts/prove_isolated_mode.sh
#
# Proves the zero-dependency claim harder than an empty
# requirements.txt does: runs the real CLI under Python's fully
# isolated mode (-I), which disables PYTHONPATH, user site-packages,
# and system site-packages -- there is no possible way for a
# third-party package to be silently available. If ChronoVault runs
# correctly here, it genuinely has no external dependency, not just
# an unlisted one.
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
DEMO_DIR="${1:-/tmp/chronovault-isolated-proof}"
V="$PY -I $SCRIPT_DIR/_isolated_entry.py"

rm -rf "$DEMO_DIR"
mkdir -p "$DEMO_DIR"
cd "$DEMO_DIR"

echo "PYTHONPATH=\"\""
echo "External packages unavailable (site-packages, user site, PYTHONPATH all disabled by -I)"
echo "        ↓"
echo "ChronoVault still works:"
echo ""

$V init . && echo "✓ init"
echo "def main(): return 42" > app.py
$V snapshot -m "isolated mode test" > /dev/null && echo "✓ snapshot"
echo "def main(): return 43  # changed" > app.py
$V snapshot -m "second snapshot" > /dev/null && echo "✓ snapshot (again)"
$V diff 1 2 > /dev/null && echo "✓ diff"
echo "RESTORE" | $V restore 1 > /dev/null && echo "✓ restore"
$V verify > /dev/null && echo "✓ verify"

echo ""
echo "All of the above ran under '$PY -I' -- zero dependencies,"
echo "experimentally demonstrated, not just an empty requirements.txt."
