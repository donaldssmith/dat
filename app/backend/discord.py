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


def _state_path(project_root: Path) -> Path:
    return project_root / "config" / "local" / "部署台.bot.state.json"


def load_state(project_root: Path) -> dict[str, Any]:
    """读取 Discord 收件箱状态；状态损坏时回退为空状态。"""
    path = _state_path(project_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    handled = data.get("handled_message_ids", []) if isinstance(data, dict) else []
    if not isinstance(handled, list):
        handled = []
    return {"handled_message_ids": sorted({str(item) for item in handled if str(item).strip()})}


def save_state(project_root: Path, state: dict[str, Any]) -> None:
    path = _state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handled = state.get("handled_message_ids", [])
    clean = sorted({str(item) for item in handled if str(item).strip()})
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(json.dumps({"handled_message_ids": clean}, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    except OSError as exc:
        raise DiscordError(f"Discord 状态无法保存：{path}") from exc


def delete_message(config: dict[str, Any], message_id: str) -> bool:
    """删除 Discord 消息；消息已不存在时视为成功。"""
    channel_id = str(config.get("channel_id") or "").strip()
    message_id = str(message_id or "").strip()
    if not channel_id or not message_id:
        return False
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
    request = urllib.request.Request(
        url,
        method="DELETE",
        headers={
            "Authorization": f"Bot {config['bot_token']}",
            "User-Agent": "Fool4School-Backend/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status in {200, 204}
    except urllib.error.HTTPError as exc:
        # A prior cleanup may already have removed the message.
        if exc.code in {404, 410}:
            return True
        raise DiscordError(f"Discord 删除消息失败 HTTP {exc.code}") from exc
    except OSError as exc:
        raise DiscordError(f"Discord 删除消息失败：{exc}") from exc


def acknowledge_message(
    project_root: Path,
    message_id: str,
    *,
    delete_processed_messages: bool | None = None,
) -> dict[str, bool]:
    """记录消息已处理，并按配置选择删除原消息。"""
    config = load_config(project_root)
    state = load_state(project_root)
    message_id = str(message_id or "").strip()
    if not message_id:
        return {"handled": False, "deleted": False}
    handled = set(state["handled_message_ids"])
    handled.add(message_id)
    state["handled_message_ids"] = sorted(handled)
    save_state(project_root, state)
    should_delete = (
        bool(config.get("delete_processed_messages", False))
        if delete_processed_messages is None
        else bool(delete_processed_messages)
    )
    deleted = False
    if should_delete:
        # A failed cleanup must not turn a completed book import into a
        # failed queue job. The handled marker is durable, so the next
        # history cleanup can try deletion again if needed.
        try:
            deleted = delete_message(config, message_id)
        except DiscordError:
            deleted = False
    return {"handled": True, "deleted": deleted}


def cleanup_history(project_root: Path) -> dict[str, int]:
    """整理当前 Discord 历史中的 F4S_JOB 消息。

    只处理协议消息；是否删除由 ``delete_processed_messages`` 配置决定。
    已经标记过的消息不会重复调用删除接口。
    """
    config = load_config(project_root)
    messages = fetch_messages(config)
    state = load_state(project_root)
    handled = set(state["handled_message_ids"])
    stats = {"fetched": len(messages), "recognized": 0, "already_handled": 0, "handled": 0, "deleted": 0}
    for message in messages:
        message_id = str(message.get("id") or "").strip()
        if not message_id or parse_message(message) is None:
            continue
        stats["recognized"] += 1
        if message_id in handled:
            stats["already_handled"] += 1
            # If deletion was enabled and a previous attempt failed, retry
            # the delete during explicit history cleanup. The handled marker
            # itself remains idempotent and is never removed.
            if config.get("delete_processed_messages", False):
                try:
                    stats["deleted"] += int(delete_message(config, message_id))
                except DiscordError:
                    pass
            continue
        result = acknowledge_message(project_root, message_id)
        handled.add(message_id)
        stats["handled"] += int(result["handled"])
        stats["deleted"] += int(result["deleted"])
    return stats


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
    handled = set(load_state(project_root)["handled_message_ids"])
    stats = {"fetched": len(messages), "accepted": 0, "duplicates": 0, "handled": 0, "ignored": 0}
    for message in messages:
        message_id = str(message.get("id") or "").strip()
        job = parse_message(message)
        if not message_id or job is None:
            stats["ignored"] += 1
            continue
        if message_id in handled:
            stats["handled"] += 1
            continue
        existing = next((item for item in store.list() if item.get("identity") == message_id), None)
        record = store.submit(job.to_dict(), source_message_id=message_id)
        if existing is None:
            stats["accepted"] += 1
        else:
            stats["duplicates"] += 1
            if existing.get("status") == "succeeded":
                # A worker may have completed before the process was
                # interrupted while acknowledging Discord.
                acknowledge_message(project_root, message_id)
    return stats
