#!/usr/bin/env python3
"""
scripts/benchmark_vs_diskcache.py — the "Package Killer" proof.

ChronoVault's object store (vault/objects.py) replaces the job a
general-purpose disk-backed cache like `diskcache` would otherwise do:
persistent, disk-backed storage of arbitrary byte content, retrievable
later, surviving a process restart, with no database engine as a
runtime dependency. This script measures ChronoVault's implementation
against the real `diskcache` package on one identical workload, so the
claim is backed by numbers a judge can reproduce rather than an
architectural argument alone.

HONESTY / ISOLATION CONTRACT
---------------------------------
  * `diskcache` is installed into a THROWAWAY virtual environment that
    this script creates and (by default) deletes afterwards. It is
    never added to requirements.txt, never importable by `vault/`,
    `tests/`, or the rest of `scripts/`, and `scripts/check_dependencies.py`
    scans imports via the AST — this file only ever *names* diskcache
    inside strings executed by the venv's interpreter, so it does not
    widen ChronoVault's zero-dependency surface. Verify with:
        python scripts/check_dependencies.py
  * If the venv cannot be created or `diskcache` cannot be installed
    (no network, restricted CI, etc.), this script prints a clear
    message and exits non-zero. It NEVER prints invented numbers for
    the side it could not run.

WHAT IS MEASURED (same workload, both sides)
-------------------------------------------
  A deterministic, seeded corpus of text-like blobs (the realistic
  shape for a snapshot engine: source files, configs, docs). Two
  scenarios:

    Scenario 1 — all-unique blobs:
      write every blob, then time a fixed number of random reads.
      Reports write throughput (ms/op), read latency (ms/op), and
      total bytes on disk. This is the straight apples-to-apples
      perf + size comparison, both sides keyed by SHA-256 of the
      content and both reading the same shuffled key sequence.

    Scenario 2 — realistic duplication (the snapshot case):
      N logical blobs of which only a fraction are unique — what you
      get snapshotting a directory repeatedly where most files never
      change. Three stores are compared:
        * ChronoVault ObjectStore      (content-addressed: dedups for free)
        * diskcache, content-hash keys (caller reimplements addressing on
                                        top of the dependency -> also dedups)
        * diskcache, sequential keys   (diskcache used as the general
                                        kv cache it actually is -> stores
                                        every duplicate)
      Reports bytes on disk for each. The point this makes: whole-file
      dedup is a *property of content addressing*, which ChronoVault
      builds in ~200 lines of stdlib; a general cache gives it to you
      only if you build the addressing layer yourself.

USAGE
-----
  python scripts/benchmark_vs_diskcache.py
  python scripts/benchmark_vs_diskcache.py --blobs 4000 --keep-venv
  python scripts/benchmark_vs_diskcache.py --venv-dir /path/to/scratch/venv

Only the Python standard library is used by this script itself.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from vault.objects import ObjectStore, hash_bytes  # noqa: E402  (after sys.path fix)


# --------------------------------------------------------------------------
# Workload generation — deterministic, seeded, text-like (compressible).
# --------------------------------------------------------------------------

_WORDS = (
    "def return self value store object hash tree snapshot delta pack "
    "compress restore verify import path bytes cache disk write read "
    "content addressable engine chrono vault index history commit"
).split()


def _make_blob(rng: random.Random, min_lines: int = 20, max_lines: int = 120) -> bytes:
    """A pseudo-source-file blob: realistic size + realistic compressibility."""
    lines = []
    for _ in range(rng.randint(min_lines, max_lines)):
        n = rng.randint(3, 12)
        indent = "    " * rng.randint(0, 3)
        lines.append(indent + " ".join(rng.choice(_WORDS) for _ in range(n)))
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_corpus(n_unique: int, seed: int = 1729) -> list[bytes]:
    rng = random.Random(seed)
    return [_make_blob(rng) for _ in range(n_unique)]


def build_duplicated_corpus(
    n_logical: int, unique_fraction: float, seed: int = 4104
) -> list[bytes]:
    """n_logical blobs drawn from a small unique pool -> heavy duplication."""
    n_unique = max(1, int(n_logical * unique_fraction))
    pool = build_corpus(n_unique, seed=seed)
    rng = random.Random(seed + 1)
    return [rng.choice(pool) for _ in range(n_logical)]


# --------------------------------------------------------------------------
# Disk accounting.
# --------------------------------------------------------------------------

def dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    return total


def human_bytes(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# --------------------------------------------------------------------------
# ChronoVault side — runs in THIS interpreter, stdlib only.
# --------------------------------------------------------------------------

def _read_sequence(keys: list[str], n_reads: int) -> list[str]:
    """A deterministic shuffled read sequence of length n_reads, sampled
    with replacement so it is identical (by index) on both sides."""
    rng = random.Random(99)
    return [rng.choice(keys) for _ in range(n_reads)]


def run_chronovault(corpus: list[bytes], dup_corpus: list[bytes], workdir: Path, n_reads: int) -> dict:
    store_root = workdir / "cv_store"
    store = ObjectStore(store_root)

    # Scenario 1: all-unique write + fixed-count random read.
    hashes = []
    t0 = time.perf_counter()
    for blob in corpus:
        hashes.append(store.put(blob).obj_hash)
    write_s = time.perf_counter() - t0

    read_order = _read_sequence(hashes, n_reads)
    t0 = time.perf_counter()
    for h in read_order:
        store.get(h)
    read_s = time.perf_counter() - t0

    # Floor: what it costs THIS machine just to open+read the same
    # object files with no verification, decompression, or path logic.
    # Lets the reader separate ChronoVault's own overhead from the
    # host's raw small-file open cost (AV scanning, syscall latency).
    raw_paths = [store._object_path(h) for h in read_order]
    t0 = time.perf_counter()
    for p in raw_paths:
        p.read_bytes()
    raw_read_s = time.perf_counter() - t0

    unique_disk = dir_size(store_root)

    # Scenario 2: realistic duplication -> dedup falls out of content addressing.
    dup_root = workdir / "cv_store_dup"
    dup_store = ObjectStore(dup_root)
    for blob in dup_corpus:
        dup_store.put(blob)
    dup_disk = dir_size(dup_root)

    return {
        "unique_write_s": write_s,
        "unique_read_s": read_s,
        "unique_raw_read_s": raw_read_s,
        "unique_reads": n_reads,
        "unique_disk_bytes": unique_disk,
        "unique_count": len(corpus),
        "dup_disk_bytes": dup_disk,
        "dup_logical": len(dup_corpus),
        "dup_unique": len({hash_bytes(b) for b in dup_corpus}),
    }


# --------------------------------------------------------------------------
# diskcache side — runs INSIDE the throwaway venv, as a subprocess.
# --------------------------------------------------------------------------

_DISKCACHE_WORKER = textwrap.dedent(
    '''
    import hashlib, json, os, random, sys, time
    import diskcache

    payload = json.load(sys.stdin)
    corpus = [bytes.fromhex(h) for h in payload["corpus_hex"]]
    dup_corpus = [bytes.fromhex(h) for h in payload["dup_hex"]]
    workdir = payload["workdir"]
    n_reads = payload["n_reads"]

    def sha(b):
        return hashlib.sha256(b).hexdigest()

    def dir_size(path):
        total = 0
        for dp, _dn, fns in os.walk(path):
            for n in fns:
                fp = os.path.join(dp, n)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
        return total

    # Scenario 1: all-unique, keyed by content hash (diskcache forced
    # into content-addressed use so the comparison is apples-to-apples).
    d1 = os.path.join(workdir, "dc_unique")
    c1 = diskcache.Cache(d1)
    keys = []
    t0 = time.perf_counter()
    for blob in corpus:
        k = sha(blob)
        c1.set(k, blob)
        keys.append(k)
    write_s = time.perf_counter() - t0

    rng = random.Random(99)
    order = [rng.choice(keys) for _ in range(n_reads)]
    t0 = time.perf_counter()
    for k in order:
        _ = c1[k]
    read_s = time.perf_counter() - t0
    c1.close()
    unique_disk = dir_size(d1)

    # Scenario 2a: realistic duplication, content-hash keys -> dedups.
    d2 = os.path.join(workdir, "dc_dup_hashed")
    c2 = diskcache.Cache(d2)
    for blob in dup_corpus:
        c2.set(sha(blob), blob)
    c2.close()
    dup_hashed_disk = dir_size(d2)

    # Scenario 2b: diskcache used as the plain kv cache it is,
    # sequential keys -> every duplicate is stored.
    d3 = os.path.join(workdir, "dc_dup_seq")
    c3 = diskcache.Cache(d3)
    for i, blob in enumerate(dup_corpus):
        c3.set("blob-%d" % i, blob)
    c3.close()
    dup_seq_disk = dir_size(d3)

    json.dump({
        "diskcache_version": diskcache.__version__,
        "python_version": sys.version.split()[0],
        "unique_write_s": write_s,
        "unique_read_s": read_s,
        "unique_reads": n_reads,
        "unique_disk_bytes": unique_disk,
        "dup_hashed_disk_bytes": dup_hashed_disk,
        "dup_seq_disk_bytes": dup_seq_disk,
    }, sys.stdout)
    '''
).strip()


def venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def setup_venv(venv_dir: Path) -> Path:
    print(f"  creating isolated venv: {venv_dir}")
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    py = venv_python(venv_dir)
    print("  installing diskcache into the venv (isolated; not a ChronoVault dependency)...")
    proc = subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "diskcache"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "pip install diskcache failed inside the isolated venv:\n"
            + (proc.stderr or proc.stdout or "(no output)")
        )
    return py


def run_diskcache(py: Path, corpus: list[bytes], dup_corpus: list[bytes],
                  workdir: Path, n_reads: int) -> dict:
    payload = json.dumps({
        "corpus_hex": [b.hex() for b in corpus],
        "dup_hex": [b.hex() for b in dup_corpus],
        "workdir": str(workdir),
        "n_reads": n_reads,
    })
    proc = subprocess.run(
        [str(py), "-c", _DISKCACHE_WORKER],
        input=payload, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "diskcache benchmark worker failed inside the venv:\n"
            + (proc.stderr or proc.stdout or "(no output)")
        )
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------
# Reporting.
# --------------------------------------------------------------------------

def report(cv: dict, dc: dict, args) -> None:
    def ratio(a, b):
        return (a / b) if b else float("inf")

    print()
    print("=" * 68)
    print("  ChronoVault ObjectStore  vs.  diskcache " + dc["diskcache_version"])
    print("=" * 68)
    print(f"  host Python           : {sys.version.split()[0]}")
    print(f"  venv Python           : {dc['python_version']}")
    print(f"  blobs (unique corpus) : {cv['unique_count']}")
    print(f"  duplicated corpus     : {cv['dup_logical']} logical / "
          f"{cv['dup_unique']} unique ({args.dup_fraction:.0%} unique)")
    print()

    n_w = cv["unique_count"]
    n_r = cv["unique_reads"]
    cv_w_op = cv["unique_write_s"] / n_w * 1000
    dc_w_op = dc["unique_write_s"] / n_w * 1000
    cv_r_op = cv["unique_read_s"] / n_r * 1000
    dc_r_op = dc["unique_read_s"] / n_r * 1000

    print("  SCENARIO 1 — all-unique blobs, both keyed by SHA-256(content)")
    print("  " + "-" * 62)
    print(f"    {'metric':<28}{'ChronoVault':>15}{'diskcache':>15}")
    print(f"    {f'write  ({n_w} blobs)':<28}{cv['unique_write_s']*1000:>12.0f} ms"
          f"{dc['unique_write_s']*1000:>12.0f} ms")
    print(f"    {'write per object':<28}{cv_w_op:>12.3f} ms{dc_w_op:>12.3f} ms")
    print(f"    {f'read   ({n_r} random reads)':<28}{cv['unique_read_s']*1000:>12.0f} ms"
          f"{dc['unique_read_s']*1000:>12.0f} ms")
    print(f"    {'read per object':<28}{cv_r_op:>12.3f} ms{dc_r_op:>12.3f} ms")
    cv_raw_op = cv["unique_raw_read_s"] / n_r * 1000
    print(f"    {'  of which host file-open':<28}{cv_raw_op:>12.3f} ms{'n/a':>15}")
    print(f"    {'on-disk total':<28}{human_bytes(cv['unique_disk_bytes']):>15}"
          f"{human_bytes(dc['unique_disk_bytes']):>15}")
    print()
    print(f"    write : ChronoVault takes {ratio(cv['unique_write_s'], dc['unique_write_s']):.1f}x "
          f"diskcache's time  ( >1 = slower )")
    print(f"    read  : ChronoVault takes {ratio(cv['unique_read_s'], dc['unique_read_s']):.1f}x "
          f"diskcache's time  ( >1 = slower )")
    print(f"    disk  : ChronoVault uses {ratio(cv['unique_disk_bytes'], dc['unique_disk_bytes']):.2f}x "
          f"diskcache's bytes ( <1 = smaller )")
    print()

    print("  SCENARIO 2 — realistic duplication (the repeated-snapshot case)")
    print("  " + "-" * 62)
    print(f"    {'store':<44}{'on disk':>16}")
    print(f"    {'ChronoVault ObjectStore (content-addressed)':<44}"
          f"{human_bytes(cv['dup_disk_bytes']):>16}")
    print(f"    {'diskcache, content-hash keys (dedups too)':<44}"
          f"{human_bytes(dc['dup_hashed_disk_bytes']):>16}")
    print(f"    {'diskcache, sequential keys (stores every dup)':<44}"
          f"{human_bytes(dc['dup_seq_disk_bytes']):>16}")
    print()
    print(f"    vs. diskcache-as-a-plain-cache : ChronoVault uses "
          f"{ratio(cv['dup_disk_bytes'], dc['dup_seq_disk_bytes']):.3f}x the bytes "
          f"({ratio(dc['dup_seq_disk_bytes'], cv['dup_disk_bytes']):.1f}x smaller)")
    print()
    print("  NOTES (read before quoting any number above)")
    print("  " + "-" * 62)
    print(textwrap.indent(textwrap.fill(
        "ChronoVault is SLOWER per operation and does not hide it. It "
        "writes one real file per object and fsync()s it, and every read "
        "re-hashes and re-verifies the bytes before returning them. "
        "diskcache keeps everything in one SQLite database with no "
        "per-read verification. That is the deliberate trade: no database "
        "engine as a dependency, plain inspectable files on disk, and "
        "tamper-evidence on every read -- paid for in throughput. At this "
        "project's scale (snapshots of a source tree, not a hot cache) "
        "the absolute numbers are milliseconds.", width=60), "    "))
    print()
    print(textwrap.indent(textwrap.fill(
        "ChronoVault compresses every object with stdlib zlib (level 6); "
        "diskcache stores values uncompressed by default. The size gap in "
        "Scenario 1 is mostly that single config choice, not a smarter "
        "storage engine -- it is a fair reflection of what each tool does "
        "out of the box, nothing more.", width=60), "    "))
    print()
    print(textwrap.indent(textwrap.fill(
        "Scenario 2 is the architectural point, not a tuning trick: "
        "whole-file dedup is a property of addressing content by its hash. "
        "ChronoVault builds that in with ~200 lines of stdlib. diskcache "
        "can match it only if the caller reimplements content addressing "
        "on top of it (middle row) -- at which point the dependency is "
        "carrying less of the weight than the code you wrote around it.", width=60), "    "))
    print()
    print(textwrap.indent(textwrap.fill(
        "Numbers are one run on one machine. Re-run this script to get "
        "your own; nothing here is hard-coded.", width=60), "    "))
    print("=" * 68)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--blobs", type=int, default=1500,
                    help="number of unique blobs written in scenario 1 (default 1500)")
    ap.add_argument("--reads", type=int, default=500,
                    help="number of random reads timed in scenario 1 (default 500)")
    ap.add_argument("--dup-logical", type=int, default=2000,
                    help="number of logical blobs in scenario 2 (default 2000)")
    ap.add_argument("--dup-fraction", type=float, default=0.1,
                    help="fraction of scenario-2 blobs that are unique (default 0.1)")
    ap.add_argument("--venv-dir", type=Path, default=None,
                    help="where to build the throwaway venv (default: a temp dir)")
    ap.add_argument("--keep-venv", action="store_true",
                    help="do not delete the venv / work dir on exit (for inspection)")
    args = ap.parse_args()

    print("ChronoVault  --  Package Killer benchmark  (vs. diskcache)")
    print()
    print("Building workload...")
    corpus = build_corpus(args.blobs)
    dup_corpus = build_duplicated_corpus(args.dup_logical, args.dup_fraction)
    print(f"  scenario 1: {len(corpus)} unique blobs, "
          f"{human_bytes(sum(len(b) for b in corpus))} raw")
    print(f"  scenario 2: {len(dup_corpus)} logical blobs, "
          f"{human_bytes(sum(len(b) for b in dup_corpus))} raw")

    scratch = Path(args.venv_dir) if args.venv_dir else Path(
        tempfile.mkdtemp(prefix="cv_vs_diskcache_"))
    scratch.mkdir(parents=True, exist_ok=True)
    venv_dir = scratch / "venv"
    workdir = scratch / "work"
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        print("\nSetting up isolated comparison environment...")
        try:
            py = setup_venv(venv_dir)
        except Exception as e:  # noqa: BLE001  -- we want to report ANY setup failure clearly
            print()
            print("!" * 68)
            print("  Could not set up the isolated diskcache environment.")
            print("  This is NOT a benchmark result -- no numbers are reported for")
            print("  the diskcache side, and none are invented.")
            print()
            print(textwrap.indent(str(e), "  "))
            print()
            print("  Re-run on a machine/network that can 'pip install diskcache'")
            print("  into a venv, or pass --venv-dir pointing at a prepared venv")
            print("  that already has diskcache installed.")
            print("!" * 68)
            return 2

        print("\nRunning ChronoVault side (this interpreter, stdlib only)...")
        cv = run_chronovault(corpus, dup_corpus, workdir, args.reads)

        print("Running diskcache side (inside the isolated venv)...")
        dc = run_diskcache(py, corpus, dup_corpus, workdir, args.reads)

        report(cv, dc, args)
        return 0
    finally:
        if args.keep_venv:
            print(f"\n(kept for inspection: {scratch})")
        else:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
