"""rowshuffle.py (daily rating jitter for home rows) and its server path."""
import datetime
import json
import os
import statistics
import sys
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "searchfix"))
os.environ["UPSTREAM_TIMEOUT"] = "5"
import rowshuffle as rs  # noqa: E402

ROW = {"Id": "s1", "CustomName": "Animation", "SectionType": "items",
       "SortBy": "CommunityRating,SortName", "SortOrder": "Descending",
       "Query": {"TagIds": ["306722"], "GenreIds": [], "StudioIds": []}}
SORTS = {"communityrating,sortname"}


def members(n=60):
    # Emby order: rating descending, 1-decimal ratings with ties
    return [(f"m{i}", round(9.0 - i * 0.05, 1)) for i in range(n)]


class PureTest(unittest.TestCase):
    def test_eligible(self):
        self.assertTrue(rs.eligible(ROW, SORTS))
        self.assertFalse(rs.eligible(None, SORTS))
        self.assertFalse(rs.eligible(dict(ROW, SortBy="SortName"), SORTS))
        self.assertFalse(rs.eligible(dict(ROW, SortOrder="Ascending"), SORTS))
        self.assertFalse(rs.eligible(dict(ROW, SectionType="resume"), SORTS))
        self.assertFalse(rs.eligible(dict(ROW, Query={"TagIds": []}), SORTS))
        self.assertTrue(rs.eligible(dict(ROW, SortBy="CommunityRating, SortName"), SORTS))

    def test_row_key_shared_across_users(self):
        other_users_copy = dict(ROW, Id="zz9")
        self.assertEqual(rs.row_key(ROW), rs.row_key(other_users_copy))
        self.assertNotEqual(rs.row_key(ROW), rs.row_key(dict(ROW, Query={"TagIds": ["1"]})))

    def test_day_key_rolls_over_at_hour(self):
        d = datetime.datetime
        self.assertEqual(rs.day_key(d(2026, 11, 11, 3, 59), 4), "2026-11-10")
        self.assertEqual(rs.day_key(d(2026, 11, 11, 4, 0), 4), "2026-11-11")
        self.assertEqual(rs.day_key(d(2026, 11, 11, 23, 30), 4), "2026-11-11")

    def test_z_is_standard_normalish_and_deterministic(self):
        zs = [rs.z("r", "2026-11-10", f"i{i}") for i in range(4000)]
        self.assertAlmostEqual(statistics.mean(zs), 0, delta=0.06)
        self.assertAlmostEqual(statistics.pstdev(zs), 1, delta=0.06)
        self.assertEqual(rs.z("r", "d", "x"), rs.z("r", "d", "x"))

    def test_sigma_zero_is_emby_order(self):
        m = members()
        self.assertEqual(rs.order(m, 0, "r", "d"), [i for i, _ in m])

    def test_same_day_same_order_new_day_new_order(self):
        m = members()
        a = rs.order(m, 0.4, "r", "2026-11-10")
        self.assertEqual(a, rs.order(m, 0.4, "r", "2026-11-10"))
        self.assertNotEqual(a[:12], rs.order(m, 0.4, "r", "2026-11-11")[:12])
        self.assertNotEqual(a[:12], rs.order(m, 0.4, "other row", "2026-11-10")[:12])
        self.assertEqual(sorted(a), sorted(i for i, _ in m))

    def test_adding_a_title_keeps_the_others_in_order(self):
        m = members()
        before = rs.order(m, 0.4, "r", "d")
        after = rs.order(m + [("new", 8.0)], 0.4, "r", "d")
        self.assertEqual([i for i in after if i != "new"], before)

    def test_rating_still_leads(self):
        # over many days the top page averages close to the true top ratings
        m = members(200)
        rating = dict(m)
        tops = [statistics.mean(rating[i] for i in rs.order(m, 0.4, "r", f"d{k}")[:12]) for k in range(50)]
        self.assertGreater(statistics.mean(tops), statistics.mean(r for _, r in m[:12]) - 0.25)

    def test_unrated_stays_at_back(self):
        m = members(30) + [("norate", None)]
        for k in range(20):
            self.assertGreater(rs.order(m, 0.4, "r", f"d{k}").index("norate"), 25)

    def test_pages_tile_without_overlap(self):
        ids = rs.order(members(50), 0.4, "r", "d")
        got = rs.page(ids, 0, 12) + rs.page(ids, 12, 12) + rs.page(ids, 24, 12) + rs.page(ids, 36, 12) + rs.page(ids, 48, 12)
        self.assertEqual(got, ids)
        self.assertEqual(rs.page(ids, 10, 0), ids[10:])
        self.assertEqual(rs.page(ids, 99, 12), [])


# ---------------------------------------------------------------- server path

LIB = {f"m{i}": {"Id": f"m{i}", "Name": f"Movie {i}", "Type": "Movie",
                 "CommunityRating": round(9.0 - i * 0.05, 1), "UserData": {"Played": False}}
       for i in range(40)}
OTHER = {"Id": "s2", "CustomName": "Latest", "SectionType": "items", "SortBy": "SortName",
         "SortOrder": "Ascending", "Query": {"TagIds": ["5"]}}


class FakeEmby(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []
    ids_status = 200

    def log_message(self, *a):
        pass

    def reply(self, status, obj):
        b = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        FakeEmby.seen.append(self.path)
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path.endswith("/HomeSections"):
            return self.reply(200, [ROW, OTHER])
        if "/Sections/" in u.path:
            its = list(LIB.values())          # Emby's rating order
            total = len(its)
            s, n = int(q.get("StartIndex", 0)), int(q.get("Limit", 0))
            its = its[s:s + n] if n else its[s:]
            return self.reply(200, {"Items": its, "TotalRecordCount": total, "from": "emby-section"})
        if u.path.endswith("/Items") and "Ids" in q:
            if FakeEmby.ids_status != 200:
                return self.reply(FakeEmby.ids_status, b"err")
            its = [LIB[i] for i in q["Ids"].split(",") if i in LIB]
            tot = 0 if q.get("EnableTotalRecordCount") == "false" else len(its)
            return self.reply(200, {"Items": its, "TotalRecordCount": tot})
        return self.reply(404, {})


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
        server.ROW_SHUFFLE = True
        server.ROW_SHUFFLE_SIGMA = 0.4
        cls.srv = server
        cls.fix = serve(server.Handler)
        cls.base = f"http://127.0.0.1:{cls.fix.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.fix.shutdown()
        cls.emby.shutdown()

    def setUp(self):
        FakeEmby.seen.clear()
        FakeEmby.ids_status = 200
        self.srv.ROW_SHUFFLE_SIGMA = 0.4
        self.srv.ROW_SECTIONS.clear()
        self.srv.ROW_MEMBERS.clear()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return json.loads(r.read()), dict(r.headers)

    def row(self, sec="s1", **q):
        q.setdefault("Fields", "CommunityRating")
        return self.get(f"/emby/Users/u1/Sections/{sec}/Items?" + urllib.parse.urlencode(q))

    def test_rating_row_is_reordered_and_pages_tile(self):
        p1, h = self.row(Limit=12, EnableTotalRecordCount="false")
        p2, _ = self.row(Limit=12, StartIndex=12, EnableTotalRecordCount="false")
        self.assertEqual(h.get("X-Searchfix"), "rows")
        ids = [i["Id"] for i in p1["Items"] + p2["Items"]]
        self.assertEqual(len(ids), 24)
        self.assertEqual(len(set(ids)), 24)
        self.assertNotEqual(ids[:12], [f"m{i}" for i in range(12)])   # not Emby's order
        self.assertEqual(p1["TotalRecordCount"], 0)                   # client said no total

    def test_matches_pure_order(self):
        day = self.srv.rowshuffle.day_key(rollover_hour=self.srv.ROW_SHUFFLE_ROLLOVER_HOUR)
        want = rs.order([(k, v["CommunityRating"]) for k, v in LIB.items()], 0.4, rs.row_key(ROW), day)
        got, _ = self.row(Limit=12)
        self.assertEqual([i["Id"] for i in got["Items"]], want[:12])
        self.assertEqual(got["TotalRecordCount"], 40)

    def test_sigma_zero_matches_emby_exactly(self):
        self.srv.ROW_SHUFFLE_SIGMA = 0
        got, h = self.row(Limit=12, StartIndex=12)
        self.assertEqual(h.get("X-Searchfix"), "rows")
        self.assertEqual([i["Id"] for i in got["Items"]], [f"m{i}" for i in range(12, 24)])

    def test_paging_params_not_forwarded(self):
        self.row(Limit=12, StartIndex=12, SortBy="SortName", RandomSeed="5")
        idq = [p for p in FakeEmby.seen if "Ids=" in p][0]
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(idq).query))
        for k in ("Limit", "StartIndex", "SortBy", "RandomSeed"):
            self.assertNotIn(k, q)
        self.assertEqual(q["Fields"], "CommunityRating")
        self.assertEqual(len(q["Ids"].split(",")), 12)

    def test_members_cached(self):
        self.row(Limit=12)
        self.row(Limit=12, StartIndex=12)
        self.assertEqual(sum("/Sections/" in p for p in FakeEmby.seen), 1)
        self.assertEqual(sum(p.split("?")[0].endswith("/HomeSections") for p in FakeEmby.seen), 1)

    def test_other_rows_pass_through(self):
        got, h = self.row(sec="s2", Limit=5)
        self.assertEqual(got.get("from"), "emby-section")
        self.assertNotEqual(h.get("X-Searchfix"), "rows")

    def test_past_the_end(self):
        got, _ = self.row(Limit=12, StartIndex=48)
        self.assertEqual(got["Items"], [])
        self.assertEqual(got["TotalRecordCount"], 40)

    def test_error_falls_back_to_emby(self):
        FakeEmby.ids_status = 500
        got, _ = self.row(Limit=12)
        self.assertEqual(got.get("from"), "emby-section")
        self.assertEqual([i["Id"] for i in got["Items"]], [f"m{i}" for i in range(12)])

    def test_off_passes_through(self):
        self.srv.ROW_SHUFFLE = False
        try:
            got, _ = self.row(Limit=12)
            self.assertEqual(got.get("from"), "emby-section")
        finally:
            self.srv.ROW_SHUFFLE = True


if __name__ == "__main__":
    unittest.main()
