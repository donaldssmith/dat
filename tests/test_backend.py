import tempfile
import unittest
from pathlib import Path

from app.backend.protocol import JobValidationError, parse_job
from app.backend.store import JobStore


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
    def test_protocol_rejects_wrong_version(self):
        with self.assertRaises(JobValidationError):
            parse_job(valid_job(version=2))

    def test_store_deduplicates_source_message(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp) / "jobs.json")
            first = store.submit(valid_job(), source_message_id="discord-1")
            second = store.submit(valid_job(), source_message_id="discord-1")
            self.assertEqual(first["id"], second["id"])
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


if __name__ == "__main__":
    unittest.main()
