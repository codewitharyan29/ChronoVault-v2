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

Rename awareness (v2.1): when a file disappears from one path and a
byte-for-byte identical file (same object hash) appears at a new path
in the same snapshot transition, the two are linked as one lineage,
so `vault log <either-path>` shows the history across the move. This
uses only data already in the object store -- no similarity scoring,
no new dependency. It is deliberately conservative; see
_detect_renames() and the README's "Known limitations" for exactly
what it does and does not catch. Rename links live in a separate
sidecar cache (path_renames.json); path_history.json's format is
unchanged.
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
        self.renames_path = self.vault_dir / "path_renames.json"
        self.history: dict = self._load()
        # Each entry: [old_path, new_path, snapshot_id] -- the snapshot
        # in which the content moved from old_path to new_path.
        self.renames: list = self._load_renames()

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

    def _load_renames(self) -> list:
        """
        Loads the rename sidecar, degrading to an empty list (never a
        crash) for missing / malformed / wrong-shaped content -- the
        same rebuildable-cache philosophy as _load(). Each kept entry
        is validated to be exactly [str old_path, str new_path,
        int snapshot_id]; anything else is dropped, not trusted.
        """
        if not self.renames_path.exists():
            return []
        try:
            data = json.loads(self.renames_path.read_text())
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []
        raw = data.get("renames", [])
        if not isinstance(raw, list):
            return []
        clean = []
        for entry in raw:
            if (
                isinstance(entry, (list, tuple))
                and len(entry) == 3
                and isinstance(entry[0], str)
                and isinstance(entry[1], str)
                and isinstance(entry[2], int)
                and not isinstance(entry[2], bool)
            ):
                clean.append([entry[0], entry[1], entry[2]])
        return clean

    def _persist_renames(self) -> None:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=self.vault_dir, prefix=".pathren-tmp-")
        with os.fdopen(tmp_fd, "w") as f:
            json.dump({"renames": self.renames}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.renames_path)

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

        self._detect_renames(engine, snapshot_id, flat)

        self._persist()
        # Always persisted (even when no rename was found this
        # snapshot) so path_renames.json's mere existence means
        # "renames have been scanned for" -- callers use that to
        # decide whether a rebuild is needed.
        self._persist_renames()
        return changed

    def _previous_snapshot_flat(self, engine: SnapshotEngine, snapshot_id: int):
        """The flattened {path: blob_hash} of the snapshot immediately
        preceding `snapshot_id` (by id), or None if there isn't one.
        Rebuilt from real tree data, so this is correct regardless of
        the order record_snapshot() happens to be called in."""
        earlier = [r for r in engine.list_snapshots() if r.id < snapshot_id]
        if not earlier:
            return None
        prev = max(earlier, key=lambda r: r.id)
        out: dict = {}
        self._flatten(engine, prev.root_tree_hash, "", out)
        return out

    def _detect_renames(self, engine: SnapshotEngine, snapshot_id: int, current_flat: dict) -> None:
        """
        Record CONTENT-IDENTICAL file renames across the transition
        from the immediately-preceding snapshot to this one, using
        only object hashes already in the store.

        A path that DISAPPEARS carrying blob hash H, paired one-to-one
        with a path that APPEARS carrying that same hash H, is a
        rename. Deliberately conservative:

          * Only a strict 1:1 match for a given hash counts. If N old
            paths and M new paths all share one hash (e.g. several
            identical empty __init__.py files reorganised at once),
            the pairing is ambiguous and NONE of them are linked.
          * A move that also edits the file in the SAME snapshot is
            NOT detected: the hash differs, so there is no signal that
            distinguishes it from an unrelated delete + add without a
            content-similarity heuristic (which this project has no
            dependency for, by design). It simply shows as the old
            path ending and a new path beginning.
          * Pure path swaps (a<->b) are not renames here: neither path
            actually disappears, so there is nothing to pair.

        These limits are documented in the README's Known Limitations.
        """
        prev = self._previous_snapshot_flat(engine, snapshot_id)
        if prev is None:
            return

        gone = {p: h for p, h in prev.items() if p not in current_flat}
        appeared = {p: h for p, h in current_flat.items() if p not in prev}
        if not gone or not appeared:
            return

        gone_by_hash: dict = {}
        for p, h in gone.items():
            gone_by_hash.setdefault(h, []).append(p)
        appeared_by_hash: dict = {}
        for p, h in appeared.items():
            appeared_by_hash.setdefault(h, []).append(p)

        known = {tuple(r) for r in self.renames}
        for h, old_paths in gone_by_hash.items():
            new_paths = appeared_by_hash.get(h)
            if new_paths and len(old_paths) == 1 and len(new_paths) == 1:
                rec = [old_paths[0], new_paths[0], snapshot_id]
                if tuple(rec) not in known:
                    self.renames.append(rec)
                    known.add(tuple(rec))

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

    def _name_chain(self, path: str) -> list:
        """
        Every path name this lineage has been known by, oldest name
        first, following content-identical rename links in BOTH
        directions from `path`. Bounded by the number of rename
        records and a visited-set, so a malformed or cyclic rename
        graph can never loop forever. With no renames touching `path`,
        this is just `[path]`.
        """
        limit = len(self.renames) + 1
        # dict collapse: if a name was (per a malformed cache) recorded
        # as a rename target/source more than once, the last record
        # wins -- deterministic, and the visited-set below still stops
        # any cycle.
        back = {new: old for old, new, _ in self.renames}
        fwd = {old: new for old, new, _ in self.renames}

        seen = {path}

        ancestors = []
        cur = path
        for _ in range(limit):
            prev = back.get(cur)
            if prev is None or prev in seen:
                break
            ancestors.append(prev)
            seen.add(prev)
            cur = prev
        ancestors.reverse()

        descendants = []
        cur = path
        for _ in range(limit):
            nxt = fwd.get(cur)
            if nxt is None or nxt in seen:
                break
            descendants.append(nxt)
            seen.add(nxt)
            cur = nxt

        return ancestors + [path] + descendants

    def lineage_for(self, path: str) -> list:
        """
        Like history_for(), but follows content-identical renames so
        `vault log <new-path>` (or the old path) shows one continuous
        history across the move.

        Returns a list of (snapshot_id, blob_hash, path_at_that_time),
        sorted by snapshot id. When no rename touches `path`, the
        result is exactly history_for(path) with the (unchanging) path
        attached to each row -- non-renamed output does not change.
        """
        rows = []
        for name in self._name_chain(path):
            for snap_id, blob_hash in self.history_for(name):
                rows.append((snap_id, blob_hash, name))
        rows.sort(key=lambda r: r[0])
        return rows

    def rebuild_from_scratch(self, engine: SnapshotEngine) -> int:
        """The brute-force baseline this index exists to avoid on
        every query -- walks every snapshot and records every change
        (and every content-identical rename)."""
        self.history = {}
        self.renames = []
        for record in engine.list_snapshots():
            self.record_snapshot(engine, record.id, record.root_tree_hash)
        return len(self.history)
