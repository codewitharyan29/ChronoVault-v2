"""
tests/test_demo_regression.py

END-TO-END REGRESSION LOCK FOR THE RECORDED DEMO VIDEO.

The demo video is already recorded. Every state transition it shows
must keep working, with the same snapshot numbering, the same
rename-aware history, and byte-correct restore. This test drives the
REAL CLI (`vault.cli.main`) through the exact sequence in
DEMO_VIDEO_SCRIPT.md against a fresh throwaway repo and pins the
invariants a judge would check.

`serve`, isolated-mode, the external Package-Killer benchmark and the
visual-only commands are covered by their own checks (CI's
"Isolated-mode proof" / "Single-file verification" steps,
scripts/judge_mode.py, tests/test_inspector.py) -- they cannot live
inside one unittest, and duplicating them here would be theatre.
"""

import builtins
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.cli import main


class DemoWorkflowRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cv-demo-regression-"))
        self.proj = self.tmp / "demo-project"
        self.P = str(self.proj)  # every command below points here

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def _cli(self, *argv, stdin=None):
        """Invoke the real CLI in-process. Returns (exit_code, stdout)."""
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

    def json_run(self, *argv):
        code, out = self._cli(*(argv + ("--json",)))
        return code, json.loads(out)

    # -- the recorded flow --------------------------------------------------

    def test_recorded_demo_workflow_is_stable(self):
        P = self.P

        # --- demo -> a deterministic sample project -------------------
        code, _ = self._cli("demo", P)
        self.assertEqual(code, 0)
        self.assertTrue((self.proj / "src" / "database.py").is_file())

        # --- init -------------------------------------------------------
        self.assertEqual(self._cli("init", P)[0], 0)

        # --- snapshot 1: "initial" -----------------------------------
        code, out = self._cli("snapshot", "-m", "initial", P)
        self.assertEqual(code, 0)
        self.assertIn("ID:              1", out)

        # --- status / list / info agree: 1 snapshot -----------------
        _, st = self.json_run("status", P)
        self.assertEqual(st["snapshots"], 1)
        self.assertTrue(st["integrity_ok"])
        _, ls = self.json_run("list", P)
        self.assertEqual([s["id"] for s in ls["snapshots"]], [1])
        _, nf = self.json_run("info", P)
        self.assertEqual(nf["snapshots"], 1)
        self.assertEqual(nf["hash_algorithm"], "SHA256")

        # --- show 1: contents + content-addressing directly visible -
        code, sh = self.json_run("show", "1", "--path", P)
        self.assertEqual(code, 0)
        self.assertEqual(sh["id"], 1)
        self.assertIsNone(sh["parent"])
        paths = [e["path"] for e in sh["entries"]]
        self.assertEqual(paths, sorted(paths))          # deterministic
        self.assertIn("src/database.py", paths)
        by_path = {e["path"]: e for e in sh["entries"]}
        # the demo generator writes byte-identical utils.py / legacy_utils.py:
        # `show` must report ONE shared object for both (dedup, visible)
        self.assertEqual(by_path["src/utils.py"]["object"],
                         by_path["src/legacy_utils.py"]["object"])
        self.assertEqual(by_path["src/database.py"]["kind"], "file")
        self.assertEqual(len(by_path["src/database.py"]["object"]), 64)

        # --- modify one file, snapshot 2 --------------------------
        dbfile = self.proj / "src" / "database.py"
        dbfile.write_text(dbfile.read_text(encoding="utf-8") + "\n# edited for demo\n",
                          encoding="utf-8")
        code, out = self._cli("snapshot", "-m", "after changes", P)
        self.assertEqual(code, 0)
        self.assertIn("ID:              2", out)

        # --- diff 1 2: exactly the one edited file, as "modified" --
        code, df = self.json_run("diff", "1", "2", "--path", P)
        self.assertEqual(code, 0)
        self.assertEqual(df["modified"], ["src/database.py"])
        self.assertEqual(df["added"], [])
        self.assertEqual(df["removed"], [])

        # --- explain 2 -----------------------------------------------
        code, ex = self.json_run("explain", "2", "--path", P)
        self.assertEqual(code, 0)
        self.assertEqual(ex["id"], 2)
        self.assertEqual(ex["parent"], 1)
        self.assertEqual(ex["new_objects"], 1)  # only database.py's blob is new

        # --- log src/database.py (pre-rename) ---------------------
        code, lg = self.json_run("log", "src/database.py", "--path", P)
        self.assertEqual(code, 0)
        self.assertEqual([e["snapshot"] for e in lg["entries"]], [1, 2])
        self.assertEqual(lg["also_known_as"], [])

        # --- rename database.py -> db.py, snapshot 3 -------------
        os.rename(self.proj / "src" / "database.py", self.proj / "src" / "db.py")
        code, out = self._cli("snapshot", "-m", "renamed database file", P)
        self.assertEqual(code, 0)
        self.assertIn("ID:              3", out)

        # --- rename-aware log src/db.py ---------------------------
        code, lg = self.json_run("log", "src/db.py", "--path", P)
        self.assertEqual(code, 0)
        self.assertEqual(lg["also_known_as"], ["src/database.py"])
        paths_at = [e["path_at"] for e in lg["entries"]]
        self.assertIn("src/database.py", paths_at)
        self.assertIn("src/db.py", paths_at)
        self.assertEqual([e["snapshot"] for e in lg["entries"]], [1, 2, 3])

        # --- show 3: rename moves the PATH, not the object ---------
        # snapshot 2 had src/database.py with content C; snapshot 3 has
        # src/db.py with the SAME content C -> same content hash. A pure
        # rename creates no new blob object.
        _, sh2 = self.json_run("show", "2", "--path", P)
        _, sh3 = self.json_run("show", "3", "--path", P)
        obj_at = lambda sh, pth: next(e["object"] for e in sh["entries"] if e["path"] == pth)
        self.assertEqual(obj_at(sh2, "src/database.py"), obj_at(sh3, "src/db.py"))
        self.assertEqual(sh3["parent"], 2)

        # --- tag 3 "stable" and prove the tag resolves ----------
        self.assertEqual(self._cli("tag", "3", "stable", "--path", P)[0], 0)
        _, ex_tag = self.json_run("explain", "stable", "--path", P)
        self.assertEqual(ex_tag["id"], 3)

        # --- trace an object from snapshot 3 -------------------
        db_blob = [e["blob_hash"] for e in lg["entries"] if e["path_at"] == "src/db.py"][0]
        code, tr = self._cli("trace", db_blob, "--path", P)
        self.assertEqual(code, 0)
        self.assertIn("Snapshot 3", tr)

        # --- verify (healthy) --------------------------------------
        code, vf = self.json_run("verify", P)
        self.assertEqual(code, 0)
        self.assertEqual(vf["result"], "healthy")
        self.assertEqual(vf["corrupted"], [])

        # --- recover-check 3 (recoverable) ---------------------
        code, rc = self.json_run("recover-check", "3", "--path", P)
        self.assertEqual(code, 0)
        self.assertTrue(rc["recoverable"])
        self.assertEqual(rc["issues"], [])

        # --- restore 3 --preview: NO filesystem mutation --------
        before = self._tree_snapshot()
        code, out = self._cli("restore", "3", "--preview", "--path", P)
        self.assertEqual(code, 0)
        self.assertIn("No changes applied", out)
        self.assertEqual(self._tree_snapshot(), before,
                         "restore --preview mutated the working tree")

        # --- restore 3 (real, confirmed) -----------------------
        (self.proj / "src" / "db.py").write_text("tampered\n", encoding="utf-8")
        (self.proj / "src" / "auth.py").unlink()
        code, out = self._cli("restore", "3", "--path", P, stdin="RESTORE")
        self.assertEqual(code, 0)
        self.assertIn("Restoration completed", out)
        self.assertTrue((self.proj / "src" / "auth.py").is_file())
        self.assertNotIn("tampered",
                         (self.proj / "src" / "db.py").read_text(encoding="utf-8"))
        self.assertFalse((self.proj / "src" / "database.py").exists(),
                         "snapshot 3 has db.py, not database.py")

        # --- pack, then verify + recover-check still hold -------
        code, out = self._cli("pack", "--path", P)
        self.assertEqual(code, 0)
        code, vf = self.json_run("verify", P)
        self.assertEqual(code, 0)
        self.assertEqual(vf["result"], "healthy")
        code, rc = self.json_run("recover-check", "3", "--path", P)
        self.assertEqual(code, 0)
        self.assertTrue(rc["recoverable"])

        # --- snapshot-rm 1, then gc -> verify -> restore invariant
        self.assertEqual(self._cli("snapshot-rm", "1", "--path", P)[0], 0)
        _, ls = self.json_run("list", P)
        self.assertEqual([s["id"] for s in ls["snapshots"]], [2, 3])

        code, out = self._cli("gc", P)
        self.assertEqual(code, 0)
        code, vf = self.json_run("verify", P)
        self.assertEqual(code, 0)
        self.assertEqual(vf["result"], "healthy")

        (self.proj / "src" / "db.py").write_text("break it again\n", encoding="utf-8")
        code, out = self._cli("restore", "2", "--path", P, stdin="RESTORE")
        self.assertEqual(code, 0)
        # snapshot 2 predates the rename: database.py is back, db.py is gone
        self.assertTrue((self.proj / "src" / "database.py").is_file())
        self.assertIn("# edited for demo",
                      (self.proj / "src" / "database.py").read_text(encoding="utf-8"))

        # every snapshot that survived gc is still fully recoverable
        for sid in (2, 3):
            code, rc = self.json_run("recover-check", str(sid), "--path", P)
            self.assertEqual(code, 0, f"snapshot {sid} not recoverable after gc")
            self.assertTrue(rc["recoverable"])

    # -- utilities -----------------------------------------------------

    def _tree_snapshot(self):
        """(size, mtime_ns) per file under the project, EXCLUDING .vault
        -- so read-only commands can be asserted not to touch the tree."""
        out = {}
        for p in sorted(self.proj.rglob("*")):
            if ".vault" in p.parts or not p.is_file():
                continue
            stt = p.stat()
            out[str(p.relative_to(self.proj))] = (stt.st_size, stt.st_mtime_ns)
        return out


if __name__ == "__main__":
    unittest.main()
