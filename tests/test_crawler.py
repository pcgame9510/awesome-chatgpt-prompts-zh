from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import queue
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.request import Request
import threading
import unittest

from website_keyword_scanner.crawler import (
    CrawlConfig, WebsiteCrawler, _SameHostRedirectHandler,
)
from website_keyword_scanner.gui import ScannerApp


PAGES = {
    "/": '<a href="/one">one</a><a href="/two#part">two</a><a href="https://elsewhere.invalid/out">out</a>',
    "/one": "<p>苹果 and Banana</p>",
    "/two": '<script>苹果</script><p>只有香蕉</p><a href="/one">duplicate</a>',
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append(self.path)
        if self.path in self.server.redirects:
            self.send_response(302)
            self.send_header("Location", self.server.redirects[self.path])
            self.end_headers()
            return
        if self.path == "/binary":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            return
        body = self.server.pages.get(self.path)
        if body is None:
            self.send_error(404)
            return
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


class CrawlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.requests = []
        cls.server.redirects = {}
        cls.server.pages = dict(PAGES)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.requests.clear()
        self.server.redirects.clear()
        self.server.pages = dict(PAGES)

    def crawl(self, path="/", **kwargs):
        return WebsiteCrawler(CrawlConfig(self.base + path, ("苹果",), delay=0, **kwargs)).crawl()

    def test_any_keyword_and_same_host_crawl(self):
        results = WebsiteCrawler(CrawlConfig(self.base, ("苹果", "香蕉"), delay=0)).crawl()
        self.assertEqual([f"{self.base}/one", f"{self.base}/two"], [r.url for r in results])
        self.assertEqual(("苹果",), results[0].matched_keywords)

    def test_all_keywords_case_insensitive(self):
        results = WebsiteCrawler(CrawlConfig(self.base, ("苹果", "banana"), match_all=True, delay=0)).crawl()
        self.assertEqual([f"{self.base}/one"], [r.url for r in results])

    def test_validation(self):
        with self.assertRaises(ValueError):
            CrawlConfig("not a url", ()).validated()

    def test_redirect_blocks_external_host_and_port_before_request(self):
        external = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        external.requests, external.redirects = [], {}
        external.pages = {"/secret": "苹果"}
        thread = threading.Thread(target=external.serve_forever, daemon=True)
        thread.start()
        try:
            for host, port in (("127.0.0.1", external.server_port),
                               ("localhost", self.server.server_port)):
                with self.subTest(host=host, port=port):
                    self.server.requests.clear()
                    self.server.redirects = {
                        "/redirect": "/hop",
                        "/hop": f"http://{host}:{port}/secret",
                    }
                    self.assertEqual([], self.crawl("/redirect"))
                    self.assertEqual(["/redirect", "/hop"], self.server.requests)
                    self.assertEqual([], external.requests)
        finally:
            external.shutdown()
            external.server_close()
            thread.join()

    def test_same_host_redirect_uses_final_url_for_results_and_links(self):
        self.server.redirects = {"/redirect": "/docs/page"}
        self.server.pages.update({"/docs/page": '苹果<a href="intro">next</a>',
                                  "/docs/intro": "苹果"})
        self.assertEqual([self.base + "/docs/page", self.base + "/docs/intro"],
                         [r.url for r in self.crawl("/redirect")])

    def test_redirect_normalises_host_and_default_port(self):
        handler = _SameHostRedirectHandler("http://example.com/")
        request = handler.redirect_request(Request("http://example.com/"), None,
                                           302, "Found", {}, "http://EXAMPLE.COM:80/next")
        self.assertEqual("http://example.com:80/next", request.full_url)

    def test_base_href_first_valid_relative_absolute_and_external(self):
        cases = [
            ('<base href="/docs/"><base href="/wrong/">', "/docs/intro"),
            ('<base href="../docs/">', "/docs/intro"),
            (f'<base href="{self.base}/docs/">', "/docs/intro"),
            ('<base href="http://["><base href="/docs/">', "/docs/intro"),
            ('<base href="mailto:a@example.com"><base href="/docs/">', "/docs/intro"),
            ('<base href=""><base href="/wrong/">', "/entry/intro"),
            ('<base href="https://elsewhere.invalid/docs/">', None),
        ]
        for bases, expected in cases:
            with self.subTest(bases=bases):
                self.server.requests.clear()
                # Even anchors appearing before the base use the document base.
                self.server.pages["/entry/page"] = '<a href="intro">next</a>' + bases
                self.server.pages["/docs/intro"] = "苹果"
                self.server.pages["/entry/intro"] = "苹果"
                results = self.crawl("/entry/page")
                self.assertEqual([] if expected is None else [self.base + expected],
                                 [r.url for r in results])
                self.assertEqual(["/entry/page"] + ([] if expected is None else [expected]),
                                 self.server.requests)

    def test_base_href_resolved_against_redirect_destination(self):
        self.server.redirects = {"/redirect": "/docs/page"}
        self.server.pages.update({"/docs/page": '<base href="nested/"><a href="intro">next</a>',
                                  "/docs/nested/intro": "苹果"})
        self.assertEqual([self.base + "/docs/nested/intro"],
                         [r.url for r in self.crawl("/redirect")])

    def test_interval_applies_to_errors_skips_and_redirects(self):
        self.server.pages["/"] = ''.join(f'<a href="/{p}">{p}</a>' for p in
                                         ("missing", "binary", "large", "redirect"))
        self.server.pages["/large"] = "x" * (5 * 1024 * 1024 + 1)
        self.server.redirects = {"/redirect": "/one"}
        crawler = WebsiteCrawler(CrawlConfig(self.base, ("苹果",), delay=0.25))
        with patch.object(crawler._cancel, "wait", return_value=False) as wait:
            results = crawler.crawl()
        self.assertEqual(["/", "/missing", "/binary", "/large", "/redirect", "/one"],
                         self.server.requests)
        self.assertEqual(5, wait.call_count)
        self.assertTrue(all(call.args == (0.25,) for call in wait.call_args_list))
        self.assertEqual([self.base + "/one"], [r.url for r in results])

    def test_interval_applies_to_connection_errors(self):
        crawler = WebsiteCrawler(CrawlConfig(self.base, ("苹果",), delay=0.25))
        from urllib.request import HTTPHandler
        original = HTTPHandler.http_open

        def open_or_fail(handler, request):
            if request.full_url == self.base + "/":
                return original(handler, request)
            raise URLError("connection failed")

        with patch.object(HTTPHandler, "http_open", open_or_fail), \
                patch.object(crawler._cancel, "wait", return_value=False) as wait:
            self.assertEqual([], crawler.crawl())
        self.assertEqual(2, wait.call_count)

    def test_cancellation_during_interval_prevents_next_request(self):
        crawler = WebsiteCrawler(CrawlConfig(self.base, ("苹果",), delay=60))

        def cancel(_delay):
            crawler.cancel()
            return True

        with patch.object(crawler._cancel, "wait", side_effect=cancel):
            self.assertEqual([], crawler.crawl())
        self.assertEqual(["/"], self.server.requests)

    def test_international_hostname_is_idna_encoded(self):
        config = CrawlConfig("https://例子.测试", ("keyword",)).validated()
        self.assertEqual("https://xn--fsqu00a.xn--0zwm56d/", config.start_url)


class GuiWorkerTests(unittest.TestCase):
    def test_worker_completion_restores_controls_on_success_and_exception(self):
        for error in (None, RuntimeError("worker failed"),
                      UnicodeEncodeError("ascii", "例", 0, 1, "not ASCII")):
            with self.subTest(error=error):
                app = SimpleNamespace(_crawler=Mock(), _events=queue.Queue(),
                                      _results=[], start_button=Mock(), stop_button=Mock(),
                                      status_var=Mock(), after=Mock(), _process_events=Mock())
                app._crawler.crawl.side_effect = error
                ScannerApp._run_crawler(app)
                self.assertEqual(1, app._events.qsize())
                # Worker only queues data; Tk calls occur in the event handler.
                app.start_button.configure.assert_not_called()
                with patch("website_keyword_scanner.gui.messagebox.showerror") as showerror:
                    ScannerApp._process_events(app)
                app.start_button.configure.assert_called_once_with(state="normal")
                app.stop_button.configure.assert_called_once_with(state="disabled")
                self.assertIsNone(app._crawler)
                if error is None:
                    showerror.assert_not_called()
                    self.assertIn("扫描结束", app.status_var.set.call_args.args[0])
                else:
                    showerror.assert_called_once()
                    self.assertIn(str(error), showerror.call_args.args[1])
                    self.assertIn("扫描失败", app.status_var.set.call_args.args[0])
                app.after.assert_called_once()


if __name__ == "__main__":
    unittest.main()
