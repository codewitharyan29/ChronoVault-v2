#!/usr/bin/env python3
"""
scripts/benchmark.py — produces real, measured numbers for BENCHMARKS.md.
Not estimates: this actually creates files, snapshots them, and times it.

Usage: python3 scripts/benchmark.py
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.gc import run_gc
from vault.snapshot import SnapshotEngine


def make_files(root: Path, count: int, avg_size: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        content = f"file {i}\n".encode() * (avg_size // 8 + 1)
        (root / f"file_{i:05d}.txt").write_bytes(content[:avg_size])


def human_bytes(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def run_benchmark(file_count: int, avg_size: int):
    root = Path(tempfile.mkdtemp())
    vault_dir = root / ".vault"
    source_dir = root / "project"
    make_files(source_dir, file_count, avg_size)

    engine = SnapshotEngine(vault_dir)

    t0 = time.perf_counter()
    s1 = engine.create_snapshot(source_dir, message="first")
    t_snapshot1 = time.perf_counter() - t0

    # Modify ~5% of files with UNIQUE content each — realistic edit
    # scenario. (First version of this script overwrote all modified
    # files with identical content, which meant dedup collapsed them
    # into a single new object regardless of how many files were
    # "changed" — technically correct behavior, but a misleading
    # benchmark, since it doesn't reflect what a real edit looks like.)
    modify_count = max(1, file_count // 20)
    for i in range(modify_count):
        unique_content = f"MODIFIED file {i} at {time.time()}\n".encode() * (avg_size // 32 + 1)
        (source_dir / f"file_{i:05d}.txt").write_bytes(unique_content[:avg_size])

    t0 = time.perf_counter()
    s2 = engine.create_snapshot(source_dir, message="second")
    t_snapshot2 = time.perf_counter() - t0

    t0 = time.perf_counter()
    all_hashes = list(engine.store.iter_all_hashes())
    _ = [h for h in all_hashes if not engine.store.verify_object(h)]
    t_verify = time.perf_counter() - t0

    t0 = time.perf_counter()
    gc_result = run_gc(engine)
    t_gc = time.perf_counter() - t0

    total_stored = sum(engine.store.compressed_size(h) for h in engine.store.iter_all_hashes())

    result = {
        "file_count": file_count,
        "avg_size": avg_size,
        "snapshot1_time": t_snapshot1,
        "snapshot2_time": t_snapshot2,
        "snapshot2_reused": s2.stats.reused_objects,
        "snapshot2_new": s2.stats.new_objects,
        "verify_time": t_verify,
        "verify_count": len(all_hashes),
        "gc_time": t_gc,
        "gc_deleted": gc_result.objects_deleted,
        "original_total": s1.stats.original_bytes + s2.stats.original_bytes,
        "stored_total": total_stored,
    }
    shutil.rmtree(root, ignore_errors=True)
    return result


def main():
    print("ChronoVault Benchmark\n")
    configs = [(100, 2000), (1000, 2000), (5000, 1000)]

    for file_count, avg_size in configs:
        r = run_benchmark(file_count, avg_size)
        saved_pct = 100 * (1 - r["stored_total"] / r["original_total"]) if r["original_total"] else 0

        print(f"## {file_count} files (~{human_bytes(avg_size)} each)")
        print()
        print(f"- First snapshot:   {r['snapshot1_time']:.3f}s ({file_count} new objects)")
        print(f"- Second snapshot:  {r['snapshot2_time']:.3f}s "
              f"({r['snapshot2_new']} new, {r['snapshot2_reused']} reused)")
        print(f"- Verify:           {r['verify_time']:.3f}s ({r['verify_count']} objects)")
        print(f"- GC:               {r['gc_time']:.3f}s ({r['gc_deleted']} objects deleted)")
        print(f"- Original data:    {human_bytes(r['original_total'])}")
        print(f"- Stored on disk:   {human_bytes(r['stored_total'])}")
        print(f"- Space saved:      {saved_pct:.1f}%")
        print()


if __name__ == "__main__":
    main()
