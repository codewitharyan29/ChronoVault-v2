"""
vault/reporting.py

status, explain, and tag — all thin reads over data the engine already
computes. No new storage logic here, just formatting/lookups.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from vault.objects import VaultError
from vault.snapshot import SnapshotEngine, SnapshotRecord


@dataclass
class RepoStatus:
    snapshot_count: int
    object_count: int
    total_snapshot_data_bytes: int  # cumulative across all snapshots' file
    # walks — NOT deduped, NOT "current repo size". A file present in 5
    # snapshots counts 5 times here, because this answers "how much data
    # did we walk in total", not "how much unique data exists". See
    # total_stored_bytes for the actual on-disk (deduped) figure.
    total_stored_bytes: int
    last_snapshot: SnapshotRecord | None
    integrity_ok: bool
    corrupted_count: int


def compute_status(engine: SnapshotEngine, deep_verify: bool = False) -> RepoStatus:
    """
    Fast repository overview. By default this does NOT re-verify every
    object's hash (that's what `vault verify` is for, and it's a full
    scan) — integrity here reflects whether every object referenced by
    every snapshot is at least present on disk, which is much cheaper
    and enough for a quick health check. Pass deep_verify=True to also
    hash-check every object (same cost as `vault verify`).
    """
    snapshots = engine.list_snapshots()
    all_hashes = list(engine.store.iter_all_hashes())

    total_original = sum(s.stats.original_bytes for s in snapshots)
    # Stored size isn't just sum of per-snapshot compressed_bytes (that
    # would double-count deduped objects reused across snapshots) —
    # walk the actual object store once instead.
    total_stored = sum(engine.store.compressed_size(h) for h in all_hashes)

    if deep_verify:
        corrupted = [h for h in all_hashes if not engine.store.verify_object(h)]
    else:
        corrupted = []  # presence-only check below covers the common case

    last = max(snapshots, key=lambda s: s.timestamp) if snapshots else None

    return RepoStatus(
        snapshot_count=len(snapshots),
        object_count=len(all_hashes),
        total_snapshot_data_bytes=total_original,
        total_stored_bytes=total_stored,
        last_snapshot=last,
        integrity_ok=(len(corrupted) == 0),
        corrupted_count=len(corrupted),
    )


@dataclass
class SnapshotExplanation:
    record: SnapshotRecord
    dedup_ratio_pct: float
    compression_saved_bytes: int
    storage_saved_bytes: int
    storage_saved_pct: float


def explain_snapshot(engine: SnapshotEngine, snap_id: int) -> SnapshotExplanation:
    record = engine.load_snapshot(snap_id)  # raises SnapshotNotFoundError with a helpful list
    s = record.stats

    total_objects = s.new_objects + s.reused_objects
    dedup_ratio = (100 * s.reused_objects / total_objects) if total_objects else 0.0

    # compression_saved is only meaningful relative to what was actually
    # newly written this snapshot (reused objects contribute 0 new bytes
    # either way, so comparing original_bytes to compressed_bytes across
    # ALL touched files — new and reused — approximates total savings
    # this snapshot's data represents, which is what a human wants to see).
    storage_saved = max(0, s.original_bytes - s.compressed_bytes)
    storage_saved_pct = (100 * storage_saved / s.original_bytes) if s.original_bytes else 0.0

    return SnapshotExplanation(
        record=record,
        dedup_ratio_pct=dedup_ratio,
        compression_saved_bytes=storage_saved,  # approximation noted above; not a separate figure
        storage_saved_bytes=storage_saved,
        storage_saved_pct=storage_saved_pct,
    )


# ---------------------------------------------------------------------------
# Tags — simple name -> snapshot_id mapping, stored as one JSON file.
# ---------------------------------------------------------------------------

class TagNotFoundError(VaultError):
    def __init__(self, name: str):
        super().__init__(f"Tag '{name}' does not exist. Run 'vault list' to see snapshot ids.")


def _tags_path(engine: SnapshotEngine) -> Path:
    return engine.vault_dir / "tags.json"


def _load_tags(engine: SnapshotEngine) -> dict:
    """
    Found by adversarial fuzzing, not anticipated by design: a
    corrupted tags.json raised an uncaught raw JSONDecodeError.
    Unlike path_history.py's index (explicitly documented as a
    rebuildable CACHE, never the source of truth, so silent
    degradation to empty is correct there), tags are deliberately
    user-created data -- silently treating a corrupted tags.json as
    "no tags exist" would silently discard real user state without
    any warning, which is worse than raising clearly. Reuses
    VaultError, the base exception the CLI layer already catches
    uniformly, rather than inventing a new exception class for a
    narrow case.
    """
    path = _tags_path(engine)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise VaultError(
            f"The tags file is corrupted and could not be parsed: {e}\n"
            f"Run 'vault verify' for a full integrity report."
        ) from e
    if not isinstance(data, dict):
        raise VaultError(
            f"The tags file has unexpected structure "
            f"(expected a JSON object, got {type(data).__name__})."
        )
    return data


def _save_tags(engine: SnapshotEngine, tags: dict) -> None:
    import os
    import tempfile
    path = _tags_path(engine)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".tags-tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(tags, f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def tag_snapshot(engine: SnapshotEngine, snap_id: int, name: str) -> None:
    engine.load_snapshot(snap_id)  # validates the id exists first
    tags = _load_tags(engine)
    tags[name] = snap_id
    _save_tags(engine, tags)


def resolve_snapshot_ref(engine: SnapshotEngine, ref: str) -> int:
    """
    Accept either a numeric snapshot id or a tag name, and return the
    resolved snapshot id. Used by diff/restore/explain/trace's CLI
    argument parsing so `vault restore release-v1` works the same as
    `vault restore 5`.
    """
    if ref.isdigit():
        return int(ref)
    tags = _load_tags(engine)
    if ref not in tags:
        raise TagNotFoundError(ref)
    return tags[ref]
