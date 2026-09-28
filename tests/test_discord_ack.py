import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.backend.discord import acknowledge_message, cleanup_history, sync_messages
from app.backend.store import JobStore
from app.backend.worker import run_once


def job_payload():
    return {
        "version": 1,
        "protocol": "F4S_JOB",
        "type": "nudge",
        "source": "test",
        "createdAt": "2026-09-28T00:00:00Z",
        "bookId": "100",
        "title": "Test",
        "pdf": {"mode": "none", "url": ""},
    }


class DiscordAckTests(unittest.TestCase):
    def _root(self, delete=False):
        root = Path(self.temp.name)
        config_dir = root / "config" / "local"
        config_dir.mkdir(parents=True)
        (config_dir / "部署台.bot.json").write_text(
            json.dumps({"bot_token": "test", "channel_id": "123", "delete_processed_messages": delete}),
            encoding="utf-8",
        )
        return root

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def test_acknowledge_persists_and_optionally_deletes(self):
        root = self._root(delete=True)
        with patch("app.backend.discord.delete_message", return_value=True) as delete:
            result = acknowledge_message(root, "m-1")
        self.assertEqual(result, {"handled": True, "deleted": True})
        delete.assert_called_once()
        state = json.loads((root / "config" / "local" / "部署台.bot.state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["handled_message_ids"], ["m-1"])

    def test_cleanup_history_marks_only_protocol_messages(self):
        root = self._root(delete=False)
        messages = [
            {"id": "m-1", "content": "**F4S JOB**\n```json\n" + json.dumps(job_payload()) + "\n```"},
            {"id": "m-2", "content": "ordinary chat"},
        ]
        with patch("app.backend.discord.fetch_messages", return_value=messages):
            stats = cleanup_history(root)
        self.assertEqual(stats["recognized"], 1)
        self.assertEqual(stats["handled"], 1)
        self.assertEqual(stats["deleted"], 0)

    def test_sync_skips_previously_handled_message(self):
        root = self._root()
        state_path = root / "config" / "local" / "部署台.bot.state.json"
        state_path.write_text(json.dumps({"handled_message_ids": ["m-1"]}), encoding="utf-8")
        store = JobStore(root / "jobs.json")
        message = {"id": "m-1", "content": "**F4S JOB**\n```json\n" + json.dumps(job_payload()) + "\n```"}
        with patch("app.backend.discord.fetch_messages", return_value=[message]):
            stats = sync_messages(store, root)
        self.assertEqual(stats["handled"], 1)
        self.assertEqual(store.list(), [])

    def test_worker_acknowledges_source_message_after_success(self):
        root = self._root()
        store = JobStore(root / "jobs.json")
        record = store.submit(job_payload(), source_message_id="m-2")
        with patch("app.backend.worker.acknowledge_message", return_value={"handled": True, "deleted": False}) as ack:
            self.assertTrue(run_once(store, root))
        ack.assert_called_once_with(root, "m-2")
        completed = store.get(record["id"])
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["result"]["discord"], {"handled": True, "deleted": False})


if __name__ == "__main__":
    unittest.main()
