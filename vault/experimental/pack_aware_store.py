"""
vault/experimental/pack_aware_store.py

A drop-in replacement for ObjectStore that transparently reads from
BOTH packs and loose files.

=== Format history, and a real incompatibility caught before it shipped ===

An earlier version of this file used PackedObjectStoreV2 (packfile_v2.py),
whose pack format stores each object's raw ObjectStore-encoded bytes
directly, with NO type prefix. Wiring in delta compression (which
needs DeltaAwarePackWriter/delta_pack.py, whose format prefixes every
entry with 'F' or 'D' to distinguish full vs. delta-encoded objects)
would have made `vault pack` write one format while this reader
expected another -- the first byte of every object would have been
silently misread as part of objects.py's version byte instead of the
delta_pack.py type marker. Caught by tracing the two formats before
wiring, not by a failure after the fact.

Fix: standardize on delta_pack.py's format everywhere. It's a strict
superset -- an 'F' entry is exactly a full object, so a pack with zero
deltas (the common case when delta compression finds nothing worth
delta-encoding) is fully equivalent to what the old format produced.

Implements the same interface surface the rest of the codebase calls:
get, has, put, iter_all_hashes, compressed_size, delete, _object_path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from vault.experimental.delta_pack import DeltaAwarePackedStore
from vault.objects import ObjectStat, ObjectStore


class PackAwareObjectStore:
    def __init__(self, vault_dir: Path):
        self.vault_dir = Path(vault_dir)
        self._loose = ObjectStore(self.vault_dir)
        self.pack_dir = self.vault_dir / "pack"
        self._readers = []  # one DeltaAwarePackedStore per pack file found

        if self.pack_dir.exists():
            for pack_path in sorted(self.pack_dir.glob("*.pack")):
                idx_path = pack_path.with_suffix(".idx")
                if idx_path.exists():
                    self._readers.append(DeltaAwarePackedStore(self._loose, pack_path, idx_path))

    def _find_in_packs(self, obj_hash: str):
        for reader in self._readers:
            if reader.index.find(obj_hash) is not None:
                return reader
        return None

    def get(self, obj_hash: str) -> bytes:
        reader = self._find_in_packs(obj_hash)
        if reader is not None:
            return reader.get(obj_hash)
        return self._loose.get(obj_hash)

    def has(self, obj_hash: str) -> bool:
        if self._find_in_packs(obj_hash) is not None:
            return True
        return self._loose.has(obj_hash)

    def iter_all_hashes(self) -> Iterator[str]:
        """Every object in the repository, across ALL tiers -- packed
        (in any pack) and loose. Getting this wrong is exactly what
        made `verify` report '0 objects' on a packed repo in the
        first merge attempt."""
        seen = set()
        for reader in self._readers:
            for entry in reader.index.entries:
                if entry.obj_hash not in seen:
                    seen.add(entry.obj_hash)
                    yield entry.obj_hash
        for h in self._loose.iter_all_hashes():
            if h not in seen:
                yield h

    def compressed_size(self, obj_hash: str) -> int:
        reader = self._find_in_packs(obj_hash)
        if reader is not None:
            entry = reader.index.find(obj_hash)
            return entry.length
        return self._loose.compressed_size(obj_hash)

    def verify_object(self, obj_hash: str) -> bool:
        try:
            self.get(obj_hash)
            return True
        except Exception:
            return False

    def put(self, data: bytes) -> ObjectStat:
        return self._loose.put(data)

    def delete(self, obj_hash: str) -> None:
        self._loose.delete(obj_hash)

    def _object_path(self, obj_hash: str) -> Path:
        return self._loose._object_path(obj_hash)

    def close(self) -> None:
        pass  # DeltaAwarePackedStore opens/closes a fresh handle per
        # get() call rather than holding one open -- see the honest
        # note in delta_pack.py's own module docstring about this
        # being a simpler, less-optimized read path than packfile_v2's
