"""
Tests for vault/experimental/path_history.py.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.snapshot import SnapshotEngine
from vault.experimental.path_history import PathHistoryIndex


class TestPathHistoryIndex(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.vault_dir = self.root / ".vault"
        self.source_dir = self.root / "project"
        self.source_dir.mkdir()
        self.engine = SnapshotEngine(self.vault_dir)
        self.index = PathHistoryIndex(self.vault_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, rel_path, content):
        p = self.source_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def test_records_every_change_to_a_path(self):
        self._write("app.py", "v1")
        s1 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s1.id, s1.root_tree_hash)

        self._write("app.py", "v2")
        s2 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s2.id, s2.root_tree_hash)

        self._write("app.py", "v3")
        s3 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s3.id, s3.root_tree_hash)

        history = self.index.history_for("app.py")
        self.assertEqual(len(history), 3)
        self.assertEqual([sid for sid, _ in history], [s1.id, s2.id, s3.id])

    def test_unchanged_path_does_not_get_a_new_entry(self):
        self._write("stable.txt", "never changes")
        s1 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s1.id, s1.root_tree_hash)

        self._write("other.txt", "unrelated change")
        s2 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s2.id, s2.root_tree_hash)

        history = self.index.history_for("stable.txt")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0][0], s1.id)

    def test_path_that_never_existed_returns_empty_history(self):
        self._write("a.txt", "content")
        s1 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s1.id, s1.root_tree_hash)
        self.assertEqual(self.index.history_for("nonexistent.txt"), [])

    def test_incremental_matches_full_rebuild(self):
        for i in range(5):
            self._write("evolving.py", f"version {i}")
            record = self.engine.create_snapshot(self.source_dir, message=f"v{i}")
            self.index.record_snapshot(self.engine, record.id, record.root_tree_hash)

        incremental_history = self.index.history_for("evolving.py")

        fresh_dir = self.root / "fresh_index_dir"
        fresh_dir.mkdir(parents=True, exist_ok=True)
        fresh_index = PathHistoryIndex(fresh_dir)
        fresh_index.rebuild_from_scratch(self.engine)
        rebuilt_history = fresh_index.history_for("evolving.py")

        self.assertEqual(incremental_history, rebuilt_history)
        self.assertEqual(len(incremental_history), 5)

    def test_nested_path_history(self):
        self._write("src/deep/nested/file.py", "v1")
        s1 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s1.id, s1.root_tree_hash)

        self._write("src/deep/nested/file.py", "v2")
        s2 = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, s2.id, s2.root_tree_hash)

        history = self.index.history_for("src/deep/nested/file.py")
        self.assertEqual(len(history), 2)


if __name__ == "__main__":
    unittest.main()


class TestAdversarialIndexLoading(unittest.TestCase):
    """
    Regression tests for real bugs found by adversarial fuzzing of
    the persisted index file: raw JSONDecodeError/AttributeError
    leaking for corrupted or wrong-shaped content, and -- more
    seriously -- SILENTLY WRONG output (garbage 1-character tuples)
    for malformed-but-dict-shaped content, found by checking output
    correctness, not just crash-safety.
    """
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_empty_index_file_loads_as_empty_not_crash(self):
        (self.root / "path_history.json").write_text("")
        idx = PathHistoryIndex(self.root)
        self.assertEqual(idx.history, {})

    def test_malformed_json_loads_as_empty_not_crash(self):
        (self.root / "path_history.json").write_text("not json {{{")
        idx = PathHistoryIndex(self.root)
        self.assertEqual(idx.history, {})

    def test_valid_json_wrong_top_level_type_loads_as_empty(self):
        for content in ["null", "[]", "42", '"a string"']:
            with self.subTest(content=content):
                root = Path(tempfile.mkdtemp())
                try:
                    (root / "path_history.json").write_text(content)
                    idx = PathHistoryIndex(root)
                    self.assertEqual(idx.history, {})
                finally:
                    shutil.rmtree(root, ignore_errors=True)

    def test_malformed_per_path_value_returns_empty_not_garbage(self):
        """THE MAIN FINDING: a per-path value that's a string (not a
        list) used to be silently iterated character-by-character,
        producing plausible-looking garbage tuples instead of a
        clean empty result or error."""
        (self.root / "path_history.json").write_text('{"some/path.txt": "not_a_list"}')
        idx = PathHistoryIndex(self.root)
        self.assertEqual(idx.history_for("some/path.txt"), [])

    def test_mixed_valid_and_invalid_entries_keeps_only_valid_ones(self):
        (self.root / "path_history.json").write_text(
            '{"p": [[1, "abc"], ["not", "a", "pair"], [2, "def"], "garbage"]}'
        )
        idx = PathHistoryIndex(self.root)
        self.assertEqual(idx.history_for("p"), [(1, "abc"), (2, "def")])

    def test_well_formed_index_still_works_normally(self):
        (self.root / "path_history.json").write_text('{"p": [[1, "hash_a"], [2, "hash_b"]]}')
        idx = PathHistoryIndex(self.root)
        self.assertEqual(idx.history_for("p"), [(1, "hash_a"), (2, "hash_b")])


class TestRenameAwareLineage(unittest.TestCase):
    """
    Rename detection (v2.1): a file that disappears from one path and
    reappears byte-for-byte identical at a new path in the same
    snapshot transition is linked as one lineage, so
    `vault log <either-path>` follows the move.

    Decision recorded here for the "move + content change in the same
    snapshot" case: it is deliberately NOT treated as a rename. With
    only object hashes to go on (no similarity heuristic, no new
    dependency) there is no signal separating it from an unrelated
    delete + add. It shows as the old path ending and the new path
    beginning. See path_history.py::_detect_renames and the README.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.vault_dir = self.root / ".vault"
        self.source_dir = self.root / "project"
        self.source_dir.mkdir()
        self.engine = SnapshotEngine(self.vault_dir)
        self.index = PathHistoryIndex(self.vault_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, rel_path, content):
        p = self.source_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def _snap_and_record(self):
        rec = self.engine.create_snapshot(self.source_dir)
        self.index.record_snapshot(self.engine, rec.id, rec.root_tree_hash)
        return rec

    # -- the four scenarios the spec asks for --------------------------------

    def test_simple_rename_with_unrelated_changes_alongside(self):
        self._write("auth.py", "def login():\n    return True\n")
        self._write("notes.txt", "first\n")
        s1 = self._snap_and_record()

        # rename auth.py -> security.py, AND change an unrelated file
        (self.source_dir / "auth.py").rename(self.source_dir / "security.py")
        self._write("notes.txt", "first\nsecond\n")
        s2 = self._snap_and_record()

        self.assertEqual(self.index.renames, [["auth.py", "security.py", s2.id]])
        lineage = self.index.lineage_for("security.py")
        self.assertEqual([(sid, p) for sid, _, p in lineage],
                         [(s1.id, "auth.py"), (s2.id, "security.py")])
        # queryable from the OLD name too
        self.assertEqual(self.index.lineage_for("auth.py"),
                         self.index.lineage_for("security.py"))

    def test_rename_with_no_other_changes_at_all(self):
        self._write("a.py", "x = 1\n")
        s1 = self._snap_and_record()

        (self.source_dir / "a.py").rename(self.source_dir / "b.py")
        s2 = self._snap_and_record()

        self.assertEqual(self.index.renames, [["a.py", "b.py", s2.id]])
        self.assertEqual([(sid, p) for sid, _, p in self.index.lineage_for("b.py")],
                         [(s1.id, "a.py"), (s2.id, "b.py")])

    def test_rename_across_a_later_content_change_is_followed(self):
        self._write("a.py", "v1\n")
        s1 = self._snap_and_record()
        (self.source_dir / "a.py").rename(self.source_dir / "b.py")
        s2 = self._snap_and_record()
        self._write("b.py", "v2 edited\n")
        s3 = self._snap_and_record()

        lineage = self.index.lineage_for("b.py")
        self.assertEqual([(sid, p) for sid, _, p in lineage],
                         [(s1.id, "a.py"), (s2.id, "b.py"), (s3.id, "b.py")])

    def test_move_plus_content_change_in_same_snapshot_is_NOT_a_rename(self):
        self._write("a.py", "line one\nline two\nline three\n")
        s1 = self._snap_and_record()

        # move a.py -> b.py AND change its contents in the same snapshot
        (self.source_dir / "a.py").unlink()
        self._write("b.py", "line one\nline two CHANGED\nline three\n")
        s2 = self._snap_and_record()

        self.assertEqual(self.index.renames, [])
        # a.py's recorded history simply stops at s1
        self.assertEqual([sid for sid, _ in self.index.history_for("a.py")], [s1.id])
        # b.py's history begins fresh at s2, with no link back to a.py
        self.assertEqual([(sid, p) for sid, _, p in self.index.lineage_for("b.py")],
                         [(s2.id, "b.py")])
        self.assertEqual(self.index.lineage_for("a.py"),
                         [(s1.id, h, "a.py") for _, h in self.index.history_for("a.py")])

    def test_non_renamed_paths_are_completely_unaffected(self):
        # three plain content changes, no renames anywhere
        for i in range(3):
            self._write("evolving.py", f"version {i}\n")
            self._write("sidecar.txt", f"note {i}\n")
            self._snap_and_record()

        self.assertEqual(self.index.renames, [])
        hist = self.index.history_for("evolving.py")
        lineage = self.index.lineage_for("evolving.py")
        self.assertEqual(len(hist), 3)
        # lineage_for is exactly history_for + the (unchanging) path
        self.assertEqual(lineage, [(sid, h, "evolving.py") for sid, h in hist])

    # -- extra edge coverage ----------------------------------------------------

    def test_rename_chain_a_to_b_to_c_is_one_lineage(self):
        self._write("a.py", "constant\n")
        s1 = self._snap_and_record()
        (self.source_dir / "a.py").rename(self.source_dir / "b.py")
        s2 = self._snap_and_record()
        (self.source_dir / "b.py").rename(self.source_dir / "c.py")
        s3 = self._snap_and_record()

        self.assertEqual(self.index._name_chain("c.py"), ["a.py", "b.py", "c.py"])
        self.assertEqual(self.index._name_chain("a.py"), ["a.py", "b.py", "c.py"])
        self.assertEqual([(sid, p) for sid, _, p in self.index.lineage_for("c.py")],
                         [(s1.id, "a.py"), (s2.id, "b.py"), (s3.id, "c.py")])

    def test_ambiguous_identical_content_moves_are_not_linked(self):
        # two byte-identical (empty) files, both relocated in one snapshot
        self._write("x/__init__.py", "")
        self._write("y/__init__.py", "")
        self._snap_and_record()
        shutil.rmtree(self.source_dir / "x")
        shutil.rmtree(self.source_dir / "y")
        self._write("pkg1/__init__.py", "")
        self._write("pkg2/__init__.py", "")
        self._snap_and_record()

        # 2 gone + 2 appeared with the same hash -> ambiguous -> link none
        self.assertEqual(self.index.renames, [])

    def test_copy_is_not_a_rename(self):
        # a.py stays put; an identical copy appears at b.py
        self._write("a.py", "shared body\n")
        self._snap_and_record()
        self._write("b.py", "shared body\n")   # a.py still present
        self._snap_and_record()
        self.assertEqual(self.index.renames, [])

    def test_incremental_rename_index_matches_full_rebuild(self):
        self._write("a.py", "one\n")
        self._snap_and_record()
        (self.source_dir / "a.py").rename(self.source_dir / "b.py")
        self._snap_and_record()
        self._write("b.py", "two\n")
        self._snap_and_record()

        fresh_dir = self.root / "fresh_index"
        fresh_dir.mkdir()
        fresh = PathHistoryIndex(fresh_dir)
        fresh.rebuild_from_scratch(self.engine)

        self.assertEqual(fresh.renames, self.index.renames)
        self.assertEqual(fresh.lineage_for("b.py"), self.index.lineage_for("b.py"))

    def test_first_snapshot_never_produces_a_rename(self):
        self._write("only.py", "content\n")
        self._snap_and_record()
        self.assertEqual(self.index.renames, [])

    def test_rename_sidecar_survives_corruption_like_the_main_index(self):
        for bad in ["", "not json {{", "null", "[]", "42", '{"renames": "nope"}',
                    '{"renames": [[1, 2, 3]]}', '{"renames": [["a", "b"]]}']:
            d = Path(tempfile.mkdtemp())
            try:
                (d / "path_renames.json").write_text(bad)
                idx = PathHistoryIndex(d)
                self.assertEqual(idx.renames, [], f"bad content not degraded: {bad!r}")
            finally:
                shutil.rmtree(d, ignore_errors=True)

    def test_well_formed_rename_sidecar_loads(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "path_renames.json").write_text(
                '{"renames": [["old.py", "new.py", 2], ["new.py", "final.py", 5]]}'
            )
            idx = PathHistoryIndex(d)
            self.assertEqual(idx.renames,
                             [["old.py", "new.py", 2], ["new.py", "final.py", 5]])
            self.assertEqual(idx._name_chain("final.py"),
                             ["old.py", "new.py", "final.py"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_directory_rename_is_followed_per_file(self):
        """Moving a whole directory (`mv src/ pkg/`) is not a special
        case in the index -- every file inside moves from src/<x> to
        pkg/<x> with identical content, so each is linked
        independently by the same 1:1-content rule. Files inside the
        moved directory that are byte-identical to each other (e.g.
        empty __init__.py) stay ambiguous and are NOT linked; the
        distinctly-contented files still are."""
        self._write("src/__init__.py", "")
        self._write("src/sub/__init__.py", "")          # identical to the above
        self._write("src/mod_a.py", "AAA unique alpha\n")
        self._write("src/sub/helper.py", "HHH unique helper\n")
        s1 = self._snap_and_record()

        (self.source_dir / "src").rename(self.source_dir / "pkg")
        s2 = self._snap_and_record()

        # a later edit under the NEW directory path, to prove the
        # lineage keeps going past the move
        self._write("pkg/mod_a.py", "AAA unique alpha CHANGED\n")
        s3 = self._snap_and_record()

        # distinctly-contented files: linked across the directory move
        self.assertEqual(
            sorted(self.index.renames),
            sorted([
                ["src/mod_a.py", "pkg/mod_a.py", s2.id],
                ["src/sub/helper.py", "pkg/sub/helper.py", s2.id],
            ]),
        )
        self.assertEqual(
            [(sid, p) for sid, _, p in self.index.lineage_for("pkg/mod_a.py")],
            [(s1.id, "src/mod_a.py"), (s2.id, "pkg/mod_a.py"), (s3.id, "pkg/mod_a.py")],
        )
        self.assertEqual(
            [(sid, p) for sid, _, p in self.index.lineage_for("pkg/sub/helper.py")],
            [(s1.id, "src/sub/helper.py"), (s2.id, "pkg/sub/helper.py")],
        )
        # queryable from the old directory path too
        self.assertEqual(
            self.index.lineage_for("src/mod_a.py"),
            self.index.lineage_for("pkg/mod_a.py"),
        )
        # the two identical empty __init__.py files: ambiguous, NOT linked
        self.assertNotIn(
            "src/__init__.py",
            [old for old, _new, _sid in self.index.renames],
        )
        self.assertEqual(
            [(sid, p) for sid, _, p in self.index.lineage_for("pkg/__init__.py")],
            [(s2.id, "pkg/__init__.py")],
        )

    def test_path_reused_as_rename_target_twice(self):
        """
        KNOWN LIMITATION, pinned here (documented in the README under
        "Rename detection ... content-identical only").

        Sequence: a.py -> c.py (snapshot 2); c.py deleted (3);
        b.py -> c.py (snapshot 4). The path name "c.py" is now a
        rename target twice, for two unrelated files.

        What the index does, and what this test locks in:
          * BOTH renames are still detected correctly.
          * It never crashes and never fabricates a hash -- every
            (snapshot, hash) pair shown by lineage_for() genuinely
            occurred at that path.
          * BUT lineage is grouped by PATH NAME, not by file identity,
            so reusing "c.py" over-merges: _name_chain() follows only
            the most recent link into "c.py" (b.py -> c.py), and
            lineage_for() for any name in the tangle includes the
            snapshot-2 entry (a.py's brief life as c.py) alongside
            b.py's history. This is the name-based-lineage trade-off,
            not a correctness bug in rename detection itself.
        """
        self._write("a.py", "AAA content\n")
        self._write("b.py", "BBB content\n")
        s1 = self._snap_and_record()

        (self.source_dir / "a.py").rename(self.source_dir / "c.py")   # a -> c
        s2 = self._snap_and_record()

        (self.source_dir / "c.py").unlink()                            # delete c
        s3 = self._snap_and_record()

        (self.source_dir / "b.py").rename(self.source_dir / "c.py")   # b -> c (reuse)
        s4 = self._snap_and_record()

        # 1. both renames detected
        self.assertEqual(
            self.index.renames,
            [["a.py", "c.py", s2.id], ["b.py", "c.py", s4.id]],
        )

        h_aaa = self.index.history_for("a.py")[0][1]
        h_bbb = self.index.history_for("b.py")[0][1]

        # 2. bounded + deterministic: incremental result == full rebuild
        fresh_dir = self.root / "fresh_reuse"
        fresh_dir.mkdir()
        fresh = PathHistoryIndex(fresh_dir)
        fresh.rebuild_from_scratch(self.engine)
        self.assertEqual(fresh.renames, self.index.renames)
        self.assertEqual(fresh.lineage_for("c.py"), self.index.lineage_for("c.py"))

        # 3. no fabricated data: every (snapshot, hash) in any lineage
        #    is a real recorded entry for some name
        recorded = set()
        for name in ("a.py", "b.py", "c.py"):
            for sid, h in self.index.history_for(name):
                recorded.add((sid, h))
        for name in ("a.py", "b.py", "c.py"):
            for sid, h, _ in self.index.lineage_for(name):
                self.assertIn((sid, h), recorded)

        # 4. the documented over-merge, pinned exactly:
        #    _name_chain follows only the most recent link into "c.py"
        self.assertEqual(self.index._name_chain("c.py"), ["b.py", "c.py"])
        #    querying c.py shows b.py's history PLUS a.py's snapshot-2
        #    entry (when a.py was briefly named c.py)
        self.assertEqual(
            self.index.lineage_for("c.py"),
            [(s1.id, h_bbb, "b.py"), (s2.id, h_aaa, "c.py"), (s4.id, h_bbb, "c.py")],
        )
        #    and querying a.py picks up b.py's later snapshot-4 content
        #    because both link to the shared "c.py" name
        self.assertEqual(
            self.index.lineage_for("a.py"),
            [(s1.id, h_aaa, "a.py"), (s2.id, h_aaa, "c.py"), (s4.id, h_bbb, "c.py")],
        )


class TestRenameAwareLogCommand(unittest.TestCase):
    """`vault log` output across a rename, exercised through the CLI."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.cwd = Path.cwd()
        self.source_dir = self.root / "project"
        self.source_dir.mkdir()
        import os
        os.chdir(self.source_dir)

    def tearDown(self):
        import os
        os.chdir(self.cwd)
        shutil.rmtree(self.root, ignore_errors=True)

    def _vault(self, *args):
        import io
        import contextlib
        from vault.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(list(args))
        return code, buf.getvalue()

    def test_log_follows_a_rename_and_says_so(self):
        (self.source_dir / "handler.py").write_text("def go():\n    pass\n")
        self._vault("init", ".")
        self._vault("snapshot", "-m", "v1")
        (self.source_dir / "handler.py").rename(self.source_dir / "router.py")
        self._vault("snapshot", "-m", "renamed")
        (self.source_dir / "router.py").write_text("def go():\n    return 1\n")
        self._vault("snapshot", "-m", "v3")

        code, out = self._vault("log", "router.py")
        self.assertEqual(code, 0)
        self.assertIn("(as handler.py)", out)
        self.assertIn("also been known as: handler.py", out)
        # 3 rows: as handler.py, as router.py (rename), as router.py (edit)
        self.assertEqual(out.count("Snapshot "), 3)

    def test_log_for_never_renamed_file_output_is_unchanged(self):
        (self.source_dir / "plain.py").write_text("a\n")
        self._vault("init", ".")
        self._vault("snapshot", "-m", "1")
        (self.source_dir / "plain.py").write_text("b\n")
        self._vault("snapshot", "-m", "2")

        code, out = self._vault("log", "plain.py")
        self.assertEqual(code, 0)
        self.assertNotIn("(as ", out)
        self.assertNotIn("also been known as", out)
        self.assertIn("2 change(s) recorded.", out)
