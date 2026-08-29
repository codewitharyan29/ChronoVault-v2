"""
Regression tests for the pack/CLI integration bug.

Packing deletes loose objects. Every OTHER command must keep working
against a packed repository -- this broke in the first merge attempt
(verify reported "0 objects", restore failed outright) and these
tests exist so it cannot silently break again.
"""
import shutil, tempfile, unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.snapshot import SnapshotEngine
from vault.objects import ObjectStore, hash_bytes
from vault.experimental.pack_aware_store import PackAwareObjectStore
from vault.experimental.delta_pack import find_delta_candidates, DeltaAwarePackWriter, DeltaAwarePackedStore
from vault.restore import apply_restore
from vault.gc import compute_reachable_objects


class TestPackAwareIntegration(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.vault_dir = self.root / ".vault"
        self.source_dir = self.root / "project"
        self.source_dir.mkdir()
        self.engine = SnapshotEngine(self.vault_dir)
        for i in range(3):
            (self.source_dir / f"f{i}.txt").write_text(f"content {i}")
        self.record = self.engine.create_snapshot(self.source_dir, message="v1")

        # Pack via the SAME writer `vault pack` actually uses now
        # (delta-aware format) -- matching real CLI behavior, not an
        # older, now-incompatible format.
        raw_store = ObjectStore(self.vault_dir)
        loose_hashes = sorted(raw_store.iter_all_hashes())
        candidates = find_delta_candidates(self.engine)
        writer = DeltaAwarePackWriter(raw_store, self.vault_dir / "pack")
        stats = writer.write_pack("p1", candidates)
        reader = DeltaAwarePackedStore(raw_store, stats["pack_path"], stats["idx_path"])
        for h in loose_hashes:
            data = reader.get(h)
            assert hash_bytes(data) == h
        for h in loose_hashes:
            raw_store.delete(h)

        self.engine.store = PackAwareObjectStore(self.vault_dir)

    def tearDown(self):
        try: self.engine.store.close()
        except Exception: pass
        shutil.rmtree(self.root, ignore_errors=True)

    def test_iter_all_hashes_sees_packed_objects(self):
        """The exact bug: verify reported 0 objects on a packed repo."""
        hashes = list(self.engine.store.iter_all_hashes())
        self.assertGreater(len(hashes), 0, "packed objects invisible to iter_all_hashes")

    def test_verify_style_check_passes_after_packing(self):
        hashes = list(self.engine.store.iter_all_hashes())
        corrupted = [h for h in hashes if not self.engine.store.verify_object(h)]
        self.assertEqual(corrupted, [])

    def test_restore_works_from_a_packed_repository(self):
        """The other half of the bug: restore failed outright."""
        shutil.rmtree(self.source_dir)
        result = apply_restore(self.engine, self.source_dir, self.record.id)
        self.assertEqual(result.files_written, 3)
        for i in range(3):
            self.assertEqual((self.source_dir / f"f{i}.txt").read_text(), f"content {i}")

    def test_gc_reachability_works_after_packing(self):
        reachable = compute_reachable_objects(self.engine)
        self.assertGreater(len(reachable), 0)

    def test_new_snapshots_still_work_after_packing(self):
        (self.source_dir / "new.txt").write_text("added after packing")
        record2 = self.engine.create_snapshot(self.source_dir, message="v2")
        self.assertGreater(record2.stats.files, 3)
        # And the mixed loose+packed state must still read back correctly.
        self.assertEqual(
            self.engine.store.get(
                next(e.obj_hash for e in self.engine.load_tree(record2.root_tree_hash)
                     if e.name == "new.txt")
            ), b"added after packing")


if __name__ == "__main__":
    unittest.main()
