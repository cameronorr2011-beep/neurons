import io
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import updater


def make_archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in files.items():
            payload = text.encode("utf-8")
            info = tarfile.TarInfo("neurons-main/" + name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="neurons-update-"))
        (self.root / "data").mkdir()
        (self.root / "web").mkdir()
        (self.root / "data" / "neurons.db").write_text("USER DATA", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=keep-me\n", encoding="utf-8")
        (self.root / ".agents" / "skills" / "mine").mkdir(parents=True)
        (self.root / ".agents" / "skills" / "mine" / "SKILL.md").write_text("MY SKILL", encoding="utf-8")
        (self.root / "server.py").write_text("OLD SERVER", encoding="utf-8")
        (self.root / "web" / "index.html").write_text("OLD UI", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_update_replaces_code_and_preserves_user_files(self):
        archive = make_archive({
            "VERSION": "9.9.9\n",
            "server.py": "NEW SERVER\n",
            "web/index.html": "NEW UI\n",
            "data/neurons.db": "ATTACKER",
            ".env": "SECRET=attacker\n",
            ".agents/skills/mine/SKILL.md": "ATTACKER",
        })
        with patch.dict(os.environ, {"NEURONS_DATA": str(self.root / "data")}, clear=False):
            result = updater.apply_archive(archive, app_root=self.root, version="9.9.9")

        self.assertEqual(result["version"], "9.9.9")
        self.assertEqual((self.root / "server.py").read_text(encoding="utf-8"), "NEW SERVER\n")
        self.assertEqual((self.root / "web" / "index.html").read_text(encoding="utf-8"), "NEW UI\n")
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), "9.9.9\n")
        self.assertEqual((self.root / "data" / "neurons.db").read_text(encoding="utf-8"), "USER DATA")
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), "SECRET=keep-me\n")
        self.assertEqual(
            (self.root / ".agents" / "skills" / "mine" / "SKILL.md").read_text(encoding="utf-8"),
            "MY SKILL",
        )
        self.assertTrue((self.root / "data" / "update-state.json").is_file())

    def test_archive_missing_required_files_is_rejected(self):
        archive = make_archive({"VERSION": "9.9.9\n", "server.py": "NEW"})
        with patch.dict(os.environ, {"NEURONS_DATA": str(self.root / "data")}, clear=False):
            with self.assertRaises(ValueError):
                updater.apply_archive(archive, app_root=self.root, version="9.9.9")
        self.assertEqual((self.root / "server.py").read_text(encoding="utf-8"), "OLD SERVER")


if __name__ == "__main__":
    unittest.main()
