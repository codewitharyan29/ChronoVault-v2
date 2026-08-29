"""
Tests for vault/experimental/delta.py.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.experimental.delta import (
    compute_delta, apply_delta, serialize_ops, deserialize_ops,
    CopyOp, InsertOp, BLOCK_SIZE,
)


class TestDeltaCorrectness(unittest.TestCase):
    def _roundtrip(self, base: bytes, target: bytes):
        ops = compute_delta(base, target)
        reconstructed = apply_delta(base, ops)
        self.assertEqual(reconstructed, target,
                          f"roundtrip failed: base_len={len(base)}, target_len={len(target)}")
        return ops

    def test_identical_files_produce_all_copy(self):
        data = b"x" * (BLOCK_SIZE * 10)  # exact multiple of BLOCK_SIZE --
        # see test_identical_files_with_non_aligned_tail below for the
        # honest documentation of what happens when it ISN'T a multiple.
        ops = self._roundtrip(data, data)
        self.assertTrue(all(isinstance(op, CopyOp) for op in ops))

    def test_identical_files_with_non_aligned_tail_still_correct_but_not_all_copy(self):
        """A REAL, honest limitation of fixed-block matching, not a
        bug: if the data length isn't a multiple of BLOCK_SIZE, the
        trailing partial block can never form a complete matchable
        block -- even though it's byte-identical to the base, it
        becomes a literal InsertOp. Reconstruction is still perfectly
        correct (that's what matters for correctness); the delta is
        just slightly less efficient than it theoretically could be.
        This is part of why real systems use variable-size,
        content-defined chunk boundaries instead of fixed offsets --
        see feature #4."""
        data = b"x" * 1000  # NOT a multiple of BLOCK_SIZE (64)
        ops = self._roundtrip(data, data)
        self.assertFalse(all(isinstance(op, CopyOp) for op in ops))
        self.assertIsInstance(ops[-1], InsertOp)  # the misaligned tail

    def test_completely_different_files_still_roundtrips(self):
        import os
        base = os.urandom(1000)
        target = os.urandom(1000)
        self._roundtrip(base, target)

    def test_empty_base(self):
        self._roundtrip(b"", b"some target content")

    def test_empty_target(self):
        self._roundtrip(b"some base content", b"")

    def test_both_empty(self):
        self._roundtrip(b"", b"")

    def test_single_line_change_in_middle(self):
        base = ("line one\n" * 20 + "THE ORIGINAL MIDDLE LINE\n" + "line three\n" * 20).encode()
        target = ("line one\n" * 20 + "A COMPLETELY DIFFERENT MIDDLE LINE\n" + "line three\n" * 20).encode()
        self._roundtrip(base, target)

    def test_insertion_at_start(self):
        base = b"the rest of the file stays the same " * 10
        target = b"NEW STUFF AT THE START " + base
        self._roundtrip(base, target)

    def test_insertion_at_end(self):
        base = b"the rest of the file stays the same " * 10
        target = base + b" NEW STUFF AT THE END"
        self._roundtrip(base, target)

    def test_deletion_from_middle(self):
        base = b"AAAA" * 20 + b"BBBB" * 20 + b"CCCC" * 20
        target = b"AAAA" * 20 + b"CCCC" * 20
        self._roundtrip(base, target)

    def test_target_shorter_than_one_block(self):
        base = b"x" * 1000
        target = b"short"
        self.assertLess(len(target), BLOCK_SIZE)
        self._roundtrip(base, target)

    def test_base_shorter_than_one_block(self):
        base = b"short"
        target = b"x" * 1000
        self.assertLess(len(base), BLOCK_SIZE)
        self._roundtrip(base, target)

    def test_repeated_pattern_that_could_confuse_weak_checksum(self):
        """Weak checksums CAN collide between genuinely different
        blocks -- this specifically tests that the strong-checksum
        verification step catches a weak-checksum collision rather
        than emitting a wrong COPY."""
        base = (b"AB" * 32) + (b"BA" * 32)
        target = (b"BA" * 32) + (b"AB" * 32)
        self._roundtrip(base, target)

    def test_serialize_deserialize_roundtrip(self):
        base = b"hello world " * 50
        target = b"hello there " * 50
        ops = compute_delta(base, target)
        serialized = serialize_ops(ops)
        restored_ops = deserialize_ops(serialized)
        reconstructed = apply_delta(base, restored_ops)
        self.assertEqual(reconstructed, target)

    def test_corrupted_delta_stream_raises_not_silently_wrong(self):
        with self.assertRaises(ValueError):
            deserialize_ops(b"Q" + b"garbage")


if __name__ == "__main__":
    unittest.main()
