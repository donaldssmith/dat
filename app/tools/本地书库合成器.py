import json
import os
import re
import threading
import traceback
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import messagebox, ttk

from pypdf import PdfReader, PdfWriter


def project_root_from_script() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


PROJECT_ROOT = project_root_from_script()
DIST_DIR = os.path.join(PROJECT_ROOT, "dist")
EXPORT_DIR = os.path.join(PROJECT_ROOT, "exports")
ASSETS_DIR = os.path.join(PROJECT_ROOT, "data", "ocr")


def sanitize_filename_for_path(name: str) -> str:
    text = re.sub(r'[\\/:*?"<>|]+', "-", str(name or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text or "Untitled"


def guess_title_from_assets(book_id: str) -> str:
    if not os.path.isdir(ASSETS_DIR):
        return str(book_id)
    prefixes = [f"{book_id}-", f"{book_id}_"]
    for name in os.listdir(ASSETS_DIR):
        lower = name.lower()
        if not lower.endswith(".pdf"):
            continue
        if not any(name.startswith(prefix) for prefix in prefixes):
            continue
        base = os.path.splitext(name)[0]
        title = re.sub(rf"^{re.escape(str(book_id))}[-_]+", "", base)
        title = re.sub(r"__hf-\d{8}-\d{6}$", "", title)
        title = re.sub(r"_HD_\[[^\]]+\]$", "", title)
        title = title.replace("_", " ").strip(" -_")
        if title:
            return title
    return str(book_id)


def natural_page_sort_key(value: str) -> Tuple[int, str]:
    try:
        return (int(str(value)), "")
    except ValueError:
        return (10**9, str(value))


def is_display_page_label(label: str) -> bool:
    return str(label or "").strip().isdigit()


@dataclass
class LocalBook:
    book_id: str
    title: str
    book_dir: str
    page_files: Dict[int, str]
    page_labels: List[str] = field(default_factory=list)
    page_map: List[dict] = field(default_factory=list)
    outline: List[dict] = field(default_factory=list)
    source_page_count: int = 0
    display_page_count: int = 0

    def build_label_to_source(self) -> Dict[str, int]:
        mapping: Dict[str, int] = {}
        for item in self.page_map:
            label = str(item.get("label") or "").strip()
            source_page = item.get("sourcePage")
            if label and isinstance(source_page, int):
                mapping[label] = source_page
        if not mapping and self.page_labels:
            for index, label in enumerate(self.page_labels, start=1):
                label = str(label or "").strip()
                if label:
                    mapping[label] = index
        if not mapping:
            for source_page in sorted(self.page_files):
                mapping[str(source_page)] = source_page
        return mapping

    def resolve_label(self, label: str) -> Optional[int]:
        text = str(label or "").strip()
        if not text:
            return None
        mapping = self.build_label_to_source()
        if text in mapping:
            return mapping[text]
        if text.isdigit():
            value = int(text)
            return value if value in self.page_files else None
        return None

    def get_default_range(self) -> Tuple[str, str]:
        labels = [str(item.get("label") or "").strip() for item in self.page_map if str(item.get("label") or "").strip()]
        if not labels:
            labels = [str(label or "").strip() for label in self.page_labels if str(label or "").strip()]
        if not labels:
            numeric_pages = sorted(self.page_files)
            if not numeric_pages:
                return ("1", "1")
            return (str(numeric_pages[0]), str(numeric_pages[-1]))

        display_labels = [label for label in labels if is_display_page_label(label)]
        if display_labels:
            return (display_labels[0], display_labels[-1])
        return (labels[0], labels[-1])

    def get_full_range(self) -> Tuple[str, str]:
        labels = [str(item.get("label") or "").strip() for item in self.page_map if str(item.get("label") or "").strip()]
        if not labels:
            labels = [str(label or "").strip() for label in self.page_labels if str(label or "").strip()]
        if labels:
            return (labels[0], labels[-1])
        numeric_pages = sorted(self.page_files)
        if not numeric_pages:
            return ("1", "1")
        return (str(numeric_pages[0]), str(numeric_pages[-1]))

    def range_to_source_bounds(self, from_label: str, to_label: str) -> Tuple[int, int, str, str]:
        logical_from = str(from_label or "").strip()
        logical_to = str(to_label or "").strip() or logical_from
        source_from = self.resolve_label(logical_from)
        source_to = self.resolve_label(logical_to)
        if source_from is None or source_to is None:
            raise ValueError("页码没识别出来，请输入书里显示的页码，或直接输入源页号。")
        if source_from > source_to:
            source_from, source_to = source_to, source_from
            logical_from, logical_to = logical_to, logical_from
        return source_from, source_to, logical_from, logical_to


def read_json_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def build_book_from_dist(book_id: str, book_dir: str) -> Optional[LocalBook]:
    if not os.path.isdir(book_dir):
        return None

    page_files: Dict[int, str] = {}
    for name in os.listdir(book_dir):
        match = re.fullmatch(r"(\d+)\.dat", name)
        if not match:
            continue
        source_page = int(match.group(1))
        page_files[source_page] = os.path.join(book_dir, name)

    if not page_files:
        return None

    meta_path = os.path.join(book_dir, "0.dat")
    meta = {}
    if os.path.exists(meta_path):
        try:
            meta = read_json_file(meta_path)
        except Exception:
            meta = {}

    title = str(meta.get("title") or "").strip() or guess_title_from_assets(book_id)
    page_labels = meta.get("pageLabels") if isinstance(meta.get("pageLabels"), list) else []
    page_map = meta.get("pageMap") if isinstance(meta.get("pageMap"), list) else []
    outline = meta.get("outline") if isinstance(meta.get("outline"), list) else []
    source_page_count = int(meta.get("sourcePageCount") or len(page_files))
    display_page_count = int(meta.get("displayPageCount") or len([label for label in page_labels if is_display_page_label(label)]) or 0)

    return LocalBook(
        book_id=str(book_id),
        title=title,
        book_dir=book_dir,
        page_files=page_files,
        page_labels=page_labels,
        page_map=page_map,
        outline=outline,
        source_page_count=source_page_count,
        display_page_count=display_page_count,
    )


def scan_local_books(dist_dir: str) -> List[LocalBook]:
    if not os.path.isdir(dist_dir):
        return []

    books: List[LocalBook] = []
    for name in os.listdir(dist_dir):
        book_dir = os.path.join(dist_dir, name)
        if not os.path.isdir(book_dir):
            continue
        book = build_book_from_dist(name, book_dir)
        if book:
            books.append(book)

    books.sort(key=lambda item: natural_page_sort_key(item.book_id))
    return books


def add_outline_bookmarks(writer: PdfWriter, book: LocalBook, source_from: int, source_to: int) -> int:
    outline_entries = []
    for entry in book.outline:
        try:
            source_page = int(entry.get("sourcePage"))
        except Exception:
            continue
        if source_page < source_from or source_page > source_to:
            continue
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        level = max(1, int(entry.get("level") or 1))
        outline_entries.append({
            "title": title,
            "source_page": source_page,
            "level": level,
        })

    if not outline_entries:
        return 0

    parents: Dict[int, object] = {}
    added = 0
    for entry in outline_entries:
        page_index = entry["source_page"] - source_from
        if page_index < 0 or page_index >= len(writer.pages):
            continue
        level = entry["level"]
        parent = parents.get(level - 1)
        node = writer.add_outline_item(entry["title"], page_index, parent=parent)
        parents[level] = node
        for depth in list(parents.keys()):
            if depth > level:
                parents.pop(depth, None)
        added += 1
    return added


def merge_local_book(
    book: LocalBook,
    from_label: str,
    to_label: str,
    include_outline: bool,
    output_dir: str,
    progress,
) -> str:
    source_from, source_to, logical_from, logical_to = book.range_to_source_bounds(from_label, to_label)
    source_pages = list(range(source_from, source_to + 1))
    total = len(source_pages)

    if total <= 0:
        raise ValueError("这段范围里没有可合成的页面。")

    writer = PdfWriter()
    for index, source_page in enumerate(source_pages, start=1):
        dat_path = book.page_files.get(source_page)
        if not dat_path or not os.path.exists(dat_path):
            raise FileNotFoundError(f"缺少第 {source_page} 页的本地切片。")
        progress("正在拼页面", index, total, os.path.basename(dat_path))
        with open(dat_path, "rb") as handle:
            reader = PdfReader(handle)
            if not reader.pages:
                raise RuntimeError(f"第 {source_page} 页切片是空的。")
            writer.add_page(reader.pages[0])

    bookmark_count = 0
    if include_outline:
        progress("正在写目录", total, total, "0.dat")
        bookmark_count = add_outline_bookmarks(writer, book, source_from, source_to)

    os.makedirs(output_dir, exist_ok=True)
    filename = f"{book.book_id}-{sanitize_filename_for_path(book.title)}_[{logical_from}-{logical_to}]"
    if include_outline and bookmark_count > 0:
        filename += "_含目录"
    output_path = os.path.join(output_dir, f"{filename}.pdf")

    progress("正在写出文件", total, total, os.path.basename(output_path))
    with open(output_path, "wb") as handle:
        writer.write(handle)

    return output_path


class LocalLibraryComposerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("F4S 本地书库合成器")
        self.root.geometry("1080x700")

        self.books: List[LocalBook] = []
        self.filtered_books: List[LocalBook] = []
        self.current_book: Optional[LocalBook] = None
        self.worker: Optional[threading.Thread] = None
        self.queue: Queue = Queue()

        self.search_var = tk.StringVar()
        self.from_var = tk.StringVar()
        self.to_var = tk.StringVar()
        self.include_outline_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="正在读取本地书库……")
        self.detail_var = tk.StringVar(value="")
        self.progress_var = tk.DoubleVar(value=0)

        self._build_ui()
        self._load_books()
        self.root.after(120, self._pump_queue)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x")

        ttk.Label(top, text="搜索").pack(side="left")
        search_entry = ttk.Entry(top, textvariable=self.search_var, width=42)
        search_entry.pack(side="left", padx=(8, 10))
        search_entry.bind("<KeyRelease>", lambda _event: self._apply_filter())

        ttk.Button(top, text="刷新书库", command=self._load_books).pack(side="left")
        ttk.Button(top, text="打开输出目录", command=self._open_output_dir).pack(side="left", padx=(8, 0))

        ttk.Label(
            outer,
            text=f"本地书库：{DIST_DIR}",
            foreground="#666666",
        ).pack(fill="x", pady=(8, 10))

        body = ttk.Panedwindow(outer, orient="horizontal")
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body, padding=(0, 0, 10, 0))
        right = ttk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=2)

        self.tree = ttk.Treeview(
            left,
            columns=("book_id", "title", "pages", "outline"),
            show="headings",
            selectmode="browse",
        )
        self.tree.heading("book_id", text="编号")
        self.tree.heading("title", text="书名")
        self.tree.heading("pages", text="页数")
        self.tree.heading("outline", text="目录")
        self.tree.column("book_id", width=90, anchor="center")
        self.tree.column("title", width=440, anchor="w")
        self.tree.column("pages", width=90, anchor="center")
        self.tree.column("outline", width=70, anchor="center")
        self.tree.pack(fill="both", expand=True, side="left")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        scrollbar.pack(fill="y", side="right")
        self.tree.configure(yscrollcommand=scrollbar.set)

        ttk.Label(right, text="当前选择", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(right, textvariable=self.detail_var, justify="left", wraplength=340).pack(fill="x", pady=(8, 14))

        range_row = ttk.Frame(right)
        range_row.pack(fill="x")
        ttk.Label(range_row, text="起始页").grid(row=0, column=0, sticky="w")
        ttk.Entry(range_row, textvariable=self.from_var, width=12).grid(row=0, column=1, sticky="w", padx=(8, 18))
        ttk.Label(range_row, text="结束页").grid(row=0, column=2, sticky="w")
        ttk.Entry(range_row, textvariable=self.to_var, width=12).grid(row=0, column=3, sticky="w", padx=(8, 0))

        ttk.Checkbutton(
            right,
            text="尽量写入目录书签",
            variable=self.include_outline_var,
        ).pack(anchor="w", pady=(12, 0))

        hint_lines = [
            "页码优先按 0.dat 里的书内页码识别。",
            "没有 0.dat 时，也可以直接输入源页号。",
            "默认填的是书里常规可见页码范围。",
        ]
        ttk.Label(right, text="\n".join(hint_lines), foreground="#666666", justify="left").pack(fill="x", pady=(10, 18))

        action_row = ttk.Frame(right)
        action_row.pack(fill="x")
        ttk.Button(action_row, text="整本范围", command=self._fill_full_range).pack(side="left")
        ttk.Button(action_row, text="合成当前范围", command=self._start_merge).pack(side="left", padx=(10, 0))
        ttk.Button(action_row, text="一键整本合成", command=lambda: self._start_merge(use_full_range=True)).pack(side="left", padx=(10, 0))

        ttk.Separator(right).pack(fill="x", pady=18)
        ttk.Label(right, text="运行状态", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(right, textvariable=self.status_var, justify="left", wraplength=340).pack(fill="x", pady=(8, 8))
        ttk.Progressbar(right, variable=self.progress_var, maximum=100).pack(fill="x")

    def _load_books(self) -> None:
        try:
            self.books = scan_local_books(DIST_DIR)
            self._apply_filter(preserve_selection=False)
            self.status_var.set(f"本地书库已载入，共 {len(self.books)} 本。")
        except Exception as exc:
            self.status_var.set("读取本地书库失败。")
            messagebox.showerror("读取失败", str(exc))

    def _apply_filter(self, preserve_selection: bool = True) -> None:
        previous_id = self.current_book.book_id if preserve_selection and self.current_book else ""
        keyword = self.search_var.get().strip().lower()
        if keyword:
            self.filtered_books = [
                book for book in self.books
                if keyword in book.book_id.lower() or keyword in book.title.lower()
            ]
        else:
            self.filtered_books = list(self.books)

        for item in self.tree.get_children():
            self.tree.delete(item)

        for book in self.filtered_books:
            page_text = f"{book.display_page_count or book.source_page_count}/{book.source_page_count}"
            outline_text = "有" if book.outline else "无"
            self.tree.insert("", "end", iid=book.book_id, values=(book.book_id, book.title, page_text, outline_text))

        if not self.filtered_books:
            self.current_book = None
            self.detail_var.set("没有匹配的书。")
            self.from_var.set("")
            self.to_var.set("")
            return

        selected_id = previous_id if previous_id and any(book.book_id == previous_id for book in self.filtered_books) else self.filtered_books[0].book_id
        self.tree.selection_set(selected_id)
        self.tree.focus(selected_id)
        self.tree.see(selected_id)
        self._select_book_by_id(selected_id)

    def _select_book_by_id(self, book_id: str) -> None:
        for book in self.filtered_books:
            if book.book_id == book_id:
                self.current_book = book
                default_from, default_to = book.get_default_range()
                if not self.from_var.get().strip():
                    self.from_var.set(default_from)
                if not self.to_var.get().strip():
                    self.to_var.set(default_to)
                self._refresh_detail()
                return

    def _refresh_detail(self) -> None:
        if not self.current_book:
            self.detail_var.set("没有选中书。")
            return
        default_from, default_to = self.current_book.get_default_range()
        full_from, full_to = self.current_book.get_full_range()
        detail = [
            f"编号：{self.current_book.book_id}",
            f"书名：{self.current_book.title}",
            f"本地切片：{len(self.current_book.page_files)} 页",
            f"书内常规页码：{default_from} - {default_to}",
            f"整本范围：{full_from} - {full_to}",
            f"目录条目：{len(self.current_book.outline)}",
        ]
        self.detail_var.set("\n".join(detail))

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        selected_id = selection[0]
        self.from_var.set("")
        self.to_var.set("")
        self._select_book_by_id(selected_id)

    def _fill_full_range(self) -> None:
        if not self.current_book:
            return
        full_from, full_to = self.current_book.get_full_range()
        self.from_var.set(full_from)
        self.to_var.set(full_to)
        self.status_var.set("已切换到整本范围。")

    def _open_output_dir(self) -> None:
        os.makedirs(EXPORT_DIR, exist_ok=True)
        os.startfile(EXPORT_DIR)

    def _set_busy(self, is_busy: bool) -> None:
        self.root.config(cursor="watch" if is_busy else "")

    def _start_merge(self, use_full_range: bool = False) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("稍等", "上一条任务还没做完。")
            return
        if not self.current_book:
            messagebox.showwarning("还没选书", "先在左边选一本书。")
            return

        if use_full_range:
            from_label, to_label = self.current_book.get_full_range()
            self.from_var.set(from_label)
            self.to_var.set(to_label)
        else:
            from_label = self.from_var.get().strip()
            to_label = self.to_var.get().strip() or from_label
        if not from_label:
            messagebox.showwarning("缺少页码", "先填起始页。")
            return

        self.progress_var.set(0)
        self.status_var.set("已经开始，本地合成中……")
        self._set_busy(True)

        def progress(stage: str, current: int, total: int, detail: str) -> None:
            pct = 0 if total <= 0 else max(0, min(100, current / total * 100))
            self.queue.put(("progress", stage, pct, current, total, detail))

        def worker() -> None:
            try:
                output_path = merge_local_book(
                    book=self.current_book,
                    from_label=from_label,
                    to_label=to_label,
                    include_outline=bool(self.include_outline_var.get()),
                    output_dir=EXPORT_DIR,
                    progress=progress,
                )
                self.queue.put(("done", output_path))
            except Exception as exc:
                self.queue.put(("error", f"{exc}\n\n{traceback.format_exc()}"))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _pump_queue(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, stage, pct, current, total, detail = item
                    self.progress_var.set(pct)
                    page_info = f"{current}/{total}" if total else ""
                    detail_text = f" {detail}" if detail else ""
                    self.status_var.set(f"{stage} {page_info}{detail_text}".strip())
                elif kind == "done":
                    _, output_path = item
                    self.progress_var.set(100)
                    self.status_var.set(f"已经好了：{output_path}")
                    self._set_busy(False)
                    if messagebox.askyesno("完成", f"PDF 已输出到：\n{output_path}\n\n要现在打开输出目录吗？"):
                        self._open_output_dir()
                elif kind == "error":
                    _, error_text = item
                    self.progress_var.set(0)
                    self.status_var.set("这次没做完。")
                    self._set_busy(False)
                    messagebox.showerror("合成失败", error_text)
        except Empty:
            pass
        finally:
            self.root.after(120, self._pump_queue)


def main() -> None:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    root = tk.Tk()
    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except Exception:
        pass
    LocalLibraryComposerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
