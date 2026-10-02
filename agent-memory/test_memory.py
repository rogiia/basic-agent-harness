import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from memory import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.store = MemoryStore(self.root)

    def test_persists_personal_and_user_notes(self):
        self.store.append_notes("personal", "Use uv for Python commands.")
        self.store.append_notes("user", "Prefers concise answers.")

        self.assertIn("Use uv", self.store.read_notes("personal"))
        self.assertIn("concise", self.store.read_notes("user"))

    def test_lists_and_searches_saved_sessions(self):
        self.store.create_session("session-one", [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Investigate the cache bug."},
        ])
        self.store.write_summary("session-one", "Found and fixed the cache bug.")

        self.assertEqual(self.store.list_sessions()[0]["id"], "session-one")
        self.assertEqual(self.store.search_sessions("cache")[0]["id"], "session-one")

    def test_creates_fallback_summary(self):
        self.store.create_session("session-two", [
            {"role": "user", "content": "Please inspect this project."},
        ])
        self.store.ensure_summary("session-two")

        self.assertIn("inspect", self.store.load_session("session-two")["summary"])


if __name__ == "__main__":
    unittest.main()
