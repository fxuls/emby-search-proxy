import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "searchfix"))
import rank  # noqa: E402


def it(type_, name, **kw):
    return dict(Type=type_, Name=name, **kw)


def names(items):
    return [i["Name"] for i in items]


class MatchTier(unittest.TestCase):
    def test_tiers(self):
        q = rank.norm("la la")
        self.assertEqual(rank.match_tier(rank.norm("La La Land"), q), rank.PREFIX)
        self.assertEqual(rank.match_tier(rank.norm("la la"), q), rank.EXACT)
        self.assertEqual(rank.match_tier(rank.norm("The Curse of La Llorona"), q), rank.ALL_WORDS)
        self.assertIsNone(rank.match_tier(rank.norm("La Haine"), rank.norm("la la land")))
        self.assertEqual(rank.match_tier(rank.norm("Girl, Interrupted"), "inter"), rank.WORD_PREFIX)
        self.assertEqual(rank.match_tier(rank.norm("Winterfell"), "inter"), rank.SUBSTRING)
        self.assertEqual(rank.match_tier(rank.norm("Wars of the Stars"), "star wars"), rank.ALL_WORDS)

    def test_leading_article_on_name(self):
        self.assertEqual(rank.match_tier(rank.norm("The Office"), "office"), rank.EXACT)

    def test_accents_and_punctuation(self):
        self.assertEqual(rank.match_tier(rank.norm("Amélie"), rank.norm("amelie")), rank.EXACT)
        self.assertEqual(rank.match_tier(rank.norm("Spider-Man"), rank.norm("spider man")), rank.EXACT)


class Rerank(unittest.TestCase):
    """Cases taken from the real library, 2026-09-30."""

    def test_la_la(self):
        emby = [it("Person", "La La Anthony"), it("Movie", "La Haine"),
                it("Studio", "La Cinéfacture"), it("Movie", "La La Land"),
                it("Episode", "L.A. Story")]
        self.assertEqual(rank.rerank(emby, "La La")[0]["Name"], "La La Land")

    def test_the_office_show_beats_tag(self):
        emby = [it("Tag", "office"), it("Movie", "Office Space"),
                it("Series", "The Office (US)"), it("Person", "Charles Officer")]
        self.assertEqual(names(rank.rerank(emby, "The Office"))[:2],
                         ["The Office (US)", "Office Space"])

    def test_harry_movies_beat_people(self):
        emby = [it("Movie", "Dirty Harry"), it("Person", "Harry Townes"),
                it("Person", "Harry Wallace"),
                it("Movie", "Harry Potter and the Chamber of Secrets")]
        out = names(rank.rerank(emby, "harry"))
        self.assertEqual(out[0], "Harry Potter and the Chamber of Secrets")
        self.assertEqual(out[1], "Dirty Harry")

    def test_tags_sink_below_word_matches(self):
        emby = [it("Tag", "internship"), it("Tag", "internet"),
                it("Movie", "The Intern"), it("Movie", "Interstellar")]
        # both movies are prefix matches (tie keeps Emby's order); tags go last
        self.assertEqual(sorted(names(rank.rerank(emby, "inter"))[:2]), ["Interstellar", "The Intern"])

    def test_exact_person_still_first_when_nothing_titled_that(self):
        emby = [it("Movie", "Cast Away"), it("Person", "Tom Hanks")]
        self.assertEqual(rank.rerank(emby, "tom hanks")[0]["Name"], "Tom Hanks")

    def test_original_title_counts(self):
        emby = [it("Person", "Society Smith"),
                it("Movie", "Society of the Snow", OriginalTitle="La sociedad de la nieve")]
        self.assertEqual(rank.rerank(emby, "la sociedad")[0]["Name"], "Society of the Snow")

    def test_stable_on_ties(self):
        emby = [it("Movie", "Star Wars: A"), it("Movie", "Star Wars: B"), it("Movie", "Star Wars: C")]
        self.assertEqual(names(rank.rerank(emby, "star wars")), names(emby))

    def test_unknown_type_and_missing_fields(self):
        emby = [{"Type": "SomethingNew"}, {"Name": "No type"}, it("Movie", "Match")]
        self.assertEqual(rank.rerank(emby, "match")[0]["Name"], "Match")

    def test_empty(self):
        self.assertEqual(rank.rerank([], "x"), [])


class OrderTypes(unittest.TestCase):
    def test_la_la_rows(self):
        emby = [{"Name": n} for n in ["Studio", "Tag", "Person", "Series", "Episode", "Movie"]]
        self.assertEqual([t["Name"] for t in rank.order_types(emby)],
                         ["Movie", "Series", "Episode", "Person", "Studio", "Tag"])

    def test_unknown_types_last_and_stable(self):
        emby = [{"Name": "Zzz"}, {"Name": "Tag"}, {"Name": "Aaa"}, {"Name": "Movie"}]
        self.assertEqual([t["Name"] for t in rank.order_types(emby)], ["Movie", "Tag", "Zzz", "Aaa"])


if __name__ == "__main__":
    unittest.main()
