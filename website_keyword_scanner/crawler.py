"""Small, dependency-free, same-site web crawler."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
import threading
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


class _PageParser(HTMLParser):
    """Collect links and searchable visible text from an HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.text: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.text.append(data)


@dataclass(frozen=True)
class CrawlConfig:
    start_url: str
    keywords: tuple[str, ...]
    match_all: bool = False
    case_sensitive: bool = False
    max_pages: int = 500
    timeout: float = 10.0
    delay: float = 0.1

    def validated(self) -> "CrawlConfig":
        url = self.start_url.strip()
        if not urlsplit(url).scheme:
            url = "https://" + url
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or any(char.isspace() for char in parsed.netloc)):
            raise ValueError("请输入有效的 HTTP 或 HTTPS 网址")
        keywords = tuple(dict.fromkeys(k.strip() for k in self.keywords if k.strip()))
        if not keywords:
            raise ValueError("请至少输入一个关键词")
        if not 1 <= self.max_pages <= 100_000:
            raise ValueError("最大页面数必须在 1 到 100000 之间")
        if not 1 <= self.timeout <= 120:
            raise ValueError("超时时间必须在 1 到 120 秒之间")
        if not 0 <= self.delay <= 60:
            raise ValueError("请求间隔必须在 0 到 60 秒之间")
        return CrawlConfig(_normalise_url(url), keywords, self.match_all,
                           self.case_sensitive, self.max_pages, self.timeout, self.delay)


@dataclass(frozen=True)
class CrawlResult:
    url: str
    matched_keywords: tuple[str, ...]


ProgressCallback = Callable[[str, int, int], None]
ResultCallback = Callable[[CrawlResult], None]


def _normalise_url(url: str) -> str:
    url, _ = urldefrag(url.strip())
    parts = urlsplit(url)
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


class WebsiteCrawler:
    """Breadth-first crawler limited to the starting URL's host."""

    def __init__(self, config: CrawlConfig) -> None:
        self.config = config.validated()
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def crawl(self, on_progress: ProgressCallback | None = None,
              on_result: ResultCallback | None = None) -> list[CrawlResult]:
        cfg = self.config
        allowed_netloc = urlsplit(cfg.start_url).netloc.lower()
        pending = [cfg.start_url]
        queued = {cfg.start_url}
        visited: set[str] = set()
        results: list[CrawlResult] = []

        while pending and len(visited) < cfg.max_pages and not self._cancel.is_set():
            url = pending.pop(0)
            visited.add(url)
            if on_progress:
                on_progress(url, len(visited), len(pending))
            try:
                request = Request(url, headers={"User-Agent": "WebsiteKeywordScanner/1.0"})
                with urlopen(request, timeout=cfg.timeout) as response:
                    content_type = response.headers.get_content_type()
                    if content_type not in {"text/html", "application/xhtml+xml"}:
                        continue
                    charset = response.headers.get_content_charset() or "utf-8"
                    raw = response.read(5 * 1024 * 1024 + 1)
                    if len(raw) > 5 * 1024 * 1024:
                        continue
                    html = raw.decode(charset, errors="replace")
            except (HTTPError, URLError, TimeoutError, OSError, LookupError):
                continue

            parser = _PageParser()
            try:
                parser.feed(html)
            except (ValueError, AssertionError):
                pass
            page_text = re.sub(r"\s+", " ", " ".join(parser.text))
            haystack = page_text if cfg.case_sensitive else page_text.casefold()
            matched = tuple(k for k in cfg.keywords
                            if (k if cfg.case_sensitive else k.casefold()) in haystack)
            is_match = len(matched) == len(cfg.keywords) if cfg.match_all else bool(matched)
            if is_match:
                result = CrawlResult(url, matched)
                results.append(result)
                if on_result:
                    on_result(result)

            for href in parser.links:
                if href.startswith(("mailto:", "tel:", "javascript:", "data:")):
                    continue
                try:
                    candidate = _normalise_url(urljoin(url, href))
                    parsed = urlsplit(candidate)
                except ValueError:
                    continue
                if (parsed.scheme in {"http", "https"}
                        and parsed.netloc.lower() == allowed_netloc
                        and candidate not in queued):
                    queued.add(candidate)
                    pending.append(candidate)
            if cfg.delay and pending and not self._cancel.is_set():
                time.sleep(cfg.delay)

        if on_progress:
            state = "已取消" if self._cancel.is_set() else "扫描完成"
            on_progress(state, len(visited), len(pending))
        return results
