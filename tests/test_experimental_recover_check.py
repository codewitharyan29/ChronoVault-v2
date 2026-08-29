"""
Tests for vault/experimental/recover_check.py and the
`vault recover-check <snapshot>` CLI command.

recover-check is strictly read-only: it never creates, modifies, or
deletes anything. These tests cover the four scenarios the feature
request names -- a healthy snapshot (passes), a missing object, a
broken delta base, and a corrupted pack entry (each fails with a
clear diagnosis) -- plus malformed metadata, path-traversal in a
tree, and CLI wiring / exit codes.
"""

import io
import json
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.experimental.delta_pack import DeltaAwarePackWriter, find_delta_candidates
from vault.experimental.pack_aware_store import PackAwareObjectStore
from vault.experimental.packfile import PackIndex
from vault.experimental.recover_check import check_snapshot_recoverable
from vault.objects import ObjectStore
from vault.snapshot import SnapshotEngine


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.vault_dir = self.root / ".vault"
        self.source_dir = self.root / "project"
        self.source_dir.mkdir()
        self.engine = SnapshotEngine(self.vault_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, rel_path, content):
        p = self.source_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def _pack_aware(self):
        """A fresh engine wired exactly like the CLI wires it."""
        eng = SnapshotEngine(self.vault_dir)
        eng.store = PackAwareObjectStore(self.vault_dir)
        return eng

    def _pack(self, name="pack-1"):
        """Replicates `cmd_pack`: write a delta-aware pack from the
        loose store, then prune the loose copies. Returns write stats."""
        raw = ObjectStore(self.vault_dir)
        candidates = find_delta_candidates(SnapshotEngine(self.vault_dir))
        stats = DeltaAwarePackWriter(raw, self.vault_dir / "pack").write_pack(name, candidates)
        for h in list(raw.iter_all_hashes()):
            raw.delete(h)
        return stats

    def _blob_hash_in(self, snap_id, filename):
        eng = self._pack_aware()
        rec = eng.load_snapshot(snap_id)
        for e in eng.load_tree(rec.root_tree_hash):
            if e.name == filename:
                return e.obj_hash
        raise AssertionError(f"{filename} not found in snapshot {snap_id}")

    def _corrupt_pack_payload(self, obj_hash):
        """Flip one byte inside obj_hash's stored payload in the pack
        (leaving the pack index and magic intact)."""
        idx_path = next((self.vault_dir / "pack").glob("*.idx"))
        pack_path = next((self.vault_dir / "pack").glob("*.pack"))
        entry = PackIndex.from_bytes(idx_path.read_bytes()).find(obj_hash)
        self.assertIsNotNone(entry, "object not in pack index")
        data = bytearray(pack_path.read_bytes())
        target = entry.offset + 3  # past the 'F'/'D' byte + version + marker
        data[target] ^= 0xFF
        pack_path.write_bytes(data)


class TestRecoverCheckHealthy(_Base):
    def test_healthy_loose_snapshot_is_fully_recoverable(self):
        self._write("a.py", "alpha\nbeta\n")
        self._write("cfg.ini", "k=v\n")
        self._write("sub/deep.py", "deep\n")
        s = self.engine.create_snapshot(self.source_dir)

        report = check_snapshot_recoverable(self._pack_aware(), s.id)

        self.assertTrue(report.recoverable)
        self.assertEqual(report.issues, [])
        self.assertGreater(report.objects_checked, 0)
        self.assertEqual(report.packed_objects, 0)
        self.assertEqual(report.delta_dependencies, 0)

    def test_healthy_packed_snapshot_is_fully_recoverable(self):
        self._write("a.py", "one\ntwo\nthree\n")
        self.engine.create_snapshot(self.source_dir)
        self._write("a.py", "one\ntwo CHANGED\nthree\n")
        s2 = self.engine.create_snapshot(self.source_dir)
        self._pack()

        report = check_snapshot_recoverable(self._pack_aware(), s2.id)

        self.assertTrue(report.recoverable)
        self.assertEqual(report.issues, [])
        self.assertGreater(report.packed_objects, 0)

    def test_missing_snapshot_id_is_reported_not_crashed(self):
        self._write("a.py", "x\n")
        self.engine.create_snapshot(self.source_dir)

        report = check_snapshot_recoverable(self._pack_aware(), 999)

        self.assertFalse(report.recoverable)
        self.assertEqual(len(report.issues), 1)
        self.assertIn("does not exist", report.issues[0].detail)
        self.assertEqual(report.objects_checked, 0)

    def test_malformed_snapshot_record_is_reported(self):
        self._write("a.py", "x\n")
        self.engine.create_snapshot(self.source_dir)
        (self.vault_dir / "snapshots" / "1").write_text("{ not valid json")

        report = check_snapshot_recoverable(self._pack_aware(), 1)

        self.assertFalse(report.recoverable)
        self.assertTrue(any("corrupted" in i.detail.lower() or "json" in i.detail.lower()
                            for i in report.issues))


class TestRecoverCheckFailures(_Base):
    def test_missing_object_fails_with_clear_diagnosis(self):
        self._write("f.py", "one\ntwo\nthree\n")
        s = self.engine.create_snapshot(self.source_dir)
        h = self._blob_hash_in(s.id, "f.py")
        (self.vault_dir / "objects" / h[:2] / h[2:]).unlink()

        report = check_snapshot_recoverable(self._pack_aware(), s.id)

        self.assertFalse(report.recoverable)
        missing = [i for i in report.issues if i.where == "f.py"]
        self.assertEqual(len(missing), 1)
        self.assertIn("missing", missing[0].detail)
        self.assertEqual(missing[0].obj_hash, h)

    def test_corrupted_pack_entry_fails_with_clear_diagnosis(self):
        self._write("f.py", "aaaa\nbbbb\ncccc\ndddd\neeee\n")
        self.engine.create_snapshot(self.source_dir)
        self._write("f.py", "aaaa\nBBBB-changed\ncccc\ndddd\neeee\n")
        s2 = self.engine.create_snapshot(self.source_dir)
        self._pack()
        h = self._blob_hash_in(s2.id, "f.py")
        self._corrupt_pack_payload(h)

        report = check_snapshot_recoverable(self._pack_aware(), s2.id)

        self.assertFalse(report.recoverable)
        bad = [i for i in report.issues if i.where == "f.py"]
        self.assertEqual(len(bad), 1)
        self.assertIn("fails hash/decode verification", bad[0].detail)
        self.assertIn("pack", bad[0].detail)

    def test_broken_delta_base_fails_with_clear_diagnosis(self):
        # A large file with a single localized edit: the delta easily
        # beats plain compression, so `pack` really does store the new
        # version as a delta against the old one.
        base_lines = [f"line {i} unchanged content here" for i in range(200)]
        self._write("big.py", "\n".join(base_lines) + "\n")
        self.engine.create_snapshot(self.source_dir)
        edited = list(base_lines)
        edited[3] = "line 3 -- THIS ONE CHANGED"
        self._write("big.py", "\n".join(edited) + "\n")
        s2 = self.engine.create_snapshot(self.source_dir)
        stats = self._pack()

        manifest_path = next((self.vault_dir / "pack").glob("*.deltamanifest.json"))
        manifest = json.loads(manifest_path.read_text())
        self.assertTrue(
            manifest,
            "test setup failed: `pack` produced no deltas, so the "
            "broken-delta-base path was never exercised",
        )
        base_hash = next(iter(manifest.values()))
        self._corrupt_pack_payload(base_hash)

        report = check_snapshot_recoverable(self._pack_aware(), s2.id)

        self.assertFalse(report.recoverable)
        self.assertGreaterEqual(report.delta_dependencies, 1)
        base_issues = [i for i in report.issues if i.where.startswith("delta base for")]
        self.assertTrue(base_issues, f"no delta-base issue reported; issues={report.issues}")
        self.assertIn("delta base", base_issues[0].detail)

    def test_path_traversal_entry_in_tree_is_caught(self):
        """A tampered tree object whose entry name escapes the target
        directory must be flagged, not silently walked -- this is the
        same restore.py path-traversal defense, reused."""
        evil = self.engine.store.put(b"PWNED")
        name = b"../../evil_outside.txt"
        malicious_tree = (
            b"b" + len(name).to_bytes(2, "big") + name
            + evil.obj_hash.encode("ascii")
        )
        tree_stat = self.engine.store.put(malicious_tree)
        record = {
            "id": 1, "timestamp": time.time(), "parent": None,
            "root_tree_hash": tree_stat.obj_hash, "message": "malicious",
            "stats": {"files": 1, "new_objects": 2, "reused_objects": 0,
                      "original_bytes": 0, "compressed_bytes": 0},
        }
        (self.vault_dir / "snapshots" / "1").write_text(json.dumps(record))

        report = check_snapshot_recoverable(self._pack_aware(), 1)

        self.assertFalse(report.recoverable)
        self.assertTrue(report.issues)

    def test_recover_check_does_not_modify_the_repository(self):
        """The whole point: a read-only audit. Nothing on disk under
        .vault should change, even when the snapshot is broken."""
        self._write("f.py", "one\ntwo\n")
        s = self.engine.create_snapshot(self.source_dir)
        h = self._blob_hash_in(s.id, "f.py")
        (self.vault_dir / "objects" / h[:2] / h[2:]).unlink()

        def snapshot_of_vault():
            out = {}
            for p in sorted(self.vault_dir.rglob("*")):
                if p.is_file():
                    out[str(p.relative_to(self.vault_dir))] = p.read_bytes()
            return out

        before = snapshot_of_vault()
        check_snapshot_recoverable(self._pack_aware(), s.id)
        check_snapshot_recoverable(self._pack_aware(), s.id)  # twice, for good measure
        after = snapshot_of_vault()
        self.assertEqual(before, after, "recover-check modified the repository")


class TestRecoverCheckCLI(_Base):
    def _run(self, *argv):
        from vault.cli import main
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(list(argv))
        return code, buf.getvalue()

    def test_cli_healthy_exit_0_and_summary_format(self):
        self._write("a.py", "content\n")
        self.engine.create_snapshot(self.source_dir)

        code, out = self._run("recover-check", "1", "--path", str(self.source_dir))
        self.assertEqual(code, 0)
        self.assertIn("Snapshot 1 is fully recoverable", out)
        self.assertIn("objects checked", out)
        self.assertIn("packed", out)
        self.assertIn("delta dependencies", out)
        self.assertIn("integrity errors", out)

    def test_cli_broken_snapshot_exits_1_and_lists_errors(self):
        self._write("f.py", "one\ntwo\n")
        s = self.engine.create_snapshot(self.source_dir)
        h = self._blob_hash_in(s.id, "f.py")
        (self.vault_dir / "objects" / h[:2] / h[2:]).unlink()

        code, out = self._run("recover-check", "1", "--path", str(self.source_dir))
        self.assertEqual(code, 1)
        self.assertIn("NOT fully recoverable", out)
        self.assertIn("✗", out)
        self.assertIn("f.py", out)

    def test_cli_accepts_a_tag_name(self):
        self._write("a.py", "content\n")
        self.engine.create_snapshot(self.source_dir)
        self._run("tag", "1", "golden", "--path", str(self.source_dir))

        code, out = self._run("recover-check", "golden", "--path", str(self.source_dir))
        self.assertEqual(code, 0)
        self.assertIn("fully recoverable", out)


if __name__ == "__main__":
    unittest.main()
