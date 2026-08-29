#!/usr/bin/env bash
# scripts/verify_single_file.sh
#
# Automated, reproducible proof that dist/chronovault_single.py is
# functionally equivalent to the real modular CLI -- not a one-off
# manual check. Exits non-zero on ANY failure, so this is safe to
# wire into CI or a pre-freeze checklist.
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

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SINGLE="$PY $ROOT/dist/chronovault_single.py"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

cd "$WORKDIR"
FAIL=0
check() { if [ "$1" -ne 0 ]; then echo "FAILED: $2"; FAIL=1; else echo "ok: $2"; fi; }

$SINGLE init . > /dev/null 2>&1; check $? "init"

echo "content v1" > app.py
$SINGLE snapshot -m "v1" > /dev/null 2>&1; check $? "snapshot (1st)"

echo "content v2 edited" > app.py
$SINGLE snapshot -m "v2" > /dev/null 2>&1; check $? "snapshot (2nd, delta candidate vs 1st)"

$SINGLE diff 1 2 > /dev/null 2>&1; check $? "diff"
$SINGLE log app.py > /dev/null 2>&1; check $? "log"
$SINGLE pack > /dev/null 2>&1; check $? "pack (delta compression + consolidation)"
$SINGLE verify > /dev/null 2>&1; check $? "verify (after packing)"

rm app.py
echo "RESTORE" | $SINGLE restore 2 > /dev/null 2>&1
[ "$(cat app.py)" = "content v2 edited" ]; check $? "restore (byte-correct, after packing)"

$SINGLE snapshot-rm 1 > /dev/null 2>&1
$SINGLE gc > /dev/null 2>&1; check $? "gc (delta-aware, after deleting the delta base's owning snapshot)"

$SINGLE benchmark > /dev/null 2>&1; check $? "benchmark"

STRESS_OUT="$($SINGLE stress-test --processes 8 2>&1)"
echo "$STRESS_OUT" | grep -q "Unique IDs:           8" && \
  echo "$STRESS_OUT" | grep -q "Result: PASS" ; check $? "stress-test (real concurrent self-invocation)"

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "ALL CHECKS PASSED -- dist/chronovault_single.py is functionally equivalent to the modular CLI."
  exit 0
else
  echo "SOME CHECKS FAILED -- see above."
  exit 1
fi
