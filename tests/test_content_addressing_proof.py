"""
tests/test_content_addressing_proof.py

Integration test: `scripts/content_addressing_proof.py` is the
executable statement of ChronoVault's core thesis, so it must actually
pass (not just exist) and its JSON contract must be stable.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROOF = ROOT / "scripts" / "content_addressing_proof.py"


class ContentAddressingProof(unittest.TestCase):
    def test_proof_passes_end_to_end(self):
        r = subprocess.run([sys.executable, str(PROOF)],
                           capture_output=True, text=True, cwd=str(ROOT),
                           encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("RESULT: PASS", r.stdout)
        self.assertNotIn("[FAIL]", r.stdout)

    def test_proof_json_contract(self):
        r = subprocess.run([sys.executable, str(PROOF), "--json"],
                           capture_output=True, text=True, cwd=str(ROOT),
                           encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        obj = json.loads(r.stdout)
        self.assertEqual(obj["result"], "PASS")
        self.assertEqual(len(obj["checks"]), 8)
        self.assertTrue(all(c["result"] == "PASS" for c in obj["checks"]))
        # the two thesis-defining invariants must be present by name
        names = " ".join(c["check"] for c in obj["checks"])
        self.assertIn("identical content", names)
        self.assertIn("delta-encoded objects reconstruct the original bytes", names)


if __name__ == "__main__":
    unittest.main()
