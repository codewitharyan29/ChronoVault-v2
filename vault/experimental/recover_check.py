"""
vault/experimental/recover_check.py

Backs `vault recover-check <snapshot>` -- a strictly READ-ONLY
recoverability audit for one snapshot. It answers "if I had to
restore this snapshot right now, would it succeed?" without creating,
modifying, or deleting anything.

It is composed entirely from logic that already exists elsewhere in
the codebase -- there is no new storage format and no new
failure-handling machinery here:

  * snapshot metadata / well-formedness
        -> SnapshotEngine.load_snapshot, which raises
           SnapshotNotFoundError / SnapshotCorruptedError exactly as
           `vault explain` / `vault diff` already rely on.
  * every object the tree references
        -> the same recursive load_tree() walk gc.py's
           compute_reachable_objects() and delta_pack.py's _flatten()
           already do.
  * object presence + integrity (loose AND packed)
        -> ObjectStore.has() + verify_object() -- the identical pair
           of calls `vault verify` makes. Reads of packed objects go
           through PackAwareObjectStore's DeltaAwarePackedStore /
           PackIndex, i.e. the same read path every other command uses.
  * delta-base resolvability
        -> delta_gc.load_all_delta_manifests() (the map `vault gc`
           already trusts), then has() + verify_object() on each base.
  * hash-format validity + path safety
        -> deserialize_tree()'s own checks: int(hash, 16) for the hash
           format, and _validate_entry_name() -- the restore.py
           path-traversal fix -- for every entry name. A tree that
           fails either raises ObjectCorruptedError from load_tree(),
           which this module catches and reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from vault.experimental.delta_gc import load_all_delta_manifests
from vault.objects import ObjectCorruptedError, ObjectNotFoundError
from vault.snapshot import (
    SnapshotCorruptedError,
    SnapshotEngine,
    SnapshotNotFoundError,
)

_HEX = set("0123456789abcdef")


def _is_valid_object_hash(h: str) -> bool:
    """Same shape check deserialize_tree enforces (64 lowercase hex)."""
    return isinstance(h, str) and len(h) == 64 and all(c in _HEX for c in h)


@dataclass
class RecoverIssue:
    where: str          # a file path, or "(snapshot tree)", or "delta base of <hash>"
    obj_hash: str
    detail: str


@dataclass
class RecoverReport:
    snap_id: int
    message: str = ""
    root_tree_hash: str = ""
    metadata_ok: bool = False
    objects_checked: int = 0     # unique object hashes referenced by the tree
    packed_objects: int = 0      # of those, how many live in a pack
    delta_dependencies: int = 0  # objects in this snapshot stored as a delta
    issues: list[RecoverIssue] = field(default_factory=list)

    @property
    def recoverable(self) -> bool:
        return self.metadata_ok and not self.issues


def _walk_tree(engine: SnapshotEngine, tree_hash: str, prefix: str,
               nodes: list, issues: list[RecoverIssue]) -> None:
    """Collect (path, kind, obj_hash) for the tree object itself and
    every entry beneath it. A tree that can't be loaded -- missing,
    hash-mismatched, structurally corrupt, or containing an unsafe
    entry name -- is reported as one issue and its subtree is skipped
    (the rest of the snapshot is still audited)."""
    path = prefix.rstrip("/") or "."
    try:
        entries = engine.load_tree(tree_hash)
    except (ObjectNotFoundError, ObjectCorruptedError) as e:
        issues.append(RecoverIssue(
            where=f"{path} (directory tree)" if path != "." else "(snapshot root tree)",
            obj_hash=tree_hash,
            detail=str(e).splitlines()[0],
        ))
        return

    nodes.append((path, "tree", tree_hash))
    for entry in entries:
        full = f"{prefix}{entry.name}"
        nodes.append((full, entry.kind, entry.obj_hash))
        if entry.kind == "tree":
            _walk_tree(engine, entry.obj_hash, f"{full}/", nodes, issues)


def check_snapshot_recoverable(engine: SnapshotEngine, snap_id: int) -> RecoverReport:
    report = RecoverReport(snap_id=snap_id)
    store = engine.store

    # 1. snapshot metadata exists and is well-formed
    try:
        record = engine.load_snapshot(snap_id)
    except (SnapshotNotFoundError, SnapshotCorruptedError) as e:
        report.issues.append(RecoverIssue("(snapshot metadata)", str(snap_id),
                                          str(e).splitlines()[0]))
        return report
    report.message = record.message
    report.root_tree_hash = record.root_tree_hash
    report.metadata_ok = True

    if not _is_valid_object_hash(record.root_tree_hash):
        report.issues.append(RecoverIssue(
            "(snapshot metadata)", str(record.root_tree_hash),
            "root_tree_hash is not a well-formed 64-char hex object id",
        ))
        return report

    # 2 + 6. walk the tree: object refs, tree readability, entry-name safety
    nodes: list = []
    _walk_tree(engine, record.root_tree_hash, "", nodes, report.issues)

    # unique object hashes, with the first path each was seen at (for diagnostics)
    first_path: dict = {}
    kind_of: dict = {}
    for path, kind, h in nodes:
        if h not in first_path:
            first_path[h] = path
            kind_of[h] = kind
    report.objects_checked = len(first_path)

    # which of those are packed (same source of truth iter_all_hashes uses)
    packed_hashes: set = set()
    for reader in getattr(store, "_readers", []):
        try:
            for e in reader.index.entries:
                packed_hashes.add(e.obj_hash)
        except Exception:  # noqa: BLE001 - a broken index is reported below, per-object
            pass
    report.packed_objects = len(first_path.keys() & packed_hashes)

    # 3. every referenced object exists AND reads back intact (loose or packed)
    for h, path in first_path.items():
        if kind_of[h] == "tree":
            continue  # a tree that got here already loaded == already verified
        loc = " (in a pack)" if h in packed_hashes else ""
        if not _is_valid_object_hash(h):
            report.issues.append(RecoverIssue(path, h, "referenced hash is not well-formed"))
            continue
        if not store.has(h):
            report.issues.append(RecoverIssue(
                path, h, f"referenced object is missing from the store{loc}"))
            continue
        if not store.verify_object(h):
            report.issues.append(RecoverIssue(
                path, h,
                f"object is present but fails hash/decode verification{loc}"))

    # 4. delta bases: for every object here stored as a delta, the base
    #    must exist and itself verify.
    manifest = load_all_delta_manifests(engine.vault_dir / "pack")
    for h in first_path:
        base = manifest.get(h)
        if base is None:
            continue
        report.delta_dependencies += 1
        target_path = first_path.get(h, h[:12] + "...")
        if not _is_valid_object_hash(base):
            report.issues.append(RecoverIssue(
                f"delta base for {target_path}", str(base),
                "delta base hash in the manifest is malformed"))
        elif not store.has(base):
            report.issues.append(RecoverIssue(
                f"delta base for {target_path}", base,
                "delta base object is missing -- this object cannot be reconstructed"))
        elif not store.verify_object(base):
            report.issues.append(RecoverIssue(
                f"delta base for {target_path}", base,
                "delta base is present but itself fails verification"))

    return report


def format_recover_report(report: RecoverReport) -> str:
    lines: list[str] = []
    title = f"Recovery check — Snapshot {report.snap_id}"
    if report.message:
        title += f' ("{report.message}")'
    lines.append(title)
    lines.append("")

    verdict = "fully recoverable" if report.recoverable else "NOT fully recoverable"
    lines.append(f"Snapshot {report.snap_id} is {verdict}")
    lines.append(f"  {report.objects_checked} objects checked")
    lines.append(f"  {report.packed_objects} packed")
    lines.append(f"  {report.delta_dependencies} delta dependencies")
    lines.append(f"  {len(report.issues)} integrity errors")

    if report.issues:
        lines.append("")
        for issue in report.issues:
            lines.append(f"  ✗ {issue.where}")
            lines.append(f"      {issue.obj_hash}")
            lines.append(f"      {issue.detail}")

    return "\n".join(lines)
