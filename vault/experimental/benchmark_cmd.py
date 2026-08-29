"""
vault/experimental/benchmark_cmd.py

Powers `vault benchmark` -- runs the ACTUAL claims this README/verdict
table makes (loose vs packed reads, path-history speedup) as real
measurements taken fresh on THIS machine, not numbers copy-pasted from
a prior benchmarking session. Operates entirely in a throwaway temp
directory -- never touches the user's real repository.
"""

from __future__ import annotations

import random
import shutil
import tempfile
import time
from pathlib import Path


def _real_disk_usage(path: Path) -> int:
    """
    Actual disk BLOCKS consumed (via os.stat().st_blocks, POSIX-only),
    not logical byte size (st_size). This distinction matters a lot:
    a directory of many tiny files can show a SMALL total st_size but
    consume much MORE real disk space, because most filesystems
    allocate in fixed-size blocks (commonly 4096 bytes) regardless of
    how small the actual content is.

    An earlier version of this benchmark used st_size and reported
    "Disk reduction: 0.4x" (packing looked WORSE than loose storage)
    -- which contradicted the actual finding this whole feature is
    based on. Caught before shipping by noticing the number looked
    wrong, not by assuming it was right. st_blocks is always in
    512-byte units regardless of the filesystem's actual block size,
    per POSIX.
    """
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_blocks * 512
            except AttributeError:
                # st_blocks doesn't exist on this platform (e.g. Windows) --
                # fall back to logical size, and this function's caller
                # should treat the resulting ratio as less meaningful there.
                total += f.stat().st_size
    return total


def _human_bytes(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    # Unreachable in practice (the loop always returns on "GB" at the
    # latest), but mypy correctly flags the annotated -> str return
    # type as violable without an explicit fallback -- found via
    # mypy, not by design. This same helper's sibling copy in
    # vault/cli.py already had this exact trailing return; this one
    # didn't, a real gap between the two duplicated copies.
    return f"{n:.1f} GB"


def run_benchmark_report(n_objects: int = 3000, n_reads: int = 1000, n_path_snapshots: int = 200) -> dict:
    from vault.experimental.packfile_v2 import PackedObjectStoreV2
    from vault.experimental.path_history import PathHistoryIndex
    from vault.objects import ObjectStore
    from vault.snapshot import SnapshotEngine

    scratch = Path(tempfile.mkdtemp(prefix="cv-benchmark-"))
    results = {}
    try:
        # --- Loose vs packed reads ---
        store = ObjectStore(scratch / "objstore")
        hashes = [store.put(f"small config value {i}".encode()).obj_hash for i in range(n_objects)]
        sample = random.sample(hashes, min(n_reads, n_objects))

        t0 = time.perf_counter()
        for h in sample:
            store.get(h)
        t_loose_read = time.perf_counter() - t0
        loose_disk = _real_disk_usage(scratch / "objstore" / "objects")

        pos = PackedObjectStoreV2(store, scratch / "objstore" / "pack")
        t0 = time.perf_counter()
        pos.pack_and_prune("bench")
        t_pack_time = time.perf_counter() - t0

        t0 = time.perf_counter()
        for h in sample:
            pos.get(h)
        t_packed_read = time.perf_counter() - t0
        pack_disk = _real_disk_usage(scratch / "objstore" / "pack")
        pos.close()

        results["loose_read_ms"] = t_loose_read * 1000
        results["packed_read_ms"] = t_packed_read * 1000
        results["read_speedup"] = t_loose_read / t_packed_read if t_packed_read > 0 else 0
        results["loose_disk_bytes"] = loose_disk
        results["pack_disk_bytes"] = pack_disk
        results["disk_reduction"] = loose_disk / pack_disk if pack_disk > 0 else 0
        results["pack_time_s"] = t_pack_time
        results["n_objects"] = n_objects
        results["n_reads"] = len(sample)

        # --- Path history: indexed vs brute-force ---
        vault_dir = scratch / "pathtest" / ".vault"
        source_dir = scratch / "pathtest" / "project"
        source_dir.mkdir(parents=True)
        engine = SnapshotEngine(vault_dir)
        index = PathHistoryIndex(vault_dir)
        for i in range(n_path_snapshots):
            (source_dir / "target.py").write_text(f"version {i}")
            record = engine.create_snapshot(source_dir, message=f"v{i}")
            index.record_snapshot(engine, record.id, record.root_tree_hash)

        t0 = time.perf_counter()
        index.history_for("target.py")
        t_indexed = time.perf_counter() - t0

        t0 = time.perf_counter()
        last_hash = None
        for record in engine.list_snapshots():
            entries = engine.load_tree(record.root_tree_hash)
            entry = next((e for e in entries if e.name == "target.py"), None)
            if entry:
                last_hash = entry.obj_hash
        t_brute = time.perf_counter() - t0

        results["path_history_indexed_ms"] = t_indexed * 1000
        results["path_history_brute_ms"] = t_brute * 1000
        results["path_history_speedup"] = t_brute / t_indexed if t_indexed > 0 else 0
        results["n_path_snapshots"] = n_path_snapshots

        return results
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def format_report(r: dict) -> str:
    lines = []
    lines.append("ChronoVault Performance Benchmark")
    lines.append("(measured fresh on this machine, right now -- not cached numbers)")
    lines.append("")
    lines.append(f"Object storage ({r['n_objects']} objects, {r['n_reads']} random reads):")
    lines.append(f"  Loose  read:   {r['loose_read_ms']:.1f} ms   disk: {_human_bytes(r['loose_disk_bytes'])}")
    lines.append(f"  Packed read:   {r['packed_read_ms']:.1f} ms   disk: {_human_bytes(r['pack_disk_bytes'])}")
    lines.append(f"  Pack build time: {r['pack_time_s']*1000:.1f} ms")
    lines.append("")
    lines.append(f"  Read speedup:    {r['read_speedup']:.1f}x")
    lines.append(f"  Disk reduction:  {r['disk_reduction']:.1f}x")
    lines.append("")
    lines.append(f"Path history ({r['n_path_snapshots']} snapshots):")
    lines.append(f"  Brute-force walk: {r['path_history_brute_ms']:.2f} ms")
    lines.append(f"  Indexed lookup:   {r['path_history_indexed_ms']:.4f} ms")
    lines.append(f"  Speedup:          {r['path_history_speedup']:.0f}x")
    return "\n".join(lines)
