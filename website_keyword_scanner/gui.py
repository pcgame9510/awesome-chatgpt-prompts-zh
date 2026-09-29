"""Tkinter desktop interface."""

from __future__ import annotations

import csv
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .crawler import CrawlConfig, CrawlResult, WebsiteCrawler


class ScannerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("网站关键词扫描器")
        self.geometry("920x620")
        self.minsize(760, 500)
        self._crawler: WebsiteCrawler | None = None
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._results: list[CrawlResult] = []
        self._build_ui()
        self.after(100, self._process_events)

    def _build_ui(self) -> None:
        form = ttk.LabelFrame(self, text="扫描设置", padding=12)
        form.pack(fill="x", padx=12, pady=10)
        ttk.Label(form, text="起始网址：").grid(row=0, column=0, sticky="w", pady=4)
        self.url_var = tk.StringVar(value="https://example.com")
        ttk.Entry(form, textvariable=self.url_var).grid(row=0, column=1, columnspan=5, sticky="ew", pady=4)
        ttk.Label(form, text="关键词：").grid(row=1, column=0, sticky="nw", pady=4)
        self.keywords = tk.Text(form, height=3, wrap="word")
        self.keywords.grid(row=1, column=1, columnspan=5, sticky="ew", pady=4)
        ttk.Label(form, text="每行或用逗号分隔，可输入多个关键词").grid(row=2, column=1, columnspan=5, sticky="w")

        self.match_all = tk.BooleanVar()
        self.case_sensitive = tk.BooleanVar()
        ttk.Checkbutton(form, text="页面必须包含全部关键词", variable=self.match_all).grid(row=3, column=1, sticky="w", pady=8)
        ttk.Checkbutton(form, text="区分大小写", variable=self.case_sensitive).grid(row=3, column=2, sticky="w")
        ttk.Label(form, text="最多页面：").grid(row=3, column=3, sticky="e")
        self.max_pages = tk.StringVar(value="500")
        ttk.Spinbox(form, from_=1, to=100000, textvariable=self.max_pages, width=9).grid(row=3, column=4, sticky="w")
        ttk.Label(form, text="间隔(秒)：").grid(row=3, column=5, sticky="e")
        self.delay = tk.StringVar(value="0.1")
        ttk.Entry(form, textvariable=self.delay, width=7).grid(row=3, column=6, sticky="w")
        form.columnconfigure(1, weight=1)

        actions = ttk.Frame(self)
        actions.pack(fill="x", padx=12)
        self.start_button = ttk.Button(actions, text="开始扫描", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="导出 CSV", command=self._export).pack(side="left")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(actions, textvariable=self.status_var).pack(side="right")

        result_box = ttk.LabelFrame(self, text="匹配结果", padding=8)
        result_box.pack(fill="both", expand=True, padx=12, pady=10)
        self.table = ttk.Treeview(result_box, columns=("url", "keywords"), show="headings")
        self.table.heading("url", text="页面地址")
        self.table.heading("keywords", text="命中关键词")
        self.table.column("url", width=630)
        self.table.column("keywords", width=200)
        scrollbar = ttk.Scrollbar(result_box, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.table.bind("<Double-1>", self._copy_selected)

    def _config(self) -> CrawlConfig:
        raw = self.keywords.get("1.0", "end").replace("，", ",").replace("\n", ",")
        return CrawlConfig(self.url_var.get(), tuple(raw.split(",")), self.match_all.get(),
                           self.case_sensitive.get(), int(self.max_pages.get()), delay=float(self.delay.get())).validated()

    def _start(self) -> None:
        try:
            config = self._config()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("设置有误", str(exc))
            return
        self._results.clear()
        self.table.delete(*self.table.get_children())
        self._crawler = WebsiteCrawler(config)
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        threading.Thread(target=self._run_crawler, daemon=True).start()

    def _run_crawler(self) -> None:
        error = None
        try:
            assert self._crawler is not None
            self._crawler.crawl(
                lambda url, done, waiting: self._events.put(("progress", (url, done, waiting))),
                lambda result: self._events.put(("result", result)),
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self._events.put(("done", error))

    def _process_events(self) -> None:
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                url, done, waiting = payload  # type: ignore[misc]
                self.status_var.set(f"已访问 {done}，待访问 {waiting}：{url}")
            elif kind == "result":
                result = payload
                assert isinstance(result, CrawlResult)
                self._results.append(result)
                self.table.insert("", "end", values=(result.url, "、".join(result.matched_keywords)))
            elif kind == "done":
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self._crawler = None
                if payload is not None:
                    self.status_var.set(f"扫描失败：{payload}")
                    messagebox.showerror("扫描失败", str(payload))
                else:
                    self.status_var.set(f"扫描结束，共找到 {len(self._results)} 个匹配页面")
        self.after(100, self._process_events)

    def _stop(self) -> None:
        if self._crawler:
            self._crawler.cancel()
            self.status_var.set("正在停止……")

    def _export(self) -> None:
        if not self._results:
            messagebox.showinfo("没有结果", "当前没有可导出的匹配页面。")
            return
        filename = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV 文件", "*.csv")])
        if filename:
            with Path(filename).open("w", newline="", encoding="utf-8-sig") as output:
                writer = csv.writer(output)
                writer.writerow(["页面地址", "命中关键词"])
                writer.writerows((r.url, "、".join(r.matched_keywords)) for r in self._results)

    def _copy_selected(self, _event: tk.Event[tk.Misc]) -> None:
        selected = self.table.selection()
        if selected:
            self.clipboard_clear()
            self.clipboard_append(str(self.table.item(selected[0], "values")[0]))


def main() -> None:
    ScannerApp().mainloop()


if __name__ == "__main__":
    main()
