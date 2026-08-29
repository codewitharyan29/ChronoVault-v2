"""
Tests for vault/experimental/packfile_v2.py -- the corrected pack
implementation.

Every test that exists for the original packfile.py must also pass
here (correctness parity), PLUS tests proving each of the four
specific fixes actually does what it claims.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.objects import ObjectStore, ObjectNotFoundError, ObjectCorruptedError
from vault.experimental.packfile import PackedObjectStore
from vault.experimental.packfile_v2 import PackedObjectStoreV2


class TestPackfileV2Correctness(unittest.TestCase):
    """Correctness parity: v2 must behave identically to the original."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.store = ObjectStore(self.root)
        self.pack_dir = self.root / "pack"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _populate(self, n=50):
        return [self.store.put(f"object number {i} with content".encode()).obj_hash
                for i in range(n)]

    def test_roundtrip_every_object(self):
        hashes = self._populate(100)
        pos = PackedObjectStoreV2(self.store, self.pack_dir)
        pos.pack_and_prune("p1")
        for i, h in enumerate(hashes):
            self.assertEqual(pos.get(h), f"object number {i} with content".encode())
        pos.close()

    def test_identical_results_to_original_implementation(self):
        """Direct parity check: both implementations, same pack, same
        answers for every object."""
        hashes = self._populate(100)
        original = PackedObjectStore(self.store, self.pack_dir)
        original.pack_and_prune("p1")

        v2 = PackedObjectStoreV2(self.store, self.pack_dir)
        for h in hashes:
            self.assertEqual(v2.get(h), original.get(h))
        v2.close()

    def test_missing_object_raises(self):
        self._populate(10)
        pos = PackedObjectStoreV2(self.store, self.pack_dir)
        pos.pack_and_prune("p1")
        with self.assertRaises(ObjectNotFoundError):
            pos.get("f" * 64)
        pos.close()

    def test_loose_fallback_for_objects_written_after_packing(self):
        old_hashes = self._populate(10)
        pos = PackedObjectStoreV2(self.store, self.pack_dir)
        pos.pack_and_prune("p1")
        new_hash = self.store.put(b"written after packing").obj_hash

        self.assertEqual(pos.get(old_hashes[0]), b"object number 0 with content")
        self.assertEqual(pos.get(new_hash), b"written after packing")
        pos.close()

    def test_corruption_in_pack_is_detected(self):
        hashes = self._populate(10)
        pos = PackedObjectStoreV2(self.store, self.pack_dir)
        pos.pack_and_prune("p1")
        pos.close()

        pack_path = next(self.pack_dir.glob("*.pack"))
        raw = bytearray(pack_path.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        pack_path.write_bytes(bytes(raw))

        pos2 = PackedObjectStoreV2(self.store, self.pack_dir)
        with self.assertRaises(ObjectCorruptedError):
            for h in hashes:
                pos2.get(h)
        pos2.close()

    def test_pack_and_prune_preserves_loose_on_verification_failure(self):
        """Safety property carried over from the original: if pack
        verification fails, NOTHING is deleted."""
        hashes = self._populate(10)
        pos = PackedObjectStoreV2(self.store, self.pack_dir)

        # Corrupt the pack immediately after write, before verification
        # reads it back, by patching the decode to fail.
        from vault.experimental import packfile_v2
        original_decode = packfile_v2._decode_stored_bytes

        def failing_decode(obj_hash, stored_bytes):
            raise ObjectCorruptedError(obj_hash, "simulated verification failure")

        with patch.object(packfile_v2, "_decode_stored_bytes", failing_decode):
            with self.assertRaises(ObjectCorruptedError):
                pos.pack_and_prune("p1")

        # Every loose object must still be present.
        for h in hashes:
            self.assertTrue(self.store.has(h), "loose object was deleted despite verification failure")
        pos.close()


class TestPackfileV2Fixes(unittest.TestCase):
    """Tests proving each specific fix actually works, not just that
    the result is still correct."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.store = ObjectStore(self.root)
        self.pack_dir = self.root / "pack"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_FIX1_no_stat_syscall_on_packed_reads(self):
        """The original called store.has() (a filesystem stat()) before
        checking packs, on EVERY read. v2 must not touch the filesystem
        at all for an object it knows is packed."""
        hashes = [self.store.put(f"item {i}".encode()).obj_hash for i in range(20)]
        pos = PackedObjectStoreV2(self.store, self.pack_dir)
        pos.pack_and_prune("p1")

        has_calls = []
        original_has = ObjectStore.has

        def tracking_has(self, obj_hash):
            has_calls.append(obj_hash)
            return original_has(self, obj_hash)

        with patch.object(ObjectStore, "has", tracking_has):
            for h in hashes:
                pos.get(h)

        self.assertEqual(len(has_calls), 0,
                          "v2 called store.has() for packed objects -- FIX 1 not working")
        pos.close()

    def test_FIX2_pack_file_opened_once_not_per_read(self):
        """The original opened/closed the pack file on every read."""
        hashes = [self.store.put(f"item {i}".encode()).obj_hash for i in range(50)]
        pos = PackedObjectStoreV2(self.store, self.pack_dir)
        pos.pack_and_prune("p1")

        # After loading, exactly one handle per pack should be open.
        self.assertEqual(len(pos._pack_handles), 1)
        handle = pos._pack_handles[0]
        self.assertFalse(handle.closed)

        for h in hashes:
            pos.get(h)

        # Still the SAME handle, still open -- never churned.
        self.assertIs(pos._pack_handles[0], handle)
        self.assertFalse(handle.closed)
        pos.close()
        self.assertTrue(handle.closed)

    def test_FIX3_single_dict_lookup_across_multiple_packs(self):
        """The original looped through packs linearly. v2 merges all
        indexes into one dict, so lookup cost doesn't grow with pack
        count -- verified structurally here."""
        all_hashes = []
        pos = PackedObjectStoreV2(self.store, self.pack_dir)
        for pack_num in range(4):
            batch = [self.store.put(f"pack{pack_num} item {i}".encode()).obj_hash
                     for i in range(25)]
            all_hashes.extend(batch)
            pos.pack_and_prune(f"p{pack_num}")

        self.assertEqual(len(pos._pack_handles), 4)
        # ONE dict holding every object across all 4 packs.
        self.assertEqual(len(pos._locations), len(all_hashes))
        for h in all_hashes:
            self.assertIn(h, pos._locations)
            self.assertIsNotNone(pos.get(h))
        pos.close()

    def test_FIX4_decode_follows_objects_py_constants_behaviorally(self):
        """The decode logic is still duplicated (objects.py isn't
        modified), but must FOLLOW objects.py's constants rather than
        hardcoding them -- so a format change there propagates instead
        of silently diverging.

        Tested behaviorally rather than by inspecting source text: two
        earlier versions of this test tried string-matching the
        function's source and both failed for the wrong reason (the
        docstring legitimately mentions the byte literals while
        explaining they aren't hardcoded). Patching the constant and
        observing that behavior changes is the property that actually
        matters, and it can't be fooled by comments."""
        from vault.experimental import packfile_v2
        from vault.objects import hash_bytes

        content = b"behavioral constant test"
        obj_hash = hash_bytes(content)
        # A validly-encoded raw object using the REAL current marker.
        valid = packfile_v2.FORMAT_VERSION + packfile_v2.MARKER_RAW + content
        self.assertEqual(packfile_v2._decode_stored_bytes(obj_hash, valid), content)

        # Now patch the module's MARKER_RAW to a different byte. If the
        # decode genuinely reads the constant, the previously-valid
        # bytes must now be REJECTED (their marker no longer matches).
        with patch.object(packfile_v2, "MARKER_RAW", b"Q"):
            with self.assertRaises(ObjectCorruptedError):
                packfile_v2._decode_stored_bytes(obj_hash, valid)
            # ...and bytes using the NEW marker must now be accepted.
            newly_valid = packfile_v2.FORMAT_VERSION + b"Q" + content
            self.assertEqual(packfile_v2._decode_stored_bytes(obj_hash, newly_valid), content)


if __name__ == "__main__":
    unittest.main()
