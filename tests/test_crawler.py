from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

from website_keyword_scanner.crawler import CrawlConfig, WebsiteCrawler


PAGES = {
    "/": '<a href="/one">one</a><a href="/two#part">two</a><a href="https://elsewhere.invalid/out">out</a>',
    "/one": "<p>苹果 and Banana</p>",
    "/two": '<script>苹果</script><p>只有香蕉</p><a href="/one">duplicate</a>',
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
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
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

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


if __name__ == "__main__":
    unittest.main()
