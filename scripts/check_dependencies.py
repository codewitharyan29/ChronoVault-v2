#!/usr/bin/env python3
"""
scripts/check_dependencies.py — proves ChronoVault's runtime is Python
standard library only.

Scope (stated honestly, because a dependency checker that overclaims is
worse than none):

  1. Every `import x` / `from x import ...` in every .py file is parsed
     from the AST (not grepped) and checked against
     `sys.stdlib_module_names`. Nested/function-body imports are
     included -- ast.walk visits every node.

  2. Dynamic-import escape routes are checked too: a call to
     `importlib.import_module("pkg")` or `__import__("pkg")` with a
     STRING-LITERAL argument is resolved the same way. (A non-literal
     argument -- a variable -- cannot be resolved statically and is
     reported as "unresolved", not silently passed.)

  3. Every `subprocess.*([...])` / `os.system(...)` invocation is
     collected and its executable classified (interpreter / OS tool /
     other / built-at-runtime). Separately -- and this is the part that
     matters -- every command's string-LITERAL words are scanned for a
     package installer (`pip`, `uv`, `easy_install`, ...), so a
     `[str(py), "-m", "pip", "install", "x"]` call is caught by its
     `-m pip install x` words even though the executable itself isn't a
     literal. Each installer invocation is printed with its file:line;
     one outside the benchmark tooling FAILS the check.

What this does NOT claim: to defeat a determined author who obfuscates
an import through `eval`, bytecode, or a computed module name. It
catches the ordinary ways a dependency sneaks in, and it says so.

Usage: python3 scripts/check_dependencies.py
`make verify-deps` runs this and tees the live output to deps-proof.txt,
so that committed file is a snapshot of a real run, not hand-maintained.
"""
import ast
import pathlib
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = pathlib.Path(__file__).resolve().parent.parent

if hasattr(sys, "stdlib_module_names"):  # Python 3.10+
    STDLIB_MODULES = set(sys.stdlib_module_names)
else:  # pragma: no cover - only exercised on Python < 3.10
    STDLIB_MODULES = {
        "os", "sys", "json", "hashlib", "zlib", "tempfile", "pathlib",
        "dataclasses", "typing", "argparse", "datetime", "time",
        "http", "socketserver", "urllib", "unittest", "shutil",
        "ast", "struct", "io", "collections", "functools", "__future__",
    }

# "Internal" = the project's own importable names, not PyPI packages.
# `vault/*` is always imported as `vault.x`, so the package name is
# enough for it. The loose modules under scripts/ and tests/ are
# imported by path in a few places (e.g. tests import
# check_dependencies itself to exercise it), so their bare stems count
# as internal too.
INTERNAL_MODULES = {"vault"} | {
    p.stem
    for p in [*ROOT.glob("scripts/*.py"), *ROOT.glob("tests/*.py")]
}

# Bare program names that are operating-system tools, not Python
# packages. Shelling out to these is not a runtime dependency on a
# PyPI package. `python`/`python3` mean "the interpreter".
SYSTEM_TOOLS = {
    "git", "bash", "sh", "cmd", "cmd.exe", "env", "make",
    "python", "python3", "py",
}


def _module_top(name: str) -> str:
    return name.split(".")[0]


def _loc(py_file: pathlib.Path, lineno: int) -> str:
    # forward slashes always, so the committed deps-proof.txt is
    # identical whether it was generated on Linux or Windows.
    try:
        return f"{py_file.relative_to(ROOT).as_posix()}:{lineno}"
    except ValueError:  # a file outside the repo (e.g. a unit-test fixture)
        return f"{py_file.name}:{lineno}"


def _string_arg(node: ast.AST):
    """Return the literal str value of a call's first positional arg,
    or None if it isn't a plain string literal."""
    if isinstance(node, ast.Call) and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _is_dynamic_import_call(node: ast.Call) -> bool:
    f = node.func
    if isinstance(f, ast.Name) and f.id == "__import__":
        return True
    if isinstance(f, ast.Attribute) and f.attr in ("import_module", "__import__"):
        base = f.value
        if isinstance(base, ast.Name) and base.id == "importlib":
            return True
    return False


# Words that mean "a package is being installed from an index". If one
# of these ever appears in a subprocess command, the dependency story
# needs a sentence explaining it -- so surface every occurrence with a
# file:line, regardless of how the executable itself is spelled.
INSTALLER_WORDS = {"pip", "pip3", "pipx", "easy_install", "uv", "poetry", "conda"}


def _is_subprocess_exec(node: ast.Call) -> bool:
    f = node.func
    if (isinstance(f, ast.Attribute)
            and f.attr in ("run", "Popen", "call", "check_call", "check_output")
            and isinstance(f.value, ast.Name) and f.value.id == "subprocess"):
        return True
    return (isinstance(f, ast.Attribute) and f.attr == "system"
            and isinstance(f.value, ast.Name) and f.value.id == "os")


def _command_literal_words(node: ast.Call) -> list[str]:
    """Every string-literal word in a subprocess command, in order --
    from a list `[...]`, a tuple, or a single `"prog arg arg"` string.
    Non-literal elements (`sys.executable`, `str(py)`, a variable) are
    simply skipped; this only reads what is statically visible."""
    if not node.args:
        return []
    arg = node.args[0]
    words: list[str] = []
    if isinstance(arg, (ast.List, ast.Tuple)):
        for el in arg.elts:
            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                words.append(el.value)
    elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        words.extend(arg.value.split())
    return words


def _subprocess_exec_token(node: ast.Call):
    """The executable token of a subprocess/os.system call (best
    effort), or None if `node` isn't one. Non-literal commands return
    the sentinel '<dynamic>'."""
    if not _is_subprocess_exec(node):
        return None
    if not node.args:
        return "<dynamic>"
    arg = node.args[0]
    if isinstance(arg, (ast.List, ast.Tuple)) and arg.elts:
        first = arg.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return pathlib.Path(first.value).name
        if isinstance(first, ast.Attribute) and first.attr == "executable":
            return "python"  # sys.executable
        return "<dynamic>"
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return pathlib.Path(arg.value.split()[0]).name if arg.value.split() else "<dynamic>"
    return "<dynamic>"


def _installer_hit(node: ast.Call, py_file: pathlib.Path):
    """If a subprocess command contains a package-installer word,
    return (loc, preview); else None. This is what makes the audit
    complete even when the executable is `str(py)` rather than a
    literal -- the `-m pip install ...` words are still literals."""
    if not _is_subprocess_exec(node):
        return None
    words = _command_literal_words(node)
    if any(w in INSTALLER_WORDS for w in words):
        return (_loc(py_file, node.lineno), " ".join(words) or "(no literal args)")
    return None


def scan(py_file: pathlib.Path):
    """Returns (static_imports, dynamic_imports, unresolved_dynamic,
    exec_tokens, installer_hits)."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    static, dynamic, unresolved, execs, installers = set(), set(), [], set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                static.add(_module_top(alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            static.add(_module_top(node.module))
        elif isinstance(node, ast.Call):
            if _is_dynamic_import_call(node):
                s = _string_arg(node)
                if s is None:
                    unresolved.append(_loc(py_file, node.lineno))
                else:
                    dynamic.add(_module_top(s))
            tok = _subprocess_exec_token(node)
            if tok:
                execs.add(tok)
            hit = _installer_hit(node, py_file)
            if hit:
                installers.append(hit)
    return static, dynamic, unresolved, execs, installers


def main() -> int:
    py_files = sorted(
        p for p in ROOT.glob("**/*.py") if "__pycache__" not in p.parts
    )

    all_static, all_dynamic, all_unresolved, all_execs = set(), set(), [], set()
    all_installers = []
    for f in py_files:
        s, d, u, e, inst = scan(f)
        all_static |= s
        all_dynamic |= d
        all_unresolved += u
        all_execs |= e
        all_installers += inst

    def externals(mods):
        return sorted(
            m for m in mods
            if m and m not in STDLIB_MODULES and m not in INTERNAL_MODULES
        )

    ext_static = externals(all_static)
    ext_dynamic = externals(all_dynamic)
    stdlib_used = sorted(m for m in all_static if m in STDLIB_MODULES)

    print(f"Scanning {len(py_files)} Python file(s)...\n")

    print("Standard library modules imported:")
    for m in stdlib_used:
        print(f"  {m:<20} ✓ stdlib")
    print()

    print("Dynamic imports (importlib / __import__ with a literal name):")
    if all_dynamic:
        for m in sorted(all_dynamic):
            tag = "✓ stdlib" if m in STDLIB_MODULES or m in INTERNAL_MODULES else "✗ EXTERNAL"
            print(f"  {m:<20} {tag}")
    else:
        print("  (none)")
    if all_unresolved:
        print("  unresolved (non-literal module name, cannot be checked statically):")
        for loc in all_unresolved:
            print(f"    {loc}")
    print()

    print("Subprocess / os.system executables observed:")
    for t in sorted(all_execs):
        if t == "python":
            note = "the interpreter itself"
        elif t in SYSTEM_TOOLS:
            note = "OS/system tool (not a Python package)"
        elif t == "<dynamic>":
            note = "executable built at runtime -- literal args still scanned below"
        else:
            note = "review: not a known system tool"
        print(f"  {t:<16} {note}")
    print()

    # The audit is only complete if it also shows every place a package
    # installer is invoked -- even when the executable is `str(py)` and
    # only the `-m pip install <pkg>` words are literals.
    print("Package-installer invocations (pip / uv / easy_install / ...):")
    if all_installers:
        for loc, preview in all_installers:
            in_benchmark = "benchmark" in loc
            tag = ("benchmark-only, into a throwaway venv"
                   if in_benchmark else "*** REVIEW: outside the benchmark tooling ***")
            print(f"  {loc}")
            print(f"      {preview}")
            print(f"      -> {tag}")
    else:
        print("  (none)")
    print()

    failures = []
    if ext_static:
        failures.append(f"external import(s): {', '.join(ext_static)}")
    if ext_dynamic:
        failures.append(f"external dynamic import(s): {', '.join(ext_dynamic)}")
    stray_installers = [loc for loc, _ in all_installers if "benchmark" not in loc]
    if stray_installers:
        failures.append(
            "package-installer call outside benchmark tooling: " + ", ".join(stray_installers)
        )

    if failures:
        print("External dependencies found:\n")
        for line in failures:
            print(f"  {line}")
        print("\nStatus: FAILED — external dependencies present")
        return 1

    print("External dependencies found:\n\n  NONE\n")
    print("Status: ZERO DEPENDENCY VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
