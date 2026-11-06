"""Letterboxd and Metacritic ratings via the MDBList API, cached in ratings/mdblist.json.

MDBList (https://mdblist.com) aggregates ratings from several sites and looks movies up by IMDb ID.
The cache file is committed back to the repo by the GitHub workflow, so each daily run only asks
MDBList about new movies and the stalest cached entries. Without an API key, the build just uses
whatever is already cached.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.mdblist.com/imdb/{kind}?apikey={key}"
MDBLIST_KINDS = {"movie": "movie", "series": "show"}   # Stremio type -> MDBList media type
BATCH_SIZE = 100            # MDBList allows up to 200; 100 matches what established clients use
REFRESH_DAYS = 30           # re-check a movie's ratings after this long
MAX_REQUESTS = int(os.environ.get("MDBLIST_MAX_REQUESTS", "400"))  # free plan: 1,000/day
CACHE_PATH = Path("ratings/mdblist.json")
CACHE_VERSION = 2           # bump when parse_item keeps new fields; older entries get refetched
MAX_PLOT_CHARS = 350        # plots are copied into many list files, so keep them short


def load_cache(path: Path = CACHE_PATH) -> dict[str, dict]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_cache(cache: dict[str, dict], path: Path = CACHE_PATH) -> None:
    """One movie per line, sorted, so daily git diffs stay small."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{json.dumps(k)}:{json.dumps(cache[k], separators=(',', ':'))}" for k in sorted(cache)]
    path.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")


def _source(item: dict, name: str) -> dict | None:
    for r in item.get("ratings") or []:
        if r.get("source") == name:
            return r
    return None


def short_plot(text: str | None) -> str | None:
    """Trim to MAX_PLOT_CHARS at a sentence end if possible, else a word end."""
    text = " ".join((text or "").split())
    if not text:
        return None
    if len(text) <= MAX_PLOT_CHARS:
        return text
    cut = text[:MAX_PLOT_CHARS]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= MAX_PLOT_CHARS // 2:
        return cut[:end + 1]
    return cut[:cut.rfind(" ")].rstrip(",;:") + "…"


def youtube_id(url: str | None) -> str | None:
    """MDBList gives trailers as YouTube URLs; Stremio wants the bare video id."""
    if not url:
        return None
    m = re.search(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def parse_item(item: dict, today: str) -> dict:
    """Keep only what the catalogs use. Letterboxd on its 0-5 scale, Metacritic 0-100."""
    entry: dict = {"d": today, "v": CACHE_VERSION}
    plot = short_plot(item.get("description"))
    if plot:
        entry["plot"] = plot
    if isinstance(item.get("runtime"), int) and item["runtime"] > 0:
        entry["min"] = item["runtime"]
    trailer = youtube_id(item.get("trailer"))
    if trailer:
        entry["yt"] = trailer
    lb = _source(item, "letterboxd")
    if lb and (lb.get("score") is not None or lb.get("value") is not None):
        if lb.get("score") is not None:
            stars = lb["score"] / 20               # score is normalized to 0-100
        else:
            stars = float(lb["value"])
            if stars > 5:                          # batch responses can report it out of 10
                stars /= 2
        if stars > 0:
            entry["lb"] = round(stars, 2)
            entry["lbv"] = lb.get("votes") or 0
    mc = _source(item, "metacritic")
    if mc and mc.get("value"):
        entry["mc"] = int(mc["value"])
        entry["mcv"] = mc.get("votes") or 0        # number of critic reviews
    return entry


def _post(key: str, kind: str, ids: list[str]) -> list[dict]:
    body = json.dumps({"ids": ids}).encode()
    url = API_URL.format(kind=MDBLIST_KINDS[kind], key=key)
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "imdb-stremio-catalog"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
        remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining is not None:
        _post.remaining = remaining  # type: ignore[attr-defined]
    if not isinstance(data, list):
        raise ValueError(f"unexpected MDBList response: {str(data)[:200]}")
    return data


def update(movies: list[dict], log, cache_path: Path = CACHE_PATH) -> dict[str, dict]:
    """Refresh the cache for `movies` within the daily request budget, then return it."""
    cache = load_cache(cache_path)
    key = os.environ.get("MDBLIST_API_KEY", "").strip()
    if not key:
        log(f"  no MDBLIST_API_KEY set; using {len(cache)} cached ratings")
        return cache

    today = dt.date.today()
    stale_before = (today - dt.timedelta(days=REFRESH_DAYS)).isoformat()
    # Never-fetched movies first, then entries saved by an older version of parse_item, then
    # the oldest cached entries. Most-voted first within each group.
    by_votes = sorted(movies, key=lambda m: -m["votes"])
    missing = [m for m in by_votes if m["id"] not in cache]
    outdated = [m for m in by_votes
                if m["id"] in cache and cache[m["id"]].get("v", 1) < CACHE_VERSION]
    stale = sorted((m for m in movies if m["id"] in cache
                    and cache[m["id"]].get("v", 1) >= CACHE_VERSION
                    and cache[m["id"]]["d"] < stale_before),
                   key=lambda m: cache[m["id"]]["d"])
    queue = missing + outdated + stale
    log(f"  {len(cache)} cached, {len(missing)} new, {len(outdated)} outdated, {len(stale)} stale; "
        f"budget {MAX_REQUESTS} requests x {BATCH_SIZE}")

    # MDBList looks movies and shows up through different endpoints, so batch each type apart
    # (series first: there are few of them), keeping the priority order within each type.
    batches = []
    for kind in ("series", "movie"):
        ids = [m["id"] for m in queue if m.get("type", "movie") == kind]
        batches += [(kind, ids[i:i + BATCH_SIZE]) for i in range(0, len(ids), BATCH_SIZE)]

    requests = fetched = 0
    for kind, batch in batches:
        if requests >= MAX_REQUESTS:
            log("  request budget used up; the rest will be fetched on later runs")
            break
        for attempt in range(3):
            try:
                items = _post(key, kind, batch)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    log("  MDBList daily limit reached; saving progress")
                    save_cache(cache, cache_path)
                    return cache
                if e.code in (401, 403):
                    log(f"  MDBList rejected the API key (HTTP {e.code}); skipping ratings update")
                    return cache
                err = e
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                err = e
            time.sleep(5 * (attempt + 1))
        else:
            log(f"  MDBList request failed 3 times ({err}); saving progress")
            break
        requests += 1
        stamp = today.isoformat()
        seen = set()
        for item in items:
            imdb_id = (item.get("ids") or {}).get("imdb")
            if imdb_id in batch:
                cache[imdb_id] = parse_item(item, stamp)
                seen.add(imdb_id)
        for imdb_id in batch:            # unknown to MDBList: remember, retry after REFRESH_DAYS
            if imdb_id not in seen:
                cache[imdb_id] = {"d": stamp, "v": CACHE_VERSION}
        fetched += len(batch)
        time.sleep(0.5)

    save_cache(cache, cache_path)
    left = getattr(_post, "remaining", "?")
    log(f"  fetched {fetched} titles in {requests} requests ({left} requests left today); "
        f"{sum(1 for v in cache.values() if 'lb' in v)} have Letterboxd, "
        f"{sum(1 for v in cache.values() if 'mc' in v)} have Metacritic")
    return cache
