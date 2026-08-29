"""
Tests for vault/reporting.py — status, explain, and tag resolution.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.reporting import (
    TagNotFoundError,
    compute_status,
    explain_snapshot,
    resolve_snapshot_ref,
    tag_snapshot,
)
from vault.snapshot import SnapshotEngine


class TestReporting(unittest.TestCase):
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

    def test_status_on_empty_repo(self):
        status = compute_status(self.engine)
        self.assertEqual(status.snapshot_count, 0)
        self.assertIsNone(status.last_snapshot)
        self.assertTrue(status.integrity_ok)

    def test_status_reflects_snapshots_and_objects(self):
        self._write("a.txt", "hello")
        self.engine.create_snapshot(self.source_dir, message="first")
        status = compute_status(self.engine)
        self.assertEqual(status.snapshot_count, 1)
        self.assertGreater(status.object_count, 0)
        self.assertEqual(status.last_snapshot.message, "first")

    def test_status_stored_bytes_reflects_dedup_not_snapshot_count(self):
        """Snapshotting identical content twice should barely grow total
        stored bytes on disk (only a new tree/snapshot record, no new
        blob) — proving total_stored_bytes reflects real disk usage via
        the object store, not a naive per-snapshot sum that would double
        the shared blob's contribution."""
        self._write("shared.txt", "x" * 10_000)  # large enough that a
        # doubled blob would be an obvious, unmistakable jump
        self.engine.create_snapshot(self.source_dir)
        after_first = compute_status(self.engine).total_stored_bytes

        self._write("shared.txt", "x" * 10_000)  # identical content again
        self.engine.create_snapshot(self.source_dir)
        after_second = compute_status(self.engine).total_stored_bytes

        growth = after_second - after_first
        # Only a new (small) tree object should have been added — nowhere
        # near the ~10KB the blob itself would cost if it were duplicated.
        self.assertLess(growth, 200)

    def test_explain_reports_dedup_ratio(self):
        self._write("a.txt", "content")
        self.engine.create_snapshot(self.source_dir)
        self._write("a.txt", "content")  # unchanged
        self._write("b.txt", "new")
        record = self.engine.create_snapshot(self.source_dir)

        explanation = explain_snapshot(self.engine, record.id)
        self.assertGreater(explanation.dedup_ratio_pct, 0)

    def test_explain_on_missing_snapshot_raises_helpful_error(self):
        from vault.snapshot import SnapshotNotFoundError
        with self.assertRaises(SnapshotNotFoundError):
            explain_snapshot(self.engine, 999)

    def test_tag_and_resolve(self):
        self._write("a.txt", "v1")
        s1 = self.engine.create_snapshot(self.source_dir, message="stable")
        tag_snapshot(self.engine, s1.id, "release-v1")
        resolved = resolve_snapshot_ref(self.engine, "release-v1")
        self.assertEqual(resolved, s1.id)

    def test_numeric_ref_resolves_directly_without_needing_a_tag(self):
        self._write("a.txt", "v1")
        s1 = self.engine.create_snapshot(self.source_dir)
        self.assertEqual(resolve_snapshot_ref(self.engine, str(s1.id)), s1.id)

    def test_unknown_tag_raises(self):
        with self.assertRaises(TagNotFoundError):
            resolve_snapshot_ref(self.engine, "nonexistent-tag")

    def test_tag_on_missing_snapshot_raises(self):
        from vault.snapshot import SnapshotNotFoundError
        with self.assertRaises(SnapshotNotFoundError):
            tag_snapshot(self.engine, 999, "bad-tag")

    def test_multiple_tags_persist_across_engine_instances(self):
        """Tags are written to disk, not just held in memory — a new
        SnapshotEngine instance pointed at the same .vault must see them."""
        self._write("a.txt", "v1")
        s1 = self.engine.create_snapshot(self.source_dir)
        tag_snapshot(self.engine, s1.id, "v1-tag")

        fresh_engine = SnapshotEngine(self.vault_dir)
        self.assertEqual(resolve_snapshot_ref(fresh_engine, "v1-tag"), s1.id)


if __name__ == "__main__":
    unittest.main()


class TestAdversarialTagsFileLoading(unittest.TestCase):
    """
    Regression tests for a real bug found by adversarial fuzzing: a
    corrupted tags.json raised an uncaught raw JSONDecodeError. Tags
    are deliberately user-created data (unlike a rebuildable cache),
    so silently discarding them on corruption would be worse than
    raising clearly.
    """
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.vault_dir = self.root / ".vault"
        self.source_dir = self.root / "project"
        self.source_dir.mkdir()
        self.engine = SnapshotEngine(self.vault_dir)
        (self.source_dir / "a.txt").write_text("content")
        self.record = self.engine.create_snapshot(self.source_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _tags_path(self):
        from vault.reporting import _tags_path
        return _tags_path(self.engine)

    def test_corrupted_tags_json_raises_clean_vaulterror_not_raw_jsondecodeerror(self):
        from vault.objects import VaultError
        self._tags_path().write_text("not valid json {{{")
        with self.assertRaises(VaultError):
            resolve_snapshot_ref(self.engine, "sometag")

    def test_wrong_type_tags_json_raises_clean_error(self):
        from vault.objects import VaultError
        self._tags_path().write_text("[1, 2, 3]")  # valid JSON, wrong shape
        with self.assertRaises(VaultError):
            resolve_snapshot_ref(self.engine, "sometag")

    def test_missing_tags_file_is_not_an_error(self):
        # No tags file at all is the normal, expected case (no tags
        # created yet) -- must NOT raise.
        with self.assertRaises(TagNotFoundError):  # tag genuinely doesn't exist
            resolve_snapshot_ref(self.engine, "sometag")

    def test_well_formed_tags_file_still_works_normally(self):
        tag_snapshot(self.engine, self.record.id, "release-v1")
        self.assertEqual(resolve_snapshot_ref(self.engine, "release-v1"), self.record.id)
