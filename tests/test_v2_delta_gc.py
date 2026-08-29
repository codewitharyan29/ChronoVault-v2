"""
Tests for vault/experimental/delta_gc.py.

The most important test doesn't just check the fix works -- it first
PROVES the disaster it prevents is real, by running v1's UNMODIFIED
gc.py against a delta-packed repository and confirming it would
incorrectly consider a needed base unreachable.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.snapshot import SnapshotEngine
from vault.experimental.delta_pack import find_delta_candidates, DeltaAwarePackWriter, DeltaAwarePackedStore
from vault.experimental.delta_gc import (
    load_all_delta_manifests, compute_expanded_reachable_objects, run_delta_aware_gc
)


class TestDeltaGC(unittest.TestCase):
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

    def _make_delta_packed_repo(self):
        lines = [f"def f_{i}(): return {i}\n" for i in range(200)]
        for edit in range(5):
            lines[edit * 10] = f"def f_{edit*10}(): return {edit*10} + 1  # edited\n"
            self._write("app.py", "".join(lines))
            self.engine.create_snapshot(self.source_dir, message=f"edit {edit}")

        candidates = find_delta_candidates(self.engine)
        writer = DeltaAwarePackWriter(self.engine.store, self.pack_dir)
        stats = writer.write_pack("p1", candidates)
        self.assertGreater(stats["delta"], 0, "test setup didn't produce any delta objects")
        return stats

    def test_manifest_is_written_and_loadable(self):
        self._make_delta_packed_repo()
        manifest = load_all_delta_manifests(self.pack_dir)
        self.assertGreater(len(manifest), 0)

    def test_THE_DISASTER_v1_unmodified_gc_would_delete_a_needed_base(self):
        """
        Proves the risk was real, not theoretical -- and finding the
        RIGHT scenario to prove it took a failed attempt first: an
        earlier version of this test kept all 5 snapshots alive, which
        meant the snapshot that originally held the base blob STILL
        directly referenced it through its own tree, so v1's ordinary
        reachability found it anyway -- no danger present, and the
        test failed for exactly that reason (see commit log).

        The real disaster requires the ORIGINAL snapshot (the one
        whose tree directly references the base) to be DELETED,
        removing the only direct tree-reference, while a DIFFERENT,
        still-live snapshot's delta-encoded blob keeps depending on
        that same base for reconstruction.
        """
        self._make_delta_packed_repo()
        manifest = load_all_delta_manifests(self.pack_dir)
        target_hash, base_hash = next(iter(manifest.items()))

        # Find and delete whichever live snapshot's tree is the ONLY
        # one directly referencing base_hash -- simulating a user
        # running `vault snapshot-rm` on an old version after packing.
        from vault.gc import compute_reachable_objects
        reachable_before = compute_reachable_objects(self.engine)
        owning_snapshot_ids = reachable_before.get(base_hash, set())
        self.assertTrue(owning_snapshot_ids, "test setup problem: base should be tree-reachable before deletion")

        for snap_id in owning_snapshot_ids:
            self.engine.delete_snapshot(snap_id)

        # NOW: v1's real, unmodified reachability computation, after
        # the deletion.
        v1_reachable = compute_reachable_objects(self.engine)

        self.assertIn(target_hash, v1_reachable,
                      "test setup problem: the delta target should still be reachable "
                      "via a different, surviving snapshot")
        self.assertNotIn(
            base_hash, v1_reachable,
            "expected v1's gc to be blind to this dependency now that no tree "
            "directly references the base -- if this fails, the danger this "
            "feature protects against isn't present in this scenario"
        )

    def test_delta_aware_gc_protects_the_base_v1_would_have_missed(self):
        self._make_delta_packed_repo()
        manifest = load_all_delta_manifests(self.pack_dir)
        target_hash, base_hash = next(iter(manifest.items()))

        from vault.gc import compute_reachable_objects
        owning_snapshot_ids = compute_reachable_objects(self.engine).get(base_hash, set())
        for snap_id in owning_snapshot_ids:
            self.engine.delete_snapshot(snap_id)

        expanded = compute_expanded_reachable_objects(self.engine, self.pack_dir)
        self.assertIn(base_hash, expanded, "delta-aware GC failed to protect a needed base")

    def test_delta_aware_gc_does_not_delete_a_needed_base_end_to_end(self):
        """Full end-to-end proof. Includes the SAME snapshot-deletion
        step as the disaster test above -- without it, this test would
        pass trivially (the base would still be tree-reachable on its
        own, same flaw the original disaster-test attempt had), which
        wouldn't actually prove anything about delta-awareness."""
        stats = self._make_delta_packed_repo()
        manifest = load_all_delta_manifests(self.pack_dir)
        target_hash, base_hash = next(iter(manifest.items()))

        from vault.gc import compute_reachable_objects
        owning_snapshot_ids = compute_reachable_objects(self.engine).get(base_hash, set())
        for snap_id in owning_snapshot_ids:
            self.engine.delete_snapshot(snap_id)

        reader = DeltaAwarePackedStore(self.engine.store, stats["pack_path"], stats["idx_path"])
        before = reader.get(target_hash)  # must succeed before gc

        run_delta_aware_gc(self.engine, self.pack_dir)

        self.assertTrue(self.engine.store.has(base_hash),
                         "delta-aware gc deleted a base object a live delta target still needs")

        reader2 = DeltaAwarePackedStore(self.engine.store, stats["pack_path"], stats["idx_path"])
        after = reader2.get(target_hash)
        self.assertEqual(before, after)

    def test_no_deltas_present_behaves_identically_to_v1(self):
        self._write("a.txt", "no edits, no deltas here")
        self.engine.create_snapshot(self.source_dir)

        from vault.gc import compute_reachable_objects
        v1_reachable = compute_reachable_objects(self.engine)
        expanded = compute_expanded_reachable_objects(self.engine, self.pack_dir)
        self.assertEqual(set(v1_reachable.keys()), set(expanded.keys()))

    def test_missing_manifest_file_degrades_safely_not_crash(self):
        self.pack_dir.mkdir(parents=True, exist_ok=True)
        (self.pack_dir / "broken.deltamanifest.json").write_text("not valid json {{{")
        self._write("a.txt", "content")
        self.engine.create_snapshot(self.source_dir)
        expanded = compute_expanded_reachable_objects(self.engine, self.pack_dir)
        self.assertIsInstance(expanded, dict)


if __name__ == "__main__":
    unittest.main()


class TestAdversarialManifestLoading(unittest.TestCase):
    """
    Regression tests for a real bug found by adversarial fuzzing:
    valid-JSON-but-wrong-type manifest content (null, a bare number,
    a bare string) crashed dict.update() with an uncaught TypeError
    or ValueError, not caught by the existing JSONDecodeError/OSError
    guard. This sits in the GC safety path -- a crash here could mean
    `vault gc` never runs at all.
    """
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.pack_dir = self.root / "pack"
        self.pack_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_null_manifest_content_loads_as_empty_not_crash(self):
        (self.pack_dir / "p1.deltamanifest.json").write_text("null")
        result = load_all_delta_manifests(self.pack_dir)
        self.assertEqual(result, {})

    def test_number_manifest_content_loads_as_empty_not_crash(self):
        (self.pack_dir / "p1.deltamanifest.json").write_text("42")
        result = load_all_delta_manifests(self.pack_dir)
        self.assertEqual(result, {})

    def test_string_manifest_content_loads_as_empty_not_crash(self):
        (self.pack_dir / "p1.deltamanifest.json").write_text('"just a string"')
        result = load_all_delta_manifests(self.pack_dir)
        self.assertEqual(result, {})

    def test_one_bad_manifest_does_not_prevent_loading_others(self):
        (self.pack_dir / "p1.deltamanifest.json").write_text("null")
        (self.pack_dir / "p2.deltamanifest.json").write_text('{"target_a": "base_a"}')
        result = load_all_delta_manifests(self.pack_dir)
        self.assertEqual(result, {"target_a": "base_a"})
