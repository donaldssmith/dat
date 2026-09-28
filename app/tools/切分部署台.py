import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import glob
import urllib.error
import urllib.parse
import urllib.request

import fitz  # PyMuPDF
from pypdf import PdfReader, PdfWriter


logging.basicConfig(level=logging.INFO, format="%(message)s")

DEFAULT_COMMIT_MESSAGE = "update"
BLOCKED_DEPLOY_PREFIXES = ("data/", "config/local/", ".user/")
PREVIEW_LIMIT = 12
RECOMMENDED_MODE = "1"
BOT_CONFIG_FILENAME = "部署台.bot.json"
BOT_STATE_FILENAME = "部署台.bot.state.json"
DEPLOY_CONFIG_FILENAME = "部署台.deploy.json"
PROGRESS_BAR_WIDTH = 24
LAST_PROGRESS_RENDER_LEN = 0
MAX_SINGLE_PAGE_SIZE_BYTES = 16 * 1024 * 1024
DOWNLOAD_RETRY_LIMIT = 4
DOWNLOAD_RETRY_BASE_DELAY = 1.2

DEFAULT_DEPLOY_CONFIG = {
    "primary_target": "huggingface",
    "targets": {
        "huggingface": {
            "label": "Hugging Face（主发布）",
            "enabled": True,
            "blocked": False,
            "block_reason": "",
            "remote_url": "https://huggingface.co/datasets/donaldssmith/dat",
            "branch": "main",
            "token": "",
            "token_env": "HF_TOKEN",
        },
        "github": {
            "label": "GitHub（后备）",
            "enabled": True,
            "blocked": True,
            "block_reason": "当前默认只把 GitHub 当后备，不做日常部署。",
            "remote_name": "origin",
            "remote_url": "https://donaldssmith@github.com/donaldssmith/dat.git",
            "branch": "main",
            "token": "",
            "token_env": "",
        },
    },
}


def load_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_bot_config_path(base_dir):
    return os.path.join(base_dir, BOT_CONFIG_FILENAME)


def get_bot_state_path(base_dir):
    return os.path.join(base_dir, BOT_STATE_FILENAME)


def get_deploy_config_path(base_dir):
    return os.path.join(base_dir, DEPLOY_CONFIG_FILENAME)


def deep_merge_dict(base, override):
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def load_bot_config(base_dir):
    config_path = get_bot_config_path(base_dir)
    default = {
        "bot_token": "",
        "channel_id": "",
        "message_limit": 50,
        "show_handled_messages": False,
        "hide_already_in_cloud": True,
        "hide_invalid_links": True,
        "dedupe_by_book": True,
        "delete_processed_messages": False,
    }
    data = load_json_file(config_path, default)
    return {**default, **data}


def load_deploy_config(base_dir):
    config_path = get_deploy_config_path(base_dir)
    data = load_json_file(config_path, {})
    return deep_merge_dict(DEFAULT_DEPLOY_CONFIG, data)


def ensure_deploy_config(base_dir):
    config_path = get_deploy_config_path(base_dir)
    if os.path.exists(config_path):
        return config_path
    save_json_file(config_path, DEFAULT_DEPLOY_CONFIG)
    return config_path


def ensure_bot_config(base_dir):
    config_path = get_bot_config_path(base_dir)
    if os.path.exists(config_path):
        return config_path
    save_json_file(config_path, {
        "bot_token": "在这里填 Discord Bot Token",
        "channel_id": "在这里填接收 webhook 的频道 ID",
        "message_limit": 50,
        "show_handled_messages": False,
        "hide_already_in_cloud": True,
        "hide_invalid_links": True,
        "dedupe_by_book": True,
        "delete_processed_messages": False
    })
    return config_path


def load_bot_state(base_dir):
    state_path = get_bot_state_path(base_dir)
    default = {"handled_message_ids": []}
    data = load_json_file(state_path, default)
    handled = data.get("handled_message_ids", [])
    return {"handled_message_ids": [str(x) for x in handled]}


def save_bot_state(base_dir, state):
    save_json_file(get_bot_state_path(base_dir), state)


def get_project_root(base_dir):
    return os.path.dirname(os.path.dirname(base_dir))


def get_dist_dir(base_dir):
    return os.path.join(get_project_root(base_dir), "dist")


def format_bytes(num):
    value = float(num)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024


def mask_url_secret(url):
    target = str(url or "").strip()
    if not target:
        return "(未设置)"
    return re.sub(r"(?<=://[^/:]+:)[^@/]+(?=@)", "***", target)


def get_target_branch(target):
    return str(target.get("branch") or "main").strip() or "main"


def get_target_display_name(target_key, target):
    label = str(target.get("label") or "").strip()
    if label:
        return label
    return str(target_key or "deploy").strip() or "deploy"


def build_target_push_url(target_key, target):
    remote_url = str(target.get("remote_url") or "").strip()
    if not remote_url:
        return ""

    token = str(target.get("token") or "").strip()
    token_env = str(target.get("token_env") or "").strip()
    if token_env:
        token = token or str(os.environ.get(token_env) or "").strip()

    if not token or "huggingface.co/" not in remote_url or not remote_url.startswith("https://"):
        return remote_url

    parsed = urllib.parse.urlsplit(remote_url)
    username = parsed.username or "hf_user"
    netloc = f"{urllib.parse.quote(username, safe='')}:{urllib.parse.quote(token, safe='')}@{parsed.hostname or ''}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def resolve_deploy_targets(project_root, deploy_config):
    primary_key = str(deploy_config.get("primary_target") or "").strip() or "huggingface"
    targets = deploy_config.get("targets") or {}
    resolved = []

    for key, target in targets.items():
        if not isinstance(target, dict):
            continue

        enabled = bool(target.get("enabled", True))
        blocked = bool(target.get("blocked", False))
        remote_name = str(target.get("remote_name") or "").strip()
        remote_url = str(target.get("remote_url") or "").strip()

        if remote_name and not remote_url:
            remote_url = get_git_value(["remote", "get-url", remote_name], project_root, "")

        push_url = build_target_push_url(key, {**target, "remote_url": remote_url})
        resolved.append({
            "key": key,
            "label": get_target_display_name(key, target),
            "enabled": enabled,
            "blocked": blocked,
            "block_reason": str(target.get("block_reason") or "").strip(),
            "remote_name": remote_name,
            "remote_url": remote_url,
            "push_url": push_url,
            "branch": get_target_branch(target),
            "is_primary": key == primary_key,
        })

    resolved.sort(key=lambda item: (not item["is_primary"], item["key"]))
    return resolved


def pick_active_deploy_target(project_root, deploy_config):
    targets = resolve_deploy_targets(project_root, deploy_config)
    for target in targets:
        if target["is_primary"] and target["enabled"]:
            return target, targets
    for target in targets:
        if target["enabled"]:
            return target, targets
    return None, targets


def print_progress(prefix, current, total, detail=""):
    global LAST_PROGRESS_RENDER_LEN
    total = max(int(total or 1), 1)
    current = max(0, min(int(current), total))
    filled = int(PROGRESS_BAR_WIDTH * current / total)
    bar = "#" * filled + "-" * (PROGRESS_BAR_WIDTH - filled)
    suffix = f" {detail}" if detail else ""
    text = f"\r{prefix} [{bar}] {current}/{total}{suffix}"
    visible_len = len(text) - 1
    padding = " " * max(0, LAST_PROGRESS_RENDER_LEN - visible_len)
    sys.stdout.write(text + padding)
    sys.stdout.flush()
    LAST_PROGRESS_RENDER_LEN = visible_len
    if current >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()
        LAST_PROGRESS_RENDER_LEN = 0


def run_with_spinner(message, fn, *args, **kwargs):
    stop_event = threading.Event()

    def spinner():
        frames = "|/-\\"
        index = 0
        while not stop_event.is_set():
            sys.stdout.write(f"\r{message} {frames[index % len(frames)]}")
            sys.stdout.flush()
            index += 1
            time.sleep(0.12)
        clear_width = len(message) + 4
        sys.stdout.write("\r" + (" " * clear_width) + "\r")
        sys.stdout.flush()

    thread = threading.Thread(target=spinner, daemon=True)
    thread.start()
    try:
        return fn(*args, **kwargs)
    finally:
        stop_event.set()
        thread.join(timeout=1)


def discord_api_request(bot_token, url):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bot {bot_token}",
            "User-Agent": "Fool4School-DeployBot/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def discord_api_delete(bot_token, url):
    req = urllib.request.Request(
        url,
        method="DELETE",
        headers={
            "Authorization": f"Bot {bot_token}",
            "User-Agent": "Fool4School-DeployBot/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def fetch_discord_messages(bot_token, channel_id, limit=30):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}"
    return run_with_spinner("正在读取收件箱", discord_api_request, bot_token, url)


def looks_like_temp_sh_html(url, content_type, data):
    if "temp.sh/" not in str(url):
        return False
    if "text/html" not in str(content_type or "").lower():
        return False
    preview = data[:4096].decode("utf-8", errors="ignore")
    return "Temp.sh |" in preview and 'method="POST"' in preview


def should_use_temp_sh_post(url):
    return "temp.sh/" in str(url)


def http_download(url, label="", method="GET", data=None):
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"User-Agent": "Fool4School-DeployBot/1.0"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        content_type = resp.headers.get("Content-Type") or ""
        chunks = []
        loaded = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            chunks.append(chunk)
            loaded += len(chunk)
            if label:
                if total > 0:
                    print_progress(label, loaded, total, format_bytes(loaded))
                else:
                    sys.stdout.write(f"\r{label} 已下载 {format_bytes(loaded)}")
                    sys.stdout.flush()
        if label and total <= 0:
            sys.stdout.write("\n")
            sys.stdout.flush()
        return b"".join(chunks), content_type


def download_bytes(url, label=""):
    last_error = None
    for attempt in range(1, DOWNLOAD_RETRY_LIMIT + 1):
        try:
            if should_use_temp_sh_post(url):
                payload, content_type = http_download(url, label=label, method="POST", data=b"")
            else:
                payload, content_type = http_download(url, label=label, method="GET")
                if looks_like_temp_sh_html(url, content_type, payload):
                    payload, content_type = http_download(url, label=label, method="POST", data=b"")
            return payload
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code not in {429, 500, 502, 503, 504} or attempt >= DOWNLOAD_RETRY_LIMIT:
                raise
        except Exception as e:
            last_error = e
            if attempt >= DOWNLOAD_RETRY_LIMIT:
                raise

        delay = DOWNLOAD_RETRY_BASE_DELAY * attempt
        if label:
            print(f"{label} 失败，正在重试 ({attempt}/{DOWNLOAD_RETRY_LIMIT})...")
        time.sleep(delay)

    if last_error:
        raise last_error
    raise RuntimeError(f"下载失败：{url}")


def download_json(url, label=""):
    raw = download_bytes(url, label=label)
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(text)
    except Exception as e:
        preview = text[:200].replace("\n", " ")
        raise RuntimeError(f"JSON 解析失败: {preview}") from e


def parse_discord_job(message):
    content = message.get("content") or ""
    if "**F4S JOB**" not in content:
        return None

    match = re.search(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL)
    if not match:
        return None

    try:
        data = json.loads(match.group(1))
    except Exception:
        return None

    if data.get("protocol") != "F4S_JOB" or data.get("version") != 1:
        return None

    job_type = str(data.get("type") or "").strip()
    if job_type not in {"full_upload", "meta_upload", "nudge", "inventory_signal"}:
        return None

    book_id = str(data.get("bookId") or "").strip()
    title = str(data.get("title") or "").strip()
    if not book_id:
        return None

    pdf = data.get("pdf") or {}
    meta = data.get("meta") or {}
    outline = data.get("outline") or {}

    return {
        "message_id": str(message.get("id", "")),
        "kind": job_type,
        "book_id": book_id,
        "title": title or f"教材 {book_id}",
        "pdf_mode": str(pdf.get("mode") or "none"),
        "pdf_url": str(pdf.get("url") or ""),
        "pdf_label": str(pdf.get("label") or ""),
        "pdf_parts": pdf.get("parts") or [],
        "meta_url": str(meta.get("url") or ""),
        "outline_status": str(outline.get("status") or "unknown"),
        "outline_count": int(outline.get("count") or 0),
        "source_page_count": int(data.get("sourcePageCount") or 0),
        "wanted": bool(data.get("wanted")),
        "reporter": data.get("reporter") or {},
        "raw_content": content,
    }


def load_wanted_book_ids(dist_dir):
    wanted_path = os.path.join(dist_dir, "-1.dat")
    data = load_json_file(wanted_path, {})
    wanted = data.get("wantedBooks", [])
    return {str(item).strip() for item in wanted if str(item).strip()}


def inspect_dist_book_state(dist_dir, book_id):
    book_dist = os.path.join(dist_dir, str(book_id))
    meta_exists = os.path.exists(os.path.join(book_dist, "0.dat"))
    page_count = 0
    if os.path.isdir(book_dist):
        for name in os.listdir(book_dist):
            if re.fullmatch(r"[1-9]\d*\.dat", name):
                page_count += 1
    return {
        "exists": os.path.isdir(book_dist),
        "meta_exists": meta_exists,
        "page_count": page_count,
    }


def is_job_satisfied_by_repo(job, dist_dir, wanted_book_ids):
    book_id = str(job["book_id"])
    repo_state = inspect_dist_book_state(dist_dir, book_id)
    is_wanted = book_id in wanted_book_ids

    if job["kind"] == "meta_upload":
        return repo_state["meta_exists"]

    if job["kind"] == "full_upload":
        expected_pages = int(job.get("source_page_count") or 0)
        if expected_pages > 0:
            return repo_state["meta_exists"] and repo_state["page_count"] >= expected_pages
        return repo_state["meta_exists"] and repo_state["page_count"] > 0

    if job["kind"] in {"nudge", "inventory_signal"}:
        return repo_state["exists"] or is_wanted

    return False


def probe_url_status(url):
    target = str(url or "").strip()
    if not target:
        return "missing"

    req = urllib.request.Request(
        target,
        headers={
            "User-Agent": "Fool4School-DeployBot/1.0",
            "Range": "bytes=0-255",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if 200 <= resp.status < 400:
                return "alive"
            return "unknown"
    except urllib.error.HTTPError as e:
        if e.code in {404, 410}:
            return "dead"
        return "unknown"
    except Exception:
        return "unknown"


def is_job_hard_dead(job):
    kind = str(job.get("kind") or "")
    if kind == "meta_upload":
        return probe_url_status(job.get("meta_url")) == "dead"

    if kind == "full_upload":
        pdf_mode = str(job.get("pdf_mode") or "")
        if pdf_mode == "manifest":
            return probe_url_status(job.get("pdf_url")) == "dead"
        if pdf_mode == "direct":
            return probe_url_status(job.get("pdf_url")) == "dead"

    return False


def normalize_discord_jobs(jobs, state, config, dist_dir, include_handled=False):
    handled = set(state.get("handled_message_ids", []))
    normalized = []
    seen_keys = set()
    wanted_book_ids = load_wanted_book_ids(dist_dir)

    for job in jobs:
        if not include_handled and not config.get("show_handled_messages", False) and job["message_id"] in handled:
            continue

        if config.get("hide_already_in_cloud", True) and is_job_satisfied_by_repo(job, dist_dir, wanted_book_ids):
            continue

        if config.get("hide_invalid_links", True) and is_job_hard_dead(job):
            continue

        dedupe_key = (job["kind"], job["book_id"])
        if config.get("dedupe_by_book", True):
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

        normalized.append(job)

    return normalized


def collect_discord_jobs(base_dir, include_handled=False, include_hidden=False):
    config = load_bot_config(base_dir)
    if not config.get("bot_token") or not config.get("channel_id"):
        config_path = ensure_bot_config(base_dir)
        raise RuntimeError(f"请先填写 Bot 配置文件：{config_path}")

    messages = fetch_discord_messages(
        config["bot_token"],
        config["channel_id"],
        int(config.get("message_limit") or 30),
    )
    jobs = []
    for message in messages:
        job = parse_discord_job(message)
        if job:
            jobs.append(job)
    if include_hidden:
        return jobs
    state = load_bot_state(base_dir)
    dist_dir = get_dist_dir(base_dir)
    return normalize_discord_jobs(jobs, state, config, dist_dir, include_handled=include_handled)


def delete_discord_message(base_dir, message_id):
    config = load_bot_config(base_dir)
    if not config.get("bot_token") or not config.get("channel_id"):
        return False
    url = f"https://discord.com/api/v10/channels/{config['channel_id']}/messages/{message_id}"
    try:
        discord_api_delete(config["bot_token"], url)
        return True
    except Exception:
        return False


def mark_jobs_handled(base_dir, state, jobs, delete_after=False):
    handled = set(state.get("handled_message_ids", []))
    total = len(jobs)
    for index, job in enumerate(jobs, start=1):
        handled.add(job["message_id"])
        if delete_after:
            delete_discord_message(base_dir, job["message_id"])
        action_text = "标记并删除" if delete_after else "标记已处理"
        print_progress(f"正在整理历史消息（{action_text}）", index, total, f"{job['book_id']} / {job['title']}")
    state["handled_message_ids"] = sorted(handled)
    save_bot_state(base_dir, state)


def job_kind_label(kind):
    return {
        "full_upload": "整本补库",
        "meta_upload": "补书库信息",
        "nudge": "催维护",
        "inventory_signal": "缺书线索",
    }.get(kind, kind)


def job_kind_priority(kind):
    return {
        "meta_upload": 0,
        "full_upload": 1,
        "nudge": 2,
        "inventory_signal": 3,
    }.get(kind, 9)


def sort_discord_jobs_for_inbox(jobs):
    indexed = list(enumerate(jobs))
    indexed.sort(key=lambda pair: (job_kind_priority(pair[1]["kind"]), pair[0]))
    return [job for _, job in indexed]


def split_inbox_views(jobs):
    ordered_jobs = sort_discord_jobs_for_inbox(jobs)
    actionable_jobs = [job for job in ordered_jobs if job["kind"] in {"meta_upload", "full_upload"}]
    decision_jobs = [job for job in ordered_jobs if job["kind"] == "nudge"]
    signal_jobs = [job for job in ordered_jobs if job["kind"] == "inventory_signal"]
    return {
        "1": ("立即处理", "能直接改仓库的任务", actionable_jobs),
        "2": ("需要你判断", "只是提醒，不会自动部署", decision_jobs),
        "3": ("缺书线索", "只说明云端没有，不代表已经拿到资产", signal_jobs),
        "4": ("全部列表", "把所有类型混在一起看", ordered_jobs),
    }


def prompt_inbox_view_selection(views):
    print("Discord 收件箱")
    print("先选你现在要做的事：")
    for key in ["1", "2", "3", "4"]:
        label, desc, items = views[key]
        print(f"{key}. {label}（{len(items)} 条）")
        print(f"   {desc}")
    raw = input("请选择入口 [默认 1]: ").strip() or "1"
    if raw not in views:
        print("输入无效，操作取消。")
        return None
    return raw


def print_discord_jobs(jobs, state):
    handled = set(state.get("handled_message_ids", []))
    if not jobs:
        print("Discord 收件箱里暂时没有可识别任务。")
        return
    ordered_jobs = sort_discord_jobs_for_inbox(jobs)
    print(f"共 {len(ordered_jobs)} 条。可输入序号，也可直接输入书号。")
    for index, job in enumerate(ordered_jobs, start=1):
        badge = "已处理" if job["message_id"] in handled else "未处理"
        kind = job_kind_label(job["kind"])
        reporter = job.get("reporter") or {}
        reporter_label = reporter.get("name") or reporter.get("installId") or ""
        reporter_suffix = f" / {reporter_label}" if reporter_label else ""
        print(f"{index}. [{badge}/{kind}] [{job['book_id']}] {job['title']}{reporter_suffix}")


def prompt_discord_job_selection(jobs):
    if not jobs:
        return None
    ordered_jobs = sort_discord_jobs_for_inbox(jobs)
    raw = input("请选择任务序号或书号 [默认 1]: ").strip() or "1"
    if not raw.isdigit():
        print("输入无效，操作取消。")
        return None
    direct_book_matches = [job for job in ordered_jobs if job["book_id"] == raw]
    if direct_book_matches:
        direct_book_matches.sort(key=lambda job: job_kind_priority(job["kind"]))
        return direct_book_matches[0]
    choice = int(raw)
    if choice < 1 or choice > len(ordered_jobs):
        print("超出范围，操作取消。")
        return None
    return ordered_jobs[choice - 1]


def print_discord_cleanup_summary(jobs):
    full_uploads = [job for job in jobs if job["kind"] == "full_upload"]
    meta_uploads = [job for job in jobs if job["kind"] == "meta_upload"]
    nudges = [job for job in jobs if job["kind"] == "nudge"]
    signals = [job for job in jobs if job["kind"] == "inventory_signal"]
    print("当前可识别历史：")
    print(f"  - 总任务数：{len(jobs)}")
    print(f"  - 整本补库：{len(full_uploads)}")
    print(f"  - 补书库信息：{len(meta_uploads)}")
    print(f"  - 催更消息：{len(nudges)}")
    print(f"  - 缺书线索：{len(signals)}")
    preview = jobs[:PREVIEW_LIMIT]
    for job in preview:
        kind = {
            "full_upload": "整本补库",
            "meta_upload": "补书库信息",
            "nudge": "催维护",
            "inventory_signal": "缺书线索",
        }.get(job["kind"], job["kind"])
        reporter = job.get("reporter") or {}
        reporter_label = reporter.get("name") or reporter.get("installId") or ""
        reporter_suffix = f" / {reporter_label}" if reporter_label else ""
        print(f"    - [{kind}] [{job['book_id']}] {job['title']}{reporter_suffix}")
    if len(jobs) > PREVIEW_LIMIT:
        print(f"    - ... 另外还有 {len(jobs) - PREVIEW_LIMIT} 条")


def download_pdf_from_job(job, temp_dir):
    if job["kind"] != "full_upload":
        raise RuntimeError("只有整本补库任务才包含可部署 PDF。")

    target_pdf = os.path.join(temp_dir, f"{job['book_id']}.pdf")

    def merge_part_files(parts):
        part_files = []
        total_parts = len(parts)
        for index, part in enumerate(parts, start=1):
            part_path = os.path.join(temp_dir, part.get("name") or f"part-{part.get('index', 0)}.bin")
            with open(part_path, "wb") as f:
                f.write(download_bytes(part["url"], label=f"[{job['book_id']}] 下载 PDF 分片 {index}/{total_parts}"))
            part_files.append(part_path)
            print_progress(f"[{job['book_id']}] 分片准备", index, total_parts, part.get("name") or "")

        with open(target_pdf, "wb") as out:
            total_files = len(part_files)
            for index, path in enumerate(part_files, start=1):
                with open(path, "rb") as src:
                    shutil.copyfileobj(src, out)
                print_progress(f"[{job['book_id']}] 合并 PDF", index, total_files, os.path.basename(path))
        return target_pdf

    def find_cached_pdf():
        pattern = os.path.join(tempfile.gettempdir(), "f4s-bot-*", f"{job['book_id']}.pdf")
        candidates = [path for path in glob.glob(pattern) if os.path.isfile(path)]
        if not candidates:
            return ""
        candidates.sort(key=lambda path: os.path.getmtime(path), reverse=True)
        return candidates[0]

    if job["pdf_mode"] == "manifest":
        inline_parts = job.get("pdf_parts") or []
        if inline_parts:
            return merge_part_files(inline_parts)

        if job["pdf_url"]:
            try:
                manifest = download_json(job["pdf_url"], label=f"[{job['book_id']}] 下载任务清单")
                parts = manifest.get("parts", [])
                if parts:
                    return merge_part_files(parts)
            except Exception as e:
                cached_pdf = find_cached_pdf()
                if cached_pdf:
                    logging.warning(f"[{job['book_id']}] 任务清单不可用，改用本地缓存 PDF：{cached_pdf}")
                    shutil.copy2(cached_pdf, target_pdf)
                    return target_pdf
                raise RuntimeError(f"任务清单不可用，且未找到本地缓存 PDF：{e}") from e

    if job["pdf_mode"] == "direct" and job["pdf_url"].startswith("http"):
        with open(target_pdf, "wb") as f:
            f.write(download_bytes(job["pdf_url"], label=f"[{job['book_id']}] 下载整本 PDF"))
        return target_pdf

    raise RuntimeError("这条补库消息里没有可识别的 PDF 下载链接。")


def download_meta_to_dist(job, dist_dir):
    if not job.get("meta_url"):
        return False
    book_dist = os.path.join(dist_dir, job["book_id"])
    os.makedirs(book_dist, exist_ok=True)
    meta_path = os.path.join(book_dist, "0.dat")
    with open(meta_path, "wb") as f:
        f.write(download_bytes(job["meta_url"], label=f"[{job['book_id']}] 下载 0.dat"))
    return True


def sanitize_filename_for_path(name):
    text = re.sub(r'[\\/:*?"<>|]+', "-", str(name or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text or "Untitled"


def build_abbyy_pdf_name(book_id, title):
    return f"{book_id}-{sanitize_filename_for_path(title)}.pdf"


def build_abbyy_staged_name(book_id, title):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{book_id}-{sanitize_filename_for_path(title)}__hf-{stamp}.pdf"


def find_matching_abbyy_output(assets_dir, book_id, src_mtime, preferred_name=""):
    candidates = []
    preferred_path = os.path.join(assets_dir, preferred_name) if preferred_name else ""
    if preferred_path and os.path.exists(preferred_path):
        candidates.append(preferred_path)

    prefix = f"{book_id}-"
    for name in os.listdir(assets_dir):
        if not name.lower().endswith(".pdf"):
            continue
        if not name.startswith(prefix):
            continue
        full_path = os.path.join(assets_dir, name)
        if full_path not in candidates:
            candidates.append(full_path)

    valid = []
    for path in candidates:
        try:
            if os.path.getmtime(path) >= src_mtime - 1:
                valid.append(path)
        except OSError:
            continue

    if not valid:
        return ""
    valid.sort(key=lambda item: os.path.getmtime(item), reverse=True)
    return valid[0]


def handoff_to_abbyy_hotfolder(downloaded_pdf_path, project_root, assets_dir, job):
    src_dir = os.path.join(project_root, "data", "source")
    os.makedirs(src_dir, exist_ok=True)

    staged_name = build_abbyy_staged_name(job["book_id"], job["title"])
    canonical_name = build_abbyy_pdf_name(job["book_id"], job["title"])
    src_path = os.path.join(src_dir, staged_name)
    expected_asset_path = os.path.join(assets_dir, canonical_name)
    shutil.copy2(downloaded_pdf_path, src_path)
    src_mtime = os.path.getmtime(src_path)

    print(f"[{job['book_id']}] 已把整本 PDF 放进 ABBYY Hot Folder 输入区。")
    print("下一步请手动去 ABBYY Hot Folder 点 Run Now。")
    print(f"输入目录：{src_path}")
    print(f"期待输出：{expected_asset_path}")
    print("提示：src 只是一次性投递区，OCR 成功后旧文件可以删。")

    while True:
        ready_path = find_matching_abbyy_output(assets_dir, job["book_id"], src_mtime, staged_name)
        if ready_path:
            print(f"[{job['book_id']}] 已检测到 OCR 输出：{ready_path}")
            return ready_path

        choice = input("ABBYY 跑完后按回车继续检查；输入 s 跳过 OCR 直接切分；输入 q 取消: ").strip().lower()
        if choice in {"q", "quit"}:
            raise RuntimeError("已取消。请等 ABBYY 跑完后再回来继续。")
        if choice in {"s", "skip"}:
            logging.warning(f"[{job['book_id']}] 本次跳过 ABBYY，直接使用临时 PDF 切分。")
            return downloaded_pdf_path
        print(f"[{job['book_id']}] 还没在 assets 看到新的 OCR 输出，请先去 ABBYY 点 Run Now。")


def write_single_page_pdf(src_doc, page_num, target_path):
    new_doc = fitz.open()
    new_doc.insert_pdf(src_doc, from_page=page_num, to_page=page_num, final=0)
    new_doc.save(target_path, garbage=4, deflate=True)
    new_doc.close()


def write_single_page_pdf_raster(src_doc, page_num, target_path):
    page = src_doc[page_num]
    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
    image_bytes = pix.tobytes("jpg", jpg_quality=82)
    page_rect = page.rect
    new_doc = fitz.open()
    new_page = new_doc.new_page(width=page_rect.width, height=page_rect.height)
    new_page.insert_image(new_page.rect, stream=image_bytes)
    new_doc.save(target_path, garbage=4, deflate=True)
    new_doc.close()


def process_book(pdf_path, dist_dir, book_id, force=False):
    book_dist = os.path.join(dist_dir, book_id)

    if force and os.path.exists(book_dist):
        logging.info(f"[{book_id}] 强制模式：正在清理旧切片目录。")
        shutil.rmtree(book_dist)

    os.makedirs(book_dist, exist_ok=True)

    try:
        src_doc = fitz.open(pdf_path)
    except Exception as e:
        logging.error(f"[{book_id}] 打开 PDF 失败：{e}")
        return False

    total_pages = len(src_doc)
    logging.info(f"[{book_id}] 开始处理，共 {total_pages} 页。")

    processed = 0
    success = True
    source_size = os.path.getsize(pdf_path)
    sane_limit = max(MAX_SINGLE_PAGE_SIZE_BYTES, min(source_size // 4, 64 * 1024 * 1024))
    force_raster_mode = False
    warned_shared_resource_pdf = False
    for page_num in range(total_pages):
        page_index = page_num + 1
        dat_path = os.path.join(book_dist, f"{page_index}.dat")

        if not force and os.path.exists(dat_path):
            continue

        try:
            if force_raster_mode:
                write_single_page_pdf_raster(src_doc, page_num, dat_path)
                page_size = os.path.getsize(dat_path)
            else:
                write_single_page_pdf(src_doc, page_num, dat_path)
                page_size = os.path.getsize(dat_path)
                if page_size > sane_limit:
                    if not warned_shared_resource_pdf:
                        logging.warning(
                            f"[{book_id}] 检测到共享资源型 PDF：单页抽取会把整本资源一起带出。"
                            " 本书后续页面将直接使用保底切分，避免重复生成超大文件。"
                        )
                        warned_shared_resource_pdf = True
                    force_raster_mode = True
                    try:
                        os.remove(dat_path)
                    except FileNotFoundError:
                        pass
                    write_single_page_pdf_raster(src_doc, page_num, dat_path)
                    page_size = os.path.getsize(dat_path)
                    if page_size > sane_limit:
                        raise RuntimeError(
                            f"保底切分后仍异常巨大：{format_bytes(page_size)}，已中止以防止生成超大目录。"
                        )
            processed += 1
            print_progress(f"[{book_id}] 切分进度", page_index, total_pages, os.path.basename(dat_path))
        except Exception as e:
            logging.error(f"[{book_id}] 第 {page_index} 页切分失败：{e}")
            success = False
            break

    src_doc.close()

    if processed > 0:
        logging.info(f"[{book_id}] 已生成 {processed} 个切片，目标总页数 {total_pages}。")
    else:
        logging.info(f"[{book_id}] 已跳过：现有切片已存在，内容未校验。如需重切，请使用强制模式。")

    return success


def inspect_book_status(pdf_path, dist_dir, book_id):
    book_dist = os.path.join(dist_dir, book_id)
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        doc.close()
    except Exception:
        total_pages = 0

    existing_pages = 0
    if os.path.isdir(book_dist):
        for name in os.listdir(book_dist):
            if re.fullmatch(r"\d+\.dat", name):
                existing_pages += 1

    if not os.path.isdir(book_dist):
        status = "new"
    elif total_pages > 0 and existing_pages >= total_pages:
        status = "complete"
    else:
        status = "incomplete"

    return {
        "status": status,
        "total_pages": total_pages,
        "existing_pages": existing_pages,
    }


def parse_book_info(filename):
    parts = filename.split("-")

    if parts and parts[0].isdigit():
        book_id = parts[0]
        title_part = "-".join(parts[1:]) if len(parts) > 1 else filename
    else:
        match = re.search(r"(\d+)", filename)
        if not match:
            return None
        book_id = match.group(1)
        title_part = filename

    title = re.sub(r"\.pdf$", "", title_part, flags=re.IGNORECASE).strip()
    title = re.sub(r"(?:_HD_.*)$", "", title, flags=re.IGNORECASE).strip()
    title = title or f"教材 {book_id}"
    return {"id": book_id, "title": title, "filename": filename}


def discover_books(assets_dir, only=None):
    books = []
    for file in sorted(os.listdir(assets_dir)):
        if not file.lower().endswith(".pdf"):
            continue
        info = parse_book_info(file)
        if not info:
            continue
        if only and info["id"] != only:
            continue
        books.append(info)
    return books


def enrich_books_with_status(books, assets_dir, dist_dir):
    enriched = []
    total = len(books)
    for index, book in enumerate(books, start=1):
        pdf_path = os.path.join(assets_dir, book["filename"])
        meta = inspect_book_status(pdf_path, dist_dir, book["id"])
        enriched.append({**book, **meta})
        print_progress("正在扫描本地教材", index, total, f"{book['id']} / {book['title']}")
    return enriched


def select_books_for_mode(books, mode_label):
    if mode_label == "新书上架":
        return [book for book in books if book["status"] != "complete"]
    return books


def prompt_book_selection(books):
    print("可用教材：")
    for index, book in enumerate(books, start=1):
        print(f"{index}. [{book['id']}] {book['title']}")

    raw = input("请选择教材序号 [默认 1]: ").strip() or "1"
    if not raw.isdigit():
        print("输入无效，操作取消。")
        return None

    choice = int(raw)
    if choice < 1 or choice > len(books):
        print("超出范围，操作取消。")
        return None

    return books[choice - 1]["id"]


def prompt_commit_message():
    return prompt_commit_message_with_default(DEFAULT_COMMIT_MESSAGE)


def suggest_commit_message(mode_label="", job=None):
    if job:
        if job["kind"] == "full_upload":
            return f"sync {job['book_id']}"
        if job["kind"] == "meta_upload":
            return f"meta {job['book_id']}"
        if job["kind"] == "nudge":
            return f"note {job['book_id']}"
        if job["kind"] == "inventory_signal":
            return f"signal {job['book_id']}"

    if mode_label == "新书上架":
        return "sync"
    if mode_label == "继续切分":
        return "slice"
    if mode_label == "单本强制重新切分":
        return "reslice one"
    if mode_label == "全部强制重新切分":
        return "reslice all"
    if mode_label == "仅部署当前仓库":
        return DEFAULT_COMMIT_MESSAGE
    return DEFAULT_COMMIT_MESSAGE


def prompt_commit_message_with_default(default_message):
    raw = input(f"提交说明 [默认: {default_message}]: ").strip()
    return raw or default_message


def print_workflow_guide():
    print("教材切分部署台")
    print("你现在要解决哪件事？")
    print("提示：data/source 是 ABBYY Hot Folder 输入区，data/ocr 是 OCR 后成品区。")


def print_book_plan(books, label):
    print(f"\n本次流程：{label}")
    if not books:
        print("这次没有匹配到任何教材。")
        return
    print(f"本次将处理 {len(books)} 本教材：")
    for book in books[:PREVIEW_LIMIT]:
        status_text = {
            "new": "全新",
            "incomplete": "待补齐",
            "complete": "已完整",
        }.get(book.get("status"), "未知")
        extra = ""
        if book.get("total_pages"):
            extra = f" ({book.get('existing_pages', 0)}/{book['total_pages']} 页)"
        print(f"  - [{book['id']}] {book['title']} [{status_text}]{extra}")
    if len(books) > PREVIEW_LIMIT:
        print(f"  - ... 另外还有 {len(books) - PREVIEW_LIMIT} 本")


def prompt_interactive_args():
    print_workflow_guide()
    print("\n1. 有人已经把任务发来了，我现在处理它（推荐）")
    print("   例如补书库信息、整本补库、提醒维护")
    print("2. 我自己已经拿到 PDF，现在上架")
    print("   适合你本地已经有 OCR 后 PDF；如果还没 OCR，请先放进 src 并去 ABBYY 点 Run Now")
    print("3. 我只是做维护，不处理新书")
    print("   例如只推送当前仓库，或清理历史消息")
    main_choice = input(f"请选择入口 [默认 {RECOMMENDED_MODE}]: ").strip() or RECOMMENDED_MODE

    if main_choice not in {"1", "2", "3"}:
        print("输入无效，操作取消。")
        return None

    force = False
    only = None
    deploy = False
    deploy_only = False
    discord_inbox = False
    discord_cleanup = False
    commit_message = None
    mode_label = ""

    if main_choice == "1":
        discord_inbox = True
        mode_label = "处理收件箱任务"
    elif main_choice == "2":
        print("\n你手里已经有 PDF。现在要怎么处理它：")
        print("1. 直接上架（默认）")
        print("   切分新增内容，并推送到云端")
        print("2. 先只切分，暂时不推送")
        print("   适合你想先看结果")
        print("3. 重做某一本")
        print("   适合某一本切坏了，想单独重跑")
        print("4. 全部重做")
        print("   适合大改规则后整批重跑")
        sub_choice = input("请选择模式 [默认 1]: ").strip() or "1"
        if sub_choice not in {"1", "2", "3", "4"}:
            print("输入无效，操作取消。")
            return None

        if sub_choice == "1":
            deploy = True
            mode_label = "新书上架"
        elif sub_choice == "2":
            mode_label = "继续切分"
        elif sub_choice == "3":
            force = True
            mode_label = "单本强制重新切分"
            base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ocr")
            books = discover_books(base_dir)
            if not books:
                print("没有找到可选教材。")
                return None
            only = prompt_book_selection(books)
            if not only:
                print("未选择教材，操作取消。")
                return None
        elif sub_choice == "4":
            force = True
            mode_label = "全部强制重新切分"
    else:
        print("\n维护工具：")
        print("1. 只推送当前仓库")
        print("   不切分，只把已经改好的内容推上去")
        print("2. 整理 Discord 历史")
        print("   把旧消息标记为已处理，必要时删除")
        sub_choice = input("请选择模式 [默认 1]: ").strip() or "1"
        if sub_choice not in {"1", "2"}:
            print("输入无效，操作取消。")
            return None
        if sub_choice == "1":
            deploy = True
            deploy_only = True
            mode_label = "仅部署当前仓库"
        else:
            discord_cleanup = True
            mode_label = "Discord 历史整理"

    if deploy:
        commit_message = prompt_commit_message_with_default(suggest_commit_message(mode_label))

    return argparse.Namespace(
        force=force,
        only=only,
        deploy=deploy,
        deploy_only=deploy_only,
        discord_inbox=discord_inbox,
        discord_cleanup=discord_cleanup,
        message=commit_message,
        mode_label=mode_label,
    )


def run_git(args, cwd, check=True):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "git 命令执行失败").strip())
    return result


def get_git_value(args, cwd, fallback=""):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return fallback
    return (result.stdout or "").strip() or fallback


def split_lines(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def parse_git_status_paths(status_text):
    files = []
    for raw_line in (status_text or "").splitlines():
        if not raw_line.strip():
            continue
        line = raw_line.rstrip()
        if len(line) < 4:
            continue
        path_part = line[3:]
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        files.append(path_part.replace("\\", "/"))
    return files


def collect_paths_for_staging(project_root, include_paths=None):
    if include_paths:
        collected = []
        for relative in include_paths:
            normalized = relative.replace("\\", "/")
            absolute = os.path.join(project_root, normalized.replace("/", os.sep))
            if os.path.isdir(absolute):
                for root, _, files in os.walk(absolute):
                    for name in files:
                        file_path = os.path.join(root, name)
                        collected.append(os.path.relpath(file_path, project_root).replace("\\", "/"))
            elif os.path.exists(absolute):
                collected.append(normalized)
        return sorted(set(collected))

    status_before = run_git(["status", "--porcelain"], project_root, check=False)
    return sorted(set(parse_git_status_paths(status_before.stdout)))


def stage_paths_with_progress(project_root, paths):
    if not paths:
        return
    total = len(paths)
    for index, relative_path in enumerate(paths, start=1):
        run_git(["add", "--", relative_path], project_root)
        print_progress("正在加入暂存区", index, total, relative_path)


def preview_staged_files(project_root):
    staged = run_git(["diff", "--cached", "--name-only"], project_root, check=False)
    files = split_lines(staged.stdout)
    return files


def contains_blocked_paths(files):
    blocked = []
    for file in files:
        normalized = file.replace("\\", "/")
        if normalized.startswith(BLOCKED_DEPLOY_PREFIXES):
            blocked.append(normalized)
    return blocked


def print_file_preview(files):
    print(f"本次准备提交 {len(files)} 个文件：")
    for file in files[:PREVIEW_LIMIT]:
        print(f"  - {file}")
    if len(files) > PREVIEW_LIMIT:
        print(f"  - ... 另外还有 {len(files) - PREVIEW_LIMIT} 个文件")


def deploy_repo(project_root, commit_message, include_paths=None, deploy_config=None):
    deploy_config = deploy_config or load_deploy_config(os.path.join(project_root, "config", "local"))
    active_target, all_targets = pick_active_deploy_target(project_root, deploy_config)
    branch = get_git_value(["branch", "--show-current"], project_root, "(未知分支)")
    user_name = get_git_value(["config", "--get", "user.name"], project_root, "(未设置)")
    user_email = get_git_value(["config", "--get", "user.email"], project_root, "(未设置)")

    if not active_target:
        print("\n没有可用的发布目标。请先检查 config/local/部署台.deploy.json。")
        return False

    print("\n准备部署当前仓库：")
    print(f"本地分支: {branch}")
    print(f"发布目标: {active_target['label']}")
    print(f"目标分支: {active_target['branch']}")
    print(f"目标地址: {mask_url_secret(active_target['remote_url'] or active_target['remote_name'])}")
    print(f"提交身份: {user_name} <{user_email}>")

    standby_targets = [target for target in all_targets if target["key"] != active_target["key"] and target["enabled"]]
    if standby_targets:
        print("备用通道：")
        for standby in standby_targets:
            state = "已阻断" if standby["blocked"] else "可用"
            detail = f" / {standby['block_reason']}" if standby["blocked"] and standby["block_reason"] else ""
            print(f"  - {standby['label']}：{state}{detail}")

    if active_target["blocked"]:
        print(f"当前主发布目标被阻断：{active_target['block_reason'] or '未填写原因'}")
        print("想启用它时，去改 config/local/部署台.deploy.json 里的 blocked 即可。")
        return False

    confirm = input("确认继续推送到上面的目标吗？[Y/n]: ").strip().lower()
    if confirm not in {"", "y", "yes"}:
        print("已取消部署。")
        return False

    status_before = run_git(["status", "--short"], project_root, check=False)
    if status_before.stdout.strip():
        print("检测到未提交变更，正在加入暂存区...")
    else:
        print("当前工作区看起来是干净的，会检查是否已有暂存内容。")

    paths_to_stage = collect_paths_for_staging(project_root, include_paths=include_paths)
    stage_paths_with_progress(project_root, paths_to_stage)

    staged_files = preview_staged_files(project_root)
    if not staged_files:
        print("没有可提交的变更，本次不执行提交和推送。")
        return True

    blocked = contains_blocked_paths(staged_files)
    if blocked:
        print("检测到本次提交里混入了不应部署的路径，已中止：")
        for file in blocked:
            print(f"  - {file}")
        print("请先检查 .gitignore / 强制 add / 手工暂存状态。")
        return False

    print_file_preview(staged_files)

    confirm_commit = input("确认按上面的文件列表创建提交并推送吗？[Y/n]: ").strip().lower()
    if confirm_commit not in {"", "y", "yes"}:
        print("已取消部署。")
        return False

    print("正在创建提交...")
    commit_result = run_with_spinner("正在创建提交", run_git, ["commit", "-m", commit_message], project_root, False)
    if commit_result.returncode != 0:
        combined = (commit_result.stderr or "") + "\n" + (commit_result.stdout or "")
        if "nothing to commit" in combined.lower():
            print("没有新的提交内容，本次不执行推送。")
            return True
        raise RuntimeError(combined.strip())

    print("正在推送...")
    push_target = active_target["push_url"] or active_target["remote_name"] or active_target["remote_url"]
    push_result = run_with_spinner("正在推送", run_git, ["push", push_target, f"HEAD:{active_target['branch']}"], project_root, False)
    if push_result.returncode != 0:
        error_text = (push_result.stderr or push_result.stdout or "推送失败").strip()
        if active_target["push_url"] and active_target["push_url"] != (active_target["remote_url"] or ""):
            error_text = error_text.replace(active_target["push_url"], mask_url_secret(active_target["push_url"]))
        raise RuntimeError(error_text)

    print(f"部署完成：已推送到 {active_target['label']}。")
    return True


def main():
    parser = argparse.ArgumentParser(description="教材切分部署台")
    parser.add_argument("--force", action="store_true", help="强制重切：先删除旧切片再完整重建")
    parser.add_argument("--only", type=str, help="只处理指定教材编号，例如 3984")
    parser.add_argument("--deploy", action="store_true", help="切分后自动部署当前仓库")
    parser.add_argument("--deploy-only", action="store_true", help="只部署当前仓库，不做切分")
    parser.add_argument("--discord-inbox", action="store_true", help="从 Discord 收件箱拉取补库/催更消息")
    parser.add_argument("--discord-cleanup", action="store_true", help="整理 Discord 历史消息")
    parser.add_argument("--message", type=str, help="部署时使用的提交说明")

    if len(sys.argv) == 1:
        args = prompt_interactive_args()
        if args is None:
            input("按回车退出...")
            return
    else:
        args = parser.parse_args()
        if args.deploy or args.deploy_only:
            args.message = args.message or DEFAULT_COMMIT_MESSAGE
        args.discord_inbox = bool(getattr(args, "discord_inbox", False))
        args.discord_cleanup = bool(getattr(args, "discord_cleanup", False))
        args.mode_label = "命令行模式"

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    base_dir = os.path.join(project_root, "config", "local")
    assets_dir = os.path.join(project_root, "data", "ocr")
    dist_dir = os.path.join(project_root, "dist")

    os.makedirs(dist_dir, exist_ok=True)
    ensure_deploy_config(base_dir)

    succeeded = True
    deploy_paths = None

    if getattr(args, "discord_cleanup", False):
        print(f"\n本次流程：{args.mode_label}")
        state = load_bot_state(base_dir)
        config = load_bot_config(base_dir)
        try:
            raw_jobs = collect_discord_jobs(base_dir, include_handled=True, include_hidden=True)
        except Exception as e:
            logging.error(f"读取 Discord 历史失败：{e}")
            input("按回车退出...")
            return

        if not raw_jobs:
            print("没有可识别的 Discord 历史消息。")
            input("按回车退出...")
            return

        print_discord_cleanup_summary(raw_jobs)
        delete_hint = "会顺手删除这些消息" if config.get("delete_processed_messages", False) else "默认只标记为已处理，不会删除"
        print(f"整理策略：{delete_hint}")
        confirm_cleanup = input("确认把这些历史消息统一整理掉吗？[y/N]: ").strip().lower()
        if confirm_cleanup not in {"y", "yes"}:
            input("按回车退出...")
            return

        mark_jobs_handled(
            base_dir,
            state,
            raw_jobs,
            delete_after=bool(config.get("delete_processed_messages", False)),
        )
        print("历史整理完成。")
        input("按回车退出...")
        return
    elif getattr(args, "discord_inbox", False):
        print(f"\n本次流程：{args.mode_label}")
        state = load_bot_state(base_dir)
        config = load_bot_config(base_dir)
        try:
            jobs = collect_discord_jobs(base_dir)
        except Exception as e:
            logging.error(f"读取 Discord 收件箱失败：{e}")
            input("按回车退出...")
            return

        views = split_inbox_views(jobs)
        view_choice = prompt_inbox_view_selection(views)
        if not view_choice:
            input("按回车退出...")
            return

        view_label, _, view_jobs = views[view_choice]
        if not view_jobs:
            print(f"{view_label} 里当前没有任务。")
            input("按回车退出...")
            return

        print(f"\n当前入口：{view_label}")
        print_discord_jobs(view_jobs, state)
        selected_job = prompt_discord_job_selection(view_jobs)
        if not selected_job:
            input("按回车退出...")
            return

        print(f"已选择：[{selected_job['book_id']}] {selected_job['title']}")

        if selected_job["kind"] == "nudge":
            print("这是一条催维护提醒。")
            print("意思是：有人说这本书应该尽快处理，但这条消息本身不带可正式入库的资产。")
            print("你现在可以：")
            print("  1. 手动决定是否把这本书加入 dist/-1.dat")
            print("  2. 等待真正的补库消息出现后再处理")
            mark = input("是否把这条催更消息标记为已看？[y/N]: ").strip().lower()
            if mark in {"y", "yes"}:
                mark_jobs_handled(
                    base_dir,
                    state,
                    [selected_job],
                    delete_after=bool(config.get("delete_processed_messages", False)),
                )
                print("已标记。")
            input("处理完成，按回车退出...")
            return

        if selected_job["kind"] == "inventory_signal":
            reporter = selected_job.get("reporter") or {}
            reporter_label = reporter.get("name") or reporter.get("installId") or "未知来源"
            print("这是一条缺书线索。")
            print("意思是：有人书架里有这本书，但云端目前没有。它不是补库包，只是情报。")
            print(f"来源：{reporter_label}")
            if reporter.get("language") or reporter.get("timezone"):
                print(f"环境：{reporter.get('language') or '未知语言'} / {reporter.get('timezone') or '未知时区'}")
            print("你现在可以：")
            print("  1. 判断是否把这本书加入 dist/-1.dat")
            print("  2. 如果你已经拿到整本，也可以直接补库")
            mark = input("是否把这条缺书线索标记为已看？[y/N]: ").strip().lower()
            if mark in {"y", "yes"}:
                mark_jobs_handled(
                    base_dir,
                    state,
                    [selected_job],
                    delete_after=bool(config.get("delete_processed_messages", False)),
                )
                print("已标记。")
            input("处理完成，按回车退出...")
            return

        if not args.message:
            args.message = prompt_commit_message_with_default(suggest_commit_message(job=selected_job))

        with tempfile.TemporaryDirectory(prefix="f4s-bot-") as temp_dir:
            try:
                if selected_job["kind"] == "full_upload":
                    pdf_path = download_pdf_from_job(selected_job, temp_dir)
                    ocr_pdf_path = handoff_to_abbyy_hotfolder(pdf_path, project_root, assets_dir, selected_job)
                    process_ok = process_book(ocr_pdf_path, dist_dir, selected_job["book_id"], force=True)
                    if not process_ok:
                        raise RuntimeError("切分失败")
                    download_meta_to_dist(selected_job, dist_dir)
                elif selected_job["kind"] == "meta_upload":
                    print("这是一条补书库信息任务。")
                    print("意思是：云端 PDF 已经有了，这次只需要补 0.dat。")
                    ok = download_meta_to_dist(selected_job, dist_dir)
                    if not ok:
                        raise RuntimeError("这条补书库信息任务里没有 0.dat 链接。")
                else:
                    raise RuntimeError("未知任务类型。")
                deploy_paths = [f"dist/{selected_job['book_id']}"]
            except Exception as e:
                logging.error(f"处理 Discord 任务失败：{e}")
                input("处理完成，按回车退出...")
                return

        try:
            deploy_ok = deploy_repo(
                project_root,
                args.message or DEFAULT_COMMIT_MESSAGE,
                include_paths=deploy_paths,
            )
            if deploy_ok:
                mark_jobs_handled(
                    base_dir,
                    state,
                    [selected_job],
                    delete_after=bool(config.get("delete_processed_messages", False)),
                )
            else:
                print("这条任务还保留在收件箱里，下次可以继续处理。")
        except Exception as e:
            logging.error(f"部署失败：{e}")
            input("处理完成，按回车退出...")
            return
    elif not args.deploy_only:
        books = discover_books(assets_dir, args.only)
        books = enrich_books_with_status(books, assets_dir, dist_dir)
        books = select_books_for_mode(books, args.mode_label)
        print_book_plan(books, args.mode_label)
        if not books:
            if args.mode_label == "新书上架":
                logging.warning("没有待上架的新书或待补齐教材，本次不会部署。")
                args.deploy = False
            else:
                logging.warning("未在 assets 目录中找到符合条件的 PDF。")
        else:
            deploy_paths = [f"dist/{book['id']}" for book in books]
            total_books = len(books)
            for index, book in enumerate(books, start=1):
                pdf_path = os.path.join(assets_dir, book["filename"])
                if not process_book(pdf_path, dist_dir, book["id"], args.force):
                    succeeded = False
                print_progress("整批教材处理", index, total_books, f"{book['id']} / {book['title']}")
    else:
        print(f"\n本次流程：{args.mode_label}")
        print("这次不会切分教材，只会检查并部署当前仓库。")

    if succeeded and (args.deploy or args.deploy_only):
        try:
            deploy_repo(
                project_root,
                args.message or DEFAULT_COMMIT_MESSAGE,
                include_paths=deploy_paths if not args.deploy_only else None,
            )
        except Exception as e:
            logging.error(f"部署失败：{e}")
            succeeded = False

    input("处理完成，按回车退出...")


if __name__ == "__main__":
    main()
