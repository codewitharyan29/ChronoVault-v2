"""
vault/experimental/delta.py

An rsync-style delta encoder: given a BASE and a TARGET, produce a
compact instruction stream (COPY from base / INSERT literal) that
reconstructs the target. This is the actual algorithm family real
systems use for cross-object redundancy (see EXPERIMENTAL.md /
commit log for the full theory + real-system comparison) --
implemented here, not simplified into something that only looks
similar.

=== The rolling hash ===

A "rolling" checksum can be updated in O(1) when the window slides by
one byte, instead of being recomputed from scratch (O(window size)).
This is what makes scanning an entire target file for block-sized
matches computationally feasible -- without it, finding matches would
be O(target_size * block_size) instead of O(target_size).

The classic rsync weak checksum (Adler-32-style, two accumulators):

    a = sum of bytes in the window
    b = weighted sum (each byte's contribution scaled by its position)
    checksum = (b << 16) | a

Sliding the window forward by one byte (dropping the leftmost byte
`out`, adding a new rightmost byte `in`) updates both WITHOUT
rescanning the window:

    a' = a - out + in
    b' = b - (window_size * out) + a'

This module implements exactly that update rule.

=== Instruction stream format ===

    [1 byte: opcode -- 'C' (copy) or 'I' (insert)]
    COPY:   [8 bytes: base offset][4 bytes: length]
    INSERT: [4 bytes: length][length bytes: literal data]

A delta is just a sequence of these, terminated by end-of-stream (the
consumer knows the total target length up front, from metadata stored
alongside the delta, and stops once that many bytes are reconstructed).
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Optional

BLOCK_SIZE = 64  # bytes per block for matching -- smaller finds more
# matches (better compression) at the cost of a bigger checksum table
# and more per-block overhead; this is a real tunable, not a fixed
# constant of the algorithm itself.

MOD = 1 << 16  # keep the weak checksum's accumulators bounded to 16 bits
# each, matching the classic Adler-32-style scheme's word size


def _weak_checksum(data: bytes) -> int:
    a = sum(data) % MOD
    b = sum((len(data) - i) * data[i] for i in range(len(data))) % MOD
    return (b << 16) | a


def _strong_checksum(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()[:8]  # truncated -- 8 bytes is
    # plenty to make an accidental collision, GIVEN the weak checksum
    # already matched, astronomically unlikely; this mirrors rsync's
    # own use of a truncated strong hash for the same reason


@dataclass
class BlockSignature:
    weak: int
    strong: bytes
    offset: int


def _compute_signatures(base: bytes, block_size: int = BLOCK_SIZE) -> dict:
    """
    One signature per fixed-size block of the base. Returned as
    {weak_checksum: [BlockSignature, ...]} -- a dict, not a list,
    because the whole point is O(1) average lookup by weak checksum
    while scanning the target; a list would force a linear scan per
    candidate position, defeating the purpose.
    """
    table: dict = {}
    for offset in range(0, len(base), block_size):
        block = base[offset:offset + block_size]
        weak = _weak_checksum(block)
        strong = _strong_checksum(block)
        table.setdefault(weak, []).append(BlockSignature(weak, strong, offset))
    return table


@dataclass
class CopyOp:
    base_offset: int
    length: int


@dataclass
class InsertOp:
    data: bytes


def compute_delta(base: bytes, target: bytes, block_size: int = BLOCK_SIZE) -> list:
    """
    The core matching scan, using a GENUINE O(1) rolling update -- not
    a full recomputation at every position, which would defeat the
    entire point of using a rolling checksum in the first place (an
    earlier version of this function made exactly that mistake;
    caught before shipping, fixed here, and worth being honest about
    in the commit log rather than quietly correcting it).

    Algorithm: slide a block_size-wide window through `target` one
    byte at a time. On a weak-checksum hit, verify with the strong
    checksum against every base block sharing that weak value (weak
    checksums collide by design -- that's why the second check
    exists). On a real match, emit a COPY and jump the scan forward by
    block_size, recomputing the rolling state fresh for the new
    window position (a fresh computation after a block-aligned jump is
    fine and expected -- the O(1) INCREMENTAL update specifically
    matters for the byte-by-byte NON-matching case, which is the
    common case when base and target are similar but not identical).
    """
    if len(base) == 0:
        return [InsertOp(target)] if target else []

    signatures = _compute_signatures(base, block_size)
    ops = []
    literal_buffer = bytearray()

    pos = 0
    n = len(target)

    def weak_checksum_parts(data: bytes):
        a = sum(data) % MOD
        b = sum((len(data) - i) * data[i] for i in range(len(data))) % MOD
        return a, b

    while pos < n:
        window = target[pos:pos + block_size]
        if len(window) < block_size:
            literal_buffer.extend(window)
            pos += len(window)
            break

        a, b = weak_checksum_parts(window)  # fresh computation for this
        # window -- either we just started, or we just jumped after a
        # match. From here, until the next match or jump, we roll.

        while True:
            weak = (b << 16) | a
            match: Optional[BlockSignature] = None
            for candidate in signatures.get(weak, []):
                if _strong_checksum(window) == candidate.strong:
                    match = candidate
                    break

            if match is not None:
                if literal_buffer:
                    ops.append(InsertOp(bytes(literal_buffer)))
                    literal_buffer = bytearray()
                ops.append(CopyOp(base_offset=match.offset, length=block_size))
                pos += block_size
                break  # exit the inner rolling loop -- jump to a fresh
                # window at the new position, outer while re-enters

            # No match at this position: emit ONE literal byte and
            # slide the window forward by one, updating the rolling
            # checksum in O(1) instead of recomputing from scratch --
            # THIS is the actual rolling-hash property the algorithm
            # is named for.
            literal_buffer.append(window[0])
            pos += 1
            if pos + block_size > n:
                # Not enough target bytes left for a full block --
                # drop out and let the outer loop's tail-handling
                # (window shorter than block_size) take over.
                break

            out_byte = window[0]
            in_byte = target[pos + block_size - 1]
            a = (a - out_byte + in_byte) % MOD
            b = (b - block_size * out_byte + a) % MOD
            window = target[pos:pos + block_size]

    if literal_buffer:
        ops.append(InsertOp(bytes(literal_buffer)))

    return ops


def apply_delta(base: bytes, ops: list) -> bytes:
    """Reconstruct the target from base + instruction stream. This is
    the read-path cost of delta compression: every read of a delta-
    encoded object needs its base available and needs to replay this
    reconstruction, unlike a plain stored object which is just
    decompress-and-done."""
    result = bytearray()
    for op in ops:
        if isinstance(op, CopyOp):
            result.extend(base[op.base_offset:op.base_offset + op.length])
        elif isinstance(op, InsertOp):
            result.extend(op.data)
        else:
            raise ValueError(f"unknown op type: {op!r}")
    return bytes(result)


def serialize_ops(ops: list) -> bytes:
    parts = []
    for op in ops:
        if isinstance(op, CopyOp):
            parts.append(b"C" + struct.pack(">QI", op.base_offset, op.length))
        elif isinstance(op, InsertOp):
            parts.append(b"I" + struct.pack(">I", len(op.data)) + op.data)
    return b"".join(parts)


def deserialize_ops(data: bytes) -> list:
    ops = []
    pos = 0
    while pos < len(data):
        opcode = data[pos:pos + 1]
        pos += 1
        if opcode == b"C":
            base_offset, length = struct.unpack_from(">QI", data, pos)
            pos += 12
            ops.append(CopyOp(base_offset, length))
        elif opcode == b"I":
            length = struct.unpack_from(">I", data, pos)[0]
            pos += 4
            ops.append(InsertOp(data[pos:pos + length]))
            pos += length
        else:
            raise ValueError(f"corrupt delta stream: unknown opcode {opcode!r} at position {pos-1}")
    return ops
