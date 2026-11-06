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
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.mdblist.com/imdb/movie?apikey={key}"
BATCH_SIZE = 100            # MDBList allows up to 200; 100 matches what established clients use
REFRESH_DAYS = 30           # re-check a movie's ratings after this long
MAX_REQUESTS = int(os.environ.get("MDBLIST_MAX_REQUESTS", "400"))  # free plan: 1,000/day
CACHE_PATH = Path("ratings/mdblist.json")


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


def parse_item(item: dict, today: str) -> dict:
    """Keep only what the catalogs use. Letterboxd on its 0-5 scale, Metacritic 0-100."""
    entry: dict = {"d": today}
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


def _post(key: str, ids: list[str]) -> list[dict]:
    body = json.dumps({"ids": ids}).encode()
    req = urllib.request.Request(API_URL.format(key=key), data=body, method="POST", headers={
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
    # Movies never fetched come first (most-voted first), then the oldest cached entries.
    missing = [m for m in sorted(movies, key=lambda m: -m["votes"]) if m["id"] not in cache]
    stale = sorted((m for m in movies if m["id"] in cache and cache[m["id"]]["d"] < stale_before),
                   key=lambda m: cache[m["id"]]["d"])
    queue = [m["id"] for m in missing + stale]
    log(f"  {len(cache)} cached, {len(missing)} new, {len(stale)} stale; "
        f"budget {MAX_REQUESTS} requests x {BATCH_SIZE}")

    requests = fetched = 0
    for start in range(0, len(queue), BATCH_SIZE):
        if requests >= MAX_REQUESTS:
            log("  request budget used up; the rest will be fetched on later runs")
            break
        batch = queue[start:start + BATCH_SIZE]
        for attempt in range(3):
            try:
                items = _post(key, batch)
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
                cache[imdb_id] = {"d": stamp}
        fetched += len(batch)
        time.sleep(0.5)

    save_cache(cache, cache_path)
    left = getattr(_post, "remaining", "?")
    log(f"  fetched {fetched} movies in {requests} requests ({left} requests left today); "
        f"{sum(1 for v in cache.values() if 'lb' in v)} have Letterboxd, "
        f"{sum(1 for v in cache.values() if 'mc' in v)} have Metacritic")
    return cache
