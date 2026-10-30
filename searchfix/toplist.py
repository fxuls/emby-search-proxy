"""Home Screen Companion top-lists: the rank number stays on the home row.

HSC builds each ranked row ("Top 10 this week") from a hidden library of .strm
copies, one per title, with the rank number drawn into the copy's own poster.
Opening a card on that row opens the copy, so the detail page shows the
number too, and the copy's item id changes whenever HSC rebuilds it (an app
holding an old row then gets "item not found").

For home rows (GET .../Users/{u}/Sections/{id}/Items) we swap every copy for
the real title:
  * on a top-list row (the section's ParentItem is an HSC library) the card
    keeps the copy's numbered Primary/Thumb through a marker image tag,
    "hsc<copyId>_<copyTag>"; an image request carrying that tag is served from
    the copy (see image_target);
  * on any other row (Continue Watching, ...) the real title is shown as is.
Tapping the card then opens the real title, with its own poster and an id that
doesn't change every night.

CopyMap is rebuilt in a background thread from Emby's library list: a copy is
any Movie/Series whose library location is under HSC's plugin folder, and its
original is the item with the same IMDb (else TMDb) id in a normal library.
Until the first build finishes, nothing is rewritten.
"""
import json
import re
import threading
import time
import urllib.parse
import urllib.request

HSC_MARKER = "/plugins/homescreencompanion/"
TYPES = "Movie,Series"

_TAG_RE = re.compile(r"hsc(\d+)_(.+)")
_SECTION_RE = re.compile(r"^(?P<prefix>.*)/users/(?P<user>[^/]+)/sections/(?P<section>[^/]+)/items/?$", re.I)
_IMAGE_RE = re.compile(r"^(?P<pre>.*/items/)(?P<id>[^/]+)(?P<post>/images/.*)$", re.I)


def make_tag(copy_id, copy_tag):
    return f"hsc{copy_id}_{copy_tag}"


def parse_tag(tag):
    """(copy_id, copy_tag) for a marker tag, else None."""
    m = _TAG_RE.fullmatch(tag or "")
    return (m.group(1), m.group(2)) if m else None


def section_request(method, path):
    """(path prefix, user id, section id) for a home-row request, else None."""
    if method != "GET":
        return None
    m = _SECTION_RE.match(path)
    return (m.group("prefix"), m.group("user"), m.group("section")) if m else None


def image_target(method, path, pairs):
    """For an image request with a marker tag: (copy path, query pairs for the
    copy, query pairs for the original without the marker). Else None."""
    if method not in ("GET", "HEAD"):
        return None
    tag_vals = [v for k, v in pairs if k.lower() == "tag"]
    parsed = parse_tag(tag_vals[0]) if tag_vals else None
    m = _IMAGE_RE.match(path)
    if not parsed or not m:
        return None
    copy_id, copy_tag = parsed
    copy_pairs = [(k, copy_tag if k.lower() == "tag" else v) for k, v in pairs]
    plain_pairs = [(k, v) for k, v in pairs if k.lower() != "tag"]
    return m.group("pre") + copy_id + m.group("post"), copy_pairs, plain_pairs


def swap_items(items, original_of, originals, numbered):
    """Replace HSC copies in a row with their real titles.

    original_of: copy id -> original id. originals: original id -> item (as
    the user's Emby returned it). numbered: keep the copy's rank artwork.
    A copy whose original wasn't returned stays as it is. A title already in
    the row isn't added twice.
    """
    out, seen = [], set()
    for item in items:
        new = item
        orig = originals.get(original_of(item.get("Id")) or "")
        if orig is not None:
            new = dict(orig)
            if numbered:
                copy_tags = item.get("ImageTags") or {}
                tags = dict(orig.get("ImageTags") or {})
                for kind in ("Primary", "Thumb"):
                    if copy_tags.get(kind):
                        tags[kind] = make_tag(item["Id"], copy_tags[kind])
                new["ImageTags"] = tags
                if "PrimaryImageAspectRatio" in item:
                    new["PrimaryImageAspectRatio"] = item["PrimaryImageAspectRatio"]
        if new.get("Id") in seen:
            continue
        seen.add(new.get("Id"))
        out.append(new)
    return out


class CopyMap:
    def __init__(self, upstream, api_key, refresh_minutes=10.0, log=print):
        self.upstream = upstream.rstrip("/")
        self.api_key = api_key
        self.refresh_s = refresh_minutes * 60
        self.log = log
        self._map = None          # copy id -> original id
        self.libraries = set()    # ids of HSC's hidden libraries
        self.built_at = None

    @property
    def ready(self):
        return self._map is not None

    def __len__(self):
        return len(self._map or {})

    def original(self, item_id):
        return (self._map or {}).get(item_id)

    def _get(self, path, **q):
        q["api_key"] = self.api_key
        url = f"{self.upstream}{path}?{urllib.parse.urlencode(q)}"
        with urllib.request.urlopen(url, timeout=120) as r:
            return json.load(r)

    def _items(self, library_id):
        return self._get("/emby/Items", ParentId=library_id, Recursive="true",
                         IncludeItemTypes=TYPES, Fields="ProviderIds,Path",
                         EnableImages="false", EnableUserData="false").get("Items", [])

    def build(self):
        folders = self._get("/emby/Library/VirtualFolders")
        hsc, normal = set(), []
        for f in folders:
            locs = [loc.lower().replace("\\", "/") for loc in f.get("Locations") or []]
            if any(HSC_MARKER in loc for loc in locs):
                hsc.add(f.get("ItemId"))
            elif locs:
                normal.append(f.get("ItemId"))
        by_key = {}
        for lib in normal:
            for i in self._items(lib):
                for key in _keys(i):
                    by_key.setdefault(key, i["Id"])
        mapping = {}
        for lib in hsc:
            for i in self._items(lib):
                orig = next((by_key[k] for k in _keys(i) if k in by_key), None)
                if orig and orig != i["Id"]:
                    mapping[i["Id"]] = orig
        self._map, self.libraries = mapping, hsc   # swap; requests keep the old ones
        self.built_at = time.time()
        self.log(f"toplist: {len(mapping)} copies in {len(hsc)} HSC libraries")

    def start(self):
        def loop():
            while True:
                try:
                    self.build()
                except Exception as e:
                    self.log(f"toplist: build failed ({e!r})")
                time.sleep(self.refresh_s)
        threading.Thread(target=loop, name="toplist", daemon=True).start()
        return self


def _keys(item):
    ids = {k.lower(): v for k, v in (item.get("ProviderIds") or {}).items() if v}
    return [(item.get("Type"), src, ids[src]) for src in ("imdb", "tmdb") if src in ids]
