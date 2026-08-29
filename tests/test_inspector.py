"""
Tests for vault/inspector.py's route() function — the pure routing
logic behind `vault serve`, tested without opening a real socket.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.inspector import route
from vault.snapshot import SnapshotEngine


class TestInspectorRouting(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.vault_dir = self.root / ".vault"
        self.source_dir = self.root / "project"
        self.source_dir.mkdir()
        self.engine = SnapshotEngine(self.vault_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, rel_path: str, content: str):
        p = self.source_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def test_index_returns_html(self):
        status, content_type, body = route(self.engine, "/", {})
        self.assertEqual(status, 200)
        self.assertIn("html", content_type)
        self.assertIn(b"ChronoVault Inspector", body)

    def test_status_endpoint_on_empty_repo(self):
        status, _content_type, body = route(self.engine, "/api/status", {})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["snapshot_count"], 0)
        self.assertTrue(payload["integrity_ok"])

    def test_snapshots_endpoint_reflects_real_data(self):
        self._write("a.txt", "hello")
        self.engine.create_snapshot(self.source_dir, message="first")
        _status, _, body = route(self.engine, "/api/snapshots", {})
        payload = json.loads(body)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["message"], "first")

    def test_explain_endpoint(self):
        self._write("a.txt", "content")
        record = self.engine.create_snapshot(self.source_dir)
        status, _, body = route(self.engine, "/api/explain", {"id": [str(record.id)]})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["files"], 1)

    def test_explain_endpoint_on_missing_snapshot_returns_400_not_500(self):
        status, _, body = route(self.engine, "/api/explain", {"id": ["999"]})
        self.assertEqual(status, 400)
        payload = json.loads(body)
        self.assertIn("error", payload)

    def test_diff_endpoint(self):
        self._write("a.txt", "v1")
        s1 = self.engine.create_snapshot(self.source_dir)
        self._write("a.txt", "v2")
        s2 = self.engine.create_snapshot(self.source_dir)
        _status, _, body = route(self.engine, "/api/diff", {"a": [str(s1.id)], "b": [str(s2.id)]})
        payload = json.loads(body)
        self.assertEqual(payload["modified"], ["a.txt"])

    def test_verify_endpoint_healthy_repo(self):
        self._write("a.txt", "content")
        self.engine.create_snapshot(self.source_dir)
        _status, _, body = route(self.engine, "/api/verify", {})
        payload = json.loads(body)
        self.assertTrue(payload["healthy"])
        self.assertEqual(payload["corrupted"], [])

    def test_verify_endpoint_detects_corruption(self):
        self._write("a.txt", "content")
        record = self.engine.create_snapshot(self.source_dir)
        entries = self.engine.load_tree(record.root_tree_hash)
        blob_hash = next(e.obj_hash for e in entries if e.kind == "blob")
        path = self.engine.store._object_path(blob_hash)
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 0xFF
        path.write_bytes(bytes(raw))

        _status, _, body = route(self.engine, "/api/verify", {})
        payload = json.loads(body)
        self.assertFalse(payload["healthy"])
        self.assertIn(blob_hash, payload["corrupted"])

    def test_unknown_route_returns_404(self):
        status, _, _ = route(self.engine, "/nonexistent", {})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()


class TestAdversarialQueryParams(unittest.TestCase):
    """
    Regression tests for a real bug found by adversarial fuzzing:
    query.get(key, [default])[0] assumed the value list is non-empty
    whenever the key is present -- {"id": []} crashed with an
    uncaught IndexError instead of a clean 400 response.
    """
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.vault_dir = self.root / ".vault"
        self.source_dir = self.root / "project"
        self.source_dir.mkdir()
        self.engine = SnapshotEngine(self.vault_dir)
        (self.source_dir / "a.txt").write_text("content")
        self.engine.create_snapshot(self.source_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_explain_with_empty_id_list_returns_400_not_crash(self):
        status, ctype, body = route(self.engine, "/api/explain", {"id": []})
        self.assertEqual(status, 400)

    def test_diff_with_empty_a_list_returns_400_not_crash(self):
        status, ctype, body = route(self.engine, "/api/diff", {"a": [], "b": ["1"]})
        self.assertEqual(status, 400)

    def test_diff_with_empty_b_list_returns_400_not_crash(self):
        status, ctype, body = route(self.engine, "/api/diff", {"a": ["1"], "b": []})
        self.assertEqual(status, 400)

    def test_explain_still_works_normally_with_valid_id(self):
        status, ctype, body = route(self.engine, "/api/explain", {"id": ["1"]})
        self.assertEqual(status, 200)


class TestAdversarialRouteFuzzing(unittest.TestCase):
    """
    Regression test for a real gap found by fuzzing route() directly
    with malformed query dicts: {"id": [None]} raised an uncaught
    TypeError (int(None)), not caught by the route()'s exception
    handler, which only caught ValueError/VaultError at the time.
    Not reachable via a real HTTP request (parse_qs() never produces
    non-string values), but route() is documented as a public,
    directly-callable function -- the same defense-in-depth standard
    already applied to a similar prior finding ({"id": []}).
    """
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.vault_dir = self.root / ".vault"
        self.source_dir = self.root / "project"
        self.source_dir.mkdir()
        self.engine = SnapshotEngine(self.vault_dir)
        (self.source_dir / "a.txt").write_text("content")
        self.engine.create_snapshot(self.source_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_none_value_in_query_returns_400_not_uncaught_typeerror(self):
        status, ctype, body = route(self.engine, "/api/explain", {"id": [None]})
        self.assertEqual(status, 400)

    def test_empty_id_list_still_returns_400(self):
        status, ctype, body = route(self.engine, "/api/explain", {"id": []})
        self.assertEqual(status, 400)

    def test_multiple_id_values_uses_first_without_crashing(self):
        status, ctype, body = route(self.engine, "/api/explain", {"id": ["1", "2", "3"]})
        self.assertIn(status, (200, 400))  # must not crash either way

    def test_huge_number_id_returns_400_not_crash(self):
        status, ctype, body = route(self.engine, "/api/explain", {"id": ["99999999999999999999"]})
        self.assertEqual(status, 400)

    def test_null_byte_in_path_returns_404_not_crash(self):
        status, ctype, body = route(self.engine, "/\x00/status", {})
        self.assertEqual(status, 404)
