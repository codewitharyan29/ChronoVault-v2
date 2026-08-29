"""
vault/experimental/delta_pack.py

The integration work explicitly deferred in feature #3's verdict:
wires delta compression into actual pack creation, with the real
decision policy that was missing before ("try delta, keep it only if
smaller than independent compression, otherwise fall back cleanly").

Built as a SEPARATE writer/reader from packfile.py's PackWriter/
PackReader, not a modification of them -- those are already tested
(feature #2), and adding a type-byte prefix to every entry (needed to
distinguish full vs. delta entries) would break their "packed bytes
match the loose file exactly" tests. Composing, not modifying.

=== Entry format ===

    [1 byte: 'F' (full) or 'D' (delta)]
    F: [ObjectStore's normal stored bytes -- version+marker+payload]
    D: [32 bytes: base object's hash]
       [4 bytes: length of the zlib-compressed delta instruction stream]
       [that many bytes: zlib-compressed serialize_ops() output]

=== Base selection ===

find_delta_candidates() uses the tree diff (vault/diff.py) to find
"same path, different snapshot" blob pairs -- provably "the same
file, a later version," not a name/size guess. Bases are constrained
to single-level (never delta-encode a blob that is ITSELF used as
another blob's base) -- the no-chaining decision from feature #3,
enforced by construction here, not just documented.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import TypedDict

from vault.diff import diff_trees
from vault.experimental.delta import (
    apply_delta,
    compute_delta,
    deserialize_ops,
    serialize_ops,
)
from vault.experimental.packfile import MAGIC, PackIndex, PackIndexEntry
from vault.objects import ObjectNotFoundError, ObjectStore, hash_bytes
from vault.snapshot import SnapshotEngine


class PackWriteStats(TypedDict):
    """write_pack()'s return value. Found by running mypy, not by
    design up front: this used to be a bare `dict` initialized with
    only int values (full/delta/delta_bytes_saved), then had three
    Path values (pack_path/idx_path/manifest_path) stuffed into it
    later — mypy correctly inferred dict[str, int] from the first
    assignment and flagged the later Path assignments as incompatible.
    A real type-safety gap, not a false positive: nothing previously
    stopped a future edit from assigning yet another mismatched type
    into the same loosely-typed dict. TypedDict fixes this while
    staying a plain dict at runtime -- every existing stats["key"]
    access (vault/cli.py, scripts/security_demo.py, the test suite)
    keeps working unchanged."""
    full: int
    delta: int
    delta_bytes_saved: int
    pack_path: Path
    idx_path: Path
    manifest_path: Path


def find_delta_candidates(engine: SnapshotEngine) -> dict:
    """
    Walks every consecutive pair of snapshots, diffs their trees, and
    for every path reported MODIFIED, records {new_blob_hash: old_blob_hash}.
    """
    snapshots = engine.list_snapshots()
    candidates = {}

    for i in range(1, len(snapshots)):
        prev, curr = snapshots[i - 1], snapshots[i]
        diff = diff_trees(engine, prev.root_tree_hash, curr.root_tree_hash)
        if not diff.modified:
            continue

        prev_entries = {}
        curr_entries = {}
        _flatten(engine, prev.root_tree_hash, "", prev_entries)
        _flatten(engine, curr.root_tree_hash, "", curr_entries)

        for path in diff.modified:
            old_hash = prev_entries.get(path)
            new_hash = curr_entries.get(path)
            if old_hash and new_hash and old_hash != new_hash:
                if old_hash not in candidates:  # never let a base also be a target -- no chains
                    candidates[new_hash] = old_hash

    return candidates


def _flatten(engine: SnapshotEngine, tree_hash: str, prefix: str, out: dict) -> None:
    for entry in engine.load_tree(tree_hash):
        path = f"{prefix}{entry.name}"
        if entry.kind == "tree":
            _flatten(engine, entry.obj_hash, f"{path}/", out)
        else:
            out[path] = entry.obj_hash


class DeltaAwarePackWriter:
    def __init__(self, store: ObjectStore, pack_dir: Path):
        self.store = store
        self.pack_dir = Path(pack_dir)
        self.pack_dir.mkdir(parents=True, exist_ok=True)

    def write_pack(self, pack_name: str, delta_candidates: dict) -> PackWriteStats:
        """
        For every loose object: if it's a delta candidate AND its base
        is actually available, try the delta, compare compressed sizes
        against the plain stored bytes, keep WHICHEVER IS SMALLER --
        the decision policy that was the missing piece in feature #3.

        Also persists a DELTA MANIFEST ({target_hash: base_hash} for
        every object actually stored as a delta) as its own JSON file
        alongside the pack. This is the piece that was missing before:
        without it, `vault gc` has no way to know that a delta-encoded
        object's BASE must stay alive even if nothing in any snapshot
        tree directly references that base's hash. See
        vault/experimental/delta_gc.py for how this manifest is
        actually consumed.
        """
        pack_path = self.pack_dir / f"{pack_name}.pack"
        idx_path = self.pack_dir / f"{pack_name}.idx"
        manifest_path = self.pack_dir / f"{pack_name}.deltamanifest.json"

        hashes = sorted(self.store.iter_all_hashes())
        entries = []
        # All six fields populated upfront (the three Path values are
        # already known at this point) rather than adding pack_path/
        # idx_path/manifest_path later -- see PackWriteStats above.
        stats: PackWriteStats = {
            "full": 0, "delta": 0, "delta_bytes_saved": 0,
            "pack_path": pack_path, "idx_path": idx_path, "manifest_path": manifest_path,
        }
        delta_manifest = {}  # target_hash -> base_hash, ONLY for objects
        # actually chosen as deltas (not every candidate -- some lose
        # to the "keep whichever is smaller" comparison below)

        with open(pack_path, "wb") as f:
            f.write(MAGIC)
            f.write(struct.pack(">I", len(hashes)))

            for obj_hash in hashes:
                full_stored = self.store._object_path(obj_hash).read_bytes()
                full_entry = b"F" + full_stored

                chosen_entry = full_entry
                base_hash = delta_candidates.get(obj_hash)
                if base_hash and self.store.has(base_hash):
                    target_content = self.store.get(obj_hash)
                    base_content = self.store.get(base_hash)
                    ops = compute_delta(base_content, target_content)
                    delta_bytes = zlib.compress(serialize_ops(ops), 6)
                    delta_entry = (
                        b"D" + bytes.fromhex(base_hash)
                        + struct.pack(">I", len(delta_bytes)) + delta_bytes
                    )
                    if len(delta_entry) < len(full_entry):
                        chosen_entry = delta_entry
                        stats["delta"] += 1
                        stats["delta_bytes_saved"] += len(full_entry) - len(delta_entry)
                        delta_manifest[obj_hash] = base_hash  # THE FIX: record
                        # this dependency so GC can find it later
                    else:
                        stats["full"] += 1
                else:
                    stats["full"] += 1

                offset = f.tell()
                f.write(chosen_entry)
                entries.append(PackIndexEntry(obj_hash, offset, len(chosen_entry)))

        index = PackIndex(entries)
        idx_path.write_bytes(index.to_bytes())

        import json
        manifest_path.write_text(json.dumps(delta_manifest))

        return stats


class DeltaAwarePackedStore:
    """Read path that understands the F/D entry format. Resolves a
    delta's base by checking THIS PACK's own index first -- the base
    is normally written in the same batch, and once packing prunes
    loose copies, the base's loose file no longer exists at all, only
    its packed copy. A real bug was found exactly here (see the
    commit log): the original version resolved bases ONLY via the raw
    loose store, which broke the instant a pack was written and its
    loose copies deleted -- 'vault verify' reported real corruption on
    a repository that was actually fine, because the delta's base was
    unreachable through the only path this code knew to look."""

    def __init__(self, store: ObjectStore, pack_path: Path, idx_path: Path):
        self.store = store
        self.pack_path = Path(pack_path)
        self.index = PackIndex.from_bytes(Path(idx_path).read_bytes())

    def _get_raw_entry(self, obj_hash: str):
        """Returns the raw (type-prefixed) bytes for obj_hash if it's
        in THIS pack's index, else None. Used both by get() directly
        and by base resolution, so both paths share one lookup."""
        entry = self.index.find(obj_hash)
        if entry is None:
            return None
        with open(self.pack_path, "rb") as f:
            f.seek(entry.offset)
            return f.read(entry.length)

    def get(self, obj_hash: str) -> bytes:
        raw = self._get_raw_entry(obj_hash)
        if raw is None:
            if self.store.has(obj_hash):
                return self.store.get(obj_hash)
            raise ObjectNotFoundError(obj_hash)

        entry_type = raw[0:1]
        if entry_type == b"F":
            from vault.experimental.packfile import _decode_stored_bytes
            return _decode_stored_bytes(obj_hash, raw[1:])
        elif entry_type == b"D":
            base_hash = raw[1:33].hex()
            delta_len = struct.unpack_from(">I", raw, 33)[0]
            compressed_ops = raw[37:37 + delta_len]
            ops = deserialize_ops(zlib.decompress(compressed_ops))
            # THE FIX: resolve the base through self.get(), which now
            # checks this pack's own index FIRST (via _get_raw_entry)
            # before falling back to loose. Single-level only (a base
            # is never itself a 'D' entry, by the no-chaining
            # candidate-selection rule in find_delta_candidates), so
            # this recursion terminates in exactly one extra hop.
            base_content = self.get(base_hash)
            reconstructed = apply_delta(base_content, ops)
            if hash_bytes(reconstructed) != obj_hash:
                from vault.objects import ObjectCorruptedError
                raise ObjectCorruptedError(obj_hash, "delta reconstruction hash mismatch")
            return reconstructed
        else:
            from vault.objects import ObjectCorruptedError
            raise ObjectCorruptedError(obj_hash, f"unknown delta-pack entry type: {entry_type!r}")
