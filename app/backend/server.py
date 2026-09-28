"""F4S_JOB HTTP 接收端。只用标准库，便于在本地或 Hugging Face Space 启动。"""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .store import JobStore


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "F4SBackend/1.0"

    def _json(self, status: int, payload: object) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if path == "" or path == "/health":
            self._json(200, {"ok": True, "service": "f4s-backend"})
            return
        if path == "/api/jobs":
            query = urlparse(self.path).query
            status = self.headers.get("X-Job-Status") or None
            if not status and query.startswith("status="):
                status = query.split("=", 1)[1]
            self._json(200, {"jobs": self.server.store.list(status)})
            return
        if path.startswith("/api/jobs/"):
            job = self.server.store.get(path.rsplit("/", 1)[-1])
            self._json(200 if job else 404, job or {"error": "job not found"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if path != "/api/jobs":
            self._json(404, {"error": "not found"})
            return
        expected_key = getattr(self.server, "api_key", "")
        if expected_key and self.headers.get("X-API-Key", "") != expected_key:
            self._json(401, {"error": "invalid api key"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            record = self.server.store.submit(payload, self.headers.get("X-Source-Message-Id", ""))
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, record)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[f4s-backend] {fmt % args}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fool4School F4S_JOB 后端")
    parser.add_argument("--host", default=os.getenv("F4S_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("F4S_PORT", "8787")))
    parser.add_argument("--state", default=os.getenv("F4S_STATE", ".f4s/jobs.json"))
    parser.add_argument("--api-key", default=os.getenv("F4S_API_KEY", ""))
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    store = JobStore(Path(args.state))
    server = ThreadingHTTPServer((args.host, args.port), ApiHandler)
    server.store = store
    server.api_key = args.api_key
    print(f"F4S 后端已启动：http://{args.host}:{args.port}")
    print(f"任务状态：{store.path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nF4S 后端已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
