"""
vault/experimental/stress_test_cmd.py

Powers `vault stress-test`. Runs the ACTUAL race-condition proof (real
concurrent subprocess invocations of the real CLI against a scratch
repository) plus a real corruption-and-recovery demonstration, on
demand, from inside the tool itself -- not just documented in
BENCHMARKS.md/commit.md.

Everything happens in a throwaway temp directory. Never touches the
user's real repository.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CHRONOVAULT_PY = Path(__file__).resolve().parent.parent.parent / "chronovault.py"


def run_concurrency_stress_test(n_processes: int = 10) -> dict:
    """
    Spawns N REAL OS processes (subprocess, not threads -- the race
    this is proving is in file I/O, which threads wouldn't exercise
    the same way) all running `vault snapshot` against the SAME
    scratch repository at once.
    """
    scratch = Path(tempfile.mkdtemp(prefix="cv-stress-"))
    try:
        subprocess.run([sys.executable, str(CHRONOVAULT_PY), "init", str(scratch)],
                        capture_output=True, check=True)
        (scratch / "f.txt").write_text("initial content")

        procs = []
        for i in range(n_processes):
            (scratch / f"marker_{i}.txt").write_text(f"content {i}")
            proc = subprocess.Popen(
                [sys.executable, str(CHRONOVAULT_PY), "snapshot", "-m", f"concurrent {i}", str(scratch)],
                cwd=str(scratch), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            procs.append(proc)

        for proc in procs:
            proc.wait(timeout=60)

        list_result = subprocess.run(
            [sys.executable, str(CHRONOVAULT_PY), "list", str(scratch)],
            capture_output=True, text=True,
        )
        snapshot_lines = [l for l in list_result.stdout.splitlines() if l.strip() and l.strip()[0].isdigit()]
        snapshot_ids = [int(l.split()[0]) for l in snapshot_lines]

        return {
            "processes_launched": n_processes,
            "snapshots_created": len(snapshot_ids),
            "unique_ids": len(set(snapshot_ids)),
            "duplicate_ids": len(snapshot_ids) - len(set(snapshot_ids)),
            "passed": len(set(snapshot_ids)) == len(snapshot_ids) and len(snapshot_ids) == n_processes,
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_corruption_recovery_demo() -> dict:
    """
    The failure/recovery story, run for real: create a repo, snapshot
    it, corrupt an object on disk (simulating a crashed write or disk
    fault), confirm verify DETECTS it, confirm restore of THAT
    snapshot correctly ABORTS (doesn't write corrupted data), then
    confirm a DIFFERENT, uncorrupted snapshot still restores
    correctly -- proving the repository as a whole remains usable
    even with one damaged object.
    """
    from vault.objects import VaultError
    from vault.restore import apply_restore
    from vault.snapshot import SnapshotEngine

    scratch = Path(tempfile.mkdtemp(prefix="cv-crash-"))
    result = {"steps": []}
    try:
        vault_dir = scratch / ".vault"
        source_dir = scratch / "project"
        source_dir.mkdir()
        engine = SnapshotEngine(vault_dir)

        (source_dir / "important.txt").write_text("critical data v1")
        s1 = engine.create_snapshot(source_dir, message="before corruption")
        result["steps"].append("Created snapshot 1 (healthy)")

        (source_dir / "important.txt").write_text("critical data v2")
        s2 = engine.create_snapshot(source_dir, message="after edit")
        result["steps"].append("Created snapshot 2 (healthy)")

        # Simulate a crash/disk fault: corrupt the object s1 needs.
        entries = engine.load_tree(s1.root_tree_hash)
        target_hash = next(e.obj_hash for e in entries if e.name == "important.txt")
        obj_path = engine.store._object_path(target_hash)
        raw = bytearray(obj_path.read_bytes())
        raw[-1] ^= 0xFF
        obj_path.write_bytes(bytes(raw))
        result["steps"].append(f"Simulated corruption of object {target_hash[:12]}...")

        # verify must detect it.
        all_hashes = list(engine.store.iter_all_hashes())
        corrupted = [h for h in all_hashes if not engine.store.verify_object(h)]
        result["verify_detected_corruption"] = target_hash in corrupted
        result["steps"].append(f"vault verify: {'DETECTED' if target_hash in corrupted else 'MISSED'} the corruption")

        # Restoring the CORRUPTED snapshot must abort safely.
        shutil.rmtree(source_dir)
        source_dir.mkdir()
        try:
            apply_restore(engine, source_dir, s1.id)
            result["corrupted_restore_aborted_safely"] = False
        except VaultError:
            result["corrupted_restore_aborted_safely"] = True
        result["steps"].append(
            f"Restoring corrupted snapshot 1: {'ABORTED SAFELY' if result['corrupted_restore_aborted_safely'] else 'DID NOT ABORT (bad)'}"
        )

        # A DIFFERENT, healthy snapshot must still restore correctly --
        # the repo isn't a total loss just because one object is bad.
        apply_restore(engine, source_dir, s2.id)
        recovered_correctly = (source_dir / "important.txt").read_text() == "critical data v2"
        result["healthy_snapshot_recovered"] = recovered_correctly
        result["steps"].append(
            f"Restoring healthy snapshot 2: {'RECOVERED CORRECTLY' if recovered_correctly else 'FAILED (bad)'}"
        )

        result["passed"] = (
            result["verify_detected_corruption"]
            and result["corrupted_restore_aborted_safely"]
            and result["healthy_snapshot_recovered"]
        )
        return result
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
