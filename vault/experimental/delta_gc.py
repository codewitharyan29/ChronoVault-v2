"""
vault/experimental/delta_gc.py

The piece that was missing when delta compression was deferred: v1's
gc.py computes reachability purely from snapshot tree walks. It has
NO concept of "object X can only be reconstructed if object Y (its
delta base) still exists" -- so if any object in a repository is
delta-encoded, running v1's gc.py unmodified is genuinely unsafe: a
base object with no direct tree reference could be deleted even
though a live, reachable delta object still depends on it.

This module does NOT modify vault/gc.py. It composes it: run v1's
real, tested compute_reachable_objects() first, then EXPAND that set
using the delta manifests written by delta_pack.py's write_pack(),
adding every base a reachable delta object depends on. Deletion never
happens against v1's raw reachable set when delta manifests exist --
only against this expanded one.
"""

from __future__ import annotations

import json
from pathlib import Path

from vault.gc import GCResult, compute_reachable_objects
from vault.snapshot import SnapshotEngine


def load_all_delta_manifests(pack_dir: Path) -> dict:
    """Merges every pack's delta manifest into one
    {target_hash: base_hash} map.

    Found by adversarial fuzzing, not anticipated by design: the
    existing JSONDecodeError/OSError guard didn't catch valid-JSON-
    but-wrong-type content (a bare `null`, a number, or a string all
    parse successfully but aren't dict-shaped) -- dict.update() on
    any of those raised TypeError or ValueError, uncaught. This
    matters more here than in most places: it sits directly in the
    GC safety path, so a crash here could mean `vault gc` never runs
    at all rather than degrading to "treat this pack's deltas as
    unknown," which is the correct, conservative fallback.
    """
    merged = {}
    if not pack_dir.exists():
        return merged
    for manifest_path in pack_dir.glob("*.deltamanifest.json"):
        try:
            data = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        merged.update(data)
    return merged


def compute_expanded_reachable_objects(engine: SnapshotEngine, pack_dir: Path) -> dict:
    """
    v1's real reachability, expanded to also include every delta
    base a reachable object transitively needs. Single-level only
    (matching feature #3's no-chaining design decision) -- but
    computed as a fixed-point loop anyway, defensively, in case that
    invariant is ever violated: a base that is ITSELF a delta target
    would otherwise be silently under-protected.
    """
    reachable = compute_reachable_objects(engine)
    delta_manifest = load_all_delta_manifests(pack_dir)

    if not delta_manifest:
        return reachable

    changed = True
    while changed:
        changed = False
        for target_hash in list(reachable.keys()):
            base_hash = delta_manifest.get(target_hash)
            if base_hash and base_hash not in reachable:
                reachable[base_hash] = set(reachable[target_hash])
                changed = True

    return reachable


def run_delta_aware_gc(engine: SnapshotEngine, pack_dir: Path) -> GCResult:
    """
    Same deletion logic as v1's run_gc(), but against the EXPANDED
    reachable set -- an object that is a delta base for a live
    snapshot's data is now correctly protected, even though no tree
    in any snapshot directly references its hash.
    """
    reachable = compute_expanded_reachable_objects(engine, pack_dir)
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
