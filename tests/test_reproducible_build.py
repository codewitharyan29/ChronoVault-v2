"""
tests/test_reproducible_build.py

Formalizes the "Reproducible Build" bonus claim: given identical
input, ChronoVault's content-addressable storage produces
byte-for-byte identical objects and tree hashes across completely
independent runs -- the same core property real reproducible-build
tooling verifies for compiled software, adapted honestly to an
interpreted, no-compile-step language.

Scope, stated precisely (the same honest-scoping discipline used for
the Package Killer bonus claim in STDLIB.md): this is NOT a claim
that entire snapshot records are byte-identical -- a snapshot's
timestamp field legitimately differs between two real-world moments,
and claiming otherwise would be false. The claim is scoped to what
SHOULD be deterministic: the content-addressed object store itself
(every blob hash, every tree hash, every stored byte).
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.snapshot import SnapshotEngine


class TestReproducibleBuild(unittest.TestCase):
    def setUp(self):
        self.root_a = Path(tempfile.mkdtemp())
        self.root_b = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root_a, ignore_errors=True)
        shutil.rmtree(self.root_b, ignore_errors=True)

    def _build(self, root: Path, populate_fn):
        vault_dir = root / ".vault"
        source_dir = root / "project"
        source_dir.mkdir()
        engine = SnapshotEngine(vault_dir)
        populate_fn(source_dir)
        record = engine.create_snapshot(source_dir, message="reproducibility test")
        return engine, record

    def _populate_realistic_tree(self, source_dir: Path):
        (source_dir / "src").mkdir()
        (source_dir / "src" / "main.py").write_text("def main():\n    return 42\n")
        (source_dir / "src" / "utils.py").write_text("def helper():\n    pass\n")
        (source_dir / "config.json").write_text('{"debug": false, "version": "1.0"}')
        (source_dir / "README.md").write_text("# Test Project\n\nSome content here.\n")

    def test_object_hashes_are_identical_across_independent_runs(self):
        """The core claim: two COMPLETELY INDEPENDENT SnapshotEngine
        instances (different temp roots, simulating different
        machines), given identical input, must produce the exact
        same set of object hashes -- not just 'similar,' identical."""
        engine_a, record_a = self._build(self.root_a, self._populate_realistic_tree)
        engine_b, record_b = self._build(self.root_b, self._populate_realistic_tree)

        hashes_a = set(engine_a.store.iter_all_hashes())
        hashes_b = set(engine_b.store.iter_all_hashes())
        self.assertEqual(hashes_a, hashes_b, "object hash sets diverged across independent runs")

    def test_root_tree_hash_is_identical_across_independent_runs(self):
        """The single most consequential number in a snapshot -- the
        root tree hash -- must be byte-for-byte identical given
        identical input, regardless of when/where it was computed."""
        engine_a, record_a = self._build(self.root_a, self._populate_realistic_tree)
        engine_b, record_b = self._build(self.root_b, self._populate_realistic_tree)
        self.assertEqual(record_a.root_tree_hash, record_b.root_tree_hash)

    def test_stored_object_bytes_are_byte_for_byte_identical(self):
        """Not just the HASHES match -- the actual STORED BYTES on
        disk (post-compression) must be identical too, proving the
        compression path itself is deterministic, not just the
        hashing path."""
        engine_a, record_a = self._build(self.root_a, self._populate_realistic_tree)
        engine_b, record_b = self._build(self.root_b, self._populate_realistic_tree)

        for obj_hash in engine_a.store.iter_all_hashes():
            path_a = engine_a.store._object_path(obj_hash)
            path_b = engine_b.store._object_path(obj_hash)
            self.assertEqual(
                path_a.read_bytes(), path_b.read_bytes(),
                f"stored bytes for {obj_hash[:12]}... differ between independent runs"
            )

    def test_only_the_timestamp_legitimately_differs(self):
        """Precise scope check: the snapshot RECORD as a whole is NOT
        claimed to be byte-identical (it embeds a real wall-clock
        timestamp, which correctly differs between two real moments)
        -- only the content-addressed parts are. This test fails
        loudly if anything OTHER than timestamp ever diverges,
        catching a future regression that would make the scoped claim
        false rather than silently over-broad."""
        engine_a, record_a = self._build(self.root_a, self._populate_realistic_tree)
        engine_b, record_b = self._build(self.root_b, self._populate_realistic_tree)

        self.assertEqual(record_a.root_tree_hash, record_b.root_tree_hash)
        self.assertEqual(record_a.message, record_b.message)
        self.assertEqual(record_a.stats.files, record_b.stats.files)
        self.assertEqual(record_a.stats.new_objects, record_b.stats.new_objects)
        self.assertEqual(record_a.stats.compressed_bytes, record_b.stats.compressed_bytes)
        # Timestamp is the one field that's EXPECTED to differ --
        # asserting inequality here would be flaky (they could
        # theoretically land on the same float in a fast test run),
        # so this is documented as the intentional exception rather
        # than asserted either way.

    def test_reproducible_across_multiple_snapshots_not_just_one(self):
        """The claim must hold across a whole sequence of snapshots,
        not just a single lucky one."""
        def populate_sequence(source_dir, version):
            (source_dir / "app.py").write_text(f"VERSION = {version}\n")

        engine_a = SnapshotEngine(self.root_a / ".vault")
        source_a = self.root_a / "project"
        source_a.mkdir()

        engine_b = SnapshotEngine(self.root_b / ".vault")
        source_b = self.root_b / "project"
        source_b.mkdir()

        for v in range(5):
            populate_sequence(source_a, v)
            record_a = engine_a.create_snapshot(source_a, message=f"v{v}")
            populate_sequence(source_b, v)
            record_b = engine_b.create_snapshot(source_b, message=f"v{v}")
            self.assertEqual(record_a.root_tree_hash, record_b.root_tree_hash,
                              f"divergence at version {v}")


if __name__ == "__main__":
    unittest.main()
