"""HSC top-list rows (toplist.py): pure helpers, CopyMap build, and searchfix end to end."""
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
os.environ["UPSTREAM_TIMEOUT"] = "5"

import toplist  # noqa: E402

COPY_TAGS = {"Primary": "cp1", "Thumb": "ct1", "Logo": "cl1"}


def copy_item(cid, name):
    return {"Id": cid, "Name": name, "Type": "Movie", "ImageTags": dict(COPY_TAGS),
            "PrimaryImageAspectRatio": 0.6667}


def real_item(rid, name):
    return {"Id": rid, "Name": name, "Type": "Movie",
            "ImageTags": {"Primary": "rp", "Logo": "rl"}, "UserData": {"Played": True}}


class HelperTest(unittest.TestCase):
    def test_tag_round_trip(self):
        self.assertEqual(toplist.parse_tag(toplist.make_tag("394401", "d024_6392")), ("394401", "d024_6392"))
        self.assertIsNone(toplist.parse_tag("0123456789abcdef0123456789abcdef_639000000000000000"))
        self.assertIsNone(toplist.parse_tag(""))
        self.assertIsNone(toplist.parse_tag(None))

    def test_section_request(self):
        self.assertEqual(toplist.section_request("GET", "/emby/Users/abc/Sections/s1/Items"),
                         ("/emby", "abc", "s1"))
        self.assertEqual(toplist.section_request("GET", "/Users/abc/Sections/latestmedia_5/Items"),
                         ("", "abc", "latestmedia_5"))
        self.assertIsNone(toplist.section_request("POST", "/emby/Users/abc/Sections/s1/Items"))
        self.assertIsNone(toplist.section_request("GET", "/emby/Users/abc/Items"))

    def test_image_target(self):
        pairs = [("maxHeight", "248"), ("tag", toplist.make_tag("900", "cp1")), ("quality", "90")]
        path, copy_pairs, plain = toplist.image_target("GET", "/emby/Items/100/Images/Primary", pairs)
        self.assertEqual(path, "/emby/Items/900/Images/Primary")
        self.assertEqual(copy_pairs, [("maxHeight", "248"), ("tag", "cp1"), ("quality", "90")])
        self.assertEqual(plain, [("maxHeight", "248"), ("quality", "90")])
        # Android TV spells it Tag and adds an index
        path, copy_pairs, _ = toplist.image_target(
            "GET", "/emby/Items/100/Images/Thumb/0", [("Tag", toplist.make_tag("900", "ct1"))])
        self.assertEqual(path, "/emby/Items/900/Images/Thumb/0")
        self.assertEqual(copy_pairs, [("Tag", "ct1")])

    def test_image_target_ignores_normal_images(self):
        self.assertIsNone(toplist.image_target("GET", "/emby/Items/100/Images/Primary", [("tag", "abc_123")]))
        self.assertIsNone(toplist.image_target("GET", "/emby/Items/100/Images/Primary", []))
        self.assertIsNone(toplist.image_target("POST", "/emby/Items/100/Images/Primary",
                                               [("tag", toplist.make_tag("9", "x"))]))

    def test_playback_request(self):
        self.assertTrue(toplist.playback_request("POST", "/emby/Items/361616/PlaybackInfo"))
        self.assertTrue(toplist.playback_request("GET", "/Items/361616/PlaybackInfo"))
        self.assertFalse(toplist.playback_request("POST", "/emby/Items/361616/Images/Primary"))
        self.assertFalse(toplist.playback_request("DELETE", "/emby/Items/361616/PlaybackInfo"))

    def test_item_request(self):
        self.assertTrue(toplist.item_request("GET", "/emby/Users/abc/Items/361616"))
        self.assertTrue(toplist.item_request("GET", "/Users/abc/Items/361616"))
        self.assertFalse(toplist.item_request("GET", "/emby/Users/abc/Items/Resume"))
        self.assertFalse(toplist.item_request("GET", "/emby/Users/abc/Items/Latest"))
        self.assertFalse(toplist.item_request("GET", "/emby/Users/abc/Items"))
        self.assertFalse(toplist.item_request("POST", "/emby/Users/abc/Items/361616"))
        self.assertFalse(toplist.item_request("GET", "/emby/Items/361616/PlaybackInfo"))

    def test_strip_copy_sources(self):
        is_copy = {"900"}.__contains__
        item = {"MediaSources": [{"Id": "mediasource_100", "ItemId": "100"},
                                 {"Id": "mediasource_900", "ItemId": "900"}]}
        self.assertTrue(toplist.strip_copy_sources(item, is_copy))
        self.assertEqual([m["ItemId"] for m in item["MediaSources"]], ["100"])
        # the copy's own page: its own entry goes, the real file stays
        item = {"MediaSources": [{"ItemId": "100"}, {"ItemId": "900"}]}
        toplist.strip_copy_sources(item, is_copy)
        self.assertEqual(item["MediaSources"], [{"ItemId": "100"}])
        # real multi-version movies, a lone copy source, no sources: untouched
        for srcs in ([{"ItemId": "100"}, {"ItemId": "101"}], [{"ItemId": "900"}], [], None):
            item = {"MediaSources": srcs}
            self.assertFalse(toplist.strip_copy_sources(item, is_copy))
            self.assertEqual(item["MediaSources"], srcs)

    def test_swap_numbered_keeps_rank_art_on_real_item(self):
        items = [copy_item("900", "Knives Out"), copy_item("901", "Mario")]
        originals = {"100": real_item("100", "Knives Out")}
        out = toplist.swap_items(items, {"900": "100", "901": "101"}.get, originals, numbered=True)
        self.assertEqual([i["Id"] for i in out], ["100", "901"])   # 101 not returned: copy stays
        self.assertEqual(out[0]["ImageTags"]["Primary"], "hsc900_cp1")
        self.assertEqual(out[0]["ImageTags"]["Thumb"], "hsc900_ct1")
        self.assertEqual(out[0]["ImageTags"]["Logo"], "rl")        # the real title's own logo
        self.assertEqual(out[0]["PrimaryImageAspectRatio"], 0.6667)
        self.assertTrue(out[0]["UserData"]["Played"])              # the real title's user data
        self.assertEqual(originals["100"]["ImageTags"]["Primary"], "rp")   # input not mutated

    def test_swap_plain_row_shows_real_poster(self):
        out = toplist.swap_items([copy_item("900", "Knives Out")], {"900": "100"}.get,
                                 {"100": real_item("100", "Knives Out")}, numbered=False)
        self.assertEqual(out[0]["ImageTags"]["Primary"], "rp")

    def test_swap_drops_duplicate_title(self):
        items = [real_item("100", "Knives Out"), copy_item("900", "Knives Out")]
        out = toplist.swap_items(items, {"900": "100"}.get,
                                 {"100": real_item("100", "Knives Out")}, numbered=False)
        self.assertEqual([i["Id"] for i in out], ["100"])


class FakeEmby(BaseHTTPRequestHandler):
    """Two libraries: Movies (real titles) and an HSC top-list of copies."""
    seen = []
    home_sections_status = 200
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def reply(self, status, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        FakeEmby.seen.append((self.path, dict(self.headers)))
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        p = u.path
        if p == "/emby/Library/VirtualFolders":
            return self.reply(200, [
                {"ItemId": "5", "Name": "Movies", "Locations": ["/media/movies"]},
                {"ItemId": "77", "Name": "Weekly", "Locations": ["/config/plugins/HomeScreenCompanion/toplists/Weekly"]},
                {"ItemId": "24", "Name": "Collections", "Locations": []}])
        if p == "/emby/Items" and q.get("ParentId") == "5":
            return self.reply(200, {"Items": [
                {"Id": "100", "Type": "Movie", "ProviderIds": {"Imdb": "tt1"}},
                {"Id": "101", "Type": "Movie", "ProviderIds": {"Tmdb": "55"}},
                {"Id": "102", "Type": "Movie", "ProviderIds": {"Imdb": "tt3"}}]})
        if p == "/emby/Items" and q.get("ParentId") == "77":
            return self.reply(200, {"Items": [
                {"Id": "900", "Type": "Movie", "ProviderIds": {"Imdb": "tt1", "Tmdb": "1"}},
                {"Id": "901", "Type": "Movie", "ProviderIds": {"Tmdb": "55"}},
                {"Id": "902", "Type": "Movie", "ProviderIds": {"Imdb": "tt-missing"}}]})
        if p.endswith("/HomeSections"):
            return self.reply(FakeEmby.home_sections_status, [
                {"Id": "top", "SectionType": "items", "ParentItem": {"Id": "77"}},
                {"Id": "cw", "SectionType": "resume"}])
        if p.endswith("/Sections/top/Items"):
            return self.reply(200, {"Items": [copy_item("900", "Knives Out"), copy_item("901", "Mario"),
                                              copy_item("902", "Unmapped")], "TotalRecordCount": 3})
        if p.endswith("/Sections/cw/Items"):
            return self.reply(200, {"Items": [real_item("102", "Other"), copy_item("900", "Knives Out")]})
        if p.endswith("/Sections/latest/Items"):
            return self.reply(200, {"Items": [real_item("102", "Other")]})
        if p.endswith("/Sections/gone/Items"):
            return self.reply(500, b"NullReferenceException", "text/plain")
        if p.endswith("/Items") and "SearchTerm" in q:
            # Emby lists merged versions once and may pick the copy
            return self.reply(200, {"Items": [copy_item("900", "Knives Out"), real_item("102", "Knives Out Too")],
                                    "TotalRecordCount": 2})
        if p.endswith("/Items") and "Ids" in q:
            return self.reply(200, {"Items": [real_item(i, "real " + i) for i in q["Ids"].split(",")]})
        if p == "/emby/Users/u1/Items/100":
            return self.reply(200, {"Id": "100", "Name": "Knives Out", "MediaSources": [
                {"Id": "mediasource_100", "ItemId": "100"}, {"Id": "mediasource_900", "ItemId": "900"}]})
        if p == "/emby/Users/u1/Items/102":
            return self.reply(200, {"Id": "102", "MediaSources": [{"Id": "mediasource_102", "ItemId": "102"}]})
        if p == "/emby/Items/900/Images/Primary":
            return self.reply(200, b"NUMBERED-" + q.get("tag", "").encode(), "image/jpeg")
        if p == "/emby/Items/950/Images/Primary":
            return self.reply(500, b"gone", "text/plain")   # what Emby does for a deleted item
        if p == "/emby/Items/100/Images/Primary":
            return self.reply(200, b"PLAIN tag=" + q.get("tag", "-").encode(), "image/jpeg")
        return self.reply(200, b"passthrough", "text/plain")

    do_HEAD = do_GET
    bodies = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        FakeEmby.bodies.append(self.rfile.read(n) if n else b"")
        FakeEmby.seen.append((self.path, dict(self.headers)))
        if urllib.parse.urlsplit(self.path).path == "/emby/Items/100/PlaybackInfo":
            return self.reply(200, {"PlaySessionId": "ps", "MediaSources": [
                {"Id": "mediasource_100", "ItemId": "100", "TranscodingUrl": "/x"},
                {"Id": "mediasource_900", "ItemId": "900"}]})
        return self.reply(200, b"posted", "text/plain")


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class CopyMapTest(unittest.TestCase):
    def test_build_maps_by_imdb_then_tmdb(self):
        emby = serve(FakeEmby)
        try:
            cm = toplist.CopyMap(f"http://127.0.0.1:{emby.server_port}", "k", log=lambda m: None)
            self.assertFalse(cm.ready)
            cm.build()
            self.assertTrue(cm.ready)
            self.assertEqual(cm.libraries, {"77"})
            self.assertEqual(cm.original("900"), "100")
            self.assertEqual(cm.original("901"), "101")   # TMDb fallback
            self.assertIsNone(cm.original("902"))          # no original in the library
            self.assertIsNone(cm.original("100"))
            self.assertEqual(len(cm), 2)
        finally:
            emby.shutdown()


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.emby = serve(FakeEmby)
        os.environ["EMBY_UPSTREAM"] = f"http://127.0.0.1:{cls.emby.server_port}"
        import importlib
        import server
        importlib.reload(server)
        cm = toplist.CopyMap(os.environ["EMBY_UPSTREAM"], "k", log=lambda m: None)
        cm.build()
        server.COPIES = cm
        cls.server_mod = server
        cls.fix = serve(server.Handler)
        cls.base = f"http://127.0.0.1:{cls.fix.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.fix.shutdown()
        cls.emby.shutdown()

    def setUp(self):
        FakeEmby.seen.clear()
        FakeEmby.home_sections_status = 200
        self.server_mod._SECTIONS.clear()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_toplist_row_swapped_and_numbered(self):
        auth = {"X-Emby-Authorization": 'MediaBrowser Client="AndroidTv", Token="usertoken"'}
        s, h, b = self.get("/emby/Users/u1/Sections/top/Items?Fields=PrimaryImageAspectRatio&Limit=12", auth)
        items = json.loads(b)["Items"]
        self.assertEqual([i["Id"] for i in items], ["100", "101", "902"])
        self.assertEqual(items[0]["ImageTags"]["Primary"], "hsc900_cp1")
        self.assertEqual(items[1]["ImageTags"]["Primary"], "hsc901_cp1")
        self.assertEqual(h.get("X-Searchfix"), "row")
        ids_call = [p for p, _ in FakeEmby.seen if "Ids=" in p][0]
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(ids_call).query))
        self.assertEqual(urllib.parse.urlsplit(ids_call).path, "/emby/Users/u1/Items")
        self.assertEqual(q["Ids"], "100,101")
        self.assertEqual(q["Fields"], "PrimaryImageAspectRatio")
        self.assertNotIn("Limit", q)
        # fetched with the user's own credentials
        self.assertTrue(all("usertoken" in hd.get("X-Emby-Authorization", "") for _, hd in FakeEmby.seen))

    def test_other_row_gets_plain_real_title(self):
        s, h, b = self.get("/emby/Users/u1/Sections/cw/Items")
        items = json.loads(b)["Items"]
        self.assertEqual([i["Id"] for i in items], ["102", "100"])
        self.assertEqual(items[1]["ImageTags"]["Primary"], "rp")

    def test_row_without_copies_untouched(self):
        s, h, b = self.get("/emby/Users/u1/Sections/latest/Items")
        self.assertEqual([i["Id"] for i in json.loads(b)["Items"]], ["102"])
        self.assertIsNone(h.get("X-Searchfix"))
        self.assertEqual(len(FakeEmby.seen), 1)   # no extra lookups

    def test_section_list_cached(self):
        self.get("/emby/Users/u1/Sections/top/Items")
        self.get("/emby/Users/u1/Sections/top/Items")
        self.assertEqual(sum(p.endswith("/HomeSections") or "/HomeSections?" in p for p, _ in FakeEmby.seen), 1)

    def test_unknown_section_list_falls_back_to_all_copies(self):
        FakeEmby.home_sections_status = 401
        s, h, b = self.get("/emby/Users/u1/Sections/top/Items")
        items = json.loads(b)["Items"]
        self.assertEqual(items[0]["ImageTags"]["Primary"], "rp")   # 902 isn't mapped -> not all copies
        s, h, b = self.get("/emby/Users/u1/Sections/cw/Items")
        self.assertEqual(json.loads(b)["Items"][1]["ImageTags"]["Primary"], "rp")

    def test_stale_section_error_passed_through(self):
        s, h, b = self.get("/emby/Users/u1/Sections/gone/Items")
        self.assertEqual(s, 500)

    def test_marker_image_served_from_copy(self):
        s, h, b = self.get("/emby/Items/100/Images/Primary?maxHeight=248&tag=hsc900_cp1")
        self.assertEqual((s, b), (200, b"NUMBERED-cp1"))
        self.assertEqual(h.get("Content-Type"), "image/jpeg")

    def test_marker_image_for_rebuilt_copy_falls_back_to_real_poster(self):
        s, h, b = self.get("/emby/Items/100/Images/Primary?maxHeight=248&tag=hsc950_old")
        self.assertEqual((s, b), (200, b"PLAIN tag=-"))

    def test_normal_image_passthrough(self):
        s, h, b = self.get("/emby/Items/100/Images/Primary?tag=rp")
        self.assertEqual(b, b"PLAIN tag=rp")

    def test_search_lone_copy_becomes_real_title(self):
        uid = "0123456789abcdef0123456789abcdef"
        s, h, b = self.get(f"/emby/Users/{uid}/Items?SearchTerm=Knives&Recursive=true&Limit=10",
                           {"X-Emby-Token": "usertoken"})
        items = json.loads(b)["Items"]
        self.assertEqual(sorted(i["Id"] for i in items), ["100", "102"])
        self.assertEqual([i for i in items if i["Id"] == "100"][0]["ImageTags"]["Primary"], "rp")
        ids_call = [p for p, hd in FakeEmby.seen if "Ids=" in p][0]
        self.assertTrue(urllib.parse.urlsplit(ids_call).path.endswith(f"/emby/Users/{uid}/Items"))

    def test_search_without_user_leaves_copy(self):
        s, h, b = self.get("/emby/Items?SearchTerm=Knives&Recursive=true&Limit=10")
        self.assertIn("900", [i["Id"] for i in json.loads(b)["Items"]])

    def test_api_prefix(self):
        self.assertEqual(self.server_mod.api_prefix("/emby/Users/abc/Items"), "/emby")
        self.assertEqual(self.server_mod.api_prefix("/Users/abc/Items"), "")
        self.assertEqual(self.server_mod.api_prefix("/emby/Items"), "/emby")
        self.assertEqual(self.server_mod.api_prefix("/Items"), "")

    def test_detail_page_hides_copy_version(self):
        s, h, b = self.get("/emby/Users/u1/Items/100")
        self.assertEqual([m["ItemId"] for m in json.loads(b)["MediaSources"]], ["100"])
        self.assertEqual(h.get("X-Searchfix"), "item")

    def test_detail_page_without_copy_untouched(self):
        s, h, b = self.get("/emby/Users/u1/Items/102")
        self.assertEqual(len(json.loads(b)["MediaSources"]), 1)
        self.assertIsNone(h.get("X-Searchfix"))

    def post(self, path, body, headers=None):
        req = urllib.request.Request(self.base + path, data=body, headers=headers or {}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()

    def test_playbackinfo_hides_copy_version_and_forwards_body(self):
        FakeEmby.bodies.clear()
        profile = json.dumps({"DeviceProfile": {"Name": "x" * 5000}}).encode()
        s, h, b = self.post("/emby/Items/100/PlaybackInfo?UserId=u1", profile,
                            {"Content-Type": "application/json"})
        d = json.loads(b)
        self.assertEqual([m["ItemId"] for m in d["MediaSources"]], ["100"])
        self.assertEqual(d["MediaSources"][0]["TranscodingUrl"], "/x")
        self.assertEqual(d["PlaySessionId"], "ps")
        self.assertEqual(FakeEmby.bodies, [profile])

    def test_bodies_on_one_keepalive_connection(self):
        # 2026-10-29: the body was cached per connection, so the 2nd POST reused the
        # 1st body and its own was parsed as the next request line (501).
        import http.client
        FakeEmby.bodies.clear()
        conn = http.client.HTTPConnection("127.0.0.1", self.fix.server_port, timeout=5)
        statuses = []
        for body in (b'{"n":1}', b'{"n":2}', b'{"n":3}'):
            conn.request("POST", "/emby/Items/100/PlaybackInfo", body=body,
                         headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            r.read()
            statuses.append(r.status)
        conn.request("GET", "/emby/Users/u1/Items/100")
        r = conn.getresponse()
        r.read()
        statuses.append(r.status)
        conn.close()
        self.assertEqual(statuses, [200, 200, 200, 200])
        self.assertEqual(FakeEmby.bodies, [b'{"n":1}', b'{"n":2}', b'{"n":3}'])

    def test_playbackinfo_chunked_body_forwarded(self):
        import http.client
        FakeEmby.bodies.clear()
        conn = http.client.HTTPConnection("127.0.0.1", self.fix.server_port, timeout=5)
        conn.putrequest("POST", "/emby/Items/100/PlaybackInfo")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.putheader("Content-Type", "application/json")
        conn.endheaders()
        for chunk in (b'{"DeviceProfile":', b'{"Name":"tv"}}'):
            conn.send(b"%x\r\n%s\r\n" % (len(chunk), chunk))
        conn.send(b"0\r\n\r\n")
        r = conn.getresponse()
        d = json.loads(r.read())
        conn.close()
        self.assertEqual(FakeEmby.bodies, [b'{"DeviceProfile":{"Name":"tv"}}'])
        self.assertEqual(len(d["MediaSources"]), 1)

    def test_rows_untouched_until_map_ready(self):
        saved = self.server_mod.COPIES
        self.server_mod.COPIES = toplist.CopyMap("http://x", "k", log=lambda m: None)
        try:
            s, h, b = self.get("/emby/Users/u1/Sections/top/Items")
            self.assertEqual([i["Id"] for i in json.loads(b)["Items"]], ["900", "901", "902"])
        finally:
            self.server_mod.COPIES = saved


if __name__ == "__main__":
    unittest.main()
