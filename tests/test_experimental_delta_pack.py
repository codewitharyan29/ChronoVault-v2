"""
Tests for vault/experimental/delta_pack.py -- the actual integration
of delta compression (feature #3) into pack files (feature #2).
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.snapshot import SnapshotEngine
from vault.experimental.delta_pack import (
    find_delta_candidates, DeltaAwarePackWriter, DeltaAwarePackedStore
)


class TestDeltaAwarePacking(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.vault_dir = self.root / ".vault"
        self.source_dir = self.root / "project"
        self.source_dir.mkdir()
        self.engine = SnapshotEngine(self.vault_dir)
        self.pack_dir = self.vault_dir / "pack"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, rel_path, content):
        p = self.source_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def test_find_delta_candidates_identifies_same_path_evolution(self):
        self._write("app.py", "line one\n" * 100)
        s1 = self.engine.create_snapshot(self.source_dir)
        self._write("app.py", "line one (edited)\n" + "line one\n" * 99)
        s2 = self.engine.create_snapshot(self.source_dir)

        candidates = find_delta_candidates(self.engine)
        self.assertEqual(len(candidates), 1)
        entries_s1 = {e.name: e.obj_hash for e in self.engine.load_tree(s1.root_tree_hash)}
        entries_s2 = {e.name: e.obj_hash for e in self.engine.load_tree(s2.root_tree_hash)}
        self.assertEqual(candidates[entries_s2["app.py"]], entries_s1["app.py"])

    def test_no_candidates_when_nothing_changed(self):
        self._write("app.py", "stable content\n" * 50)
        self.engine.create_snapshot(self.source_dir)
        self.engine.create_snapshot(self.source_dir)
        candidates = find_delta_candidates(self.engine)
        self.assertEqual(len(candidates), 0)

    def test_delta_pack_roundtrip_correctness(self):
        lines = [f"def f_{i}(): return {i}\n" for i in range(200)]
        for edit in range(5):
            lines[edit * 10] = f"def f_{edit*10}(): return {edit*10} + 1  # edited\n"
            self._write("app.py", "".join(lines))
            self.engine.create_snapshot(self.source_dir, message=f"edit {edit}")

        candidates = find_delta_candidates(self.engine)
        writer = DeltaAwarePackWriter(self.engine.store, self.pack_dir)
        stats = writer.write_pack("p1", candidates)

        reader = DeltaAwarePackedStore(self.engine.store, stats["pack_path"], stats["idx_path"])

        for obj_hash in self.engine.store.iter_all_hashes():
            expected = self.engine.store.get(obj_hash)
            actual = reader.get(obj_hash)
            self.assertEqual(actual, expected, f"mismatch for {obj_hash}")

    def test_delta_actually_used_and_saves_bytes(self):
        lines = [f"def f_{i}(): return {i}\n" for i in range(300)]
        for edit in range(5):
            lines[edit * 20] = f"def f_{edit*20}(): return {edit*20} + 1  # edited\n"
            self._write("app.py", "".join(lines))
            self.engine.create_snapshot(self.source_dir, message=f"edit {edit}")

        candidates = find_delta_candidates(self.engine)
        writer = DeltaAwarePackWriter(self.engine.store, self.pack_dir)
        stats = writer.write_pack("p1", candidates)

        print(f"\n  [delta-pack test] full={stats['full']}, delta={stats['delta']}, "
              f"bytes saved={stats['delta_bytes_saved']}")
        self.assertGreater(stats["delta"], 0, "expected at least one object to use delta encoding")
        self.assertGreater(stats["delta_bytes_saved"], 0)

    def test_falls_back_to_full_when_delta_is_not_smaller(self):
        import os
        s = self.engine.store
        h1 = s.put(os.urandom(3000)).obj_hash
        h2 = s.put(os.urandom(3000)).obj_hash
        fake_candidates = {h2: h1}

        writer = DeltaAwarePackWriter(s, self.pack_dir)
        stats = writer.write_pack("p2", fake_candidates)
        self.assertEqual(stats["delta"], 0)

    def test_chain_prevention_a_base_is_never_also_a_delta_target(self):
        self._write("app.py", "version 1\n" * 50)
        self.engine.create_snapshot(self.source_dir)
        self._write("app.py", "version 2\n" * 50)
        self.engine.create_snapshot(self.source_dir)
        self._write("app.py", "version 3\n" * 50)
        self.engine.create_snapshot(self.source_dir)

        candidates = find_delta_candidates(self.engine)
        bases = set(candidates.values())
        targets = set(candidates.keys())
        self.assertEqual(bases & targets, set())


if __name__ == "__main__":
    unittest.main()
