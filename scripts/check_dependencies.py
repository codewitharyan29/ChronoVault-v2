#!/usr/bin/env python3
"""
scripts/check_dependencies.py — scans every .py file in this project
via the AST (not a naive grep) and verifies every top-level import
resolves to a Python standard library module. Exits non-zero and
prints exactly what failed if it finds anything that isn't stdlib.

Usage: python3 scripts/check_dependencies.py
Output is also saved to deps-proof.txt (see Makefile's `verify-deps`).
"""
import ast
import pathlib
import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = pathlib.Path(__file__).resolve().parent.parent

# sys.stdlib_module_names exists from Python 3.10+. Fall back to a
# manual list for older interpreters rather than failing outright.
if hasattr(sys, "stdlib_module_names"):
    STDLIB_MODULES = set(sys.stdlib_module_names)
else:  # pragma: no cover - only exercised on Python < 3.10
    STDLIB_MODULES = {
        "os", "sys", "json", "hashlib", "zlib", "tempfile", "pathlib",
        "dataclasses", "typing", "argparse", "datetime", "time",
        "http", "socketserver", "urllib", "unittest", "shutil",
        "ast", "struct", "io", "collections", "functools", "__future__",
    }

# This project's own package name — not a "dependency" in any
# meaningful sense, but ast import scanning surfaces it.
INTERNAL_MODULES = {"vault"}


def find_imports(py_file: pathlib.Path) -> set:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            # node.level == 0 excludes relative imports ("from . import x"),
            # which don't name an external module at all.
            found.add(node.module.split(".")[0])
    return found


def main() -> int:
    py_files = sorted(ROOT.glob("**/*.py"))
    py_files = [p for p in py_files if "__pycache__" not in p.parts]

    all_imports = set()
    for f in py_files:
        all_imports |= find_imports(f)

    external = sorted(
        m for m in all_imports
        if m not in STDLIB_MODULES and m not in INTERNAL_MODULES
    )
    stdlib_used = sorted(m for m in all_imports if m in STDLIB_MODULES)

    print(f"Scanning {len(py_files)} Python file(s)...\n")
    print("Standard library modules used:")
    for m in stdlib_used:
        print(f"  {m:<20} ✓ stdlib")
    print()

    if external:
        print("External dependencies found:\n")
        for m in external:
            print(f"  {m}")
        print("\nStatus: FAILED — external dependencies present")
        return 1
    else:
        print("External dependencies found:\n\n  NONE\n")
        print("Status: ZERO DEPENDENCY VERIFIED")
        return 0


if __name__ == "__main__":
    sys.exit(main())
