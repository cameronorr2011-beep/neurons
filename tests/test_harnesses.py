import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import harnesses


class HarnessTests(unittest.TestCase):
    def test_catalog_contains_coding_agents_and_marks_them_training_exempt(self):
        items = harnesses.catalog(lambda name: "configured" if name == "OPENAI_API_KEY" else "")
        ids = {item["id"] for item in items}
        for expected in ("claude-code", "codex", "antigravity", "freebuff", "hermes"):
            self.assertIn(expected, ids)
        self.assertTrue(all(item["training_exempt"] for item in items))
        codex = next(item for item in items if item["id"] == "codex")
        self.assertTrue(codex["auth_configured"])
        self.assertIn("installed", codex)


if __name__ == "__main__":
    unittest.main()
