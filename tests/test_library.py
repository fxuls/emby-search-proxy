import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "searchfix"))
import rank  # noqa: E402
from library import LibraryIndex  # noqa: E402

STAR = {"Id": "p_star", "Name": "Star Person"}
LIBRARY = {
    "Movie": [
        {"Id": "m1", "Name": "Movie One", "People": [STAR, {"Id": "p_extra", "Name": "Extra"}]},
        {"Id": "m2", "Name": "Movie Two", "People": [STAR]},
        {"Id": "m3", "Name": "Movie Three", "People": [STAR, {"Name": "no id"}]},
    ],
    "Series": [{"Id": "s1", "Name": "Show One", "People": [STAR]}],
    "Episode": [  # 3 guest spots on one show = 1 title; star already credited on s1
        {"Id": "e1", "Name": "Ep", "SeriesId": "s2", "People": [{"Id": "p_guest", "Name": "Guest"}]},
        {"Id": "e2", "Name": "Ep", "SeriesId": "s2", "People": [{"Id": "p_guest", "Name": "Guest"}]},
        {"Id": "e3", "Name": "Ep", "SeriesId": "s2", "People": [{"Id": "p_guest", "Name": "Guest"}]},
        {"Id": "e4", "Name": "Ep", "SeriesId": "s1", "People": [STAR]},
    ],
    "BoxSet": [{"Id": "b1", "Name": "Movie Collection"}],
}


class FakeEmby(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
        FakeEmby.calls.append(q)
        body = json.dumps({"Items": LIBRARY[q["IncludeItemTypes"]]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LibraryIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeEmby)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.snap = os.path.join(self.tmp, "people.json")

    def make(self):
        return LibraryIndex(self.url, "KEY", self.snap, log=lambda m: None)

    def test_not_ready_until_built(self):
        pc = self.make()
        self.assertFalse(pc.ready)
        self.assertIsNone(pc.get("p_star"))

    def test_counts_titles_episodes_roll_up_to_series(self):
        pc = self.make()
        pc.build()
        self.assertEqual(pc.get("p_star"), 4)    # m1, m2, m3, s1 (e4 is also s1)
        self.assertEqual(pc.get("p_guest"), 1)   # three episodes of one show
        self.assertEqual(pc.get("p_extra"), 1)
        self.assertEqual(pc.get("unknown"), 0)
        self.assertEqual(FakeEmby.calls[-1]["api_key"], "KEY")
        self.assertEqual(FakeEmby.calls[0]["Fields"], "People")

    def test_names_cover_titles_collections_and_people_not_episodes(self):
        pc = self.make()
        pc.build()
        names = {(t, n) for _, t, n in pc.names}
        self.assertIn(("Movie", "Movie One"), names)
        self.assertIn(("Series", "Show One"), names)
        self.assertIn(("BoxSet", "Movie Collection"), names)
        self.assertIn(("Person", "Star Person"), names)
        self.assertNotIn("Episode", {t for t, _ in names})

    def test_on_build_callback(self):
        pc = self.make()
        seen = []
        pc.on_build.append(lambda lib: seen.append(len(lib.names)))
        pc.build()
        self.assertEqual(len(seen), 1)

    def test_snapshot_round_trip(self):
        self.make().build()
        pc2 = self.make()  # no build: loads the snapshot
        self.assertTrue(pc2.ready)
        self.assertEqual(pc2.get("p_star"), 4)
        self.assertIn(("b1", "BoxSet", "Movie Collection"), pc2.names)

    def test_corrupt_snapshot_ignored(self):
        with open(self.snap, "w") as f:
            f.write("{not json")
        self.assertFalse(self.make().ready)


class PersonBonusTest(unittest.TestCase):
    def test_curve(self):
        self.assertEqual(rank.person_bonus(None), 0)
        self.assertEqual(rank.person_bonus(0), 0)
        self.assertEqual(rank.person_bonus(1), 0)
        self.assertEqual(rank.person_bonus(2), -1)
        self.assertEqual(rank.person_bonus(4), -2)
        self.assertEqual(rank.person_bonus(8), -3)
        self.assertEqual(rank.person_bonus(16), -4)
        self.assertEqual(rank.person_bonus(500), -4)

    def test_within_mode_never_moves_people_past_other_types(self):
        # "gilbert" 2026-10-01: famous Gilbert Gottfried jumped the studio Gilbert Films
        items = [{"Type": "Person", "Id": "gg", "Name": "Gilbert Gottfried"},
                 {"Type": "Studio", "Id": "s", "Name": "Gilbert Films"},
                 {"Type": "Person", "Id": "nobody", "Name": "Gilbert Nobody"}]
        out = rank.rerank(items, "gilbert", {"gg": 40, "nobody": 1}.get)
        self.assertEqual([i["Id"] for i in out], ["s", "gg", "nobody"])

    def test_famous_person_stays_below_later_word_movie(self):
        # 2026-10-01 decision A: Harry Shearer must not outrank "Dirty Harry"
        items = [{"Type": "Person", "Id": "shearer", "Name": "Harry Shearer"},
                 {"Type": "Movie", "Id": "m", "Name": "Dirty Harry"}]
        out = rank.rerank(items, "harry", {"shearer": 500}.get)
        self.assertEqual([i["Type"] for i in out], ["Movie", "Person"])

    def test_bonus_never_jumps_a_match_level(self):
        self.assertLess(rank.PERSON_BONUS_MAX, rank.PREFIX - rank.EXACT)

    def test_prolific_people_lead_the_people_block(self):
        counts = {"a": 1, "b": 2, "c": 1, "d": 12}
        people = [{"Type": "Person", "Id": i, "Name": f"Harry {i.upper()}"} for i in "abcd"]
        out = [p["Id"] for p in rank.rerank(people, "harry", counts.get)]
        self.assertEqual(out, ["d", "b", "a", "c"])

    def test_better_match_still_wins_over_credits(self):
        counts = {"famous": 50, "exact": 1}
        people = [{"Type": "Person", "Id": "famous", "Name": "Jackee Harry"},   # later word
                  {"Type": "Person", "Id": "exact", "Name": "Harry Nobody"}]    # starts with
        self.assertEqual(rank.rerank(people, "harry", counts.get)[0]["Id"], "exact")

    def test_signal_off_is_neutral(self):
        people = [{"Type": "Person", "Id": i, "Name": f"Harry {i}"} for i in "abc"]
        self.assertEqual(rank.rerank(people, "harry"), people)


if __name__ == "__main__":
    unittest.main()
