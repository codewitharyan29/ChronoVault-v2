"""
Tests for vault/experimental/path_history.py.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.snapshot import SnapshotEngine
from vault.experimental.path_history import PathHistoryIndex


class TestPathHistoryIndex(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.vault_dir = self.root / ".vault"
        self.source_dir = self.root / "project"
        self.source_dir.mkdir()
        self.engine = SnapshotEngine(self.vault_dir)
        self.index = PathHistoryIndex(self.vault_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, rel_path, content):
        p = self.source_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def test_records_every_change_to_a_path(self):
        self._write("app.py", "v1")
        s1 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s1.id, s1.root_tree_hash)

        self._write("app.py", "v2")
        s2 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s2.id, s2.root_tree_hash)

        self._write("app.py", "v3")
        s3 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s3.id, s3.root_tree_hash)

        history = self.index.history_for("app.py")
        self.assertEqual(len(history), 3)
        self.assertEqual([sid for sid, _ in history], [s1.id, s2.id, s3.id])

    def test_unchanged_path_does_not_get_a_new_entry(self):
        self._write("stable.txt", "never changes")
        s1 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s1.id, s1.root_tree_hash)

        self._write("other.txt", "unrelated change")
        s2 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s2.id, s2.root_tree_hash)

        history = self.index.history_for("stable.txt")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0][0], s1.id)

    def test_path_that_never_existed_returns_empty_history(self):
        self._write("a.txt", "content")
        s1 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s1.id, s1.root_tree_hash)
        self.assertEqual(self.index.history_for("nonexistent.txt"), [])

    def test_incremental_matches_full_rebuild(self):
        for i in range(5):
            self._write("evolving.py", f"version {i}")
            record = self.engine.create_snapshot(self.source_dir, message=f"v{i}")
            self.index.record_snapshot(self.engine, record.id, record.root_tree_hash)

        incremental_history = self.index.history_for("evolving.py")

        fresh_dir = self.root / "fresh_index_dir"
        fresh_dir.mkdir(parents=True, exist_ok=True)
        fresh_index = PathHistoryIndex(fresh_dir)
        fresh_index.rebuild_from_scratch(self.engine)
        rebuilt_history = fresh_index.history_for("evolving.py")

        self.assertEqual(incremental_history, rebuilt_history)
        self.assertEqual(len(incremental_history), 5)

    def test_nested_path_history(self):
        self._write("src/deep/nested/file.py", "v1")
        s1 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s1.id, s1.root_tree_hash)

        self._write("src/deep/nested/file.py", "v2")
        s2 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s2.id, s2.root_tree_hash)

        history = self.index.history_for("src/deep/nested/file.py")
        self.assertEqual(len(history), 2)


if __name__ == "__main__":
    unittest.main()


class TestAdversarialIndexLoading(unittest.TestCase):
    """
    Regression tests for real bugs found by adversarial fuzzing of
    the persisted index file: raw JSONDecodeError/AttributeError
    leaking for corrupted or wrong-shaped content, and -- more
    seriously -- SILENTLY WRONG output (garbage 1-character tuples)
    for malformed-but-dict-shaped content, found by checking output
    correctness, not just crash-safety.
    """
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_empty_index_file_loads_as_empty_not_crash(self):
        (self.root / "path_history.json").write_text("")
        idx = PathHistoryIndex(self.root)
        self.assertEqual(idx.history, {})

    def test_malformed_json_loads_as_empty_not_crash(self):
        (self.root / "path_history.json").write_text("not json {{{")
        idx = PathHistoryIndex(self.root)
        self.assertEqual(idx.history, {})

    def test_valid_json_wrong_top_level_type_loads_as_empty(self):
        for content in ["null", "[]", "42", '"a string"']:
            with self.subTest(content=content):
                root = Path(tempfile.mkdtemp())
                try:
                    (root / "path_history.json").write_text(content)
                    idx = PathHistoryIndex(root)
                    self.assertEqual(idx.history, {})
                finally:
                    shutil.rmtree(root, ignore_errors=True)

    def test_malformed_per_path_value_returns_empty_not_garbage(self):
        """THE MAIN FINDING: a per-path value that's a string (not a
        list) used to be silently iterated character-by-character,
        producing plausible-looking garbage tuples instead of a
        clean empty result or error."""
        (self.root / "path_history.json").write_text('{"some/path.txt": "not_a_list"}')
        idx = PathHistoryIndex(self.root)
        self.assertEqual(idx.history_for("some/path.txt"), [])

    def test_mixed_valid_and_invalid_entries_keeps_only_valid_ones(self):
        (self.root / "path_history.json").write_text(
            '{"p": [[1, "abc"], ["not", "a", "pair"], [2, "def"], "garbage"]}'
        )
        idx = PathHistoryIndex(self.root)
        self.assertEqual(idx.history_for("p"), [(1, "abc"), (2, "def")])

    def test_well_formed_index_still_works_normally(self):
        (self.root / "path_history.json").write_text('{"p": [[1, "hash_a"], [2, "hash_b"]]}')
        idx = PathHistoryIndex(self.root)
        self.assertEqual(idx.history_for("p"), [(1, "hash_a"), (2, "hash_b")])
