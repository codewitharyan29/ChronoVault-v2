"""
vault/objects.py

The content-addressable object store at the heart of ChronoVault.

Design (see ARCHITECTURE.md for the full writeup):
  - Every piece of content (a file's bytes, a tree listing, a snapshot
    record) is stored as an "object": SHA-256(content) -> compressed bytes.
  - Objects are IMMUTABLE. Once written, an object at a given hash never
    changes — that's what makes whole-file deduplication safe and free
    (if the hash already exists on disk, we just skip the write).
  - Crash safety comes from ATOMIC RENAME, not a write-ahead log:
    we write to a temp file in the same directory, then os.replace()
    it into place. os.replace() is atomic on POSIX and Windows, so a
    reader can never observe a half-written object. There is no
    "in-progress" state to recover from after a crash, because a
    partially-written temp file is simply never renamed into the
    object store and is invisible to every other operation.
  - No WAL. The object store is append-only and immutable, so a WAL
    (built to protect in-place mutations) solves a problem this
    system doesn't have. Documented explicitly in STDLIB.md.

Object types (see snapshot.py for how these compose):
  - blob:     raw file contents
  - tree:     a directory listing (name -> object hash, plus mode/type)
  - snapshot: a point-in-time root tree + metadata + parent pointer
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

HASH_ALGO = "sha256"
COMPRESS_LEVEL = 6  # zlib default; good speed/ratio tradeoff for a hackathon-scale repo

# On-disk format: [1 byte format version][1 byte encoding marker][payload].
# zlib adds a small header/footer (~11 bytes) to every compressed blob,
# which means compression can make TINY objects (a short config file, a
# demo-sized snippet) larger than the original, not smaller. Rather
# than accept that rounding-error-looking "storage saved: -46%" on
# small files, each object independently picks whichever representation
# is actually smaller and records which one it used.
#
# The version byte costs one extra byte per object today but means a
# future format change (e.g. adding a new encoding) can be introduced
# without breaking the ability to read objects written by this version —
# get() can branch on FORMAT_VERSION before interpreting the rest.
# Documented in STDLIB.md and FORMAT.md.
FORMAT_VERSION = b"\x01"
MARKER_COMPRESSED = b"Z"
MARKER_RAW = b"R"


class VaultError(Exception):
    """Base class for all ChronoVault errors — lets the CLI layer do
    a single `except VaultError as e: print(e)` instead of listing
    every specific exception type."""


class ObjectError(VaultError):
    """Base class for object-store-level errors."""


class RestoreError(VaultError):
    """Base class for restore-operation errors — e.g. aborting because
    a needed object failed integrity verification."""


class ObjectNotFoundError(ObjectError):
    """Raised when an object hash isn't present in the store."""

    def __init__(self, obj_hash: str):
        self.obj_hash = obj_hash
        super().__init__(
            f"Object {obj_hash[:12]}... not found in the object store.\n"
            f"The repository may be corrupted — try running 'vault verify'."
        )


class ObjectCorruptedError(ObjectError):
    """Raised when an object's stored bytes fail to decompress or hash-check."""

    def __init__(self, obj_hash: str, reason: str):
        self.obj_hash = obj_hash
        self.reason = reason
        super().__init__(
            f"Object {obj_hash[:12]}... is corrupted ({reason}).\n"
            f"Run 'vault verify' for a full integrity report."
        )


def hash_bytes(data: bytes) -> str:
    """Return the hex digest used as this content's object id."""
    return hashlib.new(HASH_ALGO, data).hexdigest()


@dataclass
class ObjectStat:
    obj_hash: str
    original_size: int
    compressed_size: int
    is_new: bool  # False if this object already existed (deduplicated)


class ObjectStoreLike(Protocol):
    """
    The read/write surface every object store backend must provide.

    Found by running mypy, not by design up front: `SnapshotEngine.store`
    (see snapshot.py) is deliberately swappable at runtime — `vault/cli.py`
    reassigns it from a plain `ObjectStore` to a `PackAwareObjectStore`
    (see experimental/pack_aware_store.py) so every command transparently
    works whether a repository has been packed or not. Neither class
    inherits from the other by design (v1's `ObjectStore` and v2's
    packing layer are independent, and `PackAwareObjectStore`'s own
    docstring already documents itself as duck-typing "the same
    interface surface the rest of the codebase calls"). Without a
    shared type naming that interface, mypy only ever saw the first
    concrete type assigned to `self.store` and flagged the second
    assignment as incompatible — a real type-safety gap, not a false
    positive, since nothing previously *guaranteed* the two classes
    stayed in sync. A `Protocol` makes that implicit contract explicit
    and machine-checked without requiring either class to change its
    hierarchy.
    """

    def _object_path(self, obj_hash: str) -> Path: ...
    def has(self, obj_hash: str) -> bool: ...
    def put(self, data: bytes) -> ObjectStat: ...
    def get(self, obj_hash: str) -> bytes: ...
    def compressed_size(self, obj_hash: str) -> int: ...
    def iter_all_hashes(self) -> Iterator[str]: ...
    def verify_object(self, obj_hash: str) -> bool: ...
    def delete(self, obj_hash: str) -> None: ...


class ObjectStore:
    """
    Filesystem-backed content-addressable store.

    Layout (mirrors git's fan-out convention to avoid huge flat directories):

        <root>/objects/<hash[:2]>/<hash[2:]>

    Every object on disk is: zlib-compressed(original_bytes).
    The object's hash is always computed over the *original*
    (uncompressed) bytes, so verification doesn't depend on
    compression being deterministic.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.objects_dir.mkdir(parents=True, exist_ok=True)

    # -- path helpers ---------------------------------------------------

    def _validate_hash_format(self, obj_hash: str) -> None:
        """
        Raises ObjectCorruptedError for anything that isn't a
        well-formed hex digest of the expected length.

        Found by adversarial fuzzing, not anticipated by design: an
        empty string (or any hash shorter than 2 characters) makes
        obj_hash[:2] and obj_hash[2:] both empty, and pathlib silently
        DROPS empty path components -- `objects_dir / "" / ""`
        resolves to `objects_dir` itself, not a new subpath. Without
        this check, get() would call read_bytes() on that DIRECTORY
        and raise an uncaught IsADirectoryError instead of a clean,
        expected error.
        """
        expected_len = hashlib.new(HASH_ALGO).digest_size * 2  # hex chars
        if len(obj_hash) != expected_len or not all(c in "0123456789abcdef" for c in obj_hash):
            raise ObjectCorruptedError(
                obj_hash if obj_hash else "(empty)",
                f"malformed object hash: expected {expected_len} lowercase hex characters, "
                f"got {len(obj_hash)} characters"
            )

    def _object_path(self, obj_hash: str) -> Path:
        self._validate_hash_format(obj_hash)
        return self.objects_dir / obj_hash[:2] / obj_hash[2:]

    def has(self, obj_hash: str) -> bool:
        # Deliberately does NOT propagate a malformed-hash error --
        # has() is called throughout this codebase as a plain boolean
        # guard (`if store.has(x):`) with no expectation it can raise.
        # A malformed identifier simply doesn't reference anything
        # real, which IS what False correctly communicates here.
        try:
            return self._object_path(obj_hash).exists()
        except ObjectCorruptedError:
            return False

    # -- writes -----------------------------------------------------------

    def put(self, data: bytes) -> ObjectStat:
        """
        Store `data` if it isn't already present (content-addressed
        dedup). Returns stats including whether this was a new write
        or a dedup hit.

        Crash-safety: written to a temp file, then atomically renamed
        into place. If a snapshot operation crashes mid-put, the temp
        file is simply orphaned in the OS temp dir / same directory
        and the object store itself is never left in a partial state.
        """
        obj_hash = hash_bytes(data)
        dest = self._object_path(obj_hash)

        if dest.exists():
            # Whole-file dedup: identical content already stored.
            return ObjectStat(
                obj_hash=obj_hash,
                original_size=len(data),
                compressed_size=dest.stat().st_size,
                is_new=False,
            )

        compressed = zlib.compress(data, COMPRESS_LEVEL)
        # Pick whichever representation is actually smaller — for small
        # or already-incompressible data, raw + overhead beats zlib's
        # own header/footer overhead.
        if len(compressed) < len(data):
            stored_bytes = FORMAT_VERSION + MARKER_COMPRESSED + compressed
        else:
            stored_bytes = FORMAT_VERSION + MARKER_RAW + data

        dest.parent.mkdir(parents=True, exist_ok=True)

        # Write to a temp file in the SAME directory as the destination
        # so os.replace() is a same-filesystem atomic rename, not a
        # cross-device copy (which is not guaranteed atomic).
        fd, tmp_path = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(stored_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, dest)  # atomic on POSIX & Windows
        except BaseException:
            # Best-effort cleanup of the temp file on any failure.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return ObjectStat(
            obj_hash=obj_hash,
            original_size=len(data),
            compressed_size=len(stored_bytes),
            is_new=True,
        )

    # -- reads ------------------------------------------------------------

    def get(self, obj_hash: str) -> bytes:
        """
        Return the original (decompressed) bytes for an object.

        This is a STRICT read: the hash is re-verified before the data
        is returned, so a caller of get() never has to remember to
        separately call verify_object() to be safe — get() either
        returns provably-correct data, or raises. A bit-flip that
        still decompresses cleanly (garbage-but-valid zlib output) is
        exactly the case this catches; without this check it would
        have silently returned corrupted content.
        """
        path = self._object_path(obj_hash)
        if not path.exists():
            raise ObjectNotFoundError(obj_hash)
        stored_bytes = path.read_bytes()
        if len(stored_bytes) < 2:
            raise ObjectCorruptedError(obj_hash, "object file too short (missing version/format header)")

        version, marker, payload = stored_bytes[:1], stored_bytes[1:2], stored_bytes[2:]
        if version != FORMAT_VERSION:
            raise ObjectCorruptedError(
                obj_hash, f"unsupported object format version: {version!r} (expected {FORMAT_VERSION!r})"
            )
        if marker == MARKER_COMPRESSED:
            try:
                data = zlib.decompress(payload)
            except zlib.error as e:
                raise ObjectCorruptedError(obj_hash, f"decompression failed: {e}") from e
        elif marker == MARKER_RAW:
            data = payload
        else:
            raise ObjectCorruptedError(obj_hash, f"unknown storage format marker: {marker!r}")

        if hash_bytes(data) != obj_hash:
            raise ObjectCorruptedError(
                obj_hash, "hash mismatch — stored content does not match this object's id"
            )
        return data

    def compressed_size(self, obj_hash: str) -> int:
        return self._object_path(obj_hash).stat().st_size

    # -- integrity ----------------------------------------------------------

    def iter_all_hashes(self):
        """Yield every object hash currently on disk (for verify/gc)."""
        for shard in self.objects_dir.iterdir():
            if not shard.is_dir():
                continue
            for entry in shard.iterdir():
                yield shard.name + entry.name

    def verify_object(self, obj_hash: str) -> bool:
        """
        Non-raising integrity check: True if the object is present and
        its content matches its hash, False otherwise. Used by callers
        that need to keep scanning past a bad object (a bulk `vault
        verify` walking every object should report every corrupted one,
        not stop at the first) — get() itself raises immediately, which
        is what a single read that must succeed wants, but is the wrong
        shape for a scan.
        """
        try:
            self.get(obj_hash)
            return True
        except (ObjectNotFoundError, ObjectCorruptedError):
            return False

    def delete(self, obj_hash: str) -> None:
        """Remove an object. Used only by gc, after reachability analysis."""
        path = self._object_path(obj_hash)
        path.unlink(missing_ok=True)
