"""
Crash-injection proof that `SnapshotEngine.create_snapshot` is
atomic at the "does this snapshot exist?" boundary: a hard crash
(simulated with os._exit, which -- like SIGKILL -- runs no `finally`,
no atexit, no buffer flush) at any point during snapshot creation
leaves the repository either with the snapshot FULLY PRESENT and
valid, or FULLY ABSENT. Orphan (unreferenced) objects left behind by
an interrupted run are allowed and expected -- `vault gc` collects
them; they are still well-formed objects and never corruption.

Injection is done entirely from the child-process worker script
below -- it monkeypatches os.replace / ObjectStore.put / the id
counter at runtime, in that process only. `vault/snapshot.py` and
every other production module are untouched.

The crash points, matched to create_snapshot's real sequence:
  walk                  -- mid object-writing, before any id is allocated
  after_counter         -- the instant _next_snapshot_id() bumps the
                           on-disk counter, before the record is written
  before_record_replace -- immediately before the atomic os.replace
                           that promotes .vault/snapshots/.tmp-N to
                           .vault/snapshots/N   (the "fully absent" case)
  after_record_replace  -- immediately after that os.replace succeeds
                           (the "fully committed" case)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.experimental.pack_aware_store import PackAwareObjectStore
from vault.experimental.recover_check import check_snapshot_recoverable
from vault.restore import apply_restore
from vault.snapshot import SnapshotEngine

CRASH_CODE = 137  # conventional "process killed by SIGKILL" exit status

_WORKER = r'''
import os, sys

repo_root, vault_dir, source_dir, mode = sys.argv[1:5]
sys.path.insert(0, repo_root)

import vault.snapshot as snap
from vault.objects import ObjectStore

CRASH_CODE = 137
assert mode in {"walk", "after_counter", "before_record_replace",
                "after_record_replace", "control"}, "unknown mode: " + mode

# ---- test-only crash injection (this worker only; no production code) ----
if mode == "walk":
    _orig_put = ObjectStore.put
    _n = [0]
    def _put(self, data):
        r = _orig_put(self, data)
        _n[0] += 1
        if _n[0] >= 1:
            os._exit(CRASH_CODE)
        return r
    ObjectStore.put = _put

elif mode == "after_counter":
    _orig_nsi = snap.SnapshotEngine._next_snapshot_id
    def _nsi(self):
        _orig_nsi(self)          # really bump the on-disk counter...
        os._exit(CRASH_CODE)     # ...then die before the record is written
    snap.SnapshotEngine._next_snapshot_id = _nsi

elif mode in ("before_record_replace", "after_record_replace"):
    # Patch os.replace globally but only fire for the snapshot RECORD's
    # own rename (dest = .vault/snapshots/<digits>). The id-counter
    # rename and every object rename still work normally.
    _orig_replace = os.replace
    def _replace(src, dst):
        p = os.fspath(dst)
        is_record = (os.path.basename(p).isdigit()
                     and os.path.basename(os.path.dirname(p)) == "snapshots")
        if is_record:
            if mode == "after_record_replace":
                _orig_replace(src, dst)
            os._exit(CRASH_CODE)
        return _orig_replace(src, dst)
    os.replace = _replace

eng = snap.SnapshotEngine(vault_dir)
rec = eng.create_snapshot(source_dir, message="crash-test")
print("COMPLETED", rec.id)
sys.exit(0)
'''


class TestSnapshotCrashSafety(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.vault_dir = self.root / ".vault"
        self.source_dir = self.root / "project"
        self.source_dir.mkdir()
        (self.source_dir / "a.py").write_text("alpha\nbeta\n")
        (self.source_dir / "b.txt").write_text("bee\n")
        (self.source_dir / "sub").mkdir()
        (self.source_dir / "sub" / "c.py").write_text("cee\n")
        SnapshotEngine(self.vault_dir)  # create objects/ + snapshots/
        self.worker = self.root / "_crash_worker.py"
        self.worker.write_text(_WORKER)
        self.repo_root = str(Path(__file__).resolve().parent.parent)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, mode):
        return subprocess.run(
            [sys.executable, str(self.worker), self.repo_root,
             str(self.vault_dir), str(self.source_dir), mode],
            capture_output=True, text=True, timeout=90,
        )

    def _snapshot_ids_on_disk(self):
        d = self.vault_dir / "snapshots"
        return sorted(int(p.name) for p in d.iterdir() if p.name.isdigit())

    def _orphan_objects_all_valid(self):
        """True iff every object currently in the store verifies. Orphan
        (unreferenced) objects are fine -- only actual corruption fails."""
        eng = SnapshotEngine(self.vault_dir)
        eng.store = PackAwareObjectStore(self.vault_dir)
        eng.list_snapshots()  # must not raise
        return all(eng.store.verify_object(h) for h in eng.store.iter_all_hashes())

    # -- control ------------------------------------------------------------

    def test_control_no_crash_produces_one_valid_snapshot(self):
        r = self._run("control")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("COMPLETED 1", r.stdout)
        self.assertEqual(self._snapshot_ids_on_disk(), [1])
        self.assertTrue(self._orphan_objects_all_valid())

    # -- fully absent: crash before commit --------------------------------

    def test_crash_during_object_walk_leaves_no_snapshot(self):
        r = self._run("walk")
        self.assertEqual(r.returncode, CRASH_CODE)
        self.assertNotIn("COMPLETED", r.stdout)
        self.assertEqual(self._snapshot_ids_on_disk(), [])
        self.assertFalse((self.vault_dir / "next_id").exists(),
                         "no id should be allocated if the crash is before id allocation")
        self.assertTrue(self._orphan_objects_all_valid(),
                        "orphan objects from the interrupted walk must still be valid")
        # the repo still works: a fresh snapshot commits normally, as id 1
        r2 = self._run("control")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(self._snapshot_ids_on_disk(), [1])

    def test_crash_before_final_replace_snapshot_is_fully_absent(self):
        """Core 'fully absent' proof: killed the instant before the
        atomic os.replace that would promote .vault/snapshots/.tmp-1 to
        .vault/snapshots/1."""
        r = self._run("before_record_replace")
        self.assertEqual(r.returncode, CRASH_CODE)
        self.assertEqual(self._snapshot_ids_on_disk(), [],
                         "a crash before os.replace must leave the snapshot fully absent")
        # id 1 is burned: the counter was bumped before the record write
        self.assertEqual((self.vault_dir / "next_id").read_text().strip(), "2")
        # The interrupted run leaves a half-promoted record file
        # (.vault/snapshots/.tmp-1). It is harmless litter: not a
        # digit-named file, so every command ignores it. Nothing
        # currently sweeps it -- documented in the README.
        leftover = sorted(p.name for p in (self.vault_dir / "snapshots").iterdir())
        self.assertIn(".tmp-1", leftover)
        eng = SnapshotEngine(self.vault_dir)
        self.assertEqual(eng.list_snapshots(), [])
        self.assertTrue(self._orphan_objects_all_valid())
        # recovery: the next real snapshot commits as id 2 and is valid
        r2 = self._run("control")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("COMPLETED 2", r2.stdout)
        self.assertEqual(self._snapshot_ids_on_disk(), [2])

    # -- fully committed: crash after commit -----------------------------

    def test_crash_after_final_replace_snapshot_is_fully_committed(self):
        """Core 'fully committed' proof: killed the instant AFTER the
        atomic os.replace. The snapshot must exist, load, verify, and
        restore."""
        r = self._run("after_record_replace")
        self.assertEqual(r.returncode, CRASH_CODE)
        self.assertEqual(self._snapshot_ids_on_disk(), [1])

        eng = SnapshotEngine(self.vault_dir)
        eng.store = PackAwareObjectStore(self.vault_dir)
        rec = eng.load_snapshot(1)
        self.assertEqual(rec.id, 1)

        report = check_snapshot_recoverable(eng, 1)
        self.assertTrue(report.recoverable, report.issues)

        for p in self.source_dir.rglob("*"):
            if p.is_file():
                p.unlink()
        apply_restore(eng, self.source_dir, 1)
        self.assertEqual((self.source_dir / "a.py").read_text(), "alpha\nbeta\n")
        self.assertEqual((self.source_dir / "sub" / "c.py").read_text(), "cee\n")

    # -- the documented "burned id" gap ---------------------------------

    def test_burned_id_after_counter_bump_is_harmless_and_permanent(self):
        """A crash between the counter bump and the record write skips
        an id forever. This is a documented, deliberate trade-off (ids
        come from a monotonic counter, never from max(existing)+1) --
        it must never corrupt anything or block later snapshots."""
        r = self._run("after_counter")
        self.assertEqual(r.returncode, CRASH_CODE)
        self.assertEqual(self._snapshot_ids_on_disk(), [])
        self.assertEqual((self.vault_dir / "next_id").read_text().strip(), "2")

        eng = SnapshotEngine(self.vault_dir)
        self.assertEqual(eng.list_snapshots(), [])
        self.assertTrue(self._orphan_objects_all_valid())

        # id 1 is gone for good; subsequent snapshots are 2, 3, ...
        self.assertIn("COMPLETED 2", self._run("control").stdout)
        self.assertIn("COMPLETED 3", self._run("control").stdout)
        self.assertEqual(self._snapshot_ids_on_disk(), [2, 3])

    def test_burned_id_from_a_pre_commit_crash_also_permanent(self):
        """Same guarantee via the before_record_replace path: the id it
        allocated is never reused even though its snapshot never existed."""
        self.assertEqual(self._run("before_record_replace").returncode, CRASH_CODE)
        self.assertIn("COMPLETED 2", self._run("control").stdout)
        self.assertEqual(self._snapshot_ids_on_disk(), [2])


if __name__ == "__main__":
    unittest.main()
