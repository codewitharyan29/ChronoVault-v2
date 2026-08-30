#!/usr/bin/env python3
"""
scripts/content_addressing_proof.py

An EXECUTABLE proof of ChronoVault's central engineering thesis:

    content is stored once, addressed by its SHA-256, and every snapshot
    just references those objects -- so identical content is never
    duplicated, a change creates exactly one new object, and packing /
    delta-encoding / GC never alter the logical identity or the bytes.

This is not a product feature. It runs the REAL storage operations
(SnapshotEngine, the `pack` and `gc` CLI paths, PackAwareObjectStore)
against a throwaway directory, checks eight invariants, and prints a
compact PASS/FAIL scorecard. Stdlib only. Nothing outside the temp
directory is touched.

    python scripts/content_addressing_proof.py
    python scripts/content_addressing_proof.py --json
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vault.cli import main as cli_main
from vault.objects import hash_bytes
from vault.experimental.delta_gc import load_all_delta_manifests
from vault.experimental.pack_aware_store import PackAwareObjectStore
from vault.snapshot import SnapshotEngine

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def _cli(*argv) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli_main(list(argv))
    return code, buf.getvalue()


def _entries(engine: SnapshotEngine, snap_id: int) -> dict[str, str]:
    """{path: object_hash} for every file in a snapshot."""
    rec = engine.load_snapshot(snap_id)
    return {
        path: obj_hash
        for path, kind, obj_hash in engine.walk_tree_entries(rec.root_tree_hash)
        if kind == "file"
    }


def run_proof(work: Path) -> list[tuple[str, bool, str]]:
    """Returns [(check, passed, note), ...]."""
    checks: list[tuple[str, bool, str]] = []
    repo = work / "repo"
    src = repo / "src"
    src.mkdir(parents=True)

    CONTENT_X = b"def a():\n    return 1\n" + b"# padding line\n" * 40
    CONTENT_Y = b"config = {'v': 1}\n" + b"# note\n" * 40

    # byte-identical content at two different paths
    (src / "a.py").write_bytes(CONTENT_X)
    (src / "b.py").write_bytes(CONTENT_X)
    (src / "c.py").write_bytes(CONTENT_Y)
    # a file we will grow across snapshots so `pack` has a real delta to make
    big_v1 = b"".join(b"line %d\n" % i for i in range(400))
    (src / "big.py").write_bytes(big_v1)

    _cli("init", str(repo))
    _cli("snapshot", "-m", "s1", str(repo))
    engine = SnapshotEngine(repo / ".vault")
    e1 = _entries(engine, 1)

    # 1. identical content -> identical hash (pure function, computed twice)
    checks.append((
        "identical content -> identical object hash",
        hash_bytes(CONTENT_X) == hash_bytes(CONTENT_X)
        and e1["src/a.py"] == hash_bytes(CONTENT_X),
        f"hash={e1['src/a.py'][:16]}...",
    ))

    # 2. two paths, one object -- dedup within a snapshot
    checks.append((
        "two paths with equal bytes share one object",
        e1["src/a.py"] == e1["src/b.py"] and e1["src/a.py"] != e1["src/c.py"],
        "src/a.py and src/b.py resolve to the same hash",
    ))

    # 3. a second snapshot with no changes reuses the same objects
    _cli("snapshot", "-m", "s2 (no change)", str(repo))
    e2 = _entries(engine, 2)
    checks.append((
        "unchanged files across snapshots reference the same object",
        e2 == e1,
        "snapshot 2's object hashes are identical to snapshot 1's",
    ))

    # 4. modify one file -> exactly one new object identity; the rest reused
    (src / "c.py").write_bytes(CONTENT_Y + b"\nchanged = True\n")
    (src / "big.py").write_bytes(big_v1 + b"appended once\n")
    _cli("snapshot", "-m", "s3 (edit c + big)", str(repo))
    e3 = _entries(engine, 3)
    changed = {p for p in e3 if e3[p] != e2.get(p)}
    checks.append((
        "modification creates a new object; unchanged files keep theirs",
        changed == {"src/c.py", "src/big.py"}
        and e3["src/a.py"] == e1["src/a.py"],
        f"changed objects: {sorted(changed)}",
    ))

    # snapshot big.py once more so pack definitely finds a delta chain
    (src / "big.py").write_bytes(big_v1 + b"appended once\nappended twice\n")
    _cli("snapshot", "-m", "s4 (grow big again)", str(repo))
    e4 = _entries(engine, 4)

    # capture the exact bytes of every object BEFORE packing
    pre_pack_store = PackAwareObjectStore(repo / ".vault")
    all_hashes = set()
    for sid in (1, 2, 3, 4):
        all_hashes |= set(_entries(engine, sid).values())
    original_bytes = {h: pre_pack_store.get(h) for h in all_hashes}

    # 5. pack: every object still resolves to bytes that re-hash to itself
    pcode, pout = _cli("pack", "--path", str(repo))
    manifest = load_all_delta_manifests(repo / ".vault" / "pack")
    post_pack_store = PackAwareObjectStore(repo / ".vault")
    identity_ok = pcode == 0 and all(
        hash_bytes(post_pack_store.get(h)) == h for h in all_hashes
    )
    checks.append((
        "pack preserves every object's logical identity",
        identity_ok,
        f"{len(all_hashes)} objects re-hash correctly out of the pack",
    ))

    # 6. delta round-trip: at least one object was delta-encoded, and it
    #    reconstructs to the exact original bytes
    delta_targets = [h for h in all_hashes if h in manifest]
    delta_ok = bool(delta_targets) and all(
        post_pack_store.get(h) == original_bytes[h] for h in delta_targets
    )
    checks.append((
        "delta-encoded objects reconstruct the original bytes exactly",
        delta_ok,
        f"{len(delta_targets)} delta object(s); byte-identical after reconstruction"
        if delta_targets else "no delta produced by pack -- fixture did not force one",
    ))

    # 7. gc: the shared object (a.py/b.py) survives and is still intact
    gcode, _ = _cli("gc", str(repo))
    after_gc_store = PackAwareObjectStore(repo / ".vault")
    shared = e1["src/a.py"]
    gc_ok = gcode == 0 and hash_bytes(after_gc_store.get(shared)) == shared
    checks.append((
        "GC keeps reachable shared objects intact",
        gc_ok,
        f"shared object {shared[:16]}... still verifies after gc",
    ))

    # 8. verify agrees the repository is healthy end to end
    vcode, vout = _cli("verify", str(repo))
    checks.append((
        "verify confirms full integrity after pack + gc",
        vcode == 0 and "healthy" in vout.lower(),
        vout.strip().splitlines()[-1] if vout.strip() else "",
    ))

    return checks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="cv-ca-proof-"))
    try:
        checks = run_proof(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    passed = all(ok for _, ok, _ in checks)

    if args.json:
        print(json.dumps({
            "checks": [
                {"check": name, "result": "PASS" if ok else "FAIL", "note": note}
                for name, ok, note in checks
            ],
            "result": "PASS" if passed else "FAIL",
        }, indent=2, sort_keys=True))
        return 0 if passed else 1

    print("CONTENT-ADDRESSING PROOF")
    print("=" * 56)
    for name, ok, note in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if note:
            print(f"       {note}")
    print("=" * 56)
    print(f"RESULT: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
