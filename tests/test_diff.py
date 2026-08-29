"""
Tests for vault/diff.py: snapshot-to-snapshot diff and
working-directory-vs-snapshot diff (the restore --preview path).
Both must agree on the same comparison logic.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.diff import diff_trees, diff_working_directory_against_snapshot
from vault.snapshot import SnapshotEngine


class TestDiff(unittest.TestCase):
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

    def test_diff_trees_detects_added_removed_modified(self):
        self._write("keep.txt", "same")
        self._write("change.txt", "before")
        self._write("gone.txt", "will be deleted")
        s1 = self.engine.create_snapshot(self.source_dir)

        (self.source_dir / "gone.txt").unlink()
        self._write("change.txt", "after")
        self._write("new.txt", "brand new")
        s2 = self.engine.create_snapshot(self.source_dir)

        result = diff_trees(self.engine, s1.root_tree_hash, s2.root_tree_hash)
        self.assertEqual(result.added, ["new.txt"])
        self.assertEqual(result.removed, ["gone.txt"])
        self.assertEqual(result.modified, ["change.txt"])
        self.assertEqual(result.unchanged_count, 1)  # keep.txt

    def test_diff_trees_no_changes_is_empty(self):
        self._write("a.txt", "x")
        s1 = self.engine.create_snapshot(self.source_dir)
        s2 = self.engine.create_snapshot(self.source_dir)
        result = diff_trees(self.engine, s1.root_tree_hash, s2.root_tree_hash)
        self.assertTrue(result.is_empty())
        self.assertEqual(result.unchanged_count, 1)

    def test_diff_trees_handles_nested_paths(self):
        self._write("src/a.py", "1")
        s1 = self.engine.create_snapshot(self.source_dir)
        self._write("src/a.py", "2")
        self._write("src/lib/b.py", "3")
        s2 = self.engine.create_snapshot(self.source_dir)
        result = diff_trees(self.engine, s1.root_tree_hash, s2.root_tree_hash)
        self.assertEqual(result.modified, ["src/a.py"])
        self.assertEqual(result.added, ["src/lib/b.py"])

    def test_restore_preview_matches_working_dir_reality(self):
        self._write("a.txt", "snapshot version")
        s1 = self.engine.create_snapshot(self.source_dir)

        # Simulate "accidentally deleted a file" + "made an unrelated edit"
        (self.source_dir / "a.txt").unlink()
        self._write("b.txt", "added after snapshot")

        preview = diff_working_directory_against_snapshot(
            self.engine, self.source_dir, s1.root_tree_hash
        )
        # a.txt is gone from disk but present in the snapshot -> restore would add it back
        self.assertEqual(preview.added, ["a.txt"])
        # b.txt exists now but wasn't in the snapshot -> reported, but (per restore's
        # non-destructive design) restore will NOT delete it
        self.assertEqual(preview.removed, ["b.txt"])

    def test_restore_preview_is_read_only(self):
        """Preview must never write to the object store."""
        self._write("a.txt", "v1")
        s1 = self.engine.create_snapshot(self.source_dir)
        objects_before = set(self.engine.store.iter_all_hashes())

        self._write("a.txt", "v2 — not snapshotted")
        diff_working_directory_against_snapshot(self.engine, self.source_dir, s1.root_tree_hash)

        objects_after = set(self.engine.store.iter_all_hashes())
        self.assertEqual(objects_before, objects_after)


if __name__ == "__main__":
    unittest.main()
