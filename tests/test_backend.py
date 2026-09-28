import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.backend.protocol import JobValidationError, parse_job
from app.backend.store import JobStore
from app.backend.discord import parse_message, sync_messages


def valid_job(**overrides):
    payload = {
        "version": 1,
        "protocol": "F4S_JOB",
        "type": "meta_upload",
        "source": "test",
        "createdAt": "2026-09-28T00:00:00Z",
        "bookId": "6505",
        "title": "Big Bang 7",
        "meta": {"url": "https://example.test/6505-0.dat"},
        "pdf": {"mode": "none", "url": ""},
    }
    payload.update(overrides)
    return payload


class BackendTests(unittest.TestCase):
    def test_discord_message_extracts_job(self):
        job = valid_job()
        content = "**F4S JOB**\n```json\n" + json.dumps(job) + "\n```"
        parsed = parse_message({"id": "discord-1", "content": content})
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.book_id, "6505")

    def test_discord_message_ignores_plain_text(self):
        self.assertIsNone(parse_message({"id": "discord-2", "content": "hello"}))

    def test_discord_sync_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config" / "local").mkdir(parents=True)
            (root / "config" / "local" / "部署台.bot.json").write_text(
                json.dumps({"bot_token": "test", "channel_id": "123"}), encoding="utf-8"
            )
            store = JobStore(root / "jobs.json")
            job = valid_job()
            message = {"id": "discord-3", "content": "**F4S JOB**\n```json\n" + json.dumps(job) + "\n```"}
            with patch("app.backend.discord.fetch_messages", return_value=[message]):
                first = sync_messages(store, root)
                second = sync_messages(store, root)
            self.assertEqual(first["accepted"], 1)
            self.assertEqual(second["duplicates"], 1)
            self.assertEqual(len(store.list()), 1)

    def test_protocol_rejects_wrong_version(self):
        with self.assertRaises(JobValidationError):
            parse_job(valid_job(version=2))

    def test_store_deduplicates_source_message(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp) / "jobs.json")
            first = store.submit(valid_job(), source_message_id="discord-1")
            second = store.submit(valid_job(), source_message_id="discord-1")
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(first["sourceMessageId"], "discord-1")
            self.assertEqual(len(store.list()), 1)

    def test_store_claim_and_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp) / "jobs.json")
            record = store.submit(valid_job(), source_message_id="discord-2")
            claimed = store.claim_next()
            self.assertEqual(claimed["id"], record["id"])
            self.assertEqual(claimed["status"], "processing")
            done = store.complete(record["id"], {"ok": True})
            self.assertEqual(done["status"], "succeeded")
            self.assertTrue(done["result"]["ok"])

    def test_store_failed_tasks_can_be_requeued(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp) / "jobs.json")
            record = store.submit(valid_job(), source_message_id="discord-failed")
            claimed = store.claim_next()
            self.assertEqual(claimed["id"], record["id"])
            failed = store.fail(record["id"], "temporary upstream outage")
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error"], "temporary upstream outage")

            self.assertEqual(store.retry_failed(), 1)
            retried = store.get(record["id"])
            self.assertEqual(retried["status"], "pending")
            self.assertEqual(retried["error"], "")
            self.assertEqual(retried["lastError"], "temporary upstream outage")
            self.assertEqual(retried["attempts"], 1)
            self.assertEqual(store.retry_failed(), 0)


if __name__ == "__main__":
    unittest.main()
