"""
vault/experimental/packfile.py

Bundles many loose objects into one .pack file + one .idx file,
instead of one file per object. Composes the existing ObjectStore's
public API -- vault/objects.py is never modified, not even in this
experimental fork.

=== Pack file format ===

    [4 bytes magic: b"PACK"]
    [4 bytes: object count, big-endian]
    [object 1's stored bytes -- the SAME bytes ObjectStore already
     writes to a loose file: version + encoding marker + payload]
    [object 2's stored bytes]
    ...

Objects are concatenated with no separators between them -- the index
(below) is what makes them findable, since the pack file alone has no
way to know where one object ends and the next begins without it.

=== Index file format (a real fan-out table, like Git's .idx) ===

    [256-entry fan-out table, 4 bytes each, big-endian]
        fanout[b] = number of index entries whose hash's first byte is <= b
    [N entries, SORTED by hash, each:]
        [32 bytes: hash, as raw bytes not hex -- half the size of the
         hex string, since every byte of a hash is meaningful data]
        [8 bytes: offset into the pack file, big-endian]
        [4 bytes: length in the pack file, big-endian]

Why a fan-out table: to find a hash, take its first byte B.
fanout[B-1] (or 0 if B==0) tells you where entries starting with byte
B begin in the sorted entry list; fanout[B] tells you where they end.
That's an O(1) jump to a slice roughly N/256 entries long, THEN a
binary search within just that slice -- not the whole index. This is
the actual technique Git's real .idx v2 format uses, not a simplified
stand-in for it.
"""

from __future__ import annotations

import bisect
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from vault.objects import ObjectStore

MAGIC = b"PACK"
FANOUT_ENTRIES = 256
INDEX_ENTRY_SIZE = 32 + 8 + 4  # hash + offset + length


@dataclass
class PackIndexEntry:
    obj_hash: str  # hex string, for compatibility with the rest of ChronoVault
    offset: int
    length: int


class PackIndex:
    """
    In-memory representation of a .idx file, built either fresh (by
    PackWriter) or loaded from disk (by PackReader). Handles both the
    fan-out-accelerated lookup and (de)serialization to the binary
    format described above.
    """

    def __init__(self, entries: list[PackIndexEntry]):
        # Entries MUST be sorted by hash for both the fan-out table
        # and the binary search within a fan-out slice to be valid.
        self.entries = sorted(entries, key=lambda e: e.obj_hash)
        self._fanout = self._build_fanout(self.entries)

    @staticmethod
    def _build_fanout(sorted_entries: list[PackIndexEntry]) -> list[int]:
        fanout = [0] * FANOUT_ENTRIES
        for entry in sorted_entries:
            first_byte = int(entry.obj_hash[:2], 16)
            fanout[first_byte] += 1
        # Convert per-byte counts into cumulative counts -- this is
        # what makes fanout[b] mean "entries with first byte <= b",
        # not just "entries with first byte == b".
        running_total = 0
        for b in range(FANOUT_ENTRIES):
            running_total += fanout[b]
            fanout[b] = running_total
        return fanout

    def find(self, obj_hash: str) -> Optional[PackIndexEntry]:
        first_byte = int(obj_hash[:2], 16)
        slice_start = self._fanout[first_byte - 1] if first_byte > 0 else 0
        slice_end = self._fanout[first_byte]
        # Binary search ONLY within [slice_start, slice_end) -- this
        # is the actual payoff of the fan-out table. Without it we'd
        # binary-search the full entry list every time.
        slice_hashes = [self.entries[i].obj_hash for i in range(slice_start, slice_end)]
        pos = bisect.bisect_left(slice_hashes, obj_hash)
        if pos < len(slice_hashes) and slice_hashes[pos] == obj_hash:
            return self.entries[slice_start + pos]
        return None

    def to_bytes(self) -> bytes:
        parts = [struct.pack(">I", v) for v in self._fanout]
        for entry in self.entries:
            parts.append(bytes.fromhex(entry.obj_hash))
            parts.append(struct.pack(">Q", entry.offset))
            parts.append(struct.pack(">I", entry.length))
        return b"".join(parts)

    @staticmethod
    def from_bytes(data: bytes) -> "PackIndex":
        # Wrapped in a broad try/except and re-raised as a clean,
        # project-consistent error -- found by fuzzing truncated/
        # corrupted index bytes: the unwrapped version let raw
        # struct.error escape (e.g. "unpack requires a buffer of 1024
        # bytes"), inconsistent with how objects.py/snapshot.py wrap
        # zlib.error and struct failures into clear ObjectCorruptedError
        # messages elsewhere in this codebase. Not a crash or hang --
        # struct.error is a well-defined, catchable exception -- but a
        # confusing one for a caller not expecting to catch it by name.
        try:
            fanout_size = FANOUT_ENTRIES * 4
            fanout_bytes = data[:fanout_size]
            fanout = list(struct.unpack(f">{FANOUT_ENTRIES}I", fanout_bytes))
            total_entries = fanout[-1] if fanout else 0

            entries = []
            offset = fanout_size
            for _ in range(total_entries):
                hash_bytes = data[offset:offset + 32]; offset += 32
                entry_offset = struct.unpack(">Q", data[offset:offset + 8])[0]; offset += 8
                entry_length = struct.unpack(">I", data[offset:offset + 4])[0]; offset += 4
                entries.append(PackIndexEntry(hash_bytes.hex(), entry_offset, entry_length))
        except struct.error as e:
            from vault.objects import ObjectCorruptedError
            raise ObjectCorruptedError("(pack index)", f"malformed or truncated pack index: {e}") from e

        idx = PackIndex.__new__(PackIndex)  # bypass __init__'s re-sort/re-build --
        idx.entries = entries               # entries are already correctly
        idx._fanout = fanout                # ordered and the fanout is already built
        return idx


class PackWriter:
    """
    Reads every loose object currently in an ObjectStore and writes
    them into one .pack file + one .idx file. Does NOT delete the
    loose copies -- that's a deliberate, separate, explicit step
    (see PackedObjectStore.pack_and_prune below), so there's never a
    moment where an object exists only half-migrated.
    """

    def __init__(self, store: ObjectStore, pack_dir: Path):
        self.store = store
        self.pack_dir = Path(pack_dir)
        self.pack_dir.mkdir(parents=True, exist_ok=True)

    def write_pack(self, pack_name: str) -> tuple[Path, Path]:
        pack_path = self.pack_dir / f"{pack_name}.pack"
        idx_path = self.pack_dir / f"{pack_name}.idx"

        entries = []
        with open(pack_path, "wb") as pack_file:
            pack_file.write(MAGIC)
            hashes = sorted(self.store.iter_all_hashes())  # deterministic pack contents
            pack_file.write(struct.pack(">I", len(hashes)))

            for obj_hash in hashes:
                # Read the RAW stored bytes (already version+marker+
                # payload from objects.py's own format) -- we're
                # relocating objects, not re-encoding them. This keeps
                # the pack's per-object bytes byte-identical to what a
                # loose file would contain, which matters for the
                # "does packing alone help" isolation from Design step (d).
                stored_bytes = self.store._object_path(obj_hash).read_bytes()
                offset = pack_file.tell()
                pack_file.write(stored_bytes)
                entries.append(PackIndexEntry(obj_hash, offset, len(stored_bytes)))

        index = PackIndex(entries)
        idx_path.write_bytes(index.to_bytes())
        return pack_path, idx_path


class PackReader:
    """
    Opens an existing .pack + .idx pair for reading. Loads the index
    into memory (cheap -- ~44 bytes/entry, so even 100,000 objects is
    only ~4.4MB) but does NOT load the pack file itself into memory --
    reads seek directly to the needed offset, so pack size doesn't
    bound memory usage the way loading everything would.
    """

    def __init__(self, pack_path: Path, idx_path: Path):
        self.pack_path = Path(pack_path)
        self.index = PackIndex.from_bytes(Path(idx_path).read_bytes())

    def has(self, obj_hash: str) -> bool:
        return self.index.find(obj_hash) is not None

    def read_raw(self, obj_hash: str) -> Optional[bytes]:
        """Returns the raw stored bytes (version+marker+payload) for
        an object, or None if this pack doesn't contain it. Caller is
        responsible for decompression/verification -- same contract
        ObjectStore.get() implements for loose objects, kept separate
        here so this module stays focused on packing, not duplicating
        objects.py's decode logic."""
        entry = self.index.find(obj_hash)
        if entry is None:
            return None
        with open(self.pack_path, "rb") as f:
            f.seek(entry.offset)
            return f.read(entry.length)


def _decode_stored_bytes(obj_hash: str, stored_bytes: bytes):
    """
    DUPLICATED from ObjectStore.get()'s decode logic -- see this
    module's docstring note on why. objects.py isn't modified to
    expose this as a shared helper, so this experimental module
    re-implements the same version-byte + marker-byte + decompress +
    hash-verify sequence independently. This is a real maintenance
    hazard: the two implementations must be kept in sync by hand.
    """
    import zlib

    from vault.objects import (
        FORMAT_VERSION,
        MARKER_COMPRESSED,
        MARKER_RAW,
        ObjectCorruptedError,
        hash_bytes,
    )

    if len(stored_bytes) < 2:
        raise ObjectCorruptedError(obj_hash, "object too short (missing version/format header)")
    version, marker, payload = stored_bytes[:1], stored_bytes[1:2], stored_bytes[2:]
    if version != FORMAT_VERSION:
        raise ObjectCorruptedError(obj_hash, f"unsupported format version: {version!r}")
    if marker == MARKER_COMPRESSED:
        try:
            data = zlib.decompress(payload)
        except zlib.error as e:
            raise ObjectCorruptedError(obj_hash, f"decompression failed: {e}") from e
    elif marker == MARKER_RAW:
        data = payload
    else:
        raise ObjectCorruptedError(obj_hash, f"unknown format marker: {marker!r}")

    if hash_bytes(data) != obj_hash:
        raise ObjectCorruptedError(obj_hash, "hash mismatch -- packed content does not match its id")
    return data


class PackedObjectStore:
    """
    The multi-tier read path from Design step (b): checks loose
    objects first, then each pack in turn. Composes the real
    ObjectStore rather than replacing it -- loose reads/writes still
    go through the original, tested, unmodified objects.py code.
    """

    def __init__(self, store: ObjectStore, pack_dir: Path):
        self.store = store
        self.pack_dir = Path(pack_dir)
        self._readers: list[PackReader] = []
        self._load_existing_packs()

    def _load_existing_packs(self) -> None:
        self._readers = []
        if not self.pack_dir.exists():
            return
        for pack_path in sorted(self.pack_dir.glob("*.pack")):
            idx_path = pack_path.with_suffix(".idx")
            if idx_path.exists():
                self._readers.append(PackReader(pack_path, idx_path))

    def get(self, obj_hash: str) -> bytes:
        # Tier 1: loose objects -- the fast, simple, already-correct path.
        if self.store.has(obj_hash):
            return self.store.get(obj_hash)
        # Tier 2: check each pack. In the worst case (object in the
        # LAST pack checked) this is O(packs), which is the real,
        # permanent read-side cost this design accepts.
        for reader in self._readers:
            raw = reader.read_raw(obj_hash)
            if raw is not None:
                return _decode_stored_bytes(obj_hash, raw)
        from vault.objects import ObjectNotFoundError
        raise ObjectNotFoundError(obj_hash)

    def pack_and_prune(self, pack_name: str) -> int:
        """
        The explicit, deliberate packing operation from Design step
        (a): write everything currently loose into a new pack, verify
        every object is readable back OUT of the pack, and only THEN
        delete the loose copies. Returns the number of objects packed.
        """
        writer = PackWriter(self.store, self.pack_dir)
        pack_path, idx_path = writer.write_pack(pack_name)

        # Verification before deletion -- this is the step that makes
        # pruning safe. A pack that silently failed to write some
        # object correctly must never result in that object's only
        # copy (the loose one) being deleted.
        reader = PackReader(pack_path, idx_path)
        loose_hashes = list(self.store.iter_all_hashes())
        for obj_hash in loose_hashes:
            raw = reader.read_raw(obj_hash)
            if raw is None:
                raise RuntimeError(f"pack verification failed: {obj_hash} missing from pack")
            _decode_stored_bytes(obj_hash, raw)  # raises if corrupt -- verify(), not just presence

        for obj_hash in loose_hashes:
            self.store.delete(obj_hash)

        self._load_existing_packs()
        return len(loose_hashes)
