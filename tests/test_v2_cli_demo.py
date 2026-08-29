"""
Regression test for a real, live bug found by adversarial testing:
`vault demo <path-to-an-existing-file>` crashed with an uncaught
NotADirectoryError traceback instead of a clean error message.
Confirmed reachable through the real CLI (a completely plausible
mistake: a typo'd path, or pointing demo at the wrong thing), not
just a theoretical edge case -- the existing "target not empty" guard
didn't cover this specific shape of bad input.
"""
import argparse
import shutil
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.cli import cmd_demo


class TestCmdDemoAdversarial(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, path):
        args = argparse.Namespace(path=path, init=False, snapshot=False)
        return cmd_demo(args)

    def test_target_is_an_existing_file_returns_error_not_crash(self):
        target = self.root / "notadir"
        target.write_text("just a file")
        result = self._run(str(target))
        self.assertEqual(result, 1)  # clean failure code, not an uncaught exception

    def test_target_not_empty_still_returns_error_as_before(self):
        (self.root / "existing_file.txt").write_text("content")
        result = self._run(str(self.root))
        self.assertEqual(result, 1)

    def test_target_does_not_exist_yet_succeeds(self):
        target = self.root / "brand_new_demo_dir"
        result = self._run(str(target))
        self.assertEqual(result, 0)
        self.assertTrue((target / "src").exists())


if __name__ == "__main__":
    unittest.main()
