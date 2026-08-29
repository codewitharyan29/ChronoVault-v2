"""
vault/gc.py

Garbage collection and object provenance tracing — two views of the
same underlying question: "starting from every snapshot that still
exists, which objects are reachable?"

  - `vault gc`    deletes everything NOT in that reachable set.
  - `vault trace` reports, for one specific object, which live
    snapshots reference it (and therefore whether gc would ever
    delete it).

Both are built on `compute_reachable_objects()` below, so there's one
DAG walk, not two — matches the diff engine's "one algorithm, two
callers" pattern in vault/diff.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vault.snapshot import SnapshotEngine


def _find_paths_for_hash(engine: SnapshotEngine, tree_hash: str, target_hash: str, prefix: str) -> list:
    """Recursively search one snapshot's tree for every path that
    resolves to target_hash — a file can only match once per snapshot
    in practice (content-addressing means identical content -> one
    hash -> the file just wouldn't be duplicated at different paths
    unless the user genuinely has two copies), but a directory itself
    can also match if the whole subtree is unchanged."""
    matches = []
    if tree_hash == target_hash:
        matches.append(prefix.rstrip("/") or ".")
    for entry in engine.load_tree(tree_hash):
        full_path = f"{prefix}{entry.name}"
        if entry.obj_hash == target_hash:
            matches.append(full_path)
        if entry.kind == "tree":
            matches.extend(_find_paths_for_hash(engine, entry.obj_hash, target_hash, prefix=f"{full_path}/"))
    return matches


def find_object_locations(engine: SnapshotEngine, obj_hash: str) -> dict[int, list]:
    """For each live snapshot, the file path(s) where obj_hash appears
    — empty for snapshots that don't reference it at all. This is what
    makes `vault trace` demonstrate deduplication directly: the same
    object hash showing up under the same (or different) path across
    multiple snapshots IS the dedup story, made visible."""
    locations: dict[int, list] = {}
    for record in engine.list_snapshots():
        paths = _find_paths_for_hash(engine, record.root_tree_hash, obj_hash, prefix="")
        if paths:
            locations[record.id] = paths
    return locations


def compute_reachable_objects(engine: SnapshotEngine) -> dict[str, set[int]]:
    """
    Walk every existing snapshot's tree and return a map of
    {object_hash: {snapshot_ids that reference it}}.

    An object hash appears as a key here — with a non-empty set of
    referencing snapshot ids — if and only if it's reachable. This
    single map answers both gc's question ("what's NOT in this map?")
    and trace's question ("which snapshots does this map say
    reference object X?").
    """
    reachable: dict[str, set[int]] = {}

    def visit(tree_hash: str, snap_id: int):
        # A tree object itself is reachable too — record it, then recurse.
        reachable.setdefault(tree_hash, set()).add(snap_id)
        for entry in engine.load_tree(tree_hash):
            reachable.setdefault(entry.obj_hash, set()).add(snap_id)
            if entry.kind == "tree":
                visit(entry.obj_hash, snap_id)

    for record in engine.list_snapshots():
        visit(record.root_tree_hash, record.id)

    return reachable


@dataclass
class GCResult:
    objects_deleted: int = 0
    bytes_reclaimed: int = 0
    deleted_hashes: list = field(default_factory=list)


def run_gc(engine: SnapshotEngine) -> GCResult:
    """
    Deletes every object not reachable from any current snapshot.
    Safe by construction: reachability is recomputed fresh from the
    current on-disk snapshot list every time gc runs, so it can never
    delete something a live snapshot still needs — there's no cached
    or stale reachability state to go wrong.
    """
    reachable = compute_reachable_objects(engine)
    result = GCResult()

    for obj_hash in list(engine.store.iter_all_hashes()):
        if obj_hash not in reachable:
            try:
                size = engine.store.compressed_size(obj_hash)
            except OSError:
                size = 0
            engine.store.delete(obj_hash)
            result.objects_deleted += 1
            result.bytes_reclaimed += size
            result.deleted_hashes.append(obj_hash)

    return result


@dataclass
class TraceResult:
    obj_hash: str
    exists: bool
    locations: dict[int, list]  # snapshot_id -> [paths where this object appears]
    would_gc_delete: bool

    @property
    def referenced_by(self) -> list:
        """Just the snapshot ids, for callers that don't need paths."""
        return sorted(self.locations.keys())


def trace_object(engine: SnapshotEngine, obj_hash: str) -> TraceResult:
    """
    Report which live snapshots reference a given object hash (and
    under what file path in each), and whether `vault gc` would
    consider it deletable right now.
    """
    exists = engine.store.has(obj_hash)
    locations = find_object_locations(engine, obj_hash)

    return TraceResult(
        obj_hash=obj_hash,
        exists=exists,
        locations=locations,
        would_gc_delete=exists and len(locations) == 0,
    )
