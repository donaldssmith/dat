"""Discord F4S_JOB 收件器。

Discord 只是传输层；收到的消息会先经过 F4S_JOB v1 校验，再写入 JobStore。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .protocol import F4SJob, JobValidationError, parse_job
from .store import JobStore


class DiscordError(RuntimeError):
    """Discord API 请求失败。"""


def load_config(project_root: Path) -> dict[str, Any]:
    path = project_root / "config" / "local" / "部署台.bot.json"
    if not path.exists():
        raise DiscordError(f"未找到 Discord 配置：{path}")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DiscordError(f"Discord 配置无法读取：{path}") from exc
    token = str(config.get("bot_token") or "").strip()
    channel_id = str(config.get("channel_id") or "").strip()
    if not token or not channel_id:
        raise DiscordError(f"请填写 Discord bot_token 和 channel_id：{path}")
    return config


def fetch_messages(config: dict[str, Any]) -> list[dict[str, Any]]:
    channel_id = str(config["channel_id"]).strip()
    limit = max(1, min(100, int(config.get("message_limit") or 50)))
    query = urllib.parse.urlencode({"limit": limit})
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bot {config['bot_token']}",
            "User-Agent": "Fool4School-Backend/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise DiscordError(f"Discord API HTTP {exc.code}: {body}") from exc
    except (OSError, ValueError) as exc:
        raise DiscordError(f"Discord API 请求失败：{exc}") from exc
    if not isinstance(payload, list):
        raise DiscordError("Discord API 返回格式不是消息数组")
    return payload


def parse_message(message: dict[str, Any]) -> F4SJob | None:
    content = str(message.get("content") or "")
    if "**F4S JOB**" not in content:
        return None
    match = re.search(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    try:
        return parse_job(json.loads(match.group(1)))
    except (ValueError, json.JSONDecodeError, JobValidationError):
        return None


def sync_messages(store: JobStore, project_root: Path) -> dict[str, int]:
    config = load_config(project_root)
    messages = fetch_messages(config)
    stats = {"fetched": len(messages), "accepted": 0, "duplicates": 0, "ignored": 0}
    for message in messages:
        message_id = str(message.get("id") or "").strip()
        job = parse_message(message)
        if not message_id or job is None:
            stats["ignored"] += 1
            continue
        existing = next((item for item in store.list() if item.get("identity") == message_id), None)
        record = store.submit(job.to_dict(), source_message_id=message_id)
        if existing is None:
            stats["accepted"] += 1
        else:
            stats["duplicates"] += 1
    return stats
