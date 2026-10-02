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
    fail_media = False
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeEmby.seen.append((self.path, dict(self.headers)))
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if q.get("SearchTerm", "").lower() == "hary":
            if u.path.endswith("/ItemTypes"):
                body, status = json.dumps({"Items": [{"Name": "Person"}]}).encode(), 200
            else:
                ppl = [{"Id": "ha1", "Type": "Person", "Name": "Haryadi Anwar"}]
                if q.get("IncludeItemTypes") not in (None, "Person"):
                    ppl = []
                body, status = json.dumps({"Items": ppl}).encode(), 200
        elif q.get("SearchTerm", "").lower() == "harry":
            if u.path.endswith("/ItemTypes"):
                body, status = json.dumps({"Items": [{"Name": "Movie"}, {"Name": "Person"}]}).encode(), 200
            else:
                allx = [{"Id": "hp", "Type": "Movie", "Name": "Harry Potter"},
                        {"Id": "ht", "Type": "Person", "Name": "Harry Townes"}]
                t = q.get("IncludeItemTypes")
                its = [i for i in allx if not t or i["Type"] in t.split(",")]
                body, status = json.dumps({"Items": its}).encode(), 200
        elif u.path.endswith("/boom"):
            body, status = b"nope", 500
        elif u.path.endswith("/ItemTypes"):
            body, status = json.dumps({"Items": [{"Name": "Tag"}, {"Name": "Person"}, {"Name": "Movie"}]}).encode(), 200
        elif u.path.endswith("/Items") and "Ids" in q:
            # mimics Emby: people only come back when IncludeItemTypes=Person
            lib = {"is": {"Id": "is", "Type": "Movie", "Name": "Interstellar"},
                   "th": {"Id": "th", "Type": "Person", "Name": "Tom Hanks"}}
            want_people = q.get("IncludeItemTypes") == "Person"
            items = [lib[i] for i in q["Ids"].split(",")
                     if i in lib and (lib[i]["Type"] == "Person") == want_people]
            body, status = json.dumps({"Items": items}).encode(), 200
        elif u.path.endswith("/Items") and q.get("SearchTerm", "").lower() in ("interstelar", "tom hnks"):
            body, status = json.dumps({"Items": [{"Id": "x", "Type": "Person", "Name": "Stella Inter"}]}).encode(), 200
        elif u.path.endswith("/Items"):
            items = [{"Id": "t", "Type": "Tag", "Name": "office"},
                     {"Id": "m", "Type": "Movie", "Name": "Office Space"},
                     {"Id": "s", "Type": "Series", "Name": "The Office (US)"},
                     {"Id": "p", "Type": "Person", "Name": "Charles Officer"}]
            if "IncludeItemTypes" in q:
                allowed = q["IncludeItemTypes"].split(",")
                items = [i for i in items if i["Type"] in allowed]
                if FakeEmby.fail_media:
                    body = b"boom"
                    self.send_response(500)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
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
        upstream = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(FakeEmby.seen[0][0]).query))
        self.assertEqual(upstream["Limit"], "50")
        self.assertEqual(upstream["Fields"], "Overview,OriginalTitle,SortName")
        self.assertEqual(upstream["SearchTerm"], "The Office")

    def test_mixed_list_gets_separate_media_pool(self):
        # "john" 2026-10-01: Emby filled the 150-item pool with 147 people
        s, h, b = self.get("/emby/Users/u/Items?SearchTerm=office&Limit=10")
        self.assertEqual(len(FakeEmby.seen), 2)
        media_q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(FakeEmby.seen[1][0]).query))
        self.assertEqual(media_q["IncludeItemTypes"], "Series,Movie,BoxSet")
        names_ = [i["Name"] for i in json.loads(b)["Items"]]
        self.assertEqual(len(names_), len(set(names_)))  # merged without duplicates
        self.assertEqual(len(names_), 4)

    def test_typed_row_has_no_second_fetch(self):
        self.get("/emby/Users/u/Items?SearchTerm=office&IncludeItemTypes=Movie&Limit=10")
        self.assertEqual(len(FakeEmby.seen), 1)

    def test_media_pool_failure_is_harmless(self):
        FakeEmby.fail_media = True
        try:
            s, h, b = self.get("/emby/Users/u/Items?SearchTerm=The%20Office&Limit=2")
            self.assertEqual(s, 200)
            self.assertEqual(json.loads(b)["Items"][0]["Name"], "The Office (US)")
        finally:
            FakeEmby.fail_media = False

    def test_lowercase_param_names(self):
        s, h, b = self.get("/emby/Users/u/Items?searchTerm=The%20Office&limit=1")
        self.assertEqual([i["Name"] for i in json.loads(b)["Items"]], ["The Office (US)"])

    def upstream_query(self):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(FakeEmby.seen[-1][0]).query))

    def test_default_name_sort_is_replaced_by_relevance(self):
        # what the web client's row loader sends for every search row (2026-09-30 bug:
        # Movies row for "harry" stayed alphabetical, Dirty Harry before Harry Potter)
        s, h, b = self.get("/emby/Users/u/Items?SearchTerm=The%20Office&IncludeItemTypes=Movie,Series"
                           "&SortBy=SortName&SortOrder=Ascending&StartIndex=0&Limit=2")
        self.assertEqual([i["Name"] for i in json.loads(b)["Items"]], ["The Office (US)", "Office Space"])
        self.assertEqual(h.get("X-Searchfix"), "items")
        up = self.upstream_query()
        for k in ("SortBy", "SortOrder", "StartIndex"):
            self.assertNotIn(k, up)
        self.assertEqual(up["Limit"], "50")

    def test_episode_default_sort_replaced(self):
        s, h, b = self.get("/emby/Users/u/Items?SearchTerm=office&IncludeItemTypes=Episode"
                           "&SortBy=SeriesSortName,ParentIndexNumber,IndexNumber,SortName&Limit=2")
        self.assertEqual(h.get("X-Searchfix"), "items")

    def test_explicit_sort_left_alone(self):
        s, h, b = self.get("/emby/Users/u/Items?SearchTerm=office&SortBy=DateCreated&Limit=2")
        self.assertEqual(json.loads(b)["Items"][0]["Name"], "office")
        self.assertNotIn("X-Searchfix", h)
        self.assertEqual(self.upstream_query()["SortBy"], "DateCreated")

    def test_pages_are_slices_of_one_ranking(self):
        pages = []
        for start in range(4):
            s, h, b = self.get(f"/emby/Users/u/Items?SearchTerm=The%20Office&StartIndex={start}&Limit=1")
            pages += [i["Name"] for i in json.loads(b)["Items"]]
        self.assertEqual(pages[:2], ["The Office (US)", "Office Space"])
        self.assertEqual(len(set(pages)), 4)  # no duplicates, nothing skipped

    def test_deep_page_widens_pool(self):
        self.get("/emby/Users/u/Items?SearchTerm=x&StartIndex=60&Limit=20")
        self.assertEqual(self.upstream_query()["Limit"], "80")

    def test_no_limit_fetches_everything(self):
        s, h, b = self.get("/emby/Users/u/Items?SearchTerm=The%20Office")
        self.assertNotIn("Limit", self.upstream_query())
        self.assertEqual(len(json.loads(b)["Items"]), 4)

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


class FuzzyAndLog(ServerTest):
    """Fuzzy head + search log wired into the server (fake Emby)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import fuzzy
        import searchlog
        import tempfile
        cls.server_mod.FUZZY = fuzzy.FuzzyIndex([("is", "Movie", "Interstellar"),
                                                 ("th", "Person", "Tom Hanks"),
                                                 ("s", "Series", "The Office (US)")])
        cls.db = os.path.join(tempfile.mkdtemp(), "s.db")
        cls.server_mod.SEARCHLOG = searchlog.SearchLog(cls.db, log=lambda m: None).start()

    @classmethod
    def tearDownClass(cls):
        cls.server_mod.FUZZY = None
        cls.server_mod.SEARCHLOG = None
        super().tearDownClass()

    def test_typo_gets_fuzzy_match_fetched_with_user_credentials(self):
        s, h, b = self.get("/emby/Users/u1/Items?SearchTerm=interstelar&Limit=5&Fields=Overview",
                           headers={"X-Emby-Token": "usertoken"})
        names_ = [i["Name"] for i in json.loads(b)["Items"]]
        self.assertEqual(names_[0], "Interstellar")
        self.assertIn("Stella Inter", names_)
        ids_req = [p for p, hd in FakeEmby.seen if "Ids=" in p][0]
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(ids_req).query))
        self.assertEqual(q["Ids"], "is")
        self.assertEqual(q["Fields"], "Overview")
        self.assertNotIn("SearchTerm", q)
        self.assertTrue(urllib.parse.urlsplit(ids_req).path.startswith("/emby/Users/u1/"))
        fwd = {k.lower(): v for p, hd in FakeEmby.seen if "Ids=" in p for k, v in hd.items()}
        self.assertEqual(fwd.get("x-emby-token"), "usertoken")

    def test_fuzzy_person_fetched_with_person_type(self):
        # "tom hnks" 2026-10-01: Emby drops people from an Ids query without IncludeItemTypes=Person
        s, h, b = self.get("/emby/Users/u1/Items?SearchTerm=tom%20hnks&Limit=5")
        self.assertEqual(json.loads(b)["Items"][0]["Name"], "Tom Hanks")

    def test_existing_match_moved_not_fetched(self):
        # "the ofice": Emby (fake) returns The Office already; fuzzy moves it, no Ids fetch
        s, h, b = self.get("/emby/Users/u1/Items?SearchTerm=the%20ofice&Limit=5")
        self.assertEqual(json.loads(b)["Items"][0]["Name"], "The Office (US)")
        self.assertFalse(any("Ids=" in p for p, _ in FakeEmby.seen))

    def test_good_query_never_fuzzy(self):
        s, h, b = self.get("/emby/Users/u1/Items?SearchTerm=office&Limit=5")
        self.assertFalse(any("Ids=" in p for p, _ in FakeEmby.seen))

    def test_typed_row_never_fuzzy(self):
        self.get("/emby/Users/u1/Items?SearchTerm=interstelar&IncludeItemTypes=Movie&Limit=5")
        self.assertFalse(any("Ids=" in p for p, _ in FakeEmby.seen))

    def test_search_is_logged(self):
        import sqlite3
        import time as _t
        self.get("/emby/Users/0123456789abcdef0123456789abcdef/Items?SearchTerm=interstelar&Limit=5",
                 headers={"X-Emby-Authorization": 'MediaBrowser Client="AndroidTv", Device="Shield", '
                                                  'DeviceId="logtest", Version="3"',
                          "X-Real-IP": "192.0.2.50"})
        for _ in range(50):
            rows = sqlite3.connect(self.db).execute(
                "SELECT term, client, device, ip, user_id, fuzzy, top_name FROM searches "
                "WHERE device_id='logtest'").fetchall()
            if rows:
                break
            _t.sleep(0.05)
        self.assertEqual(rows, [("interstelar", "AndroidTv", "Shield", "192.0.2.50",
                                 "0123456789abcdef0123456789abcdef", 1, "Interstellar")])


class Expansion(FuzzyAndLog):
    """'Hary' also searches 'harry' (expand.py) through the fake Emby."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import expand
        names = ([(f"h{i}", "Person", f"Harry P{i}") for i in range(40)]
                 + [("ha1", "Person", "Haryadi Anwar"), ("ha2", "Person", "Sebastien Hary"),
                    ("hp", "Movie", "Harry Potter")])
        cls.server_mod.EXPANDER = expand.Expander(expand.Corrector(names))

    @classmethod
    def tearDownClass(cls):
        cls.server_mod.EXPANDER = None
        super().tearDownClass()

    def test_top_results_expanded(self):
        s, h, b = self.get("/emby/Users/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1/Items?SearchTerm=Hary&Limit=10")
        names_ = [i["Name"] for i in json.loads(b)["Items"]]
        self.assertEqual(names_[0], "Harry Potter")
        self.assertIn("Haryadi Anwar", names_)
        self.assertIn("expanded", h.get("X-Searchfix", ""))

    def test_rows_expanded(self):
        s, h, b = self.get("/emby/ItemTypes?userId=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1&SearchTerm=Hary")
        self.assertEqual([t["Name"] for t in json.loads(b)["Items"]], ["Movie", "Person"])
        s, h, b = self.get("/emby/Users/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1/Items?SearchTerm=Hary&IncludeItemTypes=Movie&SortBy=SortName&Limit=16")
        self.assertEqual([i["Name"] for i in json.loads(b)["Items"]], ["Harry Potter"])

    def test_correction_logged(self):
        import sqlite3
        import time as _t
        self.get("/emby/Users/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa2/Items?SearchTerm=hary&Limit=10",
                 headers={"X-Emby-Authorization": 'MediaBrowser DeviceId="exp1"'})
        for _ in range(50):
            r = sqlite3.connect(self.db).execute(
                "SELECT term, corrected FROM searches WHERE device_id='exp1'").fetchall()
            if r:
                break
            _t.sleep(0.05)
        self.assertEqual(r, [("hary", "harry")])


class TestTrafficNotLogged(FuzzyAndLog):
    def test_test_header_skips_log(self):
        import sqlite3
        import time as _t
        self.get("/emby/Users/u1/Items?SearchTerm=office&Limit=5",
                 headers={"X-Searchfix-Test": "1", "X-Emby-Authorization": 'MediaBrowser DeviceId="testhdr"'})
        _t.sleep(0.3)
        n = sqlite3.connect(self.db).execute("SELECT count(*) FROM searches WHERE device_id='testhdr'").fetchone()[0]
        self.assertEqual(n, 0)


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
        self.assertEqual(c("GET", "/emby/Users/u/Items", {"searchterm": "x", "sortby": "SortName"}), "items")
        self.assertEqual(c("GET", "/emby/Users/u/Items", {"searchterm": "x", "startindex": "0"}), "items")
        self.assertIsNone(c("GET", "/emby/Users/u/Items", {"searchterm": "x", "sortby": "DateCreated"}))
        self.assertIsNone(c("GET", "/emby/Users/u/Items", {"searchterm": "x", "sortby": "Random"}))
        self.assertIsNone(c("GET", "/emby/Users/u/Items/123", {"searchterm": "x"}))


if __name__ == "__main__":
    unittest.main()
