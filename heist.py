"""Tag heist movies, series and documentaries.

IMDb has no heist genre, so a title counts as a heist title when any of these hold:
  - Wikidata lists its genre as "heist film" (or a subgenre), or its main subject is a robbery
    or a specific real-life robbery. Wikidata links titles to IMDb ids and needs no API key.
  - "heist" appears in its title or plot (plots come from MDBList, see ratings.py).
  - For documentaries only, "robbery" or "robbers" does too: true-crime docs describe real
    heists that way, while in fiction those words also match grave robbers and minor crimes.

The Wikidata ids are cached in ratings/heist-wikidata.json (committed by the workflow), so a
Wikidata outage only means the list isn't refreshed that day.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TAG = "heist"
CACHE_PATH = Path("ratings/heist-wikidata.json")
REFRESH_DAYS = 7
SPARQL_URL = "https://query.wikidata.org/sparql"
USER_AGENT = "stremio-imdb-top-rated/1.0 (https://github.com/soygabrielme2/stremio-imdb-top-rated)"

HEIST_FILM = "wd:Q496523"
ROBBERIES = ("wd:Q53706 wd:Q806824 wd:Q7832904 wd:Q106034988 "   # robbery, bank/train/jewel
             "wd:Q329425 wd:Q1756454")                          # burglary, art theft
QUERIES = (
    f"SELECT DISTINCT ?imdb WHERE {{ ?i wdt:P136/wdt:P279* {HEIST_FILM} ; wdt:P345 ?imdb . }}",
    # Main subject is a kind of robbery, or a specific robbery (e.g. the Gardner Museum theft).
    f"""SELECT DISTINCT ?imdb WHERE {{
          VALUES ?c {{ {ROBBERIES} }}
          {{ ?i wdt:P921 ?c }} UNION {{ ?e wdt:P31 ?c . ?i wdt:P921 ?e }}
          ?i wdt:P345 ?imdb . }}""",
)

HEIST_WORDS = re.compile(r"\bheists?\b", re.I)
DOC_WORDS = re.compile(r"\b(heists?|robbery|robberies|robbers?)\b", re.I)


def _sparql(query: str) -> set[str]:
    url = SPARQL_URL + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/sparql-results+json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        rows = json.load(resp)["results"]["bindings"]
    return {r["imdb"]["value"] for r in rows if r["imdb"]["value"].startswith("tt")}


def wikidata_ids(log, path: Path = CACHE_PATH) -> set[str]:
    cached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"d": "", "ids": []}
    fresh_after = (dt.date.today() - dt.timedelta(days=REFRESH_DAYS)).isoformat()
    if cached["ids"] and cached["d"] > fresh_after:
        log(f"  Wikidata heist titles: {len(cached['ids'])} cached")
        return set(cached["ids"])
    try:
        ids = set().union(*(_sparql(q) for q in QUERIES))
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
        log(f"  Wikidata query failed ({e}); using {len(cached['ids'])} cached heist titles")
        return set(cached["ids"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"d": dt.date.today().isoformat(), "ids": sorted(ids)}, indent=0)
                    + "\n", encoding="utf-8")
    log(f"  Wikidata heist titles: {len(ids)} fetched")
    return ids


def tag(titles: list[dict], log, path: Path = CACHE_PATH) -> None:
    """Add TAG to the "tags" list of every heist title."""
    ids = wikidata_ids(log, path)
    n = 0
    for m in titles:
        words = DOC_WORDS if "Documentary" in m["genres"] else HEIST_WORDS
        if m["id"] in ids or words.search(m["name"] + " " + (m.get("plot") or "")):
            m.setdefault("tags", []).append(TAG)
            n += 1
    log(f"  {n} heist titles tagged")
