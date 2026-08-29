"""
vault/restore.py

Turns a snapshot's tree back into real files on disk.

Design decisions locked in while building this (documented here and
mirrored into STDLIB.md / README's Design Decisions section):

  - Restore is NON-DESTRUCTIVE to files outside the snapshot: it adds
    missing files and overwrites modified ones back to snapshot state,
    but never deletes a file that exists on disk and simply isn't part
    of the target snapshot. A recovery tool that silently deletes your
    recent work would be actively dangerous — that's not what
    "restore" should mean here. (`--exact` full-sync mode is a
    plausible v2 flag, not built in v1.)
  - Every restore is preceded by an integrity check (`store.verify_object`
    on every blob that will be written) — corrupted objects are
    reported and abort the restore before anything is written, rather
    than silently writing garbage to disk.
  - `--preview` computes the diff (reusing vault/diff.py) and returns
    without touching the filesystem at all.
  - A real restore (not preview) requires an explicit confirmation
    from the caller (the CLI layer prints "Type RESTORE to continue"
    and only calls `apply_restore` after that's typed — this module
    itself does not prompt, so it stays testable without stdin).
  - Files are written the same way objects are: temp file + os.replace(),
    so a crash mid-restore never leaves a half-written file in place of
    a good one.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vault.diff import (
    DiffResult,
    _flatten_tree,
    diff_working_directory_against_snapshot,
)
from vault.objects import ObjectCorruptedError, ObjectNotFoundError, RestoreError
from vault.snapshot import SnapshotEngine


@dataclass
class IntegrityIssue:
    path: str
    obj_hash: str
    reason: str


@dataclass
class RestorePreview:
    diff: DiffResult
    integrity_issues: list[IntegrityIssue]

    @property
    def safe_to_restore(self) -> bool:
        return len(self.integrity_issues) == 0


@dataclass
class RestoreResult:
    files_written: int
    bytes_written: int


def preview_restore(engine: SnapshotEngine, source_dir: Path, snap_id: int) -> RestorePreview:
    """
    Read-only. Computes what a real restore WOULD do, and checks that
    every object it would need to write is actually intact — so a
    corrupted object is caught here, before the user is asked to
    confirm, not mid-write.

    Corruption can hit two kinds of objects: leaf blobs (a file's
    content) or tree objects (a directory listing). Blob corruption is
    checked per-file below. Tree corruption is structural — it breaks
    walking the snapshot at all, so it's caught here and reported as a
    single top-level integrity issue rather than letting the exception
    propagate past the "safe to restore?" check.
    """
    record = engine.load_snapshot(snap_id)  # raises SnapshotNotFoundError with a helpful list

    try:
        diff = diff_working_directory_against_snapshot(engine, source_dir, record.root_tree_hash)
        flat_snapshot = _flatten_tree(engine, record.root_tree_hash, "")
    except (ObjectNotFoundError, ObjectCorruptedError) as e:
        return RestorePreview(
            diff=DiffResult(),
            integrity_issues=[
                IntegrityIssue(path="(snapshot tree)", obj_hash=record.root_tree_hash, reason=str(e))
            ],
        )

    issues: list[IntegrityIssue] = []
    # Only need to verify objects that will actually be written
    # (added + modified) — unchanged files aren't touched by restore.
    for path in diff.added + diff.modified:
        obj_hash = flat_snapshot[path]
        if not engine.store.verify_object(obj_hash):
            issues.append(
                IntegrityIssue(path, obj_hash, "object failed integrity verification (missing or corrupted)")
            )

    return RestorePreview(diff=diff, integrity_issues=issues)


def apply_restore(engine: SnapshotEngine, source_dir: Path, snap_id: int) -> RestoreResult:
    """
    Actually writes files to source_dir to match the snapshot.
    Caller (CLI) is responsible for having already shown a preview and
    obtained confirmation — this function does not prompt.

    Raises if any needed object fails integrity verification, so a
    corrupted repository fails loudly rather than writing bad data —
    same "never write garbage to disk" guarantee the object store's
    atomic writes give the object layer, applied here to the
    filesystem layer.
    """
    preview = preview_restore(engine, source_dir, snap_id)
    if not preview.safe_to_restore:
        bad = ", ".join(f"{i.path} ({i.reason})" for i in preview.integrity_issues)
        raise RestoreError(
            f"Restore aborted: {len(preview.integrity_issues)} object(s) failed "
            f"integrity verification: {bad}\nRepository may be corrupted — run 'vault verify'."
        )

    source_dir = Path(source_dir)
    source_dir.mkdir(parents=True, exist_ok=True)

    record = engine.load_snapshot(snap_id)
    flat_snapshot = _flatten_tree(engine, record.root_tree_hash, "")

    files_written = 0
    bytes_written = 0

    # Only touch added + modified paths — matches the non-destructive
    # design decision (unchanged and "removed" i.e. extra-on-disk files
    # are left alone).
    for path in sorted(preview.diff.added + preview.diff.modified):
        obj_hash = flat_snapshot[path]
        data = engine.store.get(obj_hash)  # already verified in preview_restore

        dest = source_dir / path
        dest.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(dir=dest.parent, prefix=".restore-tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, dest)  # atomic — never leaves a half-written file
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        files_written += 1
        bytes_written += len(data)

    return RestoreResult(files_written=files_written, bytes_written=bytes_written)
