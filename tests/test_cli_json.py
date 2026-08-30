"""
tests/test_cli_json.py

Contract tests for the opt-in `--json` output added to the read-only
commands (status, list, info, diff, explain, log, verify,
recover-check).

Two guarantees are pinned here:

  1. `--json` emits ONE valid JSON document, with the documented key
     set, deterministic ordering, and the SAME exit code the human
     path would return.
  2. The mere existence of the flag does not change default output:
     every command without `--json` still prints its human report
     (the recorded demo depends on this).
"""

import builtins
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.cli import main


def _run(*argv, stdin=None):
    buf = io.StringIO()
    real_input = builtins.input
    if stdin is not None:
        builtins.input = lambda *_a, **_k: stdin
    try:
        with redirect_stdout(buf):
            code = main(list(argv))
    finally:
        builtins.input = real_input
    return code, buf.getvalue()


class CliJsonContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cv-json-contract-"))
        cls.P = str(cls.tmp / "proj")
        _run("demo", cls.P)
        _run("init", cls.P)
        _run("snapshot", "-m", "one", cls.P)
        f = cls.tmp / "proj" / "src" / "database.py"
        f.write_text(f.read_text(encoding="utf-8") + "\n# change\n", encoding="utf-8")
        _run("snapshot", "-m", "two", cls.P)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helper: run a command with and without --json ---------------

    def _both(self, *argv, json_pos="append"):
        """Returns (human_code, human_out, json_code, json_obj)."""
        hc, ho = _run(*argv)
        if json_pos == "append":
            jc, jo = _run(*(argv + ("--json",)))
        else:
            jc, jo = _run("--json", *argv)
        return hc, ho, jc, json.loads(jo)

    # -- per-command contracts ---------------------------------------

    def test_status_json(self):
        hc, ho, jc, obj = self._both("status", self.P)
        self.assertEqual(hc, jc, 0)
        self.assertIn("ChronoVault Repository", ho)  # human output intact
        self.assertEqual(
            set(obj),
            {"snapshots", "objects", "total_snapshot_data_bytes",
             "stored_on_disk_bytes", "last_snapshot", "integrity_ok",
             "corrupted_count"},
        )
        self.assertEqual(obj["snapshots"], 2)
        self.assertIs(obj["integrity_ok"], True)

    def test_list_json(self):
        hc, ho, jc, obj = self._both("list", self.P)
        self.assertEqual((hc, jc), (0, 0))
        self.assertRegex(ho, r"^\s*1\s+\"one\"")
        ids = [s["id"] for s in obj["snapshots"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids, [1, 2])
        self.assertEqual(set(obj["snapshots"][0]),
                         {"id", "message", "timestamp", "files"})

    def test_info_json(self):
        hc, ho, jc, obj = self._both("info", self.P)
        self.assertEqual((hc, jc), (0, 0))
        self.assertIn("Format version:", ho)
        self.assertEqual(obj["hash_algorithm"], "SHA256")
        self.assertEqual(obj["snapshots"], 2)

    def test_diff_json(self):
        hc, ho, jc, obj = self._both("diff", "1", "2", "--path", self.P)
        self.assertEqual((hc, jc), (0, 0))
        self.assertIn("Snapshot 1", ho)
        self.assertEqual(obj["from"], 1)
        self.assertEqual(obj["to"], 2)
        self.assertEqual(obj["modified"], ["src/database.py"])
        for k in ("added", "modified", "removed"):
            self.assertEqual(obj[k], sorted(obj[k]))

    def test_explain_json(self):
        hc, ho, jc, obj = self._both("explain", "2", "--path", self.P)
        self.assertEqual((hc, jc), (0, 0))
        self.assertIn("Dedup ratio:", ho)
        self.assertEqual(obj["id"], 2)
        self.assertEqual(obj["parent"], 1)
        self.assertIn("dedup_ratio_pct", obj)

    def test_log_json(self):
        hc, ho, jc, obj = self._both("log", "src/database.py", "--path", self.P)
        self.assertEqual((hc, jc), (0, 0))
        self.assertIn("History for src/database.py", ho)
        self.assertEqual([e["snapshot"] for e in obj["entries"]], [1, 2])
        self.assertEqual(obj["also_known_as"], [])

    def test_log_json_missing_path_exit_1_but_valid_json(self):
        jc, jo = _run("log", "does/not/exist.py", "--path", self.P, "--json")
        obj = json.loads(jo)
        self.assertEqual(jc, 1)               # same exit code as human path
        self.assertEqual(obj["entries"], [])

    def test_verify_json_healthy(self):
        hc, ho, jc, obj = self._both("verify", self.P)
        self.assertEqual((hc, jc), (0, 0))
        self.assertIn("Repository healthy.", ho)
        self.assertEqual(obj["result"], "healthy")
        self.assertEqual(obj["corrupted"], [])
        self.assertEqual(obj["quarantined_packs"], [])

    def test_recover_check_json(self):
        hc, ho, jc, obj = self._both("recover-check", "2", "--path", self.P)
        self.assertEqual((hc, jc), (0, 0))
        self.assertIn("fully recoverable", ho)
        self.assertIs(obj["recoverable"], True)
        self.assertEqual(obj["issues"], [])
        self.assertEqual(obj["snapshot"], 2)

    def test_show_json(self):
        hc, ho, jc, obj = self._both("show", "2", "--path", self.P)
        self.assertEqual((hc, jc), (0, 0))
        self.assertIn("Root tree:", ho)                      # human report intact
        self.assertIn("OBJECT", ho)
        self.assertEqual(obj["id"], 2)
        self.assertEqual(obj["parent"], 1)
        self.assertEqual(len(obj["root_tree_hash"]), 64)
        self.assertEqual(set(obj["stats"]),
                         {"files", "new_objects", "reused_objects",
                          "original_bytes", "compressed_bytes"})
        paths = [e["path"] for e in obj["entries"]]
        self.assertEqual(paths, sorted(paths))              # deterministic ordering
        self.assertIn("src/database.py", paths)
        db = [e for e in obj["entries"] if e["path"] == "src/database.py"][0]
        self.assertEqual(db["kind"], "file")
        self.assertEqual(len(db["object"]), 64)             # full sha-256 hex
        self.assertGreater(db["size"], 0)
        # directories are content-addressed objects too, with size null
        for e in obj["entries"]:
            if e["kind"] == "dir":
                self.assertIsNone(e["size"])

    def test_show_makes_deduplication_visible(self):
        # the demo generator writes byte-identical src/utils.py and
        # src/legacy_utils.py -> `show` must report the SAME object hash
        _, obj = _run("show", "2", "--path", self.P, "--json")
        obj = json.loads(obj)
        by_path = {e["path"]: e["object"] for e in obj["entries"]}
        self.assertEqual(by_path["src/utils.py"], by_path["src/legacy_utils.py"])

    def test_show_unknown_snapshot_is_clean_error(self):
        jc, out = _run("show", "12345", "--path", self.P)          # human
        self.assertEqual(jc, 1)
        self.assertEqual(out, "")                                  # message on stderr
        jc, jo = _run("show", "12345", "--path", self.P, "--json")  # json
        self.assertEqual(jc, 1)
        self.assertIn("error", json.loads(jo))

    # -- error path is machine-readable in --json mode -----------

    def test_error_in_json_mode_is_json(self):
        jc, jo = _run("explain", "999", "--path", self.P, "--json")
        obj = json.loads(jo)
        self.assertEqual(jc, 1)
        self.assertIn("error", obj)

    def test_error_without_json_is_unchanged(self):
        # human error path still goes to stderr / exit 1, prints no JSON
        jc, out = _run("explain", "999", "--path", self.P)
        self.assertEqual(jc, 1)
        self.assertEqual(out, "")  # error went to stderr, stdout clean

    # -- determinism -----------------------------------------------

    def test_json_output_is_byte_stable_across_runs(self):
        _, a = _run("status", self.P, "--json")
        _, b = _run("status", self.P, "--json")
        self.assertEqual(a, b)
        # sorted keys => first content key is 'corrupted_count'
        self.assertLess(a.index('"corrupted_count"'), a.index('"snapshots"'))


if __name__ == "__main__":
    unittest.main()
