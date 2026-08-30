"""
tests/test_dependency_checker.py

The zero-dependency proof is a headline claim, so the checker behind it
must actually catch the ordinary ways a dependency sneaks in -- not
just plain `import`. These tests plant synthetic source and confirm
scripts/check_dependencies.py sees it.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_dependencies as chk  # noqa: E402


def _scan_src(src: str):
    """Run the checker's scanner over a synthetic file. Returns a dict
    so tests don't depend on scan()'s tuple arity."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "synthetic.py"
        f.write_text(src, encoding="utf-8")
        static, dynamic, unresolved, execs, installers = chk.scan(f)
        return {
            "static": static, "dynamic": dynamic, "unresolved": unresolved,
            "execs": execs, "installers": installers,
        }


class DependencyCheckerScope(unittest.TestCase):
    def test_plain_static_import_is_collected(self):
        r = _scan_src("import os\nfrom concurrent import futures\n")
        self.assertIn("os", r["static"])
        self.assertIn("concurrent", r["static"])

    def test_importlib_string_literal_dynamic_import_is_caught(self):
        r = _scan_src("import importlib\nm = importlib.import_module('requests')\n")
        self.assertIn("requests", r["dynamic"])
        self.assertEqual(r["unresolved"], [])

    def test_builtin_dunder_import_is_caught(self):
        r = _scan_src("mod = __import__('numpy')\n")
        self.assertIn("numpy", r["dynamic"])

    def test_non_literal_dynamic_import_is_reported_unresolved_not_ignored(self):
        r = _scan_src("import importlib\nname = 'x'\nimportlib.import_module(name)\n")
        self.assertEqual(r["dynamic"], set())
        self.assertEqual(len(r["unresolved"]), 1)

    def test_subprocess_executable_token_is_recorded(self):
        r = _scan_src("import subprocess\nsubprocess.run(['curl', '-s', 'http://x'])\n")
        self.assertIn("curl", r["execs"])

    def test_subprocess_with_interpreter_is_classified_as_python(self):
        r = _scan_src("import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'unittest'])\n")
        self.assertIn("python", r["execs"])

    def test_stdlib_classification_matches_interpreter(self):
        r = _scan_src("import importlib\nimportlib.import_module('json')\n")
        externals = [m for m in r["dynamic"]
                     if m not in chk.STDLIB_MODULES and m not in chk.INTERNAL_MODULES]
        self.assertEqual(externals, [])

    def test_pip_install_is_caught_even_when_executable_is_not_a_literal(self):
        # the real repo does exactly this: subprocess.run([str(py), "-m",
        # "pip", "install", "pkg"]) -- the executable is str(py), NOT a
        # literal, so it must be caught by the "-m pip install" words.
        r = _scan_src(
            "import subprocess\n"
            "py = 'x'\n"
            "subprocess.run([str(py), '-m', 'pip', 'install', 'diskcache'])\n"
        )
        self.assertEqual(len(r["installers"]), 1)
        loc, preview = r["installers"][0]
        self.assertIn("pip install diskcache", preview)

    def test_uv_and_easy_install_are_also_installer_words(self):
        r1 = _scan_src("import subprocess\nsubprocess.run(['uv', 'pip', 'install', 'x'])\n")
        r2 = _scan_src("import subprocess\nsubprocess.run(['easy_install', 'x'])\n")
        self.assertEqual(len(r1["installers"]), 1)
        self.assertEqual(len(r2["installers"]), 1)

    def test_plain_subprocess_without_installer_is_not_flagged(self):
        r = _scan_src("import subprocess\nsubprocess.run(['git', 'status'])\n")
        self.assertEqual(r["installers"], [])

    def test_the_only_installer_call_in_the_repo_is_in_the_benchmark_tooling(self):
        # a stray `pip install` anywhere else must FAIL check_dependencies.
        installers = []
        for p in chk.ROOT.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            installers += chk.scan(p)[4]
        self.assertTrue(installers, "expected the benchmark's pip install to be found")
        for loc, _ in installers:
            self.assertIn("benchmark", loc,
                          f"package installer invoked outside benchmark tooling: {loc}")

    def test_the_real_repo_still_verifies_zero_dependency(self):
        self.assertEqual(chk.main(), 0)


if __name__ == "__main__":
    unittest.main()
