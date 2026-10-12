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

    def test_persists_project_and_user_notes(self):
        self.store.append_notes("project", "Use uv for Python commands.")
        self.store.append_notes("user", "Prefers concise answers.")

        self.assertIn("Use uv", self.store.read_notes("project"))
        self.assertIn("concise", self.store.read_notes("user"))

    def test_migrates_existing_personal_notes_to_project(self):
        (self.root / "personal.md").write_text("Use uv for Python commands.\n")

        migrated = MemoryStore(self.root)

        self.assertFalse((self.root / "personal.md").exists())
        self.assertIn("Use uv", migrated.read_notes("project"))

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
