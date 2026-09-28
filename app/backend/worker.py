"""处理后端队列中的任务。

worker 复用原部署台的 PDF 切分和发布实现，队列和状态由 backend.store 统一管理。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import tempfile
from pathlib import Path
from typing import Any

from .store import JobStore


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
        store.complete(record["id"], result)
        print(f"已完成任务 {record['id']}：{result}")
    except Exception as exc:  # worker 必须把错误写回队列
        store.fail(record["id"], str(exc))
        print(f"任务失败 {record['id']}：{exc}")
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fool4School F4S_JOB worker")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--state", default=".f4s/jobs.json")
    parser.add_argument("--once", action="store_true", help="只处理一条任务")
    args = parser.parse_args(argv)
    store = JobStore(args.state)
    root = Path(args.project_root).resolve()
    if args.once:
        run_once(store, root)
        return
    while run_once(store, root):
        pass


if __name__ == "__main__":
    main()
