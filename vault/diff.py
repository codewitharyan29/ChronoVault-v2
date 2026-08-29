"""
vault/diff.py

One diff algorithm, two callers:
  - `vault diff A B`        -> compares snapshot A's tree to snapshot B's tree
  - `vault restore N --preview` -> compares the CURRENT working directory's
                                    (virtual) tree to snapshot N's tree

Both reduce to the same question: given two trees (as name -> object hash
maps, recursively), what's added / removed / modified? So both go through
`diff_trees()` below. The only difference is where the "before" tree comes
from — loaded from a stored snapshot, or computed live by walking the
working directory (see `hash_working_directory` — reuses the same hashing
logic as SnapshotEngine, without writing anything to the object store).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from vault.objects import ObjectStoreLike, hash_bytes
from vault.snapshot import IGNORED_DIR_NAMES, SnapshotEngine


@dataclass
class DiffResult:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    unchanged_count: int = 0

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.modified)


def _flatten_tree(engine: SnapshotEngine, tree_hash: str, prefix: str = "") -> dict[str, str]:
    """
    Recursively expand a tree object into a flat {relative_path: blob_hash}
    map. Directories don't appear as their own entries — only files do,
    which is what a diff actually needs to report.
    """
    flat: dict[str, str] = {}
    for entry in engine.load_tree(tree_hash):
        path = f"{prefix}{entry.name}"
        if entry.kind == "tree":
            flat.update(_flatten_tree(engine, entry.obj_hash, prefix=f"{path}/"))
        else:
            flat[path] = entry.obj_hash
    return flat


def diff_trees(engine: SnapshotEngine, tree_hash_a: str, tree_hash_b: str) -> DiffResult:
    """
    Compare two trees by hash. A file present in both with the same blob
    hash is unchanged (cheap: no need to read file contents, just compare
    hashes — this is the whole point of content addressing).
    """
    flat_a = _flatten_tree(engine, tree_hash_a, "") if tree_hash_a else {}
    flat_b = _flatten_tree(engine, tree_hash_b, "") if tree_hash_b else {}

    result = DiffResult()
    for path, hash_b in flat_b.items():
        if path not in flat_a:
            result.added.append(path)
        elif flat_a[path] != hash_b:
            result.modified.append(path)
        else:
            result.unchanged_count += 1
    for path in flat_a:
        if path not in flat_b:
            result.removed.append(path)

    result.added.sort()
    result.removed.sort()
    result.modified.sort()
    return result


def hash_working_directory(store: ObjectStoreLike, source_dir: Path) -> dict[str, str]:
    """
    Compute a flat {relative_path: content_hash} map for the CURRENT
    working directory, WITHOUT writing anything to the object store.
    Used for `restore --preview` (dry-run: never mutates state) and for
    the "does the working directory have unsaved changes?" safety check
    before a destructive restore.

    hash_bytes() is the same function objects.py uses for object ids, so
    a file's hash here is directly comparable to a blob's hash in a
    stored tree — no need to materialize a tree object just to diff.
    """
    flat: dict[str, str] = {}

    def walk(dir_path: Path, prefix: str):
        for child in sorted(dir_path.iterdir(), key=lambda p: p.name):
            if child.name in IGNORED_DIR_NAMES:
                continue
            if child.is_dir():
                walk(child, f"{prefix}{child.name}/")
            elif child.is_file():
                flat[f"{prefix}{child.name}"] = hash_bytes(child.read_bytes())

    if source_dir.exists():
        walk(source_dir, "")
    return flat


def diff_working_directory_against_snapshot(
    engine: SnapshotEngine, source_dir: Path, snapshot_tree_hash: str
) -> DiffResult:
    """
    The `restore --preview` diff: current on-disk state vs. a snapshot's
    tree. Reuses diff_trees' comparison logic by feeding it two flat maps
    directly rather than two tree hashes (the working directory was never
    written as a tree object, since dry-run must not touch the store).
    """
    flat_current = hash_working_directory(engine.store, source_dir)
    flat_snapshot = _flatten_tree(engine, snapshot_tree_hash, "") if snapshot_tree_hash else {}

    result = DiffResult()
    for path, hash_snap in flat_snapshot.items():
        if path not in flat_current:
            result.added.append(path)  # would be restored
        elif flat_current[path] != hash_snap:
            result.modified.append(path)  # would be overwritten
        else:
            result.unchanged_count += 1
    for path in flat_current:
        if path not in flat_snapshot:
            result.removed.append(path)  # exists now, not in snapshot

    result.added.sort()
    result.removed.sort()
    result.modified.sort()
    return result
