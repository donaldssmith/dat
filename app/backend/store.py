"""轻量级、可恢复的本地任务队列。"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .protocol import F4SJob, parse_job


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class JobStore:
    """JSON 文件队列，适合单机 worker；写入采用临时文件替换。"""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _write(self, records: list[dict[str, Any]]) -> None:
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            records = self._read()
        if status:
            records = [item for item in records if item.get("status") == status]
        return records

    def get(self, job_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list() if item.get("id") == job_id), None)

    def submit(self, raw: dict[str, Any], source_message_id: str = "") -> dict[str, Any]:
        job = parse_job(raw)
        payload = job.to_dict()
        identity = source_message_id.strip() or self._fingerprint(payload)
        with self._lock:
            records = self._read()
            for item in records:
                if item.get("identity") == identity:
                    return item
            record = {
                "id": uuid.uuid4().hex,
                "identity": identity,
                "status": "pending",
                "createdAt": _now(),
                "updatedAt": _now(),
                "attempts": 0,
                "error": "",
                "job": payload,
            }
            records.append(record)
            self._write(records)
            return record

    def claim_next(self) -> dict[str, Any] | None:
        with self._lock:
            records = self._read()
            candidate = next((item for item in records if item.get("status") == "pending"), None)
            if not candidate:
                return None
            candidate["status"] = "processing"
            candidate["attempts"] = int(candidate.get("attempts") or 0) + 1
            candidate["updatedAt"] = _now()
            self._write(records)
            return candidate

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            records = self._read()
            for item in records:
                if item.get("id") == job_id:
                    item.update(changes, updatedAt=_now())
                    self._write(records)
                    return item
        raise KeyError(job_id)

    def complete(self, job_id: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.update(job_id, status="succeeded", result=result or {}, error="")

    def fail(self, job_id: str, error: str, retry: bool = False) -> dict[str, Any]:
        return self.update(job_id, status="pending" if retry else "failed", error=str(error))

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
