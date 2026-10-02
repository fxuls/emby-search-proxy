import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "searchfix"))
import expand  # noqa: E402
import rank  # noqa: E402

# 40 "Harry …" people + 3 rare "Hary…" names, like the real library (2026-10-01)
NAMES = ([(f"h{i}", "Person", f"Harry Person{i}") for i in range(40)]
         + [("ha1", "Person", "Haryadi Anwar"), ("ha2", "Person", "Sebastien Hary"),
            ("ha3", "Person", "Cheppy Haryadi"),
            ("hp", "Movie", "Harry Potter and the Chamber of Secrets"),
            ("rm", "Series", "Rick and Morty")]
         + [(f"m{i}", "Person", f"Marty Person{i}") for i in range(30)])


class CorrectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = expand.Corrector(NAMES)

    def test_rare_misspelling_corrected(self):
        self.assertEqual(self.c.correct("Hary"), "harry")
        self.assertEqual(self.c.correct("hary potter"), "harry potter")

    def test_common_or_typing_words_left_alone(self):
        self.assertIsNone(self.c.correct("harry"))     # common
        self.assertIsNone(self.c.correct("harr"))      # still typing "harry"
        self.assertIsNone(self.c.correct("har"))       # too short
        self.assertIsNone(self.c.correct("xqzvwk"))    # nothing close

    def test_partial_rare_word_is_typing(self):
        # 2026-10-01 eval: "skan" (-> Skandar) was "corrected" to "sand"
        c = expand.Corrector(NAMES + [("sk", "Person", "Skandar Keynes")]
                             + [(f"s{i}", "Movie", f"Sand {i}") for i in range(30)])
        self.assertIsNone(c.correct("skan"))


class ExpanderTest(unittest.TestCase):
    def test_many_whole_matches_never_expanded(self):
        # "rick and morty" would be "corrected" to "rick and marty", but the
        # original matches the show, and more than FEW things here
        ex = expand.Expander(expand.Corrector(NAMES))
        pool = [{"Type": "Series", "Name": "Rick and Morty"}] * (expand.FEW + 1)
        self.assertIsNone(ex.decide("rick and morty", lambda: pool))

    def test_strong_match_blocks_expansion(self):
        # the title/person exactly as typed exists: no expansion
        ex = expand.Expander(expand.Corrector(NAMES + [("x", "Person", "Hary")]))
        self.assertIsNone(ex.decide("hary", lambda: [{"Type": "Person", "Name": "Hary"}]))

    def test_studio_name_blocks_expansion(self):
        # 2026-10-01 eval: "warner bros" -> "warner brooks" fired because studios didn't count
        ex = expand.Expander(expand.Corrector(NAMES))
        self.assertIsNone(ex.decide("hary", lambda: [{"Type": "Studio", "Name": "Hary Films"}]))

    def test_few_whole_matches_expanded_and_cached(self):
        ex = expand.Expander(expand.Corrector(NAMES))
        calls = []
        pool = [{"Type": "Person", "Name": "Haryadi Anwar"}, {"Type": "Person", "Name": "Sebastien Hary"}]
        fetch = lambda: calls.append(1) or pool  # noqa: E731
        self.assertEqual(ex.decide("Hary", fetch, key="u"), "harry")
        self.assertEqual(ex.decide("hary", fetch, key="u"), "harry")
        self.assertEqual(len(calls), 1)  # one gate fetch per term and user

    def test_no_correction_no_fetch(self):
        ex = expand.Expander(expand.Corrector(NAMES))
        self.assertIsNone(ex.decide("harry", lambda: self.fail("fetched")))


class AltTermRankTest(unittest.TestCase):
    def test_typed_match_beats_corrected_match_of_same_level(self):
        items = [{"Type": "Movie", "Name": "Harry"}, {"Type": "Movie", "Name": "Hary"}]
        self.assertEqual(rank.rerank(items, "hary", None, "harry")[0]["Name"], "Hary")

    def test_corrected_titles_beat_weak_original_people(self):
        # 2026-10-01 "Hary": three obscure people, user wanted Harry Potter
        items = [{"Type": "Person", "Name": "Haryadi Anwar"}, {"Type": "Person", "Name": "Sebastien Hary"},
                 {"Type": "Movie", "Name": "Harry Potter and the Chamber of Secrets"},
                 {"Type": "Person", "Name": "Harry Townes"}]
        out = [i["Name"] for i in rank.rerank(items, "Hary", None, "harry")]
        self.assertEqual(out[0], "Harry Potter and the Chamber of Secrets")
        # people who match what was typed come before people matching only the correction
        self.assertLess(out.index("Haryadi Anwar"), out.index("Harry Townes"))

    def test_no_alt_term_unchanged(self):
        items = [{"Type": "Person", "Name": "Haryadi Anwar"}, {"Type": "Movie", "Name": "Harry Potter"}]
        self.assertEqual(rank.rerank(items, "hary"), rank.rerank(items, "hary", None, None))


if __name__ == "__main__":
    unittest.main()
