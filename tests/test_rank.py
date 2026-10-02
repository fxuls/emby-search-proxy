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
        self.assertEqual(rank.match_tier(rank.norm("Girl, Interrupted"), "inter"),
                         rank.WORD_PREFIX + rank.PARTIAL_WORD_PENALTY)
        self.assertEqual(rank.match_tier(rank.norm("Winterfell"), "inter"), rank.SUBSTRING)
        self.assertEqual(rank.match_tier(rank.norm("Wars of the Stars"), "star wars"), rank.ALL_WORDS)

    def test_whole_word_beats_partial_word(self):
        P = rank.PARTIAL_WORD_PENALTY
        self.assertEqual(rank.match_tier("harry potter", "harry"), rank.PREFIX)
        self.assertEqual(rank.match_tier("harryhausen", "harry"), rank.PREFIX + P)
        self.assertEqual(rank.match_tier("jackee harry", "harry"), rank.WORD_PREFIX)
        self.assertEqual(rank.match_tier("ray harryhausen", "harry"), rank.WORD_PREFIX + P)
        # a later whole-word occurrence counts even if an earlier one is partial
        self.assertEqual(rank.match_tier("ray harryhausen harry", "harry"), rank.WORD_PREFIX)
        self.assertEqual(rank.match_tier(rank.norm("Star Wars: The Clone Wars"), "star wars"), rank.PREFIX)

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

    def test_top_results_lean_like_rows(self):
        # equal match quality: show > movie > collection > studio > person > episode > tag
        emby = [it("Tag", "harry"), it("Episode", "Harry"), it("Person", "Harry"),
                it("Studio", "Harry"), it("BoxSet", "Harry"), it("Movie", "Harry"),
                it("Series", "Harry")]
        self.assertEqual([i["Type"] for i in rank.rerank(emby, "harry")],
                         ["Series", "Movie", "BoxSet", "Studio", "Person", "Episode", "Tag"])

    def test_better_title_match_beats_type_order(self):
        # exact movie title beats a show that only starts with the words
        emby = [it("Series", "Fargo Stories"), it("Movie", "Fargo")]
        self.assertEqual(rank.rerank(emby, "fargo")[0]["Type"], "Movie")
        # and a show starting with the query beats a studio exactly named it
        emby = [it("Studio", "Harry"), it("Series", "Harry Wild")]
        self.assertEqual(rank.rerank(emby, "harry")[0]["Type"], "Series")

    def test_people_dont_flood_mixed_list(self):
        # 2026-10-01: "harry" had 112 people (all score 26) ahead of the studio
        # and every episode
        emby = ([it("Movie", f"Harry Potter {n}") for n in range(9)]
                + [it("Person", f"Harry Person{n}") for n in range(40)]
                + [it("Studio", "Bad Hat Harry Productions"),
                   it("Episode", "Harry and Brooks"),
                   it("Episode", "Dirty Harry")])
        out = rank.rerank(emby, "harry")
        self.assertTrue(all(i["Type"] == "Movie" for i in out[:9]))   # franchise stays together
        pos = {i["Name"]: n for n, i in enumerate(out)}
        self.assertLess(pos["Bad Hat Harry Productions"], 25)
        self.assertLess(pos["Harry and Brooks"], 15)
        self.assertLess(pos["Dirty Harry"], 30)
        self.assertEqual(len(out), len(emby))

    def test_media_types_exempt_from_repeat_penalty(self):
        emby = [it("Movie", f"Star Wars {n}") for n in range(12)] + [it("Person", "Star Wars Fan")]
        self.assertEqual(rank.rerank(emby, "star wars")[-1]["Type"], "Person")

    def test_single_type_row_order_is_pure_score(self):
        people = [it("Person", n) for n in ["Ray Harryhausen", "Harry B", "Jackee Harry", "Harry A", "Harryette"]]
        out = names(rank.rerank(people, "harry"))
        self.assertEqual(out, ["Harry B", "Harry A", "Harryette", "Jackee Harry", "Ray Harryhausen"])

    def test_rerank_is_deterministic(self):
        emby = [it("Person", f"Harry {n}") for n in range(30)] + [it("Episode", f"Harry ep {n}") for n in range(30)]
        self.assertEqual(names(rank.rerank(emby, "harry")), names(rank.rerank(list(emby), "harry")))

    def test_empty(self):
        self.assertEqual(rank.rerank([], "x"), [])


class OrderTypes(unittest.TestCase):
    def test_la_la_rows(self):
        emby = [{"Name": n} for n in ["Studio", "Tag", "Person", "Series", "Episode", "Movie"]]
        self.assertEqual([t["Name"] for t in rank.order_types(emby)],
                         ["Series", "Movie", "Studio", "Person", "Episode", "Tag"])

    def test_user_chosen_row_order(self):
        # 2026-09-30: TV shows, Movies, Collections, Genres, Studios, People,
        # Episodes, Live TV, Tags
        want = ["Series", "Movie", "BoxSet", "Genre", "Studio", "Person",
                "Episode", "Program", "TvChannel", "Tag"]
        emby = [{"Name": n} for n in reversed(want)]
        self.assertEqual([t["Name"] for t in rank.order_types(emby)], want)

    def test_unknown_types_last_and_stable(self):
        emby = [{"Name": "Zzz"}, {"Name": "Tag"}, {"Name": "Aaa"}, {"Name": "Movie"}]
        self.assertEqual([t["Name"] for t in rank.order_types(emby)], ["Movie", "Tag", "Zzz", "Aaa"])


if __name__ == "__main__":
    unittest.main()
