"""Small, dependency-free, same-site web crawler."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
import threading
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from urllib.request import BaseHandler, HTTPRedirectHandler, Request, build_opener


class _PageParser(HTMLParser):
    """Collect links and searchable visible text from an HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.base_hrefs: list[str] = []
        self.text: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        if tag == "base":
            href = dict(attrs).get("href")
            if href is not None:
                self.base_hrefs.append(href)
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
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("请输入不含用户名和密码的有效网址")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    host = f"[{host}]" if ":" in host else host
    port = parts.port
    netloc = host if port is None else f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def _host_port(url: str) -> tuple[str | None, int | None]:
    parts = urlsplit(url)
    port = parts.port if parts.port is not None else {"http": 80, "https": 443}.get(parts.scheme)
    return parts.hostname, port


class _SameHostRedirectHandler(HTTPRedirectHandler):
    def __init__(self, start_url: str) -> None:
        self.allowed_host = _host_port(start_url)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            target = _normalise_url(newurl)
            permitted = (urlsplit(target).scheme in {"http", "https"}
                         and _host_port(target) == self.allowed_host)
        except (ValueError, UnicodeError):
            permitted = False
        if not permitted:
            raise HTTPError(req.full_url, code, "拒绝跨主机重定向", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, target)


class _RequestIntervalHandler(BaseHandler):
    """Pace every attempt, including redirects, errors and non-HTML responses."""

    def __init__(self, delay: float, cancel: threading.Event) -> None:
        self.delay = delay
        self.cancel = cancel
        self.attempted = False

    def http_request(self, request):
        if self.cancel.is_set() or (self.attempted and self.cancel.wait(self.delay)):
            raise URLError("扫描已取消")
        self.attempted = True
        return request

    https_request = http_request


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
        allowed_host = _host_port(cfg.start_url)
        opener = build_opener(_SameHostRedirectHandler(cfg.start_url),
                              _RequestIntervalHandler(cfg.delay, self._cancel))
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
                with opener.open(request, timeout=cfg.timeout) as response:
                    page_url = _normalise_url(response.geturl())
                    content_type = response.headers.get_content_type()
                    if content_type not in {"text/html", "application/xhtml+xml"}:
                        continue
                    charset = response.headers.get_content_charset() or "utf-8"
                    raw = response.read(5 * 1024 * 1024 + 1)
                    if len(raw) > 5 * 1024 * 1024:
                        continue
                    html = raw.decode(charset, errors="replace")
            except HTTPError as exc:
                exc.close()
                continue
            except (URLError, TimeoutError, OSError, LookupError):
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
                result = CrawlResult(page_url, matched)
                results.append(result)
                if on_result:
                    on_result(result)

            base_url = page_url
            for href in parser.base_hrefs:
                try:
                    candidate = _normalise_url(urljoin(page_url, href))
                    if urlsplit(candidate).scheme in {"http", "https"}:
                        base_url = candidate
                        break
                except (ValueError, UnicodeError):
                    continue

            for href in parser.links:
                if href.startswith(("mailto:", "tel:", "javascript:", "data:")):
                    continue
                try:
                    candidate = _normalise_url(urljoin(base_url, href))
                    parsed = urlsplit(candidate)
                except (ValueError, UnicodeError):
                    continue
                if (parsed.scheme in {"http", "https"}
                        and _host_port(candidate) == allowed_host
                        and candidate not in queued):
                    queued.add(candidate)
                    pending.append(candidate)

        if on_progress:
            state = "已取消" if self._cancel.is_set() else "扫描完成"
            on_progress(state, len(visited), len(pending))
        return results
