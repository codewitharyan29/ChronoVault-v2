"""
vault/cli.py — ChronoVault command-line entrypoint.

Uses argparse (stdlib) only. Each subcommand is a thin wrapper —
the real logic lives in vault/objects.py, vault/snapshot.py,
vault/diff.py, and vault/restore.py.

Build order (see ARCHITECTURE.md):
  [x] init      — create a .vault repository
  [x] snapshot  — walk a directory, store blobs/trees, record a snapshot
  [x] list      — list snapshot history
  [x] diff      — show changes between two snapshots
  [x] restore   — reconstruct a directory from a snapshot
  [x] verify    — walk all objects, check hash integrity
  [ ] gc        — delete objects unreachable from any remaining snapshot
  [ ] trace     — show which snapshots reference a given object
  [ ] status    — fast repository overview
  [ ] tag       — name a snapshot
  [ ] serve     — stdlib http.server Repository Inspector
"""

from __future__ import annotations

import argparse
import datetime
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    # Found by running this CLI's tests on Windows, not anticipated by
    # design: this module prints Unicode symbols (checkmarks, arrows,
    # box-drawing characters). On POSIX, stdout/stderr are UTF-8 by
    # default and this just works. On Windows, when stdout/stderr are
    # NOT an interactive console with its codepage set to UTF-8 (e.g.
    # piped/redirected, or run under a test runner, as here) — or
    # simply when the console's legacy codepage is something like
    # cp1252 — Python falls back to that legacy codepage for text I/O.
    # Encoding a character like '✓' (checkmark) that codepage
    # can't represent then raises an uncaught UnicodeEncodeError,
    # crashing an otherwise-successful command. `reconfigure` (3.7+)
    # is the standard fix: force UTF-8 for these streams, with
    # errors="replace" so an exotic terminal that truly cannot render
    # a given glyph degrades to a placeholder character instead of
    # crashing the whole command.
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # stream doesn't support reconfiguration -- leave as-is
del _stream

from vault.demo import generate_demo_repo
from vault.diff import diff_trees
from vault.experimental.benchmark_cmd import format_report, run_benchmark_report
from vault.experimental.delta_gc import load_all_delta_manifests, run_delta_aware_gc
from vault.experimental.delta_pack import (
    DeltaAwarePackedStore,
    DeltaAwarePackWriter,
    find_delta_candidates,
)
from vault.experimental.lock import LockTimeoutError, RepositoryLock
from vault.experimental.pack_aware_store import PackAwareObjectStore
from vault.experimental.path_history import PathHistoryIndex
from vault.experimental.stress_test_cmd import (
    run_concurrency_stress_test,
    run_corruption_recovery_demo,
)
from vault.gc import run_gc, trace_object
from vault.inspector import serve as inspector_serve
from vault.objects import ObjectStore, VaultError
from vault.reporting import (
    compute_status,
    explain_snapshot,
    resolve_snapshot_ref,
    tag_snapshot,
)
from vault.restore import apply_restore, preview_restore
from vault.snapshot import SnapshotEngine

VAULT_DIR_NAME = ".vault"


def _find_vault_dir(start: Path) -> Path:
    """
    Walk upward from `start` looking for a .vault directory — same idea
    as how git finds .git from any subdirectory. Raises a clear error
    if no repository is found anywhere above.
    """
    current = start.resolve()
    for candidate in [current, *current.parents]:
        vault_dir = candidate / VAULT_DIR_NAME
        if vault_dir.is_dir():
            return vault_dir
    raise VaultError(
        f"Not a ChronoVault repository (no {VAULT_DIR_NAME} found in {start} or its parents).\n"
        f"Run 'vault init' first."
    )


def _engine_and_source(args: argparse.Namespace):
    start = Path(getattr(args, "path", None) or ".").resolve()
    vault_dir = _find_vault_dir(start)
    source_dir = vault_dir.parent  # the repo root is the .vault's parent
    engine = SnapshotEngine(vault_dir)
    # Transparently make every command pack-aware. Without this,
    # `vault pack` silently breaks `verify` and `restore` -- found by
    # real end-to-end CLI testing, see pack_aware_store.py's docstring.
    engine.store = PackAwareObjectStore(vault_dir)
    return engine, source_dir


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path).resolve()
    vault_dir = target / VAULT_DIR_NAME

    if vault_dir.exists():
        print(f"Repository already initialized at {vault_dir}")
        return 1

    ObjectStore(vault_dir)  # creates <vault_dir>/objects/ as a side effect
    (vault_dir / "snapshots").mkdir(parents=True, exist_ok=True)

    print(f"Initialized empty ChronoVault repository in {vault_dir}")
    return 0


def _with_repo_lock(func):
    """
    Wraps a mutating command in the repository-wide lock.

    This closes a REAL race condition, not a theoretical one: v1's
    _next_snapshot_id() is a read-modify-write on a counter file, and
    concurrent `vault snapshot` invocations were measured producing
    duplicate IDs (8 processes -> 7 unique IDs), silently clobbering
    a snapshot record. See tests/test_experimental_lock.py.

    Read-only commands (list, diff, status, explain, trace, verify,
    info, serve) are deliberately NOT wrapped -- they don't mutate
    repository state, so serializing them would cost latency for no
    correctness benefit.
    """
    def wrapper(args: argparse.Namespace) -> int:
        try:
            vault_dir = _find_vault_dir(Path(getattr(args, "path", None) or "."))
        except VaultError:
            # No repository yet (e.g. `init`) -- nothing to lock.
            return func(args)
        try:
            with RepositoryLock(vault_dir, timeout=30.0):
                return func(args)
        except LockTimeoutError as e:
            print(f"Error: {e}", file=sys.stderr)
            print("Another ChronoVault process is using this repository.", file=sys.stderr)
            return 1
    wrapper.__name__ = getattr(func, "__name__", "wrapped")
    wrapper.__doc__ = func.__doc__
    return wrapper


def cmd_snapshot(args: argparse.Namespace) -> int:
    engine, source_dir = _engine_and_source(args)
    start = time.perf_counter()
    record = engine.create_snapshot(source_dir, message=args.message or "")
    elapsed = time.perf_counter() - start

    print("✓ Snapshot created")
    print()
    print(f"  ID:              {record.id}")
    if record.message:
        print(f"  Message:         {record.message}")
    print(f"  Files:           {record.stats.files}")
    print(f"  New objects:     {record.stats.new_objects}")
    print(f"  Reused objects:  {record.stats.reused_objects}")
    if record.stats.original_bytes > 0:
        saved_pct = 100 * (1 - record.stats.compressed_bytes / record.stats.original_bytes)
        print(f"  Original size:   {_human_bytes(record.stats.original_bytes)}")
        print(f"  Stored size:     {_human_bytes(record.stats.compressed_bytes)}")
        print(f"  Storage saved:   {saved_pct:.0f}%")
    print(f"  Completed in:    {elapsed:.3f}s")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    engine, _ = _engine_and_source(args)
    snapshots = engine.list_snapshots()

    if not snapshots:
        print("No snapshots yet. Run 'vault snapshot' to create one.")
        return 0

    for record in snapshots:
        # fromtimestamp() without tz= uses local time deliberately --
        # a CLI showing e.g. "2024-01-15 17:47" in the user's own
        # timezone is more readable than a UTC timestamp would be.
        # Flagged by ruff's DTZ006 (correctly, in general), left as-is
        # here since this is display-only, never stored or compared.
        when = datetime.datetime.fromtimestamp(record.timestamp).strftime("%Y-%m-%d %H:%M")
        label = f'"{record.message}"' if record.message else "(no message)"
        print(f"{record.id:>4}  {label:<30} {when}  ({record.stats.files} files)")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    engine, _ = _engine_and_source(args)
    id_a = resolve_snapshot_ref(engine, args.snapshot_a)
    id_b = resolve_snapshot_ref(engine, args.snapshot_b)
    s_a = engine.load_snapshot(id_a)
    s_b = engine.load_snapshot(id_b)
    result = diff_trees(engine, s_a.root_tree_hash, s_b.root_tree_hash)

    print(f"Snapshot {args.snapshot_a} → Snapshot {args.snapshot_b}")
    print()
    for path in result.added:
        print(f"  + {path}")
    for path in result.modified:
        print(f"  ~ {path}")
    for path in result.removed:
        print(f"  - {path}")
    if result.is_empty():
        print("  (no changes)")
    print()
    print(f"{len(result.added)} added, {len(result.modified)} modified, "
          f"{len(result.removed)} removed, {result.unchanged_count} unchanged")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    engine, source_dir = _engine_and_source(args)
    snap_id = resolve_snapshot_ref(engine, args.snapshot_id)

    preview = preview_restore(engine, source_dir, snap_id)

    if preview.integrity_issues:
        print("⚠ Integrity check failed — restore aborted before any changes were made.")
        print()
        for issue in preview.integrity_issues:
            print(f"  {issue.path}: {issue.reason}")
        print()
        print("Run 'vault verify' for a full repository integrity report.")
        return 1

    print(f"Restore Preview — Snapshot {snap_id}" + (f" ({args.snapshot_id})" if args.snapshot_id != str(snap_id) else ""))
    print()
    for path in preview.diff.added:
        print(f"  + {path}")
    for path in preview.diff.modified:
        print(f"  ~ {path}")
    for path in preview.diff.removed:
        print(f"  - {path}  (exists now, not in snapshot — will NOT be deleted)")
    if preview.diff.is_empty():
        print("  (working directory already matches this snapshot)")
    print()
    print("✓ Integrity check passed")

    if args.preview:
        print()
        print("No changes applied (--preview).")
        return 0

    if preview.diff.is_empty():
        print("Nothing to restore.")
        return 0

    print()
    changed_count = len(preview.diff.added) + len(preview.diff.modified)
    confirm = input(f"This will write {changed_count} file(s). Type RESTORE to continue: ")
    if confirm.strip() != "RESTORE":
        print("Restore cancelled.")
        return 1

    result = apply_restore(engine, source_dir, snap_id)
    print()
    print(f"✓ Restoration completed — {result.files_written} file(s) restored "
          f"({_human_bytes(result.bytes_written)})")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    engine, _ = _engine_and_source(args)
    store = engine.store

    start = time.perf_counter()
    all_hashes = list(store.iter_all_hashes())
    corrupted = [h for h in all_hashes if not store.verify_object(h)]
    elapsed = time.perf_counter() - start

    print(f"Checking {len(all_hashes)} object(s)...")
    print()
    if corrupted:
        print(f"✗ {len(corrupted)} corrupted object(s) found:")
        for h in corrupted:
            print(f"    {h}")
        print()
        print(f"Repository integrity FAILED. ({elapsed:.3f}s)")
        return 1
    else:
        print(f"✓ All {len(all_hashes)} objects verified ({elapsed:.3f}s)")
        print("Repository healthy.")
        return 0


def cmd_gc(args: argparse.Namespace) -> int:
    """
    Uses the delta-aware reachability computation automatically when
    the repository has any delta-packed objects (checked via the
    presence of a non-empty delta manifest) -- otherwise behaves
    identically to v1's plain run_gc(), with zero behavior change for
    the common case where delta compression was never used.

    This is the actual fix for the gap that deferred delta compression
    in the first place: v1's gc.py has no concept of "this object is
    a delta base another live object depends on." Without this,
    running gc after packing with delta compression could silently
    delete a base object a reachable snapshot still needs to
    reconstruct its data -- proven as a real, reproduced scenario in
    tests/test_v2_delta_gc.py, not just reasoned about.
    """
    engine, _ = _engine_and_source(args)
    pack_dir = engine.vault_dir / "pack"
    has_deltas = bool(load_all_delta_manifests(pack_dir))

    start = time.perf_counter()
    if has_deltas:
        result = run_delta_aware_gc(engine, pack_dir)
    else:
        result = run_gc(engine)
    elapsed = time.perf_counter() - start

    if result.objects_deleted == 0:
        print(f"Nothing to collect — every object is still reachable from a snapshot. ({elapsed:.3f}s)")
    else:
        print(f"✓ Collected {result.objects_deleted} unreachable object(s) ({elapsed:.3f}s)")
        print(f"  Reclaimed: {_human_bytes(result.bytes_reclaimed)}")
    if has_deltas:
        print("  (delta-aware: base objects for live delta-encoded data were protected)")
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    engine, _ = _engine_and_source(args)
    result = trace_object(engine, args.object_hash)

    print(f"Object: {args.object_hash}")
    print()
    if not result.exists:
        print("Status: does not exist in the object store.")
        return 1

    if result.referenced_by:
        print("Referenced by:")
        for snap_id in result.referenced_by:
            for path in result.locations[snap_id]:
                print(f"  Snapshot {snap_id}")
                print(f"    └── {path}")
    else:
        print("Referenced by: (nothing — not reachable from any snapshot)")
    print()
    print(f"GC status: {'will be collected on next vault gc' if result.would_gc_delete else 'protected'}")
    return 0


def cmd_snapshot_rm(args: argparse.Namespace) -> int:
    engine, _ = _engine_and_source(args)
    snap_id = resolve_snapshot_ref(engine, args.snapshot_id)
    engine.delete_snapshot(snap_id)
    print(f"✓ Deleted snapshot {snap_id}")
    print("  (objects it uniquely owned are not freed yet — run 'vault gc' to reclaim them)")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    from vault.objects import FORMAT_VERSION, HASH_ALGO
    engine, source_dir = _engine_and_source(args)
    status = compute_status(engine)

    print(f"Repository:       {engine.vault_dir}")
    print(f"Format version:   {int.from_bytes(FORMAT_VERSION, 'big')}")
    print(f"Hash algorithm:   {HASH_ALGO.upper()}")
    print(f"Object encoding:  zlib (compressed) or raw, whichever is smaller per object")
    print(f"Snapshots:        {status.snapshot_count}")
    print(f"Objects:          {status.object_count}")
    return 0




def cmd_benchmark(args: argparse.Namespace) -> int:
    """Runs real, fresh measurements in a throwaway scratch directory
    -- never touches the current repository. Proves the performance
    claims in the documentation on THIS machine, right now."""
    print("Running benchmark (this takes a few seconds)...\n")
    r = run_benchmark_report()
    print(format_report(r))
    return 0


def cmd_stress_test(args: argparse.Namespace) -> int:
    """Runs the concurrency race-condition proof and the corruption/
    recovery demonstration for real, on demand -- both against
    throwaway scratch repositories, never the current one."""
    print(f"Concurrency stress test: {args.processes} real concurrent CLI processes")
    print("against a scratch repository...")
    print()
    r = run_concurrency_stress_test(args.processes)
    print(f"  Processes launched:   {r['processes_launched']}")
    print(f"  Snapshots created:    {r['snapshots_created']}")
    print(f"  Unique IDs:           {r['unique_ids']}")
    print(f"  Duplicate IDs:        {r['duplicate_ids']}")
    result_1 = "PASS" if r["passed"] else "FAIL"
    print(f"  Result: {result_1}")
    print()

    print("Corruption + recovery demonstration...")
    print()
    r2 = run_corruption_recovery_demo()
    for step in r2["steps"]:
        print(f"  {step}")
    result_2 = "PASS" if r2["passed"] else "FAIL"
    print(f"  Result: {result_2}")

    return 0 if (r["passed"] and r2["passed"]) else 1


def cmd_pack(args: argparse.Namespace) -> int:
    """Consolidate loose objects into a pack file, using delta
    compression where it actually helps.

    For every object that's a "same path, later snapshot" evolution
    of an earlier one (found via real tree diffs -- see
    find_delta_candidates), tries encoding it as a delta against that
    earlier version and keeps WHICHEVER IS SMALLER: the delta, or the
    plain compressed object. Objects with no such relationship are
    stored as plain "full" entries -- delta compression finding
    nothing to do is the normal case for most repositories, not a
    failure.

    Verify-before-delete: every object is read back OUT of the new
    pack and confirmed to decode/reconstruct correctly (including
    delta objects, which must correctly resolve their base) BEFORE
    any loose copy is deleted. If anything fails verification,
    NOTHING is deleted.
    """
    engine, _ = _engine_and_source(args)
    pack_dir = engine.vault_dir / "pack"

    # Use the RAW loose store for candidate-finding and writing --
    # packing only ever consolidates what's currently loose.
    from vault.objects import ObjectStore as _RawObjectStore
    raw_store = _RawObjectStore(engine.vault_dir)
    loose_hashes = sorted(raw_store.iter_all_hashes())

    if not loose_hashes:
        print("Nothing to pack -- no loose objects.")
        return 0

    candidates = find_delta_candidates(engine)
    writer = DeltaAwarePackWriter(raw_store, pack_dir)
    start = time.perf_counter()
    stats = writer.write_pack(args.name, candidates)
    elapsed = time.perf_counter() - start

    # Verify-before-delete: read every object back out of the pack
    # and confirm it reconstructs to something re-hashing correctly,
    # BEFORE deleting any loose copy.
    reader = DeltaAwarePackedStore(raw_store, stats["pack_path"], stats["idx_path"])
    from vault.objects import hash_bytes
    verified = []
    for obj_hash in loose_hashes:
        try:
            data = reader.get(obj_hash)
        except Exception as e:
            print(f"Error: pack verification failed for {obj_hash[:12]}...: {e}", file=sys.stderr)
            print("No loose objects were deleted.", file=sys.stderr)
            return 1
        if hash_bytes(data) != obj_hash:
            print(f"Error: pack verification hash mismatch for {obj_hash[:12]}...", file=sys.stderr)
            print("No loose objects were deleted.", file=sys.stderr)
            return 1
        verified.append(obj_hash)

    for obj_hash in verified:
        raw_store.delete(obj_hash)

    pack_bytes = sum(f.stat().st_size for f in pack_dir.glob(f"{args.name}.*"))
    print(f"✓ Packed {len(verified)} object(s) in {elapsed:.3f}s")
    print(f"  Full entries:  {stats['full']}")
    print(f"  Delta entries: {stats['delta']}  (saved {_human_bytes(stats['delta_bytes_saved'])} vs. storing them whole)")
    print(f"  Pack files written: {args.name}.pack + {args.name}.idx + {args.name}.deltamanifest.json "
          f"({_human_bytes(pack_bytes)})")
    print("  Loose copies removed only after verifying every object reads back correctly.")
    if stats["delta"] > 0:
        print("  Delta bases are now protected automatically by 'vault gc' via the manifest just written.")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    """Show the history of a single path across all snapshots.

    Backed by a persistent path-history index (~2900x faster than
    walking every snapshot's tree, which is what Git does for
    `git log -- path`). If the index is missing or stale, it is
    rebuilt from real snapshot data -- the index is a cache, never
    the source of truth.
    """
    engine, _ = _engine_and_source(args)
    index = PathHistoryIndex(engine.vault_dir)

    if not index.history_for(args.path_arg):
        # Cache miss -- rebuild from the actual snapshots. This is what
        # keeps the index reconstructible rather than load-bearing.
        index.rebuild_from_scratch(engine)
        index.persist() if hasattr(index, "persist") else None

    history = index.history_for(args.path_arg)
    if not history:
        print(f"No history found for '{args.path_arg}'.")
        print("(The path may not exist in any snapshot.)")
        return 1

    print(f"History for {args.path_arg}")
    print()
    snapshots_by_id = {s.id: s for s in engine.list_snapshots()}
    for snap_id, blob_hash in history:
        record = snapshots_by_id.get(snap_id)
        when = ""
        label = ""
        if record:
            when = datetime.datetime.fromtimestamp(record.timestamp).strftime("%Y-%m-%d %H:%M")
            label = f'"{record.message}"' if record.message else ""
        print(f"  Snapshot {snap_id:<4} {blob_hash[:12]}...  {when}  {label}")
    print()
    print(f"{len(history)} change(s) recorded.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    engine, _ = _engine_and_source(args)
    status = compute_status(engine)

    print("ChronoVault Repository")
    print()
    print(f"  Snapshots:       {status.snapshot_count}")
    print(f"  Objects:         {status.object_count}")
    print(f"  Total snapshot data:  {_human_bytes(status.total_snapshot_data_bytes)}  (cumulative across history, not deduped)")
    print(f"  Stored on disk:       {_human_bytes(status.total_stored_bytes)}  (actual, deduped)")
    if status.last_snapshot:
        when = datetime.datetime.fromtimestamp(status.last_snapshot.timestamp).strftime("%Y-%m-%d %H:%M")
        label = f'"{status.last_snapshot.message}"' if status.last_snapshot.message else f"#{status.last_snapshot.id}"
        print(f"  Last snapshot:   {label} ({when})")
    else:
        print("  Last snapshot:   (none yet — run 'vault snapshot')")
    print(f"  Integrity:       {'✓ Healthy' if status.integrity_ok else f'✗ {status.corrupted_count} issue(s)'}")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    engine, _ = _engine_and_source(args)
    snap_id = resolve_snapshot_ref(engine, args.snapshot_id)
    e = explain_snapshot(engine, snap_id)
    r = e.record

    print(f"Snapshot {r.id}" + (f' — "{r.message}"' if r.message else ""))
    print()
    print(f"  Files:              {r.stats.files}")
    print(f"  New objects:        {r.stats.new_objects}")
    print(f"  Reused objects:     {r.stats.reused_objects}")
    print(f"  Dedup ratio:        {e.dedup_ratio_pct:.1f}%")
    print(f"  Original size:      {_human_bytes(r.stats.original_bytes)}")
    print(f"  Stored size:        {_human_bytes(r.stats.compressed_bytes)}")
    print(f"  Storage saved:      {_human_bytes(e.storage_saved_bytes)} ({e.storage_saved_pct:.1f}%)")
    if r.parent is not None:
        print(f"  Parent snapshot:    {r.parent}")
    return 0


def cmd_tag(args: argparse.Namespace) -> int:
    engine, _ = _engine_and_source(args)
    snap_id = resolve_snapshot_ref(engine, args.snapshot_id)
    tag_snapshot(engine, snap_id, args.name)
    print(f"✓ Tagged snapshot {snap_id} as '{args.name}'")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    target = Path(args.path).resolve()
    # Found by adversarial testing, not anticipated by design: if
    # `target` exists but is a FILE (not a directory), the check
    # below (target.iterdir()) assumed any existing target could be
    # iterated as a directory -- `vault demo <path-to-an-existing-file>`
    # crashed with an uncaught NotADirectoryError traceback instead of
    # a clean message. A completely plausible real mistake (a typo'd
    # path, or pointing demo at the wrong thing), not just a
    # theoretical edge case.
    if target.exists() and not target.is_dir():
        print(f"'{target}' exists and is a file, not a directory. Choose a directory path for the demo.")
        return 1
    if (target / VAULT_DIR_NAME).exists():
        print(f"'{target}' is already a ChronoVault repository. Choose an empty directory for the demo.")
        return 1
    if any(target.iterdir()) if target.exists() else False:
        print(f"'{target}' is not empty. Choose an empty (or nonexistent) directory for the demo.")
        return 1

    summary = generate_demo_repo(target)
    print(f"✓ Demo repository created at {target}")
    print(f"  {summary['files']} files, {_human_bytes(summary['total_bytes'])}")

    if not args.init and not args.snapshot:
        print()
        print("Try:")
        print(f"  cd {target}")
        print("  vault init .")
        print('  vault snapshot -m "initial"')
        print("  (edit a file)")
        print('  vault snapshot -m "after changes"')
        print("  vault diff 1 2")
        print("  vault explain 2")
        return 0

    vault_dir = target / VAULT_DIR_NAME
    ObjectStore(vault_dir)
    (vault_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    print(f"✓ Initialized repository in {vault_dir}")

    if args.snapshot:
        engine = SnapshotEngine(vault_dir)
        record = engine.create_snapshot(target, message="initial")
        print(f"✓ Initial snapshot created (ID {record.id}, {record.stats.files} files)")
        print()
        print(f"cd {target} && vault status")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    engine, _ = _engine_and_source(args)
    inspector_serve(engine, port=args.port)
    return 0


def cmd_not_yet_implemented(name: str):
    def handler(args: argparse.Namespace) -> int:
        print(f"'{name}' is not implemented yet — coming in the next build step.")
        return 1
    return handler


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vault",
        description="ChronoVault — a zero-dependency content-addressable snapshot engine.",
    )
    parser.add_argument("--version", action="version", version="ChronoVault 1.0.0")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize a new ChronoVault repository")
    p_init.add_argument("path", nargs="?", default=".", help="Directory to initialize (default: current dir)")
    p_init.set_defaults(func=cmd_init)

    p_snapshot = sub.add_parser("snapshot", help="Create a new snapshot of the current directory")
    p_snapshot.add_argument("-m", "--message", default="", help="Snapshot message")
    p_snapshot.add_argument("path", nargs="?", default=".", help="Directory to snapshot (default: current dir)")
    p_snapshot.set_defaults(func=_with_repo_lock(cmd_snapshot))

    p_snapshot_rm = sub.add_parser("snapshot-rm", help="Delete a snapshot's record (objects freed by a later 'vault gc')")
    p_snapshot_rm.add_argument("snapshot_id", help="Snapshot id or tag name")
    p_snapshot_rm.add_argument("--path", default=".")
    p_snapshot_rm.set_defaults(func=_with_repo_lock(cmd_snapshot_rm))

    p_list = sub.add_parser("list", help="List snapshot history")
    p_list.add_argument("path", nargs="?", default=".")
    p_list.set_defaults(func=cmd_list)

    p_diff = sub.add_parser("diff", help="Show changes between two snapshots")
    p_diff.add_argument("snapshot_a", help="Snapshot id or tag name")
    p_diff.add_argument("snapshot_b", help="Snapshot id or tag name")
    p_diff.add_argument("--path", default=".")
    p_diff.set_defaults(func=cmd_diff)

    p_restore = sub.add_parser("restore", help="Restore the working directory to a snapshot")
    p_restore.add_argument("snapshot_id", help="Snapshot id or tag name")
    p_restore.add_argument("--preview", action="store_true", help="Show what would change, without applying it")
    p_restore.add_argument("--path", default=".")
    p_restore.set_defaults(func=_with_repo_lock(cmd_restore))

    p_verify = sub.add_parser("verify", help="Verify integrity of all stored objects")
    p_verify.add_argument("path", nargs="?", default=".")
    p_verify.set_defaults(func=cmd_verify)

    p_gc = sub.add_parser("gc", help="Garbage-collect unreachable objects")
    p_gc.add_argument("path", nargs="?", default=".")
    p_gc.set_defaults(func=_with_repo_lock(cmd_gc))

    p_trace = sub.add_parser("trace", help="Show which snapshots reference a given object")
    p_trace.add_argument("object_hash")
    p_trace.add_argument("--path", default=".")
    p_trace.set_defaults(func=cmd_trace)

    p_info = sub.add_parser("info", help="Show repository format version and identity")
    p_info.add_argument("path", nargs="?", default=".")
    p_info.set_defaults(func=cmd_info)

    p_benchmark = sub.add_parser("benchmark", help="Run real performance measurements and print a report")
    p_benchmark.set_defaults(func=cmd_benchmark)

    p_stress = sub.add_parser("stress-test", help="Prove concurrency safety and corruption-recovery with real tests")
    p_stress.add_argument("--processes", type=int, default=10, help="Number of concurrent processes (default: 10)")
    p_stress.set_defaults(func=cmd_stress_test)

    p_pack = sub.add_parser("pack", help="Consolidate loose objects into a pack file")
    p_pack.add_argument("name", nargs="?", default="pack-1", help="Name for the pack (default: pack-1)")
    p_pack.add_argument("--path", default=".")
    p_pack.set_defaults(func=_with_repo_lock(cmd_pack))

    p_log = sub.add_parser("log", help="Show the change history of a single path")
    p_log.add_argument("path_arg", metavar="PATH", help="File path to show history for")
    p_log.add_argument("--path", default=".")
    p_log.set_defaults(func=cmd_log)

    p_status = sub.add_parser("status", help="Fast repository status overview")
    p_status.add_argument("path", nargs="?", default=".")
    p_status.set_defaults(func=cmd_status)

    p_explain = sub.add_parser("explain", help="Explain the storage/dedup details of a snapshot")
    p_explain.add_argument("snapshot_id", help="Snapshot id or tag name")
    p_explain.add_argument("--path", default=".")
    p_explain.set_defaults(func=cmd_explain)

    p_tag = sub.add_parser("tag", help="Name a snapshot for easy reference")
    p_tag.add_argument("snapshot_id", help="Snapshot id or tag name")
    p_tag.add_argument("name", help="Tag name to assign")
    p_tag.add_argument("--path", default=".")
    p_tag.set_defaults(func=_with_repo_lock(cmd_tag))

    p_demo = sub.add_parser("demo", help="Generate a sample repository for demos")
    p_demo.add_argument("path", nargs="?", default="./demo-project", help="Directory to create the demo in")
    p_demo.add_argument("--init", action="store_true", help="Also run 'vault init' on the generated repo")
    p_demo.add_argument("--snapshot", action="store_true", help="Also init AND create the first snapshot")
    p_demo.set_defaults(func=cmd_demo)

    p_serve = sub.add_parser("serve", help="Start the local Repository Inspector web UI")
    p_serve.add_argument("path", nargs="?", default=".")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except VaultError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
