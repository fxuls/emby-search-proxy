"""searchfix against a fake Emby upstream, all in-process."""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "searchfix"))
os.environ["CANDIDATE_POOL"] = "50"
os.environ["UPSTREAM_TIMEOUT"] = "5"


class FakeEmby(BaseHTTPRequestHandler):
    seen = []
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeEmby.seen.append((self.path, dict(self.headers)))
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path.endswith("/boom"):
            body, status = b"nope", 500
        elif u.path.endswith("/ItemTypes"):
            body, status = json.dumps({"Items": [{"Name": "Tag"}, {"Name": "Person"}, {"Name": "Movie"}]}).encode(), 200
        elif u.path.endswith("/Items"):
            items = [{"Type": "Tag", "Name": "office"},
                     {"Type": "Movie", "Name": "Office Space"},
                     {"Type": "Series", "Name": "The Office (US)"},
                     {"Type": "Person", "Name": "Charles Officer"}]
            n = int(q.get("Limit", 100))
            body, status = json.dumps({"Items": items[:n], "TotalRecordCount": 4}).encode(), 200
        else:
            body, status = b"passthrough", 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.emby = serve(FakeEmby)
        os.environ["EMBY_UPSTREAM"] = f"http://127.0.0.1:{cls.emby.server_port}"
        import importlib
        import server
        importlib.reload(server)
        cls.server_mod = server
        cls.fix = serve(server.Handler)
        cls.base = f"http://127.0.0.1:{cls.fix.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.fix.shutdown()
        cls.emby.shutdown()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def setUp(self):
        FakeEmby.seen.clear()

    def test_health(self):
        self.assertEqual(self.get("/health")[0], 200)

    def test_item_types_reordered(self):
        s, h, b = self.get("/emby/ItemTypes?SearchTerm=office&userId=u")
        self.assertEqual([t["Name"] for t in json.loads(b)["Items"]], ["Movie", "Person", "Tag"])
        self.assertEqual(h.get("X-Searchfix"), "types")

    def test_items_widened_reranked_and_trimmed(self):
        s, h, b = self.get("/emby/Users/u/Items?SearchTerm=The%20Office&Limit=2&Fields=Overview")
        self.assertEqual([i["Name"] for i in json.loads(b)["Items"]], ["The Office (US)", "Office Space"])
        upstream = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(FakeEmby.seen[-1][0]).query))
        self.assertEqual(upstream["Limit"], "50")
        self.assertEqual(upstream["Fields"], "Overview,OriginalTitle,SortName")
        self.assertEqual(upstream["SearchTerm"], "The Office")

    def test_lowercase_param_names(self):
        s, h, b = self.get("/emby/Users/u/Items?searchTerm=The%20Office&limit=1")
        self.assertEqual([i["Name"] for i in json.loads(b)["Items"]], ["The Office (US)"])

    def test_sortby_left_alone(self):
        s, h, b = self.get("/emby/Users/u/Items?SearchTerm=office&SortBy=SortName&Limit=2")
        self.assertEqual(json.loads(b)["Items"][0]["Name"], "office")
        self.assertNotIn("X-Searchfix", h)

    def test_paged_left_alone(self):
        s, h, b = self.get("/emby/Users/u/Items?SearchTerm=office&StartIndex=0&Limit=2")
        self.assertEqual(json.loads(b)["Items"][0]["Name"], "office")
        upstream = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(FakeEmby.seen[-1][0]).query))
        self.assertEqual(upstream["Limit"], "2")

    def test_other_search_endpoints_passthrough(self):
        s, h, b = self.get("/emby/Persons?SearchTerm=tom")
        self.assertEqual(b, b"passthrough")

    def test_upstream_error_status_passed_through(self):
        s, h, b = self.get("/emby/boom?SearchTerm=x")
        self.assertEqual(s, 500)

    def test_auth_headers_forwarded_hop_headers_not(self):
        self.get("/emby/Users/u/Items?SearchTerm=x",
                 headers={"X-Emby-Token": "abc", "Accept-Encoding": "gzip"})
        fwd = {k.lower(): v for k, v in FakeEmby.seen[-1][1].items()}
        self.assertEqual(fwd.get("x-emby-token"), "abc")
        self.assertNotEqual(fwd.get("accept-encoding"), "gzip")

    def test_non_json_200_served_as_is(self):
        # rewrite failure path: Emby returned something unparseable
        orig = FakeEmby.do_GET

        def bad(handler):
            body = b"not json"
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
        FakeEmby.do_GET = bad
        try:
            s, h, b = self.get("/emby/Users/u/Items?SearchTerm=x&Limit=1")
            self.assertEqual((s, b), (200, b"not json"))
        finally:
            FakeEmby.do_GET = orig


class UpstreamDown(unittest.TestCase):
    def test_502_when_emby_unreachable(self):
        import importlib
        os.environ["EMBY_UPSTREAM"] = "http://127.0.0.1:9"  # discard port, nothing listening
        import server
        importlib.reload(server)
        fix = serve(server.Handler)
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(f"http://127.0.0.1:{fix.server_port}/emby/Items?SearchTerm=x", timeout=5)
            self.assertEqual(cm.exception.code, 502)
        finally:
            fix.shutdown()


class Classify(unittest.TestCase):
    def test_classify(self):
        import server
        c = server.classify
        self.assertEqual(c("GET", "/emby/Users/u/Items", {"searchterm": "x"}), "items")
        self.assertEqual(c("GET", "/emby/ItemTypes/", {"searchterm": "x"}), "types")
        self.assertIsNone(c("GET", "/emby/Users/u/Items", {"searchterm": ""}))
        self.assertIsNone(c("POST", "/emby/Users/u/Items", {"searchterm": "x"}))
        self.assertIsNone(c("GET", "/emby/Users/u/Items", {"searchterm": "x", "sortby": "SortName"}))
        self.assertIsNone(c("GET", "/emby/Users/u/Items", {"searchterm": "x", "startindex": "0"}))
        self.assertIsNone(c("GET", "/emby/Users/u/Items/123", {"searchterm": "x"}))


if __name__ == "__main__":
    unittest.main()
