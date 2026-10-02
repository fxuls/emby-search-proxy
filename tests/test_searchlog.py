import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "searchfix"))
import searchlog  # noqa: E402


class SearchLogTest(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "s.db")
        self.log = searchlog.SearchLog(self.db, log=lambda m: None)
        self.conn = sqlite3.connect(self.db)
        self.conn.executescript(searchlog.SCHEMA)

    def write(self, **ev):
        ev.setdefault("ts", time.time())
        ev.setdefault("device_id", "dev1")
        ev.setdefault("user_id", "u1")
        self.log._write(self.conn, ev)

    def rows(self):
        return self.conn.execute("SELECT term, results, top_name, requests FROM searches ORDER BY id").fetchall()

    def test_typing_folds_into_one_row_with_final_term(self):
        t = time.time()
        for n, term in enumerate(["h", "ha", "har", "harry"]):
            self.write(term=term, ts=t + n, results=10 + n, top_name=f"top{n}")
        self.assertEqual(self.rows(), [("harry", 13, "top3", 4)])

    def test_backspace_folds_too(self):
        t = time.time()
        self.write(term="harry poter", ts=t, results=1)
        self.write(term="harry pot", ts=t + 2, results=5)
        self.assertEqual(self.rows()[0][0], "harry pot")
        self.assertEqual(len(self.rows()), 1)

    def test_new_search_new_row(self):
        t = time.time()
        self.write(term="harry", ts=t)
        self.write(term="fox", ts=t + 3)                  # unrelated term
        self.write(term="fox", ts=t + 3 + 60)             # same term, much later
        self.write(term="fox", ts=t + 70, device_id="tv") # other device
        self.assertEqual([r[0] for r in self.rows()], ["harry", "fox", "fox", "fox"])

    def test_row_requests_without_results_keep_mixed_fields(self):
        t = time.time()
        self.write(term="harry", ts=t, results=133, top_name="HP")
        self.write(term="harry", ts=t + 1)  # a typed-row request: no results field
        self.assertEqual(self.rows(), [("harry", 133, "HP", 2)])

    def test_parse_auth_header_and_query(self):
        h = {"X-Emby-Authorization": 'MediaBrowser Client="AndroidTv", Device="Shield", '
                                     'DeviceId="abc", Version="3.4"'}
        self.assertEqual(searchlog.parse_auth(h, {}),
                         {"client": "AndroidTv", "device": "Shield", "device_id": "abc", "app_version": "3.4"})
        q = {"X-Emby-Client": "Emby Web", "X-Emby-Device-Name": "Chrome Windows",
             "X-Emby-Device-Id": "d1", "X-Emby-Client-Version": "4.10"}
        self.assertEqual(searchlog.parse_auth({}, q),
                         {"client": "Emby Web", "device": "Chrome Windows", "device_id": "d1",
                          "app_version": "4.10"})

    def test_user_id(self):
        uid = "0123456789abcdef0123456789abcdef"
        self.assertEqual(searchlog.user_id_from(f"/emby/Users/{uid}/Items", {}), uid)
        self.assertEqual(searchlog.user_id_from("/emby/ItemTypes", {"userid": uid.upper()}), uid)
        self.assertIsNone(searchlog.user_id_from("/emby/Items", {}))


if __name__ == "__main__":
    unittest.main()
