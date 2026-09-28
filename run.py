"""Fool4School 唯一项目入口。

常用命令：
  python run.py backend       启动任务后端
  python run.py worker --once 处理一条 F4S_JOB
  python run.py deploy ...    运行教材切分与发布
  python run.py library       打开本地书库合成器
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _run_legacy(relative_path: str) -> None:
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location("f4s_legacy_entry", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载入口：{path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as exc:
        if exc.name in {"fitz", "pypdf"}:
            raise RuntimeError(
                f"缺少 PDF 处理依赖 {exc.name}，请先运行：python -m pip install -r requirements.txt"
            ) from exc
        raise
    module.main()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fool4School 项目入口")
    sub = parser.add_subparsers(dest="command")
    backend = sub.add_parser("backend", help="启动 F4S_JOB HTTP 后端")
    backend.add_argument("--host")
    backend.add_argument("--port", type=int)
    backend.add_argument("--state")
    backend.add_argument("--api-key")
    worker = sub.add_parser("worker", help="处理后端任务队列")
    worker.add_argument("--project-root")
    worker.add_argument("--state")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--discord-inbox", action="store_true")
    worker.add_argument("--watch-discord", action="store_true")
    worker.add_argument("--discord-cleanup", action="store_true")
    worker.add_argument("--retry-failed", action="store_true")
    worker.add_argument("--poll-seconds", type=float)
    sub.add_parser("deploy", help="运行兼容旧版部署台")
    sub.add_parser("library", help="打开本地书库合成器")
    args, passthrough = parser.parse_known_args()

    if args.command == "backend":
        from app.backend.server import main as backend_main
        child_args = list(passthrough)
        for key, flag in (("host", "--host"), ("port", "--port"), ("state", "--state"), ("api_key", "--api-key")):
            value = getattr(args, key, None)
            if value is not None:
                child_args.extend([flag, str(value)])
        backend_main(child_args)
    elif args.command == "worker":
        from app.backend.worker import main as worker_main
        child_args = list(passthrough)
        for key, flag in (("project_root", "--project-root"), ("state", "--state")):
            value = getattr(args, key, None)
            if value is not None:
                child_args.extend([flag, str(value)])
        if getattr(args, "once", False):
            child_args.append("--once")
        if getattr(args, "discord_inbox", False):
            child_args.append("--discord-inbox")
        if getattr(args, "watch_discord", False):
            child_args.append("--watch-discord")
        if getattr(args, "discord_cleanup", False):
            child_args.append("--discord-cleanup")
        if getattr(args, "retry_failed", False):
            child_args.append("--retry-failed")
        if getattr(args, "poll_seconds", None) is not None:
            child_args.extend(["--poll-seconds", str(args.poll_seconds)])
        worker_main(child_args)
    elif args.command == "deploy":
        sys.argv = [str(ROOT / "app" / "tools" / "切分部署台.py"), *passthrough]
        _run_legacy("app/tools/切分部署台.py")
    elif args.command == "library":
        _run_legacy("app/tools/本地书库合成器.py")
    else:
        parser.print_help()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
