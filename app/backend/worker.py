"""处理后端队列中的任务。

worker 复用原部署台的 PDF 切分和发布实现，队列和状态由 backend.store 统一管理。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .store import JobStore
from .discord import DiscordError, acknowledge_message, cleanup_history, sync_messages


def _load_legacy(project_root: Path):
    path = project_root / "app" / "tools" / "切分部署台.py"
    spec = importlib.util.spec_from_file_location("f4s_legacy_deploy", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"找不到旧部署台：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def process_record(record: dict[str, Any], project_root: Path) -> dict[str, Any]:
    payload = record["job"]
    normalized = {
        "message_id": record["id"],
        "kind": payload["type"],
        "book_id": payload["bookId"],
        "title": payload["title"],
        "pdf_mode": (payload.get("pdf") or {}).get("mode", "none"),
        "pdf_url": (payload.get("pdf") or {}).get("url", ""),
        "pdf_label": (payload.get("pdf") or {}).get("label", ""),
        "pdf_parts": (payload.get("pdf") or {}).get("parts") or [],
        "meta_url": (payload.get("meta") or {}).get("url", ""),
        "source_page_count": payload.get("sourcePageCount", 0),
    }
    if normalized["kind"] in {"nudge", "inventory_signal"}:
        return {"action": "recorded", "kind": normalized["kind"]}

    legacy = _load_legacy(project_root)
    dist_dir = project_root / "dist"
    assets_dir = project_root / "data" / "ocr"
    dist_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="f4s-worker-") as temp_dir:
        if normalized["kind"] == "full_upload":
            pdf_path = legacy.download_pdf_from_job(normalized, temp_dir)
            ocr_path = legacy.handoff_to_abbyy_hotfolder(pdf_path, str(project_root), str(assets_dir), normalized)
            if not legacy.process_book(ocr_path, str(dist_dir), normalized["book_id"], force=True):
                raise RuntimeError("PDF 切分失败")
            if not legacy.download_meta_to_dist(normalized, str(dist_dir)):
                raise RuntimeError("元数据下载失败")
        elif normalized["kind"] == "meta_upload":
            if not legacy.download_meta_to_dist(normalized, str(dist_dir)):
                raise RuntimeError("元数据下载失败")
    return {"action": "processed", "bookId": normalized["book_id"], "kind": normalized["kind"]}


def run_once(store: JobStore, project_root: Path) -> bool:
    record = store.claim_next()
    if not record:
        return False
    try:
        result = process_record(record, project_root)
        # 先把本地任务置为 succeeded，避免进程在 Discord 回写前退出时
        # 留下 processing 僵尸任务。sync_messages 会对已成功任务补做 ack。
        completed = store.complete(record["id"], result)
        source_message_id = str(record.get("sourceMessageId") or "").strip()
        if source_message_id:
            try:
                discord_result = acknowledge_message(project_root, source_message_id)
                completed = store.update(
                    record["id"],
                    result={**completed.get("result", {}), "discord": discord_result},
                )
            except DiscordError as exc:
                # 任务已经完成；Discord 回写失败应在下次同步时自动补偿，
                # 不把已完成的本地任务错误地标成 failed。
                print(f"任务已完成，但 Discord 状态回写失败 {source_message_id}：{exc}")
        print(f"已完成任务 {record['id']}：{completed.get('result', result)}")
    except Exception as exc:  # worker 必须把错误写回队列
        store.fail(record["id"], str(exc))
        print(f"任务失败 {record['id']}：{exc}")
    return True


def import_discord(store: JobStore, project_root: Path) -> bool:
    try:
        stats = sync_messages(store, project_root)
        print(
            "Discord 收件箱："
            f"读取 {stats['fetched']} 条，新增 {stats['accepted']} 条，"
            f"重复 {stats['duplicates']} 条，已处理 {stats.get('handled', 0)} 条，忽略 {stats['ignored']} 条"
        )
        return True
    except DiscordError as exc:
        print(f"Discord 收件箱读取失败：{exc}")
        return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fool4School F4S_JOB worker")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--state", default=".f4s/jobs.json")
    parser.add_argument("--once", action="store_true", help="只处理一条任务")
    parser.add_argument("--discord-inbox", action="store_true", help="先从 Discord 收件箱导入 F4S_JOB")
    parser.add_argument("--watch-discord", action="store_true", help="持续轮询 Discord 并处理新请求")
    parser.add_argument("--discord-cleanup", action="store_true", help="整理 Discord 历史 F4S_JOB 消息")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="将已有 failed 任务重新入队后再处理（每次启动只执行一次）",
    )
    parser.add_argument("--poll-seconds", type=float, default=30.0, help="Discord 轮询间隔，默认 30 秒")
    args = parser.parse_args(argv)
    store = JobStore(args.state)
    root = Path(args.project_root).resolve()
    if args.discord_cleanup:
        try:
            stats = cleanup_history(root)
            print(
                "Discord 历史整理："
                f"读取 {stats['fetched']} 条，识别 {stats['recognized']} 条，"
                f"新标记 {stats['handled']} 条，已处理 {stats['already_handled']} 条，"
                f"删除 {stats['deleted']} 条"
            )
        except DiscordError as exc:
            print(f"Discord 历史整理失败：{exc}")
        return
    if args.retry_failed:
        count = store.retry_failed()
        print(f"重新入队失败任务：{count} 条")
    if args.watch_discord:
        while True:
            if not import_discord(store, root) and args.once:
                return
            while run_once(store, root):
                pass
            if args.once:
                return
            time.sleep(max(5.0, args.poll_seconds))

    if args.discord_inbox and not import_discord(store, root):
        return
    if args.once:
        run_once(store, root)
        return
    while run_once(store, root):
        pass


if __name__ == "__main__":
    main()
