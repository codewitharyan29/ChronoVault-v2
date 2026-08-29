"""
vault/experimental/fast_walk.py

A stat-cache-accelerated alternative to SnapshotEngine._walk_into_tree
-- v1's walker reads and SHA-256-hashes every file on every snapshot,
even when most are byte-identical to last time. This caches
{path: (mtime, size, blob_hash)} and skips reading a file entirely
when its mtime+size still match what was cached.

=== The racy-file problem ===

mtime+size matching is a HEURISTIC, not a guarantee. The dangerous
failure direction is a silent false negative: a file changes within
the same filesystem timestamp-resolution window as the last snapshot,
so mtime can't distinguish "before" from "after." Documented in Git's
own source as the "racily clean" problem, with a specific mitigation:
if a cached entry's mtime is too close to "now," don't trust the
cache even on a match. Implemented here as RACY_WINDOW_SECONDS.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from vault.objects import ObjectStat, ObjectStore
from vault.snapshot import IGNORED_DIR_NAMES, SnapshotStats, TreeEntry, serialize_tree

RACY_WINDOW_SECONDS = 1.0


class StatCache:
    def __init__(self, vault_dir: Path):
        self.vault_dir = Path(vault_dir)
        self.cache_path = self.vault_dir / "stat_cache.json"
        self.entries: dict = self._load()

    def _load(self) -> dict:
        if self.cache_path.exists():
            return json.loads(self.cache_path.read_text())
        return {}

    def persist(self) -> None:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=self.vault_dir, prefix=".statcache-tmp-")
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(self.entries, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.cache_path)

    def lookup(self, path: str, current_mtime: float, current_size: int, now: float):
        entry = self.entries.get(path)
        if entry is None:
            return None
        if entry["mtime"] != current_mtime or entry["size"] != current_size:
            return None
        if (now - current_mtime) < RACY_WINDOW_SECONDS:
            return None  # racy window -- don't trust it
        return entry["blob_hash"]

    def update(self, path: str, mtime: float, size: int, blob_hash: str) -> None:
        self.entries[path] = {"mtime": mtime, "size": size, "blob_hash": blob_hash}


def fast_walk_into_tree(store: ObjectStore, cache: StatCache, dir_path: Path,
                         stats: SnapshotStats, now: float, prefix: str = "") -> str:
    """
    Same structural logic as SnapshotEngine._walk_into_tree, but
    consults the stat cache before reading each file. Must produce
    byte-identical tree hashes to the real walker for the same
    directory state, or it's not a valid accelerator.
    """
    entries = []

    for child in sorted(dir_path.iterdir(), key=lambda p: p.name):
        if child.name in IGNORED_DIR_NAMES:
            continue
        if child.is_symlink():
            continue

        rel_path = f"{prefix}{child.name}"

        if child.is_dir():
            sub_hash = fast_walk_into_tree(store, cache, child, stats, now, prefix=f"{rel_path}/")
            entries.append(TreeEntry(name=child.name, kind="tree", obj_hash=sub_hash))
        elif child.is_file():
            st = child.stat()
            cached_hash = cache.lookup(rel_path, st.st_mtime, st.st_size, now)

            if cached_hash is not None and store.has(cached_hash):
                stats.files += 1
                stats.reused_objects += 1
                obj_hash = cached_hash
            else:
                data = child.read_bytes()
                stat: ObjectStat = store.put(data)
                stats.files += 1
                stats.original_bytes += stat.original_size
                stats.compressed_bytes += stat.compressed_size
                if stat.is_new:
                    stats.new_objects += 1
                else:
                    stats.reused_objects += 1
                obj_hash = stat.obj_hash
                cache.update(rel_path, st.st_mtime, st.st_size, obj_hash)

            entries.append(TreeEntry(name=child.name, kind="blob", obj_hash=obj_hash))

    tree_bytes = serialize_tree(entries)
    tree_stat = store.put(tree_bytes)
    return tree_stat.obj_hash
