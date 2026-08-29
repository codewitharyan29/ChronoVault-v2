#!/usr/bin/env python3
"""
scripts/security_demo.py

Runs REAL attacks against a live ChronoVault repository and reports
pass/fail for each -- not a description of testing that was done, an
actual live demonstration, run fresh every time. Every attack here is
one that was genuinely found, tested, and (where applicable) fixed
during this project's adversarial-testing audit.
"""

from __future__ import annotations

import sys
import json
import shutil
import tempfile
import time
from pathlib import Path

# Windows: this script's own stdout can be a non-UTF-8 console codepage
# (cp1252 "charmap"), which raises UnicodeEncodeError on the checkmark
# characters below. Force UTF-8 for our own output, independent of how
# the script is invoked (double-click, subprocess, PowerShell, VS Code).
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.snapshot import SnapshotEngine, serialize_tree, TreeEntry
from vault.objects import ObjectStore, VaultError
from vault.restore import apply_restore


def _fresh_repo():
    root = Path(tempfile.mkdtemp(prefix="cv-security-demo-"))
    vault_dir = root / ".vault"
    source_dir = root / "project"
    source_dir.mkdir()
    engine = SnapshotEngine(vault_dir)
    return root, engine, source_dir


def attack_path_traversal():
    """A hand-crafted tree entry named '../../evil.txt' -- the exact
    vulnerability found and fixed during v1's own original development."""
    root, engine, source_dir = _fresh_repo()
    try:
        blob_hash = engine.store.put(b"malicious payload").obj_hash
        try:
            serialize_tree([TreeEntry(name="../../evil.txt", kind="blob", obj_hash=blob_hash)])
            return False, "tree serialization ACCEPTED a path-traversal name"
        except VaultError:
            return True, "rejected at serialization -- name validation caught it"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def attack_symlink_cycle():
    """A directory containing a symlink to itself -- must not hang or
    crash, must simply be skipped (ChronoVault's documented design:
    symlinks are never followed).

    Windows-specific: creating a symlink requires either Administrator
    rights or Developer Mode enabled -- an ordinary unprivileged
    account gets `OSError: [WinError 1314] A required privilege is not
    held by the client` from the mere ATTEMPT to create one, before
    ChronoVault's own symlink-skipping logic is ever exercised. That's
    an OS permission boundary, not a ChronoVault defect, so it must not
    be reported as a FAILED security check -- treated as skipped
    (return None) instead, the same platform limitation this project's
    own unittest suite already accounts for (see
    tests/test_snapshot.py). Probing dynamically here (rather than a
    blanket os.name == "nt" skip) means the real property still gets
    exercised on any Windows box that DOES have Developer Mode on."""
    root, engine, source_dir = _fresh_repo()
    try:
        cycle_dir = source_dir / "cycle"
        cycle_dir.mkdir()
        try:
            (cycle_dir / "self_loop").symlink_to(cycle_dir)
        except OSError as e:
            return None, f"skipped -- cannot create symlinks on this system ({e})"
        (source_dir / "real_file.txt").write_text("genuine content")

        start = time.time()
        record = engine.create_snapshot(source_dir, message="symlink cycle test")
        elapsed = time.time() - start

        if elapsed > 5:
            return False, f"took {elapsed:.1f}s -- looks like it followed the cycle"
        if record.stats.files != 1:
            return False, f"expected 1 real file, got {record.stats.files} (symlink wasn't skipped cleanly)"
        return True, f"completed in {elapsed:.3f}s, symlink correctly skipped, only the real file captured"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def attack_corrupted_object():
    """Flip one byte in a stored object on disk -- verify must detect
    it via hash re-verification, not just trust the stored bytes."""
    root, engine, source_dir = _fresh_repo()
    try:
        (source_dir / "important.txt").write_text("data that must not silently corrupt")
        record = engine.create_snapshot(source_dir)
        entries = engine.load_tree(record.root_tree_hash)
        target_hash = entries[0].obj_hash
        obj_path = engine.store._object_path(target_hash)
        raw = bytearray(obj_path.read_bytes())
        raw[-1] ^= 0xFF
        obj_path.write_bytes(bytes(raw))

        corrupted = not engine.store.verify_object(target_hash)
        if not corrupted:
            return False, "verify_object() did NOT detect the corruption"
        return True, f"corruption in {target_hash[:12]}... detected via hash re-verification"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def attack_type_confused_tree():
    """A snapshot record whose root_tree_hash points at a real BLOB's
    hash instead of an actual tree -- type confusion. restore/diff/gc
    must all detect and refuse, not silently misinterpret the bytes."""
    root, engine, source_dir = _fresh_repo()
    try:
        (source_dir / "a.txt").write_text("content")
        record = engine.create_snapshot(source_dir)
        entries = engine.load_tree(record.root_tree_hash)
        blob_hash = entries[0].obj_hash

        real_data = json.loads((engine.vault_dir / "snapshots" / str(record.id)).read_text())
        fake_data = dict(real_data)
        fake_data["id"] = 999
        fake_data["root_tree_hash"] = blob_hash  # the actual attack
        (engine.vault_dir / "snapshots" / "999").write_text(json.dumps(fake_data))

        try:
            target_dir = root / "restore_target"
            target_dir.mkdir()
            apply_restore(engine, target_dir, 999)
            return False, "restore ACCEPTED a type-confused tree hash and wrote data"
        except VaultError:
            return True, "restore correctly rejected the type-confused root_tree_hash"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def attack_corrupted_pack():
    """Corrupt a byte inside a real pack file after packing -- verify
    must detect it, restore must refuse to write anything."""
    root, engine, source_dir = _fresh_repo()
    try:
        from vault.experimental.delta_pack import find_delta_candidates, DeltaAwarePackWriter

        (source_dir / "app.py").write_text("content for packing")
        engine.create_snapshot(source_dir, message="v1")

        writer = DeltaAwarePackWriter(engine.store, engine.vault_dir / "pack")
        stats = writer.write_pack("p1", find_delta_candidates(engine))
        for h in list(engine.store.iter_all_hashes()):
            engine.store.delete(h)

        pack_path = stats["pack_path"]
        raw = bytearray(pack_path.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        pack_path.write_bytes(bytes(raw))

        from vault.experimental.delta_pack import DeltaAwarePackedStore
        reader = DeltaAwarePackedStore(engine.store, stats["pack_path"], stats["idx_path"])
        try:
            for h in [e.obj_hash for e in engine.load_tree(engine.list_snapshots()[0].root_tree_hash)]:
                reader.get(h)
            return False, "corrupted pack byte went undetected on read-back"
        except VaultError:
            return True, "corrupted pack content detected on read-back, raised cleanly"
    finally:
        shutil.rmtree(root, ignore_errors=True)


ATTACKS = [
    ("Path traversal", "../../evil.txt tree entry", attack_path_traversal),
    ("Symlink cycle", "self-referencing directory symlink", attack_symlink_cycle),
    ("Corrupted object", "flipped byte in a stored object", attack_corrupted_object),
    ("Type-confused tree", "snapshot root_tree_hash pointing at a blob", attack_type_confused_tree),
    ("Corrupted pack", "flipped byte inside a pack file", attack_corrupted_pack),
]


def main():
    print("ChronoVault Security Demonstration")
    print("=" * 35)
    print()

    results = []
    skipped = 0
    for i, (name, attack_desc, fn) in enumerate(ATTACKS, start=1):
        print(f"[{i}/{len(ATTACKS)}] {name}")
        print(f"      Attack: {attack_desc}")
        try:
            passed, detail = fn()
        except Exception as e:
            passed, detail = False, f"UNEXPECTED EXCEPTION: {type(e).__name__}: {e}"
        if passed is None:
            # Platform capability missing (e.g. no symlink privilege on
            # this Windows account), not a security failure -- excluded
            # from the pass/fail tally rather than counted as FAILED.
            status, symbol = "SKIPPED", "-"
            skipped += 1
        else:
            status = "BLOCKED" if passed else "FAILED"
            symbol = "✓" if passed else "✗"
            results.append(passed)
        print(f"      Result: {status} {symbol}  ({detail})")
        print()

    passed_count = sum(results)
    total = len(results)
    skip_note = f" ({skipped} skipped -- platform limitation, not a defect)" if skipped else ""
    print(f"Security demonstrations: {passed_count}/{total} PASSED{skip_note}")
    return 0 if passed_count == total else 1


if __name__ == "__main__":
    sys.exit(main())
