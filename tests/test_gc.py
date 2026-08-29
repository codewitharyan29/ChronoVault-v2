"""
Tests for vault/gc.py: reachability computation, garbage collection,
and object tracing — including the case that matters most: gc must
NEVER delete something a live snapshot still needs.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.gc import compute_reachable_objects, run_gc, trace_object
from vault.snapshot import SnapshotEngine


class TestGC(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.vault_dir = self.root / ".vault"
        self.source_dir = self.root / "project"
        self.source_dir.mkdir()
        self.engine = SnapshotEngine(self.vault_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, rel_path: str, content: str):
        p = self.source_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def test_gc_deletes_nothing_when_everything_is_reachable(self):
        self._write("a.txt", "content")
        self.engine.create_snapshot(self.source_dir)
        result = run_gc(self.engine)
        self.assertEqual(result.objects_deleted, 0)

    def test_gc_removes_objects_only_from_a_snapshot_that_no_longer_exists(self):
        """The core gc scenario: an old snapshot's unique file becomes
        garbage only once that snapshot itself is gone."""
        self._write("old_only.txt", "only in snapshot 1")
        s1 = self.engine.create_snapshot(self.source_dir)
        objects_after_s1 = set(self.engine.store.iter_all_hashes())

        self.engine.delete_snapshot(s1.id)

        result = run_gc(self.engine)
        self.assertGreater(result.objects_deleted, 0)
        remaining = set(self.engine.store.iter_all_hashes())
        self.assertTrue(remaining.isdisjoint(objects_after_s1) or len(remaining) == 0)

    def test_gc_preserves_objects_shared_across_snapshots(self):
        """A file unchanged across two snapshots must survive gc even
        after the object becomes 'old' — dedup + gc must cooperate."""
        self._write("shared.txt", "never changes")
        self.engine.create_snapshot(self.source_dir)
        self._write("shared.txt", "never changes")  # identical content again
        self._write("new.txt", "added in snapshot 2")
        self.engine.create_snapshot(self.source_dir)

        result = run_gc(self.engine)
        self.assertEqual(result.objects_deleted, 0)  # both snapshots still exist

        # The shared blob must still be readable after gc.
        s2 = self.engine.load_snapshot(2)
        entries = {e.name: e.obj_hash for e in self.engine.load_tree(s2.root_tree_hash)}
        self.assertEqual(self.engine.store.get(entries["shared.txt"]).decode(), "never changes")

    def test_reachable_map_includes_tree_objects_not_just_blobs(self):
        self._write("src/a.py", "1")
        record = self.engine.create_snapshot(self.source_dir)
        reachable = compute_reachable_objects(self.engine)
        # The root tree hash itself must be marked reachable.
        self.assertIn(record.root_tree_hash, reachable)

    def test_trace_reports_referencing_snapshots(self):
        self._write("a.txt", "v1")
        s1 = self.engine.create_snapshot(self.source_dir)
        self._write("a.txt", "v1")  # unchanged -> same blob reused in s2
        self._write("b.txt", "new")
        s2 = self.engine.create_snapshot(self.source_dir)

        entries = {e.name: e.obj_hash for e in self.engine.load_tree(s1.root_tree_hash)}
        a_hash = entries["a.txt"]

        result = trace_object(self.engine, a_hash)
        self.assertTrue(result.exists)
        self.assertEqual(result.referenced_by, [s1.id, s2.id])
        self.assertFalse(result.would_gc_delete)
        # The path-aware part: trace should show WHICH file, in each
        # snapshot, resolves to this object — this is what makes dedup
        # visible rather than just asserted.
        self.assertEqual(result.locations[s1.id], ["a.txt"])
        self.assertEqual(result.locations[s2.id], ["a.txt"])

    def test_trace_reports_different_paths_across_snapshots(self):
        """The same content, moved to a different path in a later
        snapshot, is still one object — trace should show both paths."""
        self._write("old_name.txt", "identical content")
        s1 = self.engine.create_snapshot(self.source_dir)
        (self.source_dir / "old_name.txt").unlink()
        self._write("new_name.txt", "identical content")
        s2 = self.engine.create_snapshot(self.source_dir)

        entries = {e.name: e.obj_hash for e in self.engine.load_tree(s1.root_tree_hash)}
        obj_hash = entries["old_name.txt"]

        result = trace_object(self.engine, obj_hash)
        self.assertEqual(result.locations[s1.id], ["old_name.txt"])
        self.assertEqual(result.locations[s2.id], ["new_name.txt"])

    def test_trace_on_nonexistent_object(self):
        result = trace_object(self.engine, "0" * 64)
        self.assertFalse(result.exists)
        self.assertEqual(result.referenced_by, [])
        self.assertEqual(result.locations, {})


if __name__ == "__main__":
    unittest.main()
