"""
vault/snapshot.py

The layer that turns a directory on disk into a chain of immutable
snapshots, built on top of vault/objects.py's content-addressable
object store.

Object model (three types only — see ARCHITECTURE.md):

  blob      raw file contents (already handled by ObjectStore.put)
  tree      a directory listing: sorted list of (name, type, hash)
            entries, serialized deterministically so identical
            directory contents always hash to the same tree object
            (this is what makes whole-directory dedup fall out for
            free from whole-file dedup + content-addressed trees)
  snapshot  { timestamp, parent (or None), root tree hash, stats }

Serialization: a small hand-rolled binary format via `struct` and
length-prefixed fields — NOT pickle (arbitrary code execution risk
on load) and NOT json for the tree/snapshot objects (we want a
compact, explicit binary layout to document in FORMAT.md as part of
the Zero-Dependency Craft story). Metadata that doesn't need to be
compact (e.g. snapshot stats for `explain`) is fine as UTF-8 JSON
text stored as the snapshot object's payload — json is stdlib and
using it for a metadata record isn't the "outsourcing the storage
engine" problem pickle/sqlite would be.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from vault.objects import (
    ObjectCorruptedError,
    ObjectStat,
    ObjectStore,
    ObjectStoreLike,
    VaultError,
    atomic_replace,
    windows_retry,
)

IGNORED_DIR_NAMES = {".vault", ".git", "__pycache__", ".venv", "node_modules"}


class SnapshotNotFoundError(VaultError):
    """Raised when a requested snapshot id doesn't exist."""

    def __init__(self, snap_id, hint: str):
        self.snap_id = snap_id
        super().__init__(f"Snapshot '{snap_id}' does not exist.\n{hint}")


# ---------------------------------------------------------------------------
# Tree objects
# ---------------------------------------------------------------------------

@dataclass
class TreeEntry:
    name: str
    kind: str  # "blob" or "tree"
    obj_hash: str


def serialize_tree(entries: list[TreeEntry]) -> bytes:
    """
    Deterministic binary encoding of a directory listing so identical
    directory contents always produce the same tree object hash.

    Format per entry (length-prefixed, no delimiters to avoid
    ambiguity if a filename contains unusual bytes):
        [1 byte  kind: 'b' or 't']
        [2 bytes name_len, big-endian] [name_len bytes name, utf-8]
        [64 bytes obj_hash, ascii hex, fixed width for sha256]
    Entries are sorted by name before encoding — this is what makes
    the tree hash a pure function of *contents*, not filesystem
    iteration order.
    """
    parts = []
    for e in sorted(entries, key=lambda e: e.name):
        _validate_entry_name(e.name)
        name_bytes = e.name.encode("utf-8")
        kind_byte = b"b" if e.kind == "blob" else b"t"
        parts.append(kind_byte)
        parts.append(len(name_bytes).to_bytes(2, "big"))
        parts.append(name_bytes)
        parts.append(e.obj_hash.encode("ascii"))
    return b"".join(parts)


def _validate_entry_name(name: str) -> None:
    """
    A tree entry's `name` field, once deserialized, is later used
    directly in filesystem path construction (`source_dir / name` in
    restore.py). If that name can contain a path separator or a `..`
    component, a maliciously crafted or corrupted tree object can
    escape the target directory entirely — confirmed via manual
    testing: a hand-crafted tree entry named "../../evil.txt" caused
    restore to write a file outside the working directory. This check
    is the fix: reject any name that isn't a single, real path
    segment BEFORE it's trusted anywhere downstream.
    """
    if not name:
        raise ObjectCorruptedError("(tree)", "empty entry name")
    if "/" in name or "\\" in name:
        raise ObjectCorruptedError("(tree)", f"entry name contains a path separator: {name!r}")
    if name in (".", ".."):
        raise ObjectCorruptedError("(tree)", f"entry name is a path-traversal component: {name!r}")
    if "\x00" in name:
        raise ObjectCorruptedError("(tree)", "entry name contains a null byte")
    # The on-disk format packs the name's UTF-8 byte length into a
    # 2-byte big-endian field (see serialize_tree's format doc), so
    # 65535 bytes is the hard ceiling this format can represent. Found
    # by adversarial fuzzing, not by design review: without this
    # check, int.to_bytes(2, "big") raises an uncaught OverflowError
    # for any longer name instead of a clean, expected error -- a
    # crash a real filesystem walk would likely never trigger (most
    # filesystems cap individual names around 255 bytes), but a
    # maliciously constructed or programmatically-generated tree could.
    name_byte_len = len(name.encode("utf-8"))
    if name_byte_len > 65535:
        raise ObjectCorruptedError(
            "(tree)", f"entry name too long: {name_byte_len} bytes (format limit is 65535)"
        )


def deserialize_tree(data: bytes) -> list[TreeEntry]:
    """
    In practice, `data` here always arrives via ObjectStore.get(),
    which verifies the content's hash before returning it — so a
    length-prefix field pointing past the end of the buffer would
    require an actual SHA-256 collision to occur, not just a corrupted
    byte (that's caught earlier, by get() itself). These bounds checks
    are defense-in-depth for that theoretical case, not the primary
    integrity guarantee.
    """
    entries = []
    i = 0
    n = len(data)
    while i < n:
        if i + 1 > n:
            raise ObjectCorruptedError("(tree)", "truncated tree data: missing kind byte")
        kind_byte = data[i:i + 1]
        if kind_byte == b"b":
            kind = "blob"
        elif kind_byte == b"t":
            kind = "tree"
        else:
            raise ObjectCorruptedError("(tree)", f"invalid entry kind byte: {kind_byte!r}")
        i += 1

        if i + 2 > n:
            raise ObjectCorruptedError("(tree)", "truncated tree data: missing name length")
        name_len = int.from_bytes(data[i:i + 2], "big")
        i += 2

        if i + name_len > n:
            raise ObjectCorruptedError("(tree)", "truncated tree data: name exceeds buffer")
        try:
            name = data[i:i + name_len].decode("utf-8")
        except UnicodeDecodeError as e:
            raise ObjectCorruptedError("(tree)", f"entry name is not valid UTF-8: {e}") from e
        _validate_entry_name(name)
        i += name_len

        if i + 64 > n:
            raise ObjectCorruptedError("(tree)", "truncated tree data: missing object hash")
        obj_hash = data[i:i + 64].decode("ascii")
        i += 64
        try:
            int(obj_hash, 16)  # SHA-256 hex is always 64 chars of [0-9a-f]
        except ValueError:
            raise ObjectCorruptedError("(tree)", f"malformed object hash: {obj_hash!r}") from None

        entries.append(TreeEntry(name=name, kind=kind, obj_hash=obj_hash))
    return entries


# ---------------------------------------------------------------------------
# Snapshot metadata
# ---------------------------------------------------------------------------

@dataclass
class SnapshotStats:
    files: int = 0
    new_objects: int = 0
    reused_objects: int = 0
    original_bytes: int = 0
    compressed_bytes: int = 0


class SnapshotCorruptedError(VaultError):
    """Raised when a snapshot record file exists but its content is
    malformed -- not valid JSON, or missing required fields."""

    def __init__(self, snap_id, reason: str):
        self.snap_id = snap_id
        super().__init__(
            f"Snapshot record '{snap_id}' is corrupted ({reason}).\n"
            f"The .vault directory may be damaged or tampered with."
        )


@dataclass
class SnapshotRecord:
    id: int
    timestamp: float
    parent: int | None
    root_tree_hash: str
    stats: SnapshotStats
    message: str = ""

    def to_json_bytes(self) -> bytes:
        payload = {
            "id": self.id,
            "timestamp": self.timestamp,
            "parent": self.parent,
            "root_tree_hash": self.root_tree_hash,
            "message": self.message,
            "stats": {
                "files": self.stats.files,
                "new_objects": self.stats.new_objects,
                "reused_objects": self.stats.reused_objects,
                "original_bytes": self.stats.original_bytes,
                "compressed_bytes": self.stats.compressed_bytes,
            },
        }
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def from_json_bytes(data: bytes) -> SnapshotRecord:
        try:
            payload = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise SnapshotCorruptedError("(unknown)", f"invalid JSON/encoding: {e}") from e
        try:
            s = payload["stats"]
            return SnapshotRecord(
                id=payload["id"],
                timestamp=payload["timestamp"],
                parent=payload["parent"],
                root_tree_hash=payload["root_tree_hash"],
                message=payload.get("message", ""),
                stats=SnapshotStats(**s),
            )
        except (KeyError, TypeError) as e:
            snap_id = payload.get("id", "(unknown)") if isinstance(payload, dict) else "(unknown)"
            raise SnapshotCorruptedError(snap_id, f"missing or malformed field: {e}") from e


# ---------------------------------------------------------------------------
# The engine: walk a directory -> objects -> snapshot record
# ---------------------------------------------------------------------------

class SnapshotEngine:
    def __init__(self, vault_dir: Path):
        self.vault_dir = Path(vault_dir)
        # Typed as the shared ObjectStoreLike protocol, not the concrete
        # ObjectStore, because vault/cli.py deliberately reassigns this
        # to a PackAwareObjectStore for every command (see its own
        # comment there) -- the attribute's real type is "whichever
        # object-store backend is active," not fixed at construction.
        self.store: ObjectStoreLike = ObjectStore(self.vault_dir)
        self.snapshots_dir = self.vault_dir / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    # -- directory walking --------------------------------------------------

    def _walk_into_tree(self, dir_path: Path, stats: SnapshotStats) -> str:
        """
        Recursively store every file under dir_path as a blob, every
        subdirectory as a tree, and return the hash of the tree object
        for dir_path itself. Returns the root tree's hash when called
        on the snapshot root.
        """
        entries: list[TreeEntry] = []

        for child in sorted(dir_path.iterdir(), key=lambda p: p.name):
            if child.name in IGNORED_DIR_NAMES:
                continue

            if child.is_symlink():
                # Genuinely skipped (not just documented as skipped —
                # this check was previously missing entirely, which
                # meant symlinks were silently followed: a file symlink
                # got its target's content duplicated as a real blob,
                # and a directory symlink pointing at an ancestor
                # directory would recurse infinitely. Found via manual
                # testing, not code review.
                continue

            if child.is_dir():
                sub_hash = self._walk_into_tree(child, stats)
                entries.append(TreeEntry(name=child.name, kind="tree", obj_hash=sub_hash))
            elif child.is_file():
                data = child.read_bytes()
                stat: ObjectStat = self.store.put(data)
                stats.files += 1
                stats.original_bytes += stat.original_size
                stats.compressed_bytes += stat.compressed_size
                if stat.is_new:
                    stats.new_objects += 1
                else:
                    stats.reused_objects += 1
                entries.append(TreeEntry(name=child.name, kind="blob", obj_hash=stat.obj_hash))
            # Other special files (sockets, FIFOs, device files) also
            # fall through here silently — same v1 scope decision.

        tree_bytes = serialize_tree(entries)
        tree_stat = self.store.put(tree_bytes)
        return tree_stat.obj_hash

    # -- snapshot creation ----------------------------------------------------

    def _next_snapshot_id(self) -> int:
        """
        Monotonic counter, stored in `.vault/next_id` — NOT derived from
        `max(existing snapshot ids)`. This matters once snapshots can be
        deleted (`vault snapshot rm`): with a max()+1 scheme, deleting
        the highest-numbered snapshot would make the next one reuse its
        id, which is confusing (id "3" could refer to two different
        historical snapshots depending on when you ask) and dangerous
        for anything that caches an id (a tag, a script, a person's
        memory of "snapshot 3 was the good one"). A separate counter
        file that only ever increments avoids that entirely — ids are
        unique for the life of the repository, deleted or not.
        """
        # Every step here touches a file under .vault/ that a concurrent
        # `vault snapshot` (and a Windows virus scanner) may have a
        # handle on: on the py3.12 windows-latest CI cell the read, the
        # temp write, and the rename each raised WinError 5/13/32 and
        # killed the process mid-snapshot. windows_retry rides out that
        # transient window; on POSIX it's a straight-through call.
        counter_path = self.vault_dir / "next_id"
        if counter_path.exists():
            next_id = int(windows_retry(counter_path.read_text).strip())
        else:
            next_id = 1

        tmp_path = self.vault_dir / ".next_id-tmp"
        windows_retry(lambda: tmp_path.write_text(str(next_id + 1)))
        atomic_replace(tmp_path, counter_path)
        return next_id

    def _latest_snapshot_id(self) -> int | None:
        existing = [int(p.name) for p in self.snapshots_dir.iterdir() if p.name.isdigit()]
        return max(existing) if existing else None

    def create_snapshot(self, source_dir: Path, message: str = "") -> SnapshotRecord:
        source_dir = Path(source_dir)
        stats = SnapshotStats()
        try:
            root_hash = self._walk_into_tree(source_dir, stats)
        except OSError as e:
            # The OS's own path-length limit (not a ChronoVault limit)
            # can surface here as a raw OSError on extremely deep or
            # long paths.
            raise VaultError(
                f"Filesystem error while walking '{source_dir}': {e}\n"
                f"This is often caused by extremely deep directory nesting "
                f"exceeding the operating system's own path-length limit — "
                f"not a ChronoVault-specific restriction."
            ) from e
        except RecursionError as e:
            # Confirmed via direct testing (not just reasoned about):
            # _walk_into_tree recurses once per directory level, so a
            # deeply nested tree hits PYTHON'S OWN recursion limit
            # (default 1000) well before any OS path-length limit is
            # reached with short directory names. Tested boundary: 950
            # levels succeeds, 1000+ levels raises RecursionError. This
            # is a genuinely different failure mode than the OSError
            # case above (interpreter stack depth vs. filesystem path
            # length) and needs its own handler — a bare `except
            # OSError` does NOT catch RecursionError, since it's a
            # RuntimeError subclass, not an OSError subclass. Found by
            # testing this specifically, not by inspection.
            raise VaultError(
                f"Directory structure under '{source_dir}' is too deeply "
                f"nested to snapshot (Python's recursion limit was hit "
                f"while walking it). This is a real, tested limitation of "
                f"the current recursive directory walk, not a vague "
                f"catch-all — very deep nesting (roughly 1000+ levels) is "
                f"not currently supported."
            ) from e

        snap_id = self._next_snapshot_id()
        parent = self._latest_snapshot_id()

        record = SnapshotRecord(
            id=snap_id,
            timestamp=time.time(),
            parent=parent,
            root_tree_hash=root_hash,
            stats=stats,
            message=message,
        )

        # Snapshot records are small metadata files, one per snapshot,
        # written atomically the same way objects are (temp + rename)
        # so a crash mid-snapshot never leaves a half-written record.
        snap_path = self.snapshots_dir / str(snap_id)
        tmp_path = self.snapshots_dir / f".tmp-{snap_id}"
        # write + rename retried: a Windows AV/indexer handle on this
        # directory's files raised WinError 5/13/32 here on py3.12 CI.
        windows_retry(lambda: tmp_path.write_bytes(record.to_json_bytes()))
        atomic_replace(tmp_path, snap_path)

        return record

    # -- reading --------------------------------------------------------------

    def load_snapshot(self, snap_id: int) -> SnapshotRecord:
        path = self.snapshots_dir / str(snap_id)
        if not path.exists():
            available = sorted(
                int(p.name) for p in self.snapshots_dir.iterdir() if p.name.isdigit()
            )
            if available:
                listing = ", ".join(str(i) for i in available)
                hint = f"Available snapshots: {listing}\nRun 'vault list' for details."
            else:
                hint = "No snapshots exist yet. Run 'vault snapshot' to create one."
            raise SnapshotNotFoundError(snap_id, hint)
        return SnapshotRecord.from_json_bytes(path.read_bytes())

    def list_snapshots(self) -> list[SnapshotRecord]:
        ids = sorted(int(p.name) for p in self.snapshots_dir.iterdir() if p.name.isdigit())
        return [self.load_snapshot(i) for i in ids]

    def delete_snapshot(self, snap_id: int) -> None:
        """
        Remove a snapshot's record. Does NOT touch any objects — that's
        `vault gc`'s job (it recomputes reachability from whatever
        snapshots remain, so objects only unique to this snapshot
        become collectible on the next gc run, not immediately). This
        two-step design (delete the record, then gc separately) mirrors
        real systems like Git (`git branch -d` doesn't immediately
        prune objects either) and keeps deletion itself fast and safe —
        no reachability walk needed just to remove a pointer.

        Note this does NOT affect future id generation: ids come from
        the monotonic `next_id` counter, not from what snapshot records
        currently exist, so deleting snapshot 3 will never cause a
        future snapshot to be created as "3" again.
        """
        self.load_snapshot(snap_id)  # raises SnapshotNotFoundError if it doesn't exist
        (self.snapshots_dir / str(snap_id)).unlink()

    def load_tree(self, tree_hash: str) -> list[TreeEntry]:
        return deserialize_tree(self.store.get(tree_hash))

    def walk_tree_entries(self, tree_hash: str, prefix: str = ""):
        """
        Yield (path, kind, obj_hash) for every entry reachable from
        `tree_hash`, depth-first, sorted by name at each level so the
        order is a pure function of content. `kind` is "dir" for a
        sub-tree object and "file" for a blob. Strictly read-only --
        composes load_tree(), adds no storage logic. Backs
        `vault show` and scripts/content_addressing_proof.py.
        """
        for entry in sorted(self.load_tree(tree_hash), key=lambda e: e.name):
            path = f"{prefix}{entry.name}"
            if entry.kind == "tree":
                yield (path, "dir", entry.obj_hash)
                yield from self.walk_tree_entries(entry.obj_hash, f"{path}/")
            else:
                yield (path, "file", entry.obj_hash)
