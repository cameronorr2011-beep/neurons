import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="neurons-store-"))
        store.init_db(self.tmp / "test.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_profile_round_trip_and_validation(self):
        updated = store.update_profile({
            "name": "Cameron",
            "about": "Builds local tools",
            "style": "shouty",
            "theme": "neon",
        })
        self.assertEqual(updated["name"], "Cameron")
        self.assertEqual(updated["style"], "balanced")
        self.assertEqual(updated["theme"], "graphite")
        self.assertEqual(store.get_profile()["about"], "Builds local tools")

    def test_memory_dedupes_and_deletes(self):
        first = store.add_memory("Prefers concise answers")
        duplicate = store.add_memory("Prefers concise answers")
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(len(store.list_memories()), 1)
        self.assertTrue(store.delete_memory(first["id"]))
        self.assertFalse(store.delete_memory(first["id"]))
        self.assertEqual(store.list_memories(), [])

    def test_conversation_order_and_cascade(self):
        first = store.create_conversation("First")
        second = store.create_conversation("Second")
        store.append_message(first["id"], "user", "hello")
        store.append_message(first["id"], "assistant", "hi")
        store.append_message(second["id"], "user", "newer")
        chats = store.list_conversations()
        self.assertEqual(chats[0]["id"], second["id"])
        loaded = store.get_conversation(first["id"])
        self.assertEqual([m["role"] for m in loaded["messages"]], ["user", "assistant"])
        self.assertTrue(store.delete_conversation(first["id"]))
        self.assertIsNone(store.get_conversation(first["id"]))

    def test_state_selects_latest_conversation(self):
        older = store.create_conversation("Older")
        store.append_message(older["id"], "user", "old")
        newer = store.create_conversation("Newer")
        store.append_message(newer["id"], "user", "new")
        state = store.get_state()
        self.assertEqual(state["conversation"]["id"], newer["id"])
        self.assertEqual(len(state["conversations"]), 2)


if __name__ == "__main__":
    unittest.main()
