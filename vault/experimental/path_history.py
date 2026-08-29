"""
vault/experimental/path_history.py

Indexes file history BY PATH across all snapshots -- a different axis
than feature #1's snapshot-ID index. Answers "show me every version
of src/auth.py across history" without walking every snapshot's tree
on every query.

Git itself does NOT maintain a persistent index for this -- `git log
-- path` does a real-time walk with pathspec filtering on every
invocation, a well-known performance pain point on large repositories.
This module builds what Git chose not to.

Recording semantics: only append an entry when the path's blob hash
CHANGES from the previously recorded value -- matching `git log --
path`'s actual behavior.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from vault.snapshot import SnapshotEngine


class PathHistoryIndex:
    def __init__(self, vault_dir: Path):
        self.vault_dir = Path(vault_dir)
        self.index_path = self.vault_dir / "path_history.json"
        self.history: dict = self._load()

    def _load(self) -> dict:
        """
        Loads the persisted index, degrading safely to an empty
        index (not crashing) for any corrupted or malformed content.

        Found by adversarial fuzzing, not anticipated by design: raw
        JSONDecodeError leaked for malformed JSON (empty file, cut-off
        content, null bytes), and valid-but-wrong-shape JSON (null,
        a list, a bare number, a bare string -- all parse successfully
        but don't have a .get()-compatible dict shape) raised
        AttributeError once used. This index is explicitly documented
        elsewhere as a rebuildable CACHE, never the source of truth --
        so the correct behavior for corrupted content is to start
        fresh, matching the same philosophy `rebuild_from_scratch()`
        already embodies, not to crash the whole process.
        """
        if not self.index_path.exists():
            return {}
        try:
            data = json.loads(self.index_path.read_text())
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _persist(self) -> None:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=self.vault_dir, prefix=".pathhist-tmp-")
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(self.history, f)
            f.flush()
            os.fsync(f.fileno())  # matches objects.py's own atomic-write
            # pattern -- an earlier version of this method omitted this,
            # meaning the write wasn't actually guaranteed durable before
            # the atomic rename below, even though the rename itself was
            # still atomic. Caught by comparing against the project's own
            # established convention, not by a failing test.
        os.replace(tmp_path, self.index_path)

    def record_snapshot(self, engine: SnapshotEngine, snapshot_id: int, root_tree_hash: str) -> int:
        flat = {}
        self._flatten(engine, root_tree_hash, "", flat)

        changed = 0
        for path, blob_hash in flat.items():
            existing = self.history.get(path, [])
            if not existing or existing[-1][1] != blob_hash:
                existing.append([snapshot_id, blob_hash])
                self.history[path] = existing
                changed += 1

        self._persist()
        return changed

    def _flatten(self, engine: SnapshotEngine, tree_hash: str, prefix: str, out: dict) -> None:
        for entry in engine.load_tree(tree_hash):
            path = f"{prefix}{entry.name}"
            if entry.kind == "tree":
                self._flatten(engine, entry.obj_hash, f"{path}/", out)
            else:
                out[path] = entry.obj_hash

    def history_for(self, path: str) -> list:
        """
        Found by checking OUTPUT correctness, not just crash-safety:
        an earlier version of this method trusted self.history.get(path)
        to already be a list of (snapshot_id, blob_hash) pairs.
        Malformed-but-dict-shaped index content (e.g. a per-path value
        that's a plain string, not a list) didn't crash -- it silently
        iterated the string character-by-character, wrapping each
        character in a 1-tuple, producing plausible-looking GARBAGE
        indistinguishable from real history entries. That's worse
        than a crash: a user could trust `vault log <path>` output
        that's simply wrong. Now validates each raw entry's shape
        (a 2-element sequence) before trusting it, silently dropping
        anything that doesn't match rather than fabricating a tuple
        from it -- consistent with this index being a rebuildable
        cache, never the source of truth.
        """
        raw = self.history.get(path, [])
        if not isinstance(raw, list):
            return []
        result = []
        for entry in raw:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                result.append(tuple(entry))
        return result

    def rebuild_from_scratch(self, engine: SnapshotEngine) -> int:
        """The brute-force baseline this index exists to avoid on
        every query -- walks every snapshot and records every change."""
        self.history = {}
        for record in engine.list_snapshots():
            self.record_snapshot(engine, record.id, record.root_tree_hash)
        return len(self.history)
