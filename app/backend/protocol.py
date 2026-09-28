"""F4S_JOB v1 协议的单一校验入口。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


class JobValidationError(ValueError):
    """外部任务不符合 F4S_JOB v1。"""


ALLOWED_TYPES = {"full_upload", "meta_upload", "nudge", "inventory_signal"}
ALLOWED_PDF_MODES = {"direct", "manifest", "none"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class F4SJob:
    version: int
    protocol: str
    type: str
    source: str
    created_at: str
    book_id: str
    title: str
    volume: str = ""
    source_page_count: int = 0
    outline: dict[str, Any] = field(default_factory=dict)
    pdf: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    wanted: bool = False
    reporter: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["createdAt"] = data.pop("created_at")
        data["bookId"] = data.pop("book_id")
        data["sourcePageCount"] = data.pop("source_page_count")
        return data


def parse_job(raw: Mapping[str, Any]) -> F4SJob:
    if not isinstance(raw, Mapping):
        raise JobValidationError("任务必须是 JSON 对象")
    if raw.get("protocol") != "F4S_JOB":
        raise JobValidationError("protocol 必须是 F4S_JOB")
    if raw.get("version") != 1:
        raise JobValidationError("只支持 F4S_JOB v1")

    job_type = _text(raw.get("type"))
    if job_type not in ALLOWED_TYPES:
        raise JobValidationError(f"不支持的任务类型: {job_type or '(空)'}")

    book_id = _text(raw.get("bookId"))
    if not book_id:
        raise JobValidationError("bookId 不能为空")
    title = _text(raw.get("title")) or f"教材 {book_id}"
    created_at = _text(raw.get("createdAt")) or _iso_now()
    source = _text(raw.get("source")) or "unknown"

    outline = raw.get("outline") if isinstance(raw.get("outline"), Mapping) else {}
    pdf = raw.get("pdf") if isinstance(raw.get("pdf"), Mapping) else {}
    meta = raw.get("meta") if isinstance(raw.get("meta"), Mapping) else {}
    reporter = raw.get("reporter") if isinstance(raw.get("reporter"), Mapping) else {}
    pdf_mode = _text(pdf.get("mode")) or "none"
    if pdf_mode not in ALLOWED_PDF_MODES:
        raise JobValidationError(f"不支持的 pdf.mode: {pdf_mode}")

    source_page_count = raw.get("sourcePageCount") or 0
    try:
        source_page_count = max(0, int(source_page_count))
    except (TypeError, ValueError) as exc:
        raise JobValidationError("sourcePageCount 必须是整数") from exc

    if job_type == "full_upload" and pdf_mode not in {"direct", "manifest"}:
        raise JobValidationError("full_upload 必须提供 direct 或 manifest PDF")
    if job_type == "full_upload" and pdf_mode == "direct" and not _text(pdf.get("url")):
        raise JobValidationError("direct PDF 必须提供 pdf.url")
    if job_type == "full_upload" and pdf_mode == "manifest":
        has_manifest = bool(_text(pdf.get("url")) or (isinstance(pdf.get("parts"), list) and pdf.get("parts")))
        if not has_manifest:
            raise JobValidationError("manifest PDF 必须提供 pdf.url 或 pdf.parts")
    if job_type == "meta_upload" and not _text(meta.get("url")):
        raise JobValidationError("meta_upload 必须提供 meta.url")
    if job_type == "meta_upload" and pdf_mode != "none":
        raise JobValidationError("meta_upload 的 pdf.mode 必须是 none")
    if job_type in {"nudge", "inventory_signal"} and pdf_mode != "none":
        raise JobValidationError(f"{job_type} 不应包含 PDF")

    return F4SJob(
        version=1,
        protocol="F4S_JOB",
        type=job_type,
        source=source,
        created_at=created_at,
        book_id=book_id,
        title=title,
        volume=_text(raw.get("volume")),
        source_page_count=source_page_count,
        outline=dict(outline),
        pdf=dict(pdf),
        meta=dict(meta),
        wanted=bool(raw.get("wanted")),
        reporter=dict(reporter),
    )
