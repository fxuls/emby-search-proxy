import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "searchfix"))
import fuzzy  # noqa: E402

NAMES = [
    ("hp1", "Movie", "Harry Potter and the Philosopher's Stone"),
    ("hp2", "Movie", "Harry Potter and the Chamber of Secrets"),
    ("dh", "Movie", "Dirty Harry"),
    ("is", "Movie", "Interstellar"),
    ("int", "Movie", "The Intern"),
    ("bb", "Series", "Breaking Bad"),
    ("off", "Series", "The Office (US)"),
    ("lotr", "Movie", "The Lord of the Rings: The Fellowship of the Ring"),
    ("lotf", "Movie", "Lord of the Flies"),
    ("sm", "Movie", "Spider-Man"),
    ("th", "Person", "Tom Hanks"),
    ("tj", "Person", "Tom Jenkins"),
    ("ds", "Person", "Don Shank"),
    ("ss", "Movie", "The Shawshank Redemption"),
]


class FuzzyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = fuzzy.FuzzyIndex(NAMES)

    def top(self, q):
        r = self.idx.search(q)
        return r[0][1] if r else None

    def test_typos_find_the_title(self):
        # cases measured on the real library 2026-10-01
        self.assertIn(self.top("harry poter"), ("hp1", "hp2"))
        self.assertEqual(self.top("interstelar"), "is")
        self.assertEqual(self.top("brakng bad"), "bb")
        self.assertEqual(self.top("the ofice"), "off")
        self.assertEqual(self.top("lord of the rngs"), "lotr")
        self.assertEqual(self.top("spiderman"), "sm")
        self.assertEqual(self.top("tom hnks"), "th")
        self.assertEqual(self.top("shawshank"), "ss")

    def test_deterministic_with_many_ties(self):
        # 2026-10-01: "matrx" tied with thousands of "Matt..." names; the old
        # Counter-order tie-break depended on the process's hash seed
        import subprocess
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "searchfix")
        code = ("import sys; sys.path.insert(0, " + repr(path) + "); import fuzzy;"
                "names=[(str(i),'Person','Matt Atrell '+str(i)) for i in range(3000)]"
                "+[('mx','Movie','The Matrix')];"
                "r=fuzzy.FuzzyIndex(names).search('matrx'); print(r[0][1] if r else None)")
        for seed in ("0", "1", "7", "42", "1234"):
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                 env={**os.environ, "PYTHONHASHSEED": seed}).stdout.strip()
            self.assertEqual(out, "mx", f"PYTHONHASHSEED={seed}")

    def test_noise_is_not_returned(self):
        self.assertEqual(self.idx.search("xqzvwk"), [])
        self.assertEqual(self.idx.search("abc"), [])  # too short to be a typo

    def test_whole_query_match_detection(self):
        items = [{"Type": "Movie", "Name": "Dirty Harry"}, {"Type": "Movie", "Name": "Harry Potter"}]
        self.assertTrue(fuzzy.has_whole_query_match(items, "harry"))
        self.assertTrue(fuzzy.has_whole_query_match(items, "harry pot"))   # still typing
        self.assertFalse(fuzzy.has_whole_query_match(items, "harry poter"))
        self.assertFalse(fuzzy.has_whole_query_match([{"Type": "Tag", "Name": "harry poter"}], "harry poter"))
        self.assertFalse(fuzzy.has_whole_query_match([], "interstelar"))


if __name__ == "__main__":
    unittest.main()
