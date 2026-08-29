"""
Tests for vault/experimental/fast_walk.py.
"""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.objects import ObjectStore, hash_bytes
from vault.snapshot import SnapshotStats, SnapshotEngine
from vault.experimental.fast_walk import StatCache, fast_walk_into_tree


class TestFastWalk(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.vault_dir = self.root / ".vault"
        self.source_dir = self.root / "project"
        self.source_dir.mkdir()
        self.store = ObjectStore(self.vault_dir)
        self.cache = StatCache(self.vault_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, rel_path, content):
        p = self.source_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def test_produces_identical_tree_hash_to_v1_real_walker(self):
        self._write("a.txt", "content a")
        self._write("src/b.py", "content b")

        real_engine = SnapshotEngine(self.root / "real_vault")
        real_record = real_engine.create_snapshot(self.source_dir)

        stats = SnapshotStats()
        now = time.time() + 10
        fast_hash = fast_walk_into_tree(self.store, self.cache, self.source_dir, stats, now)

        self.assertEqual(fast_hash, real_record.root_tree_hash)

    def test_unchanged_file_is_never_read_on_second_walk(self):
        self._write("stable.txt", "never changes")
        stats1 = SnapshotStats()
        now = time.time() + 10
        fast_walk_into_tree(self.store, self.cache, self.source_dir, stats1, now)
        self.cache.persist()

        read_calls = []
        original_read_bytes = Path.read_bytes

        def tracking_read_bytes(self):
            read_calls.append(str(self))
            return original_read_bytes(self)

        stats2 = SnapshotStats()
        now2 = time.time() + 20
        with patch.object(Path, "read_bytes", tracking_read_bytes):
            fast_walk_into_tree(self.store, self.cache, self.source_dir, stats2, now2)

        stable_file_reads = [c for c in read_calls if "stable.txt" in c]
        self.assertEqual(len(stable_file_reads), 0, "unchanged file was read despite a valid cache hit")
        self.assertEqual(stats2.reused_objects, 1)

    def test_changed_file_is_detected_via_size_change(self):
        self._write("f.txt", "short")
        stats1 = SnapshotStats()
        fast_walk_into_tree(self.store, self.cache, self.source_dir, stats1, time.time() + 10)

        self._write("f.txt", "a much longer replacement content")
        stats2 = SnapshotStats()
        hash2 = fast_walk_into_tree(self.store, self.cache, self.source_dir, stats2, time.time() + 20)

        real_engine = SnapshotEngine(self.root / "real_vault2")
        real_record = real_engine.create_snapshot(self.source_dir)
        self.assertEqual(hash2, real_record.root_tree_hash)

    def test_THE_CRITICAL_SAFETY_TEST_racy_window_prevents_missed_change(self):
        """The dangerous failure mode, tested directly: a file's
        content changes but its mtime is forced to collide with the
        cached value (same size too) -- removing every signal except
        the racy-window protection itself."""
        self._write("f.txt", "AAAAA")  # 5 bytes
        st_before = (self.source_dir / "f.txt").stat()
        stats1 = SnapshotStats()
        fast_walk_into_tree(self.store, self.cache, self.source_dir, stats1, time.time() + 100)

        (self.source_dir / "f.txt").write_text("BBBBB")  # same size, different content
        os.utime(self.source_dir / "f.txt", (st_before.st_atime, st_before.st_mtime))

        stats2 = SnapshotStats()
        racy_now = st_before.st_mtime + 0.1  # well within RACY_WINDOW_SECONDS
        fast_walk_into_tree(self.store, self.cache, self.source_dir, stats2, racy_now)

        self.assertEqual(
            stats2.reused_objects, 0,
            "DANGEROUS: racy window failed to force a re-hash -- a real content "
            "change was silently missed due to an mtime+size collision"
        )
        self.assertEqual(stats2.new_objects, 1)


if __name__ == "__main__":
    unittest.main()
