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
        source_message_id = source_message_id.strip()
        identity = source_message_id or self._fingerprint(payload)
        with self._lock:
            records = self._read()
            for item in records:
                if item.get("identity") == identity:
                    # 兼容在 sourceMessageId 字段加入前写入的 Discord 任务。
                    if source_message_id and not item.get("sourceMessageId"):
                        item["sourceMessageId"] = source_message_id
                        item["updatedAt"] = _now()
                        self._write(records)
                    return item
            record = {
                "id": uuid.uuid4().hex,
                "identity": identity,
                "sourceMessageId": source_message_id,
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
        """记录一次失败。

        默认把任务留在 ``failed`` 状态，避免 worker 在外部依赖持续
        不可用时反复重试。``retry=True`` 仅供调用方明确要求立即重试时
        使用；普通 CLI 重试请使用 :meth:`retry_failed`，这样可以统一
        保留上一次错误信息。
        """
        message = str(error)
        if retry:
            return self.update(
                job_id,
                status="pending",
                error="",
                lastError=message,
            )
        return self.update(job_id, status="failed", error=message)

    def retry_failed(self) -> int:
        """将所有失败任务重新入队，并返回重新入队的数量。

        ``error`` 表示当前运行状态，因此重新入队时清空它；原错误保留
        在 ``lastError``，方便接口和人工排查。任务的 ``attempts`` 不会
        重置，从而能反映实际尝试次数。
        """
        with self._lock:
            records = self._read()
            count = 0
            now = _now()
            for item in records:
                if item.get("status") != "failed":
                    continue
                previous_error = str(item.get("error") or item.get("lastError") or "")
                item.update(
                    status="pending",
                    error="",
                    updatedAt=now,
                )
                if previous_error:
                    item["lastError"] = previous_error
                count += 1
            if count:
                self._write(records)
            return count

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
