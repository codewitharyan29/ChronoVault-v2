#!/usr/bin/env python3
"""
scripts/judge_mode.py

One command, the whole project's real evidence. Every checkmark below
is the result of an ACTUAL subprocess run right now -- this is an
aggregator of real checks, not a static list. If any of them fail,
this script reports the failure plainly rather than hiding it behind
a checkmark.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Windows: this script's own stdout can be a non-UTF-8 console codepage
# (cp1252 "charmap"), which raises UnicodeEncodeError on the checkmark
# characters below. Force UTF-8 for our own output, independent of how
# the script is invoked (double-click, subprocess, PowerShell, VS Code).
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def run(cmd: list, cwd=None) -> tuple:
    # Found by a real Windows user: subprocess.run()'s own internal
    # thread.join() (inside communicate()) can raise a spurious
    # KeyboardInterrupt on Windows, unrelated to any actual user
    # keypress -- the same documented Windows multiprocessing/threading
    # quirk already fixed in tests/test_experimental_lock.py's
    # p.join() calls, but showing up here too, confirming it's not
    # limited to one call site. Retry, bounded by an overall deadline,
    # so a genuine hang still surfaces as a real timeout.
    #
    # encoding="utf-8" here matters independently of the child script's
    # own stdout fix: subprocess.run(text=True) decodes the child's
    # bytes using locale.getpreferredencoding() by default, which is
    # cp1252 on Windows -- so even a child that emits UTF-8 correctly
    # can fail (or mangle) decoding on this side unless told explicitly.
    # errors="replace" keeps a decode hiccup from turning into a hang
    # or crash here; a real content problem still shows up as garbled
    # text in the report rather than silently succeeding.
    # The per-step cap has to clear the slowest legitimate step (the full
    # unittest run) on the slowest realistic machine -- a 2-core CI
    # Windows runner, not just a fast dev laptop -- while still cutting off
    # a genuine infinite hang. 900s does both; the CI job's own
    # timeout-minutes is the outer backstop.
    deadline = time.time() + 930  # a little past subprocess's own 900s timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError(f"'{' '.join(cmd)}' did not complete after repeated retries")
        try:
            result = subprocess.run(
                cmd, cwd=cwd or ROOT, capture_output=True, text=True,
                timeout=900, encoding="utf-8", errors="replace"
            )
            return result.returncode, result.stdout, result.stderr
        except KeyboardInterrupt:
            continue


def check(label: str, ok: bool, detail: str = "") -> bool:
    symbol = "✓" if ok else "✗"
    line = f"[{symbol}] {label}"
    if detail:
        line += f"  ({detail})"
    print(line)
    return ok


def main():
    print("╔════════════════════════════════════════════╗")
    print("║          CHRONOVAULT JUDGE MODE             ║")
    print("╚════════════════════════════════════════════╝")
    print()

    results = []

    # -- Zero dependencies --
    code, out, err = run([sys.executable, "scripts/check_dependencies.py"])
    results.append(check("Zero dependencies", "ZERO DEPENDENCY VERIFIED" in out))

    # -- Single-file build --
    code, out, err = run([sys.executable, "scripts/build_single_file.py"])
    build_ok = code == 0 and (ROOT / "dist" / "chronovault_single.py").exists()
    results.append(check("Single-file build", build_ok))

    # -- Full test suite --
    code, out, err = run([sys.executable, "-m", "unittest", "discover", "tests"])
    test_output = out + err
    n_tests = 0
    for line in test_output.splitlines():
        if line.startswith("Ran "):
            try:
                n_tests = int(line.split()[1])
            except (IndexError, ValueError):
                pass
    results.append(check(f"{n_tests} tests", code == 0 and "OK" in test_output, f"{n_tests} tests"))

    # -- Reproducible storage --
    code, out, err = run([sys.executable, "scripts/prove_reproducible.py"])
    results.append(check("Reproducible storage", code == 0 and "PROVEN" in out))

    # -- Security demonstrations --
    code, out, err = run([sys.executable, "scripts/security_demo.py"])
    security_line = next((l for l in out.splitlines() if "PASSED" in l), "")
    results.append(check("Security demonstrations", code == 0, security_line))

    # -- Core capability checklist: each is a real, already-proven
    # property (checked here by re-running the specific test module
    # that covers it, not a static claim) --
    capability_modules = [
        ("Snapshot / restore", "tests.test_snapshot"),
        ("Deduplication", "tests.test_objects"),
        ("Integrity verification", "tests.test_objects"),
        ("Path traversal protection", "tests.test_restore"),
        ("Symlink safety", "tests.test_snapshot"),
        ("Concurrent writers", "tests.test_experimental_lock"),
        ("Pack files", "tests.test_experimental_packfile_v2"),
        ("Delta compression", "tests.test_experimental_delta"),
        ("Delta-aware GC", "tests.test_v2_delta_gc"),
        ("Path-history index", "tests.test_experimental_path_history"),
        ("HTTP inspector", "tests.test_inspector"),
    ]
    for label, module in capability_modules:
        code, out, err = run([sys.executable, "-m", "unittest", module])
        results.append(check(label, code == 0))

    # -- Differentiation, real numbers computed fresh --
    print()
    print("Differentiation")
    print("─" * 16)
    code, out, err = run([sys.executable, "scripts/_differentiation_table.py"])
    diff_ok = code == 0
    for line in out.splitlines():
        if ("candidates" in line or "false" in line.lower()) and "║" in line:
            # Split on the box-drawing column separator and print each
            # side as its own clean line -- the raw table line has an
            # internal "║" that .strip() alone won't remove.
            parts = [p.strip() for p in line.split("║") if p.strip()]
            for p in parts:
                print(f"  {p}")
    results.append(diff_ok)

    print()
    all_pass = all(results)
    print("FINAL STATUS")
    print("═" * 46)
    if all_pass:
        print("          CHRONOVAULT: VERIFIED ✓")
    else:
        failed = len([r for r in results if not r])
        print(f"          {failed} CHECK(S) FAILED — see above")
    print("═" * 46)

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())