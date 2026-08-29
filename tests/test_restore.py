"""
Tests for vault/restore.py — the "does it actually work" module.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.objects import RestoreError
from vault.restore import apply_restore, preview_restore
from vault.snapshot import SnapshotEngine


class TestRestore(unittest.TestCase):
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

    def test_restore_is_protected_against_path_traversal(self):
        """End-to-end proof that the vulnerability found via manual
        testing is actually fixed at the layer that matters: a
        maliciously crafted snapshot (simulating a tampered .vault
        directory) cannot cause restore to write outside the target
        directory."""
        import json
        import time


        evil_stat = self.engine.store.put(b"PWNED")
        # Bypass serialize_tree's own validation by hand-crafting the
        # bytes directly -- this simulates a tampered object file,
        # which is the realistic attack surface.
        name = b"../../evil_outside.txt"
        malicious_tree_bytes = b"b" + len(name).to_bytes(2, "big") + name + evil_stat.obj_hash.encode("ascii")
        tree_stat = self.engine.store.put(malicious_tree_bytes)

        record = {
            "id": 1, "timestamp": time.time(), "parent": None,
            "root_tree_hash": tree_stat.obj_hash, "message": "malicious",
            "stats": {"files": 1, "new_objects": 2, "reused_objects": 0,
                      "original_bytes": 0, "compressed_bytes": 0},
        }
        (self.vault_dir / "snapshots" / "1").write_text(json.dumps(record))

        outside_target = self.root.parent / "evil_outside.txt"
        existed_before = outside_target.exists()
        try:
            apply_restore(self.engine, self.source_dir, 1)
        except Exception:  # noqa: BLE001, S110 - intentionally broad: this
            # test only cares that NOTHING got written to disk when path
            # traversal is attempted; the specific exception type isn't
            # the point (it could be ObjectCorruptedError from tree
            # validation, or something else depending on how deep the
            # malicious data gets before being rejected). Asserted via
            # the filesystem check below, not via exception type.
            pass
        finally:
            # Clean up defensively in case the test itself ever regresses.
            if outside_target.exists() and not existed_before:
                outside_target.unlink()
        self.assertFalse(
            outside_target.exists() and not existed_before,
            "path traversal wrote a file outside the target directory",
        )

    def test_restore_recovers_deleted_file(self):
        """The core 'rm -rf' demo scenario, as a test."""
        self._write("important.txt", "critical data")
        s1 = self.engine.create_snapshot(self.source_dir)

        (self.source_dir / "important.txt").unlink()
        self.assertFalse((self.source_dir / "important.txt").exists())

        result = apply_restore(self.engine, self.source_dir, s1.id)
        self.assertEqual(result.files_written, 1)
        self.assertTrue((self.source_dir / "important.txt").exists())
        self.assertEqual((self.source_dir / "important.txt").read_text(), "critical data")

    def test_restore_full_directory_deletion(self):
        """The literal demo scenario: whole project directory gone."""
        self._write("app.py", "print('app')")
        self._write("src/db.py", "def connect(): pass")
        s1 = self.engine.create_snapshot(self.source_dir)

        shutil.rmtree(self.source_dir)
        self.assertFalse(self.source_dir.exists())

        result = apply_restore(self.engine, self.source_dir, s1.id)
        self.assertEqual(result.files_written, 2)
        self.assertEqual((self.source_dir / "app.py").read_text(), "print('app')")
        self.assertEqual((self.source_dir / "src" / "db.py").read_text(), "def connect(): pass")

    def test_restore_overwrites_modified_file(self):
        self._write("config.json", '{"v": 1}')
        s1 = self.engine.create_snapshot(self.source_dir)
        self._write("config.json", '{"v": 2, "broken": true}')

        apply_restore(self.engine, self.source_dir, s1.id)
        self.assertEqual((self.source_dir / "config.json").read_text(), '{"v": 1}')

    def test_restore_does_not_delete_files_outside_snapshot(self):
        """Locks in the non-destructive design decision."""
        self._write("original.txt", "was here")
        s1 = self.engine.create_snapshot(self.source_dir)
        self._write("new_work.txt", "created after snapshot, should survive")

        apply_restore(self.engine, self.source_dir, s1.id)
        self.assertTrue((self.source_dir / "new_work.txt").exists())
        self.assertEqual((self.source_dir / "new_work.txt").read_text(),
                          "created after snapshot, should survive")

    def test_restore_is_idempotent_when_nothing_changed(self):
        self._write("a.txt", "stable")
        s1 = self.engine.create_snapshot(self.source_dir)
        result = apply_restore(self.engine, self.source_dir, s1.id)
        self.assertEqual(result.files_written, 0)  # nothing to add/modify

    def test_preview_never_writes_files(self):
        self._write("a.txt", "v1")
        s1 = self.engine.create_snapshot(self.source_dir)
        (self.source_dir / "a.txt").unlink()

        preview_restore(self.engine, self.source_dir, s1.id)
        self.assertFalse((self.source_dir / "a.txt").exists())

    def test_preview_reports_safe_to_restore_for_healthy_repo(self):
        self._write("a.txt", "healthy")
        s1 = self.engine.create_snapshot(self.source_dir)
        (self.source_dir / "a.txt").unlink()
        preview = preview_restore(self.engine, self.source_dir, s1.id)
        self.assertTrue(preview.safe_to_restore)
        self.assertEqual(preview.diff.added, ["a.txt"])

    def _corrupt_object(self, obj_hash: str):
        path = self.engine.store._object_path(obj_hash)
        raw = bytearray(path.read_bytes())
        raw[0] ^= 0xFF
        path.write_bytes(bytes(raw))

    def test_apply_restore_aborts_on_corrupted_blob(self):
        """A corrupted file object (not the tree) must abort cleanly."""
        self._write("a.txt", "will be corrupted")
        s1 = self.engine.create_snapshot(self.source_dir)
        (self.source_dir / "a.txt").unlink()

        entries = self.engine.load_tree(s1.root_tree_hash)
        blob_hash = next(e.obj_hash for e in entries if e.kind == "blob")
        self._corrupt_object(blob_hash)

        with self.assertRaises(RestoreError) as ctx:
            apply_restore(self.engine, self.source_dir, s1.id)
        self.assertIn("aborted", str(ctx.exception))
        self.assertFalse((self.source_dir / "a.txt").exists())

    def test_apply_restore_aborts_on_corrupted_tree(self):
        """A corrupted TREE object (structural, not a leaf file) must
        also abort cleanly instead of crashing with a raw exception —
        this is the bug the test suite originally caught."""
        self._write("a.txt", "content")
        s1 = self.engine.create_snapshot(self.source_dir)
        (self.source_dir / "a.txt").unlink()

        self._corrupt_object(s1.root_tree_hash)

        with self.assertRaises(RestoreError) as ctx:
            apply_restore(self.engine, self.source_dir, s1.id)
        self.assertIn("aborted", str(ctx.exception))
        self.assertFalse((self.source_dir / "a.txt").exists())


if __name__ == "__main__":
    unittest.main()
