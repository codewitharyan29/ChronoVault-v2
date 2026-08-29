"""
Regression tests for vault/objects.py — the content-addressable
object store. Covers roundtrip storage/retrieval, whole-file
deduplication, compression, corruption detection (both at the
decompression layer and the hash-verification layer), and object
iteration/deletion for garbage collection.

Run with: python -m unittest tests.test_objects -v
(stdlib unittest only — no pytest, keeps the project zero-dependency
even for its dev-only test tooling.)
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.objects import ObjectCorruptedError, ObjectStore, hash_bytes


class TestObjectStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.store = ObjectStore(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_put_and_get_roundtrip(self):
        data = b"hello chronovault, this is a test blob"
        stat = self.store.put(data)
        self.assertTrue(stat.is_new)
        self.assertEqual(stat.obj_hash, hash_bytes(data))
        self.assertEqual(self.store.get(stat.obj_hash), data)

    def test_dedup_on_identical_content(self):
        data = b"duplicate me" * 1000
        first = self.store.put(data)
        second = self.store.put(data)
        self.assertTrue(first.is_new)
        self.assertFalse(second.is_new)
        self.assertEqual(first.obj_hash, second.obj_hash)

    def test_compression_actually_shrinks_repetitive_data(self):
        data = b"a" * 100_000  # highly compressible
        stat = self.store.put(data)
        self.assertLess(stat.compressed_size, stat.original_size)

    def test_verify_detects_intact_object(self):
        data = b"integrity check me"
        stat = self.store.put(data)
        self.assertTrue(self.store.verify_object(stat.obj_hash))

    def test_verify_detects_corruption(self):
        data = b"corrupt me later"
        stat = self.store.put(data)
        path = self.store._object_path(stat.obj_hash)
        # Simulate on-disk corruption by flipping a byte.
        raw = bytearray(path.read_bytes())
        raw[0] ^= 0xFF
        path.write_bytes(bytes(raw))
        self.assertFalse(self.store.verify_object(stat.obj_hash))

    def test_get_raises_on_corruption_instead_of_returning_bad_data(self):
        """
        get() is a strict read: it must never silently hand back
        corrupted content. Whether the corruption breaks decompression
        (zlib error) or produces valid-but-wrong bytes (hash mismatch),
        get() should raise either way rather than returning something.
        """
        data = b"integrity matters" * 50
        stat = self.store.put(data)
        path = self.store._object_path(stat.obj_hash)
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 0xFF  # flip a byte near the end — more likely to still decompress
        path.write_bytes(bytes(raw))
        with self.assertRaises(ObjectCorruptedError):
            self.store.get(stat.obj_hash)

    def test_tiny_incompressible_data_stored_smaller_than_naive_compression(self):
        """
        A short, non-repetitive blob: zlib's own header/footer overhead
        would make naive compression LARGER than the original. The
        store should fall back to raw storage (+1 marker byte) instead.
        """
        data = b"x9k2"  # 4 bytes, nothing for zlib to compress
        stat = self.store.put(data)
        # 2 header bytes (version + marker) + 4 raw bytes = 6, still
        # smaller than zlib's ~11+ byte header/footer overhead would add.
        self.assertEqual(stat.compressed_size, len(data) + 2)
        self.assertEqual(self.store.get(stat.obj_hash), data)

    def test_iter_all_hashes(self):
        h1 = self.store.put(b"one").obj_hash
        h2 = self.store.put(b"two").obj_hash
        self.assertEqual(set(self.store.iter_all_hashes()), {h1, h2})

    def test_delete_removes_object(self):
        stat = self.store.put(b"temporary")
        self.assertTrue(self.store.has(stat.obj_hash))
        self.store.delete(stat.obj_hash)
        self.assertFalse(self.store.has(stat.obj_hash))


if __name__ == "__main__":
    unittest.main()

class TestAdversarialHashValidation(unittest.TestCase):
    """
    Regression tests for a real bug found by adversarial fuzzing: an
    empty (or otherwise malformed) hash string made _object_path()
    resolve to the objects directory itself (pathlib silently drops
    empty path components), causing get() to call read_bytes() on a
    DIRECTORY and raise an uncaught IsADirectoryError instead of a
    clean, expected error.
    """
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.store = ObjectStore(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_empty_hash_get_raises_clean_error_not_isadirectoryerror(self):
        with self.assertRaises(ObjectCorruptedError):
            self.store.get("")

    def test_empty_hash_has_returns_false_not_raises(self):
        # has() must keep its existing never-raises contract -- it's
        # called throughout the codebase as a plain boolean guard.
        self.assertFalse(self.store.has(""))

    def test_malformed_hex_hash_get_raises_clean_error(self):
        with self.assertRaises(ObjectCorruptedError):
            self.store.get("z" * 64)

    def test_wrong_length_hash_get_raises_clean_error(self):
        with self.assertRaises(ObjectCorruptedError):
            self.store.get("abc123")

    def test_malformed_hash_has_never_raises(self):
        for bad_hash in ["", "z" * 64, "abc", "a" * 200, "𝕏" * 64]:
            with self.subTest(bad_hash=repr(bad_hash)):
                self.assertFalse(self.store.has(bad_hash))

    def test_well_formed_hash_still_works_normally(self):
        stat = self.store.put(b"normal content")
        self.assertTrue(self.store.has(stat.obj_hash))
        self.assertEqual(self.store.get(stat.obj_hash), b"normal content")
