"""Search ranking rules. Pure functions, no I/O, so they can be unit-tested.

Two jobs:
  * order_types(): the order of the per-type rows on the search page.
  * rerank():      the order of the items inside a result list.

An item's score is match tier + type penalty; lower ranks higher. Ties keep
Emby's original order.
"""
import re
import unicodedata

# Row order on the search page, as chosen by the user 2026-09-30:
# TV shows, Movies, Collections, Genres, Studios, People, Episodes, Live TV,
# Tags. Media types not in the library yet (music, books, ...) sit between
# Live TV and Tags until someone decides otherwise; unknown types go last.
TYPE_ORDER = [
    "Series", "Movie", "BoxSet",
    "Genre", "MusicGenre", "GameGenre", "Studio",
    "Person",
    "Episode",
    "Program", "TvChannel",
    "Trailer", "MusicArtist", "MusicAlbum", "Audio", "MusicVideo", "Book", "Playlist",
    "Tag",
]

# Added to the match tier (tiers are 10 apart). Same order as the rows, but
# match quality dominates: within the main media types (spread 0-5) a better
# title match always wins; people/studios/episodes only beat media when they
# match clearly better (e.g. an exact person name vs a movie found via cast).
TYPE_PENALTY = {
    "Series": 0, "Movie": 1,
    "BoxSet": 3,
    "Trailer": 5, "MusicArtist": 5, "MusicAlbum": 5, "Audio": 5,
    "MusicVideo": 5, "Book": 5, "Playlist": 5,
    "Genre": 14, "MusicGenre": 14, "GameGenre": 14,
    "Studio": 15,
    "Person": 16,
    "Episode": 18,
    "Program": 20, "TvChannel": 20,
    "Tag": 25,
}
DEFAULT_PENALTY = 20

# Match tiers (see match_tier). NO_MATCH covers items Emby matched on some
# field we can't see (overview, cast), so they still rank, just last.
EXACT, PREFIX, WORD_PREFIX, SUBSTRING, ALL_WORDS, NO_MATCH = 0, 10, 20, 30, 35, 40

# Matching on OriginalTitle/SortName, or on the query without its leading
# article, is slightly weaker evidence than matching the display name.
ALT_FIELD_PENALTY = 3
BARE_QUERY_PENALTY = 5

_ARTICLE = re.compile(r"^(the|a|an) ")


def norm(s):
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def match_tier(name, q):
    """How well normalised name matches normalised query q; None if not at all."""
    if not name or not q:
        return None
    bare = _ARTICLE.sub("", name)
    if name == q or bare == q:
        return EXACT
    if name.startswith(q) or bare.startswith(q):
        return PREFIX
    if (" " + q) in (" " + name):
        return WORD_PREFIX
    if q in name:
        return SUBSTRING
    words = name.split()
    if all(any(w.startswith(t) for w in words) for t in q.split()):
        return ALL_WORDS
    return None


def score(item, q, q_bare):
    best = NO_MATCH
    for field, extra in (("Name", 0), ("OriginalTitle", ALT_FIELD_PENALTY),
                         ("SortName", ALT_FIELD_PENALTY)):
        name = norm(item.get(field))
        for query, qextra in ((q, 0), (q_bare, BARE_QUERY_PENALTY)):
            t = match_tier(name, query)
            if t is not None:
                best = min(best, t + extra + qextra)
    return best + TYPE_PENALTY.get(item.get("Type"), DEFAULT_PENALTY)


def rerank(items, term):
    """Return items sorted best-first for search term; stable on ties."""
    q = norm(term)
    q_bare = _ARTICLE.sub("", q)
    keyed = sorted(((score(it, q, q_bare), i) for i, it in enumerate(items)))
    return [items[i] for _, i in keyed]


def type_rank(name):
    try:
        return TYPE_ORDER.index(name)
    except ValueError:
        return len(TYPE_ORDER)


def order_types(type_items):
    """Sort /ItemTypes entries ({"Name": "Movie", ...}) into row order."""
    return sorted(type_items, key=lambda t: type_rank(t.get("Name")))
