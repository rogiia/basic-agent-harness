import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
from sessions import SessionStore
from resource_limits import ContextBudget, CostTracker


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(Path(self.temp.name))
        self.messages = [
            {"role": "system", "content": "Follow security policy."},
            {"role": "user", "content": "Fix cache. " * 300},
            {"role": "assistant", "content": "Changed cache.py and tests. " * 300},
            {"role": "user", "content": "What remains?"},
        ]
        self.store.create_session("one", self.messages)
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(usage=None, choices=[SimpleNamespace(
                message=SimpleNamespace(content="Fixed cache.py; tests passed; document next."))]))))

    def compact(self):
        return self.store.compact("one", self.client, ContextBudget(), CostTracker(), force=True)

    def test_lossless_compaction_and_followup(self):
        replacement = self.compact()
        record = self.store.load_session("one")
        self.assertEqual(record["transcript"], self.messages)
        self.assertEqual(replacement[-1], self.messages[-1])
        self.assertEqual(replacement[1]["role"], "user")
        followup = {"role": "assistant", "content": "Documentation."}
        self.store.save_messages("one", replacement + [followup])
        self.assertEqual(self.store.load_session("one")["transcript"], self.messages + [followup])
        self.assertEqual(len(record["compactions"]), 1)

    def test_compaction_keeps_complete_tool_batch(self):
        messages = self.messages[:1] + [self.messages[1], self.messages[2],
            {"role": "user", "content": "Inspect"},
            {"role": "assistant", "tool_calls": [{"id": "call", "function": {"name": "read_file"}}]},
            {"role": "tool", "tool_call_id": "call", "content": "file"}]
        self.store.create_session("tools", messages)
        result = self.store.compact("tools", self.client, ContextBudget(), CostTracker(), force=True)
        self.assertEqual(result[-3:], messages[-3:])

    def test_failed_compaction_does_not_change_session(self):
        before = self.store.export("one")
        self.client.chat.completions.create = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
        with self.assertRaises(RuntimeError):
            self.compact()
        self.assertEqual(self.store.export("one"), before)

    def test_empty_or_oversized_summary_retains_original_context(self):
        before = self.store.export("one")
        for summary in ("", "x" * 50000):
            self.client.chat.completions.create = lambda **kwargs: SimpleNamespace(
                usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content=summary))])
            with self.assertRaises(ValueError):
                self.compact()
            self.assertEqual(self.store.export("one"), before)

    def test_automatic_compaction_threshold_and_cost_guard(self):
        self.assertIsNone(self.store.compact("one", self.client, ContextBudget(), CostTracker()))
        self.client.chat.completions.create = Mock(side_effect=AssertionError("Must not call model"))
        with self.assertRaises(ValueError):
            self.store.compact("one", self.client, ContextBudget(), CostTracker(max_cost_usd=0), force=True)
        self.client.chat.completions.create.assert_not_called()

    def test_fork_archive_rename_export_delete(self):
        self.compact()
        child = self.store.fork("one")
        self.store.update(child, title="Branch", archived=True)
        self.assertEqual(len(self.store.list_sessions()), 1)
        self.assertEqual(len(self.store.list_sessions(include_archived=True)), 2)
        exported = json.loads(self.store.export(child))
        self.assertEqual(exported["parent_id"], "one")
        self.assertEqual(exported["transcript"], self.messages)
        self.store.delete(child)
        with self.assertRaises(ValueError):
            self.store.load_session(child)

    def test_crash_recovery_does_not_replay_tools(self):
        call = {"role": "assistant", "tool_calls": [
            {"id": "a", "function": {"name": "write_file"}},
            {"id": "b", "function": {"name": "run_bash"}}]}
        self.store.save_messages("one", self.messages + [call,
            {"role": "tool", "tool_call_id": "a", "content": "saved"},
            {"role": "user", "content": "Abort"}])
        recovered = self.store.recover("one")
        self.assertEqual(recovered[-2]["tool_call_id"], "b")
        self.assertIn("unknown", recovered[-2]["content"])
        self.assertEqual(self.store.recover("one"), recovered)

    def test_legacy_session_migration(self):
        record = self.store.load_session("one")
        for key in ("transcript", "state", "title", "archived", "compactions"):
            record.pop(key, None)
        self.store._session_path("one").write_text(json.dumps(record))
        self.assertEqual(self.store.load_session("one")["transcript"], self.messages)

    def test_concurrent_process_cannot_mutate_session(self):
        script = "from pathlib import Path; from sessions import SessionStore; " \
                 f"SessionStore(Path({self.temp.name!r})).update('one', title='bad')"
        with self.store.lease("one"):
            result = subprocess.run([sys.executable, "-c", script],
                                    cwd=Path(__file__).parent, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("in use", result.stderr)
        self.store.update("one", title="available")

    def test_ids_and_overwrites_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_session("one", [])
        with self.assertRaises(ValueError):
            self.store.load_session("../outside")

    def test_tool_arguments_count_toward_context_budget(self):
        message = {"role": "assistant", "tool_calls": [{"arguments": "x" * 4000}]}
        self.assertGreaterEqual(ContextBudget().estimate_total([message]), 1000)

    def test_agent_resumes_planning_state_and_checkpoints_reply(self):
        from agent import agent_loop, PermissionMode
        from openai.types.chat import ChatCompletionMessage
        from tools.todo import todo_store
        from tools.scratchpad import scratchpad
        self.store.update("one", state={"scratchpad": "Document cache",
            "todos": [{"id": "docs", "content": "Write docs", "status": "pending", "retries": 2}],
            "calls": 3, "tool_calls": 4})
        reply = ChatCompletionMessage(role="assistant", content="Document next.")
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(usage=None, choices=[SimpleNamespace(
                message=reply, finish_reason="stop")]))))
        with patch("builtins.input", side_effect=["Continue", "\\info", "\\exit"]), patch("builtins.print"):
            agent_loop(client, PermissionMode.DEFAULT, Path(self.temp.name), {}, Mock(),
                       tool_schemas=[], memory_store=self.store, session_id="one",
                       resumed_messages=self.messages)
        record = self.store.load_session("one")
        self.assertEqual(record["state"]["calls"], 4)
        self.assertEqual(record["state"]["tool_calls"], 4)
        self.assertEqual(record["state"]["todos"][0]["retries"], 2)
        self.assertEqual(record["transcript"][-1]["content"], "Document next.")
        self.assertEqual(scratchpad.read(), "Document cache")
        todo_store._items = []
        scratchpad.write("")

    def test_offline_cli_management(self):
        agent_path = str(Path(__file__).parent / "agent.py")

        def run(*args):
            return subprocess.run([sys.executable, agent_path, "--memory-dir", self.temp.name, *args],
                                  capture_output=True, text=True)

        result = run("--rename", "one", "Cache task")
        self.assertEqual(result.returncode, 0, result.stderr)
        exported = run("--export", "one")
        self.assertEqual(json.loads(exported.stdout)["title"], "Cache task")
        self.assertNotEqual(run("--delete", "one").returncode, 0)
        self.assertEqual(run("--delete", "one", "--yes").returncode, 0)


if __name__ == "__main__":
    unittest.main()
