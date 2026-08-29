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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from vault.experimental.delta_pack import DeltaAwarePackedStore
from vault.experimental.packfile import MAGIC
from vault.objects import ObjectStat, ObjectStore


def _first_line(exc: BaseException) -> str:
    text = str(exc)
    return text.splitlines()[0] if text else exc.__class__.__name__


@dataclass
class QuarantinedPack:
    """A pack file that was on disk but failed structural validation at
    load time, so it is being skipped instead of trusted. `hashes` is
    what its .idx *claimed* to hold, captured when the index still
    parsed -- so callers (recover-check, `vault verify`) can name
    exactly which objects became unreadable. It is empty when the
    index itself was too broken to read."""
    name: str
    reason: str
    hashes: set = field(default_factory=set)


class PackAwareObjectStore:
    def __init__(self, vault_dir: Path):
        self.vault_dir = Path(vault_dir)
        self._loose = ObjectStore(self.vault_dir)
        self.pack_dir = self.vault_dir / "pack"
        self._readers = []  # one DeltaAwarePackedStore per HEALTHY pack file
        # Packs that failed validation. A corrupt or truncated pack
        # must never brick the whole engine (every command constructs
        # this object): it is skipped and recorded here, and every
        # other pack plus all loose objects keep serving normally.
        # Being precise, per the honest-framing requirement: objects
        # that were pruned from loose storage after packing and lived
        # ONLY in a now-quarantined pack are genuinely unrecoverable --
        # this is surfaced, not silently papered over. See
        # recover_check.py and cli.py's _engine_and_source warning.
        self.quarantined_packs: list[QuarantinedPack] = []

        if self.pack_dir.exists():
            for pack_path in sorted(self.pack_dir.glob("*.pack")):
                self._load_one_pack(pack_path)

    # -- pack loading + structural validation (construction-time only) -----

    def _load_one_pack(self, pack_path: Path) -> None:
        idx_path = pack_path.with_suffix(".idx")
        if not idx_path.exists():
            self.quarantined_packs.append(QuarantinedPack(
                pack_path.name,
                "no .idx file (pack write was interrupted, or the index was lost)"))
            return
        try:
            reader = DeltaAwarePackedStore(self._loose, pack_path, idx_path)
        except Exception as e:  # noqa: BLE001 -- ANY failure to build the
            # reader means the index is untrustworthy: ObjectCorruptedError
            # / struct.error / ValueError from a truncated or malformed
            # .idx, OSError from an unreadable file. Quarantine, never crash.
            self.quarantined_packs.append(QuarantinedPack(
                pack_path.name, _first_line(e)))
            return
        problem = self._structural_problem(reader, pack_path)
        if problem is not None:
            self.quarantined_packs.append(QuarantinedPack(
                pack_path.name, problem,
                hashes={e.obj_hash for e in reader.index.entries}))
            return
        self._readers.append(reader)

    @staticmethod
    def _structural_problem(reader, pack_path: Path):
        """Cheap sanity check for a pack whose index PARSED: the .pack
        must exist, start with the magic, and every index entry's byte
        range must lie inside the file. O(entries) integer comparisons
        + one stat + a 4-byte read -- no object is decoded, so a
        healthy pack pays almost nothing and its behaviour is
        unchanged. Returns a reason string if the pack is bad, else None."""
        try:
            size = pack_path.stat().st_size
            with open(pack_path, "rb") as f:
                head = f.read(len(MAGIC))
        except OSError as e:
            return f"pack file is unreadable: {e}"
        if head != MAGIC:
            return (f"pack file does not begin with the {MAGIC!r} magic "
                    f"(truncated, empty, or not a pack file)")
        header = len(MAGIC) + 4  # magic + object-count uint32
        for entry in reader.index.entries:
            if entry.offset < header or entry.length < 0 or entry.offset + entry.length > size:
                return (f"index entry for {entry.obj_hash[:12]}... points to bytes "
                        f"[{entry.offset}, {entry.offset + entry.length}) which fall "
                        f"outside the {size}-byte pack file (pack truncated or index stale)")
        return None

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
