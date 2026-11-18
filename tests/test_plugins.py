import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import plugins


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="neurons-plugins-"))
        self.manager = plugins.PluginManager(self.tmp / "plugins.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_catalog_has_core_services_and_no_field_values(self):
        data = self.manager.catalog()
        ids = {item["id"] for item in data["plugins"]}
        for expected in ("github", "gmail", "notion", "outlook", "sigma", "slack"):
            self.assertIn(expected, ids)
        self.assertFalse(data["connected"])
        for item in data["plugins"]:
            for field in item["fields"]:
                self.assertNotIn("value", field)

    def test_connection_persists_but_never_returns_the_token(self):
        self.manager.connect("github", {"token": "ghp_test_secret"})
        public = self.manager.catalog()
        github = next(item for item in public["plugins"] if item["id"] == "github")
        self.assertTrue(github["connected"])
        self.assertNotIn("ghp_test_secret", json.dumps(public))
        saved = (self.tmp / "plugins.json").read_text(encoding="utf-8")
        self.assertIn("ghp_test_secret", saved)
        self.manager.disconnect("github")
        self.assertFalse(self.manager.catalog()["connected"])

    def test_required_fields_and_relative_paths_are_enforced(self):
        with self.assertRaises(plugins.PluginError):
            self.manager.connect("github", {})
        self.manager.connect("github", {"token": "test"})
        with self.assertRaises(plugins.PluginError):
            self.manager.call("github", "GET", "https://evil.example/steal")
        with self.assertRaises(plugins.PluginError):
            self.manager.call("github", "GET", "//evil.example/steal")

    def test_sigma_uses_client_credentials_shape(self):
        self.manager.connect("sigma", {
            "client_id": "client",
            "client_secret": "sigma-secret-value",
            "base_url": "https://api.sigmacomputing.com",
        })
        public = self.manager.catalog()
        sigma = next(item for item in public["plugins"] if item["id"] == "sigma")
        self.assertTrue(sigma["connected"])
        self.assertNotIn("sigma-secret-value", json.dumps(public))


if __name__ == "__main__":
    unittest.main()
