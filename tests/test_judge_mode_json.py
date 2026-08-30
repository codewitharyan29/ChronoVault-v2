"""
tests/test_judge_mode_json.py

`python scripts/judge_mode.py --json` is a machine-consumable
scorecard, so its shape must be stable. The full run takes minutes
(it executes every proof as a subprocess); this test exercises the
rendering contract directly with a synthetic Scorecard instead.
"""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import judge_mode  # noqa: E402


def _render(sc) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        judge_mode.render_json(sc)
    return buf.getvalue()


class JudgeModeJsonContract(unittest.TestCase):
    def _card(self, rows, test_count=271):
        sc = judge_mode.Scorecard()
        sc.test_count = test_count
        for name, status in rows:
            sc.add(name, status)
        return sc

    def test_all_pass_scorecard_is_valid_json(self):
        sc = self._card([
            ("zero dependencies", "PASS"),
            ("single-file build", "PASS"),
            ("reproducible storage", "PASS"),
            ("271 tests", "PASS"),
        ])
        out = _render(sc)
        self.assertFalse(out.startswith("﻿"))          # no BOM
        obj = json.loads(out)
        self.assertEqual(obj["result"], "PASS")
        self.assertEqual(obj["test_count"], 271)
        self.assertEqual(set(obj), {"result", "test_count", "checks", "bonuses"})
        names = [c["name"] for c in obj["checks"]]
        self.assertEqual(names, sorted(names))              # deterministic
        self.assertTrue(all(set(c) == {"name", "status", "note"} for c in obj["checks"]))

    def test_any_fail_flips_the_overall_result(self):
        sc = self._card([("zero dependencies", "PASS"),
                         ("single-file build", "FAIL"),
                         ("reproducible storage", "PASS")])
        obj = json.loads(_render(sc))
        self.assertEqual(obj["result"], "FAIL")
        self.assertEqual(sc.exit_code(), 1)

    def test_skip_does_not_fail_overall(self):
        sc = self._card([("zero dependencies", "PASS"),
                         ("something optional", "SKIP")])
        obj = json.loads(_render(sc))
        self.assertEqual(obj["result"], "PASS")
        self.assertEqual(sc.exit_code(), 0)

    def test_bonuses_block_names_the_four(self):
        sc = self._card([("single-file build", "PASS"),
                         ("reproducible storage", "PASS")])
        obj = json.loads(_render(sc))
        self.assertEqual(
            set(obj["bonuses"]),
            {"single_file", "reproducible_build", "package_killer", "stdlib_log"},
        )
        self.assertEqual(obj["bonuses"]["single_file"], "PASS")


if __name__ == "__main__":
    unittest.main()
