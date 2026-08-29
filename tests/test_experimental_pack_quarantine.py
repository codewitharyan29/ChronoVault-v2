"""
Feature 2 -- corrupt / truncated pack handling.

A pack file that fails structural validation at load time is
*quarantined* (skipped and recorded) instead of crashing the whole
engine, which every command constructs via PackAwareObjectStore.
Intact packs and all loose objects keep working; `vault verify` and
`vault recover-check` say precisely what became unrecoverable.

The first test is the regression guard the change is riskiest for: a
perfectly healthy repository (with a real pack) must behave EXACTLY
as before -- same object set, same bytes, same reader count, zero
quarantine -- proving the new logic only fires on genuinely bad packs
and never second-guesses a good one.
"""

import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.experimental.delta_pack import DeltaAwarePackWriter, find_delta_candidates
from vault.experimental.pack_aware_store import PackAwareObjectStore
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

    def _write(self, rel, content):
        p = self.source_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def _pack(self, name="pack-1"):
        """Replicate `vault pack`: candidate-finding uses a pack-aware
        engine (so it can diff trees that already span packs), the
        write itself uses the raw loose store, then loose is pruned."""
        raw = ObjectStore(self.vault_dir)
        eng = SnapshotEngine(self.vault_dir)
        eng.store = PackAwareObjectStore(self.vault_dir)
        cand = find_delta_candidates(eng)
        DeltaAwarePackWriter(raw, self.vault_dir / "pack").write_pack(name, cand)
        for h in list(raw.iter_all_hashes()):
            raw.delete(h)

    def _fingerprint(self, store):
        return {h: store.get(h) for h in sorted(store.iter_all_hashes())}


class TestHealthyRepoUnchanged(_Base):
    def test_healthy_repo_with_a_pack_behaves_exactly_as_before(self):
        # capture every object's bytes BEFORE packing, from the plain
        # loose store -- this is the ground truth the pack-aware loader
        # must reproduce byte-for-byte.
        self._write("a.py", "alpha\nbeta\ngamma\n")
        self._write("dir/b.txt", "bee\n")
        self.engine.create_snapshot(self.source_dir, message="s1")
        self._write("a.py", "alpha\nBETA-changed\ngamma\ndelta\n")
        self.engine.create_snapshot(self.source_dir, message="s2")

        loose_truth = self._fingerprint(ObjectStore(self.vault_dir))
        self.assertGreater(len(loose_truth), 3)

        self._pack()  # prunes loose; everything now lives in pack-1

        s1 = PackAwareObjectStore(self.vault_dir)
        s2 = PackAwareObjectStore(self.vault_dir)  # construct twice

        # the new logic ran (a pack is present) and found nothing wrong
        self.assertEqual(s1.quarantined_packs, [])
        self.assertEqual(s2.quarantined_packs, [])
        self.assertEqual(len(s1._readers), 1)
        self.assertEqual(len(s2._readers), 1)

        # same object set, same bytes -- as the loose store, and stable
        # across independent constructions (no second-guessing)
        fp1 = self._fingerprint(s1)
        fp2 = self._fingerprint(s2)
        self.assertEqual(fp1, loose_truth)
        self.assertEqual(fp2, loose_truth)
        self.assertEqual(sorted(s1.iter_all_hashes()), sorted(loose_truth))

        # verify_object agrees on every object; recover-check is clean
        self.assertTrue(all(s1.verify_object(h) for h in s1.iter_all_hashes()))
        eng = SnapshotEngine(self.vault_dir)
        eng.store = s1
        for rec in eng.list_snapshots():
            report = check_snapshot_recoverable(eng, rec.id)
            self.assertTrue(report.recoverable, report.issues)
            self.assertEqual(report.quarantined_packs, [])

    def test_structural_check_passes_on_a_healthy_pack(self):
        self._write("x.py", "content\n")
        self.engine.create_snapshot(self.source_dir)
        self._pack()
        store = PackAwareObjectStore(self.vault_dir)
        reader = store._readers[0]
        pack_path = next((self.vault_dir / "pack").glob("*.pack"))
        self.assertIsNone(store._structural_problem(reader, pack_path))

    def test_repo_with_no_packs_at_all_is_untouched(self):
        self._write("x.py", "content\n")
        self.engine.create_snapshot(self.source_dir)
        store = PackAwareObjectStore(self.vault_dir)  # no pack dir
        self.assertEqual(store.quarantined_packs, [])
        self.assertEqual(store._readers, [])
        self.assertGreater(len(list(store.iter_all_hashes())), 0)


class TestPackQuarantine(_Base):
    def _setup_pack_plus_loose(self):
        """snapshot 1 -> pack-1 (loose then pruned); snapshot 2 -> new
        objects that stay LOOSE. So pack-1 is the only home of s1's
        objects, and s2's objects have loose copies."""
        self._write("packed.py", "old one\nold two\nold three\n")
        s1 = self.engine.create_snapshot(self.source_dir, message="s1")
        self._pack("pack-1")
        self._write("loose.py", "brand new content\n")
        s2 = self.engine.create_snapshot(self.source_dir, message="s2")
        return s1, s2

    def _idx(self, name="pack-1"):
        return self.vault_dir / "pack" / f"{name}.idx"

    def _pack_file(self, name="pack-1"):
        return self.vault_dir / "pack" / f"{name}.pack"

    def test_truncated_index_quarantines_pack_without_crashing(self):
        self._setup_pack_plus_loose()
        b = self._idx().read_bytes()
        self._idx().write_bytes(b[: len(b) // 3])  # unparseable index

        store = PackAwareObjectStore(self.vault_dir)  # must NOT raise

        self.assertEqual(len(store.quarantined_packs), 1)
        self.assertEqual(store.quarantined_packs[0].name, "pack-1.pack")
        self.assertEqual(store._readers, [])
        # loose (snapshot 2) objects are still fully readable
        self.assertGreater(len(list(store.iter_all_hashes())), 0)
        for h in store.iter_all_hashes():
            self.assertTrue(store.verify_object(h))

    def test_corrupted_but_parseable_index_is_quarantined_by_bounds_check(self):
        self._setup_pack_plus_loose()
        # leave the .idx intact; truncate the .pack so index offsets
        # now point past EOF -- index parses, structure check catches it
        pf = self._pack_file()
        pf.write_bytes(pf.read_bytes()[: len(pf.read_bytes()) // 2])

        store = PackAwareObjectStore(self.vault_dir)

        self.assertEqual(len(store.quarantined_packs), 1)
        self.assertIn("outside the", store.quarantined_packs[0].reason)
        self.assertTrue(store.quarantined_packs[0].hashes,
                        "a parseable index should let us capture which hashes were stranded")
        self.assertEqual(store._readers, [])

    def test_missing_idx_file_quarantines_pack(self):
        self._setup_pack_plus_loose()
        self._idx().unlink()

        store = PackAwareObjectStore(self.vault_dir)

        self.assertEqual(len(store.quarantined_packs), 1)
        self.assertIn(".idx", store.quarantined_packs[0].reason)
        self.assertEqual(store._readers, [])

    def test_intact_pack_alongside_a_quarantined_one_still_serves(self):
        # two packs: pack-1 from snapshot 1, pack-2 from snapshot 2
        self._write("one.py", "first file contents\n")
        self.engine.create_snapshot(self.source_dir, message="s1")
        self._pack("pack-1")
        self._write("two.py", "second file contents\n")
        self.engine.create_snapshot(self.source_dir, message="s2")
        self._pack("pack-2")

        healthy = PackAwareObjectStore(self.vault_dir)
        self.assertEqual(healthy.quarantined_packs, [])
        pack1_hashes = {e.obj_hash for e in healthy._readers[0].index.entries}

        # now corrupt ONLY pack-2's index
        b = self._idx("pack-2").read_bytes()
        self._idx("pack-2").write_bytes(b[: len(b) // 3])

        store = PackAwareObjectStore(self.vault_dir)
        self.assertEqual([qp.name for qp in store.quarantined_packs], ["pack-2.pack"])
        self.assertEqual(len(store._readers), 1)  # pack-1 still loaded
        # every object pack-1 holds is still fully readable and verifies
        for h in pack1_hashes:
            self.assertTrue(store.has(h))
            self.assertTrue(store.verify_object(h))
        self.assertTrue(pack1_hashes.issubset(set(store.iter_all_hashes())))

    def test_recover_check_reports_objects_stranded_by_a_quarantined_pack(self):
        s1, s2 = self._setup_pack_plus_loose()
        pf = self._pack_file()
        pf.write_bytes(pf.read_bytes()[: len(pf.read_bytes()) // 2])  # parseable idx

        eng = SnapshotEngine(self.vault_dir)
        eng.store = PackAwareObjectStore(self.vault_dir)

        r1 = check_snapshot_recoverable(eng, s1.id)
        self.assertFalse(r1.recoverable)
        self.assertTrue(r1.quarantined_packs)
        self.assertTrue(any("stranded in quarantined pack" in i.detail for i in r1.issues),
                        f"expected a 'stranded' diagnosis; got {[i.detail for i in r1.issues]}")

        # snapshot 2's objects have loose copies -> still fully recoverable
        r2 = check_snapshot_recoverable(eng, s2.id)
        self.assertTrue(r2.recoverable, r2.issues)
        self.assertTrue(r2.quarantined_packs)  # still noted, just not fatal here


class TestQuarantineCLI(_Base):
    def _run(self, *argv):
        from vault.cli import main
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    def _make_quarantined_repo(self):
        self._write("a.py", "aaa\nbbb\nccc\n")
        self.engine.create_snapshot(self.source_dir, message="s1")
        raw = ObjectStore(self.vault_dir)
        DeltaAwarePackWriter(raw, self.vault_dir / "pack").write_pack("pack-1", {})
        for h in list(raw.iter_all_hashes()):
            raw.delete(h)
        self._write("b.py", "loose file\n")
        self.engine.create_snapshot(self.source_dir, message="s2")
        idx = self.vault_dir / "pack" / "pack-1.idx"
        idx.write_bytes(idx.read_bytes()[:20])

    def test_verify_fails_and_reports_when_a_pack_is_quarantined(self):
        self._make_quarantined_repo()
        code, out = self._run("verify", str(self.source_dir))
        self.assertEqual(code, 1)
        self.assertIn("quarantined", out)
        self.assertIn("recover-check", out)
        self.assertNotIn("Repository healthy.", out)

    def test_healthy_repo_verify_is_unchanged_and_says_healthy(self):
        self._write("a.py", "aaa\n")
        self.engine.create_snapshot(self.source_dir)
        raw = ObjectStore(self.vault_dir)
        DeltaAwarePackWriter(raw, self.vault_dir / "pack").write_pack("pack-1", {})
        for h in list(raw.iter_all_hashes()):
            raw.delete(h)
        code, out = self._run("verify", str(self.source_dir))
        self.assertEqual(code, 0)
        self.assertIn("Repository healthy.", out)
        self.assertNotIn("quarantin", out.lower())

    def test_engine_warns_on_stderr_and_command_still_runs(self):
        import subprocess
        self._make_quarantined_repo()
        r = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "chronovault.py"),
             "list", str(self.source_dir)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stderr)          # `list` still works
        self.assertIn("quarantined", r.stderr)
        self.assertIn("pack-1.pack", r.stderr)
        self.assertIn("s2", r.stdout)                        # snapshot 2 still listed


if __name__ == "__main__":
    unittest.main()
