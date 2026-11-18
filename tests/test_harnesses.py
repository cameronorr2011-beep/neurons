import sys
import tempfile
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

    def test_saved_keys_are_redacted_and_used_for_harness_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            harnesses.configure(tmp)
            harnesses.set_key("claude-code", "test-anthropic-key")
            items = harnesses.catalog()
            claude = next(item for item in items if item["id"] == "claude-code")
            self.assertTrue(claude["auth_configured"])
            self.assertTrue(claude["key_saved"])
            self.assertEqual(claude["key_source"], "saved")
            self.assertNotIn("test-anthropic-key", repr(items))
            harnesses.clear_key("claude-code")
            claude = next(item for item in harnesses.catalog() if item["id"] == "claude-code")
            self.assertFalse(claude["key_saved"])
            self.assertEqual(harnesses._stored_key("claude-code"), "")

    def test_unknown_or_keyless_harness_rejects_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            harnesses.configure(tmp)
            with self.assertRaises(ValueError):
                harnesses.set_key("nope", "abc")
            with self.assertRaises(ValueError):
                harnesses.set_key("hermes", "abc")
            with self.assertRaises(ValueError):
                harnesses.set_key("codex", "   ")


if __name__ == "__main__":
    unittest.main()
