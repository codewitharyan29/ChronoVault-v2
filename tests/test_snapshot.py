"""
Tests for vault/snapshot.py: directory walking into blob/tree objects,
snapshot records, the parent chain, and error handling.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.snapshot import (
    SnapshotEngine,
    SnapshotNotFoundError,
    SnapshotRecord,
    TreeEntry,
    deserialize_tree,
    serialize_tree,
)


class TestSnapshotEngine(unittest.TestCase):
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

    def test_snapshot_flat_directory(self):
        self._write("a.txt", "hello")
        self._write("b.txt", "world")
        record = self.engine.create_snapshot(self.source_dir, message="first")
        self.assertEqual(record.id, 1)
        self.assertIsNone(record.parent)
        self.assertEqual(record.message, "first")
        self.assertEqual(record.stats.files, 2)
        self.assertEqual(record.stats.new_objects, 2)  # tree object is separate

    def test_snapshot_nested_directories(self):
        self._write("src/main.py", "print(1)")
        self._write("src/lib/util.py", "def f(): pass")
        self._write("README.md", "# hi")
        record = self.engine.create_snapshot(self.source_dir)
        self.assertEqual(record.stats.files, 3)

    def test_parent_chain(self):
        self._write("a.txt", "v1")
        s1 = self.engine.create_snapshot(self.source_dir, message="v1")
        self._write("a.txt", "v2")
        s2 = self.engine.create_snapshot(self.source_dir, message="v2")
        self.assertIsNone(s1.parent)
        self.assertEqual(s2.parent, s1.id)

    def test_identical_content_dedups_across_snapshots(self):
        self._write("a.txt", "unchanged content")
        self.engine.create_snapshot(self.source_dir)
        # No changes at all -> second snapshot should reuse every blob.
        s2 = self.engine.create_snapshot(self.source_dir)
        self.assertEqual(s2.stats.new_objects, 0)
        self.assertGreater(s2.stats.reused_objects, 0)

    def test_duplicate_files_within_one_snapshot_dedup(self):
        self._write("a.txt", "same content")
        self._write("b.txt", "same content")
        record = self.engine.create_snapshot(self.source_dir)
        # Two files, identical content -> one new blob, one reused blob,
        # plus tree object(s).
        self.assertEqual(record.stats.files, 2)
        self.assertEqual(record.stats.reused_objects, 1)

    def test_empty_directory_snapshot(self):
        record = self.engine.create_snapshot(self.source_dir)
        self.assertEqual(record.stats.files, 0)

    def test_empty_file(self):
        self._write("empty.txt", "")
        record = self.engine.create_snapshot(self.source_dir)
        self.assertEqual(record.stats.files, 1)

    def test_vault_dir_itself_is_ignored_during_walk(self):
        # Guards against the object store recursively snapshotting itself
        # if source_dir and vault_dir ever overlap.
        (self.source_dir / ".vault").mkdir()
        (self.source_dir / ".vault" / "junk").write_text("should not be walked")
        self._write("real.txt", "real content")
        record = self.engine.create_snapshot(self.source_dir)
        self.assertEqual(record.stats.files, 1)

    def test_ids_never_reused_after_deletion(self):
        """The bug found during manual CLI testing: ids must stay
        unique for the life of the repo, even across deletions."""
        self._write("a.txt", "1")
        s1 = self.engine.create_snapshot(self.source_dir)
        self._write("a.txt", "2")
        s2 = self.engine.create_snapshot(self.source_dir)
        self.assertEqual((s1.id, s2.id), (1, 2))

        self.engine.delete_snapshot(s2.id)  # delete the HIGHEST id
        self._write("a.txt", "3")
        s3 = self.engine.create_snapshot(self.source_dir)
        # Must be 3, not a reused 2 — this is exactly the case max()+1 got wrong.
        self.assertEqual(s3.id, 3)

    def test_delete_snapshot_removes_it_from_list(self):
        self._write("a.txt", "1")
        s1 = self.engine.create_snapshot(self.source_dir)
        self._write("a.txt", "2")
        s2 = self.engine.create_snapshot(self.source_dir)

        self.engine.delete_snapshot(s1.id)
        remaining_ids = [s.id for s in self.engine.list_snapshots()]
        self.assertEqual(remaining_ids, [s2.id])

    def test_delete_nonexistent_snapshot_raises(self):
        with self.assertRaises(SnapshotNotFoundError):
            self.engine.delete_snapshot(999)

    def test_delete_snapshot_does_not_touch_objects(self):
        """Deletion removes the record only — objects are gc's job."""
        self._write("a.txt", "content")
        s1 = self.engine.create_snapshot(self.source_dir)
        objects_before = set(self.engine.store.iter_all_hashes())

        self.engine.delete_snapshot(s1.id)
        objects_after = set(self.engine.store.iter_all_hashes())
        self.assertEqual(objects_before, objects_after)

    def test_symlinked_file_is_skipped_not_followed(self):
        """Real bug found via manual testing: the code claimed to skip
        symlinks but never actually checked for them — a symlinked
        file was silently followed and its target duplicated as a
        real blob. Must actually be absent from the snapshot now."""
        import os
        if os.name == "nt":
            self.skipTest("symlink creation requires elevated privileges on Windows")
        self._write("real.txt", "real content")
        os.symlink(self.source_dir / "real.txt", self.source_dir / "link.txt")

        record = self.engine.create_snapshot(self.source_dir)
        self.assertEqual(record.stats.files, 1)  # only real.txt, not the symlink
        entries = {e.name for e in self.engine.load_tree(record.root_tree_hash)}
        self.assertIn("real.txt", entries)
        self.assertNotIn("link.txt", entries)

    def test_symlinked_directory_cycle_does_not_infinite_loop(self):
        """The dangerous case: a directory symlink pointing back at an
        ancestor would recurse forever without the skip check."""
        import os
        if os.name == "nt":
            self.skipTest("symlink creation requires elevated privileges on Windows")
        (self.source_dir / "real_sub").mkdir()
        self._write("real_sub/file.txt", "content")
        os.symlink(self.source_dir, self.source_dir / "real_sub" / "cycle_back")

        # Must complete without hanging or raising RecursionError.
        record = self.engine.create_snapshot(self.source_dir)
        self.assertEqual(record.stats.files, 1)

    def test_deeply_nested_directories_fail_cleanly_not_with_raw_recursion_error(self):
        """Real bug found via manual stress testing: at ~1000+ levels
        of directory nesting, Python's own recursion limit is hit
        during the recursive walk, and a bare RecursionError leaked
        through uncaught (the existing OSError handler doesn't catch
        it -- RecursionError is a RuntimeError subclass). Fixed by
        catching it explicitly. This test uses 1100 levels, comfortably
        past the confirmed ~950-1000 boundary."""
        import sys

        from vault.objects import VaultError
        current = self.source_dir
        for i in range(1100):
            current = current / "x"
            current.mkdir()
        (current / "f.txt").write_text("deep")

        try:
            with self.assertRaises(VaultError) as ctx:
                self.engine.create_snapshot(self.source_dir)
            self.assertIn("nested", str(ctx.exception).lower())
            self.assertNotIsInstance(ctx.exception, RecursionError)
        finally:
            # shutil.rmtree() is ALSO recursive and hits the same
            # Python recursion limit on cleanup -- not a ChronoVault
            # bug, but the test needs to raise the limit temporarily
            # to clean up after itself.
            old_limit = sys.getrecursionlimit()
            sys.setrecursionlimit(3000)
            try:
                shutil.rmtree(self.source_dir, ignore_errors=True)
            finally:
                sys.setrecursionlimit(old_limit)

    def test_moderately_nested_directories_still_work(self):
        """900 levels is comfortably below the recursion boundary --
        confirms the fix didn't accidentally lower the working limit."""
        import sys
        current = self.source_dir
        for i in range(900):
            current = current / "x"
            current.mkdir()
        (current / "f.txt").write_text("deep but fine")
        record = self.engine.create_snapshot(self.source_dir)
        self.assertEqual(record.stats.files, 1)
        # Same cleanup consideration as above, though 900 levels is
        # usually under shutil.rmtree's own limit too -- raise it
        # defensively anyway rather than relying on that margin.
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(3000)
        try:
            shutil.rmtree(self.source_dir, ignore_errors=True)
        finally:
            sys.setrecursionlimit(old_limit)

    def test_malformed_snapshot_json_rejected_cleanly(self):
        """Real gap found via fuzz-style testing: a corrupted/malformed
        snapshot record file used to leak a raw JSONDecodeError instead
        of failing as a proper VaultError subclass."""
        from vault.snapshot import SnapshotCorruptedError
        with self.assertRaises(SnapshotCorruptedError):
            SnapshotRecord.from_json_bytes(b"not json at all {{{")

    def test_snapshot_json_missing_required_field_rejected_cleanly(self):
        import json

        from vault.snapshot import SnapshotCorruptedError
        incomplete = json.dumps({"id": 1, "timestamp": 123.0}).encode()  # missing parent, root_tree_hash, stats
        with self.assertRaises(SnapshotCorruptedError):
            SnapshotRecord.from_json_bytes(incomplete)

    def test_load_missing_snapshot_raises_helpful_error(self):
        self._write("a.txt", "x")
        self.engine.create_snapshot(self.source_dir)
        with self.assertRaises(SnapshotNotFoundError) as ctx:
            self.engine.load_snapshot(999)
        self.assertIn("999", str(ctx.exception))
        self.assertIn("Available snapshots", str(ctx.exception))

    def test_list_snapshots_ordered(self):
        self._write("a.txt", "1")
        self.engine.create_snapshot(self.source_dir, message="one")
        self._write("a.txt", "2")
        self.engine.create_snapshot(self.source_dir, message="two")
        snaps = self.engine.list_snapshots()
        self.assertEqual([s.id for s in snaps], [1, 2])
        self.assertEqual([s.message for s in snaps], ["one", "two"])


class TestTreeSerialization(unittest.TestCase):
    def test_path_traversal_in_entry_name_rejected_at_serialization(self):
        """Real vulnerability found via manual testing: a tree entry
        name containing '../' was used unsanitized in restore.py's
        file path construction, allowing a write outside the target
        directory. Fixed by validating names at both serialize and
        deserialize time — this test covers the serialize side."""
        from vault.objects import ObjectCorruptedError
        malicious = [TreeEntry(name="../../evil.txt", kind="blob", obj_hash="a" * 64)]
        with self.assertRaises(ObjectCorruptedError):
            serialize_tree(malicious)

    def test_path_traversal_in_entry_name_rejected_at_deserialization(self):
        """Same vulnerability, but via a directly hand-crafted tree
        object bypassing serialize_tree entirely -- the realistic
        attack surface (a tampered object file on disk), not just a
        misuse of the serialize function."""
        from vault.objects import ObjectCorruptedError
        name = b"../../evil.txt"
        malicious_bytes = b"b" + len(name).to_bytes(2, "big") + name + b"a" * 64
        with self.assertRaises(ObjectCorruptedError):
            deserialize_tree(malicious_bytes)

    def test_embedded_slash_in_entry_name_rejected(self):
        from vault.objects import ObjectCorruptedError
        malicious = [TreeEntry(name="subdir/file.txt", kind="blob", obj_hash="a" * 64)]
        with self.assertRaises(ObjectCorruptedError):
            serialize_tree(malicious)

    def test_invalid_utf8_in_entry_name_rejected_cleanly(self):
        """Real gap found via fuzz-style testing: raw bytes that
        aren't valid UTF-8 in the name field used to leak a raw
        UnicodeDecodeError instead of failing as ObjectCorruptedError."""
        from vault.objects import ObjectCorruptedError
        invalid_utf8 = b"\xff\xfe\x00\x01"
        malformed = b"b" + len(invalid_utf8).to_bytes(2, "big") + invalid_utf8 + b"a" * 64
        with self.assertRaises(ObjectCorruptedError):
            deserialize_tree(malformed)

    def test_roundtrip(self):
        entries = [
            TreeEntry(name="b.txt", kind="blob", obj_hash="a" * 64),
            TreeEntry(name="a.txt", kind="blob", obj_hash="b" * 64),
            TreeEntry(name="sub", kind="tree", obj_hash="c" * 64),
        ]
        data = serialize_tree(entries)
        restored = deserialize_tree(data)
        # serialize_tree sorts by name, so restored order is deterministic.
        self.assertEqual([e.name for e in restored], ["a.txt", "b.txt", "sub"])

    def test_same_contents_same_bytes_regardless_of_input_order(self):
        e1 = [TreeEntry("a", "blob", "1" * 64), TreeEntry("b", "blob", "2" * 64)]
        e2 = [TreeEntry("b", "blob", "2" * 64), TreeEntry("a", "blob", "1" * 64)]
        self.assertEqual(serialize_tree(e1), serialize_tree(e2))

    def test_unicode_filename(self):
        entries = [TreeEntry(name="café_résumé.txt", kind="blob", obj_hash="f" * 64)]
        restored = deserialize_tree(serialize_tree(entries))
        self.assertEqual(restored[0].name, "café_résumé.txt")


    def test_invalid_kind_byte_raises_instead_of_defaulting_to_tree(self):
        # Hand-craft a malformed tree entry: kind byte 'x' instead of 'b'/'t'.
        from vault.objects import ObjectCorruptedError
        name = b"file.txt"
        malformed = b"x" + len(name).to_bytes(2, "big") + name + b"a" * 64
        with self.assertRaises(ObjectCorruptedError):
            deserialize_tree(malformed)

    def test_malformed_hash_raises(self):
        from vault.objects import ObjectCorruptedError
        name = b"file.txt"
        bad_hash = b"not-a-valid-hex-hash" + b"0" * 44  # wrong chars, still 64 bytes
        malformed = b"b" + len(name).to_bytes(2, "big") + name + bad_hash
        with self.assertRaises(ObjectCorruptedError):
            deserialize_tree(malformed)

    def test_truncated_data_raises(self):
        from vault.objects import ObjectCorruptedError
        with self.assertRaises(ObjectCorruptedError):
            deserialize_tree(b"b\x00")  # kind byte + partial name length, nothing else


if __name__ == "__main__":
    unittest.main()


class TestAdversarialNameLength(unittest.TestCase):
    """
    Regression test for a real bug found by adversarial fuzzing: the
    on-disk tree format packs a name's UTF-8 byte length into a
    2-byte field (max 65535), but serialize_tree() didn't check this
    before packing -- a longer name raised an uncaught OverflowError
    from int.to_bytes(2, "big") instead of a clean, expected error.
    """
    def test_name_over_format_limit_raises_clean_error(self):
        from vault.objects import ObjectCorruptedError
        with self.assertRaises(ObjectCorruptedError):
            serialize_tree([TreeEntry(name="x" * 70000, kind="blob", obj_hash="b" * 64)])

    def test_name_exactly_at_format_limit_is_accepted(self):
        # 65535 is the documented ceiling -- must still work.
        entries = [TreeEntry(name="x" * 65535, kind="blob", obj_hash="b" * 64)]
        data = serialize_tree(entries)
        roundtrip = deserialize_tree(data)
        self.assertEqual(roundtrip[0].name, "x" * 65535)

    def test_name_one_over_format_limit_is_rejected(self):
        from vault.objects import ObjectCorruptedError
        with self.assertRaises(ObjectCorruptedError):
            serialize_tree([TreeEntry(name="x" * 65536, kind="blob", obj_hash="b" * 64)])

    def test_ordinary_short_names_unaffected(self):
        entries = [TreeEntry(name="normal_file.txt", kind="blob", obj_hash="c" * 64)]
        data = serialize_tree(entries)
        roundtrip = deserialize_tree(data)
        self.assertEqual(roundtrip[0].name, "normal_file.txt")
