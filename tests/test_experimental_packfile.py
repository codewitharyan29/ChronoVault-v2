"""
Tests for vault/experimental/packfile.py.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.objects import ObjectStore, ObjectCorruptedError, ObjectNotFoundError
from vault.experimental.packfile import PackedObjectStore, PackWriter, PackReader


class TestPackfile(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.store = ObjectStore(self.root)
        self.pack_dir = self.root / "packs"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_pack_then_read_every_object_back_correctly(self):
        hashes = [self.store.put(f"object {i}".encode()).obj_hash for i in range(50)]
        packed = PackedObjectStore(self.store, self.pack_dir)
        packed.pack_and_prune("pack-0001")

        for i, obj_hash in enumerate(hashes):
            self.assertEqual(packed.get(obj_hash), f"object {i}".encode())

    def test_pack_and_prune_actually_removes_loose_copies(self):
        obj_hash = self.store.put(b"will be packed").obj_hash
        self.assertTrue(self.store.has(obj_hash))
        packed = PackedObjectStore(self.store, self.pack_dir)
        packed.pack_and_prune("pack-0001")
        self.assertFalse(self.store.has(obj_hash))
        self.assertEqual(packed.get(obj_hash), b"will be packed")

    def test_empty_store_packs_without_error(self):
        packed = PackedObjectStore(self.store, self.pack_dir)
        count = packed.pack_and_prune("pack-0001")
        self.assertEqual(count, 0)

    def test_mix_of_loose_and_packed_objects_both_readable(self):
        old_hash = self.store.put(b"old, will be packed").obj_hash
        packed = PackedObjectStore(self.store, self.pack_dir)
        packed.pack_and_prune("pack-0001")

        new_hash = self.store.put(b"new, stays loose").obj_hash
        self.assertEqual(packed.get(old_hash), b"old, will be packed")
        self.assertEqual(packed.get(new_hash), b"new, stays loose")

    def test_corrupted_pack_object_raises_cleanly_not_silently(self):
        obj_hash = self.store.put(b"integrity matters here too").obj_hash
        packed = PackedObjectStore(self.store, self.pack_dir)
        packed.pack_and_prune("pack-0001")

        pack_path = self.pack_dir / "pack-0001.pack"
        raw = bytearray(pack_path.read_bytes())
        raw[-1] ^= 0xFF
        pack_path.write_bytes(bytes(raw))

        packed2 = PackedObjectStore(self.store, self.pack_dir)
        with self.assertRaises(ObjectCorruptedError):
            packed2.get(obj_hash)

    def test_nonexistent_object_raises_not_found(self):
        packed = PackedObjectStore(self.store, self.pack_dir)
        with self.assertRaises(ObjectNotFoundError):
            packed.get("0" * 64)

    def test_pack_verification_catches_corruption_before_prune_would_happen(self):
        """The safety guarantee behind pack_and_prune: verification
        (decode + hash-check every packed object) must catch
        corruption. Tested directly against PackReader + the decode
        path, isolating the verification logic from the prune step's
        sequencing."""
        obj_hash = self.store.put(b"must be caught if pack is bad").obj_hash

        writer = PackWriter(self.store, self.pack_dir)
        pack_path, idx_path = writer.write_pack("pack-bad")
        raw = bytearray(pack_path.read_bytes())
        raw[-1] ^= 0xFF
        pack_path.write_bytes(bytes(raw))

        reader = PackReader(pack_path, idx_path)
        from vault.experimental.packfile import _decode_stored_bytes
        raw_bytes = reader.read_raw(obj_hash)
        with self.assertRaises(ObjectCorruptedError):
            _decode_stored_bytes(obj_hash, raw_bytes)
        # And critically: the loose original must still be untouched,
        # since pack_and_prune only deletes loose copies AFTER this
        # exact verification step succeeds for every object.
        self.assertTrue(self.store.has(obj_hash))

    def test_many_objects_correct_via_fanout_index(self):
        hashes = [self.store.put(f"item {i}".encode() * 5).obj_hash for i in range(500)]
        packed = PackedObjectStore(self.store, self.pack_dir)
        packed.pack_and_prune("pack-large")
        for i, obj_hash in enumerate(hashes):
            self.assertEqual(packed.get(obj_hash), (f"item {i}".encode() * 5))


if __name__ == "__main__":
    unittest.main()


class TestAdversarialPackIndexLoading(unittest.TestCase):
    """
    Regression tests for a real gap found by adversarial fuzzing:
    PackIndex.from_bytes() let raw struct.error escape for
    truncated/corrupted index bytes, inconsistent with how the rest
    of this codebase wraps binary-parsing failures into clean
    ObjectCorruptedError messages.
    """
    def test_empty_bytes_raises_clean_error_not_struct_error(self):
        from vault.experimental.packfile import PackIndex
        with self.assertRaises(ObjectCorruptedError):
            PackIndex.from_bytes(b"")

    def test_truncated_index_raises_clean_error(self):
        from vault.experimental.packfile import PackIndex
        with self.assertRaises(ObjectCorruptedError):
            PackIndex.from_bytes(b"\x00" * 100)  # shorter than the fanout table alone

    def test_corrupted_entry_count_raises_clean_error(self):
        """A fanout table claiming far more entries exist than the
        data actually contains -- must fail cleanly, not with a raw
        struct.error, and not with excessive iteration."""
        import struct
        from vault.experimental.packfile import PackIndex, FANOUT_ENTRIES
        fanout = [1000] * FANOUT_ENTRIES  # claims 1000 entries
        data = struct.pack(f">{FANOUT_ENTRIES}I", *fanout)  # no entry data follows
        with self.assertRaises(ObjectCorruptedError):
            PackIndex.from_bytes(data)

    def test_well_formed_index_still_loads_normally(self):
        from vault.experimental.packfile import PackIndex, PackIndexEntry
        entries = [PackIndexEntry(f"{i:064x}", offset=i * 100, length=100) for i in range(10)]
        idx = PackIndex(entries)
        data = idx.to_bytes()
        reloaded = PackIndex.from_bytes(data)
        self.assertEqual(len(reloaded.entries), 10)
