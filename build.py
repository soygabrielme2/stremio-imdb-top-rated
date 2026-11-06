#!/usr/bin/env python3
"""Build a static Stremio catalog addon of the highest-rated IMDb movies.

Downloads IMDb's official datasets (https://developer.imdb.com/non-commercial-datasets/),
ranks movies by IMDb rating, and writes plain JSON files that any static host can serve.
Stremio only needs the files; there is no server logic.

Usage:
    python build.py                 # download (if stale) and build into ./dist
    python build.py --base-url URL  # also bake the public URL into dist/index.html
"""
from __future__ import annotations

import argparse
import gzip
import itertools
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

import ratings

DATASET_URL = "https://datasets.imdbws.com/"
DATASETS = ("title.ratings.tsv.gz", "title.basics.tsv.gz")
MAX_AGE_HOURS = 20          # IMDb refreshes the files daily
PAGE_SIZE = 100             # Stremio asks for the next page with skip=<items so far>
MIN_GENRE_ITEMS = 20        # hide genres too small to be worth a filter option
POSTER = "https://images.metahub.space/poster/medium/{}/img"

# Each catalog becomes one row on the Stremio board, filterable by genre and year in Discover.
#   kind:         how the catalog ranks movies (see POOLS)
#   min_votes:    IMDb votes a movie needs to be listed at all
#   weight_votes: how hard low-vote movies are pulled toward the average (see weighted_sort)
#   limit:        length of the row and of genre-only lists; year-filtered lists show everything
# Catalogs that need Letterboxd/Metacritic data are left out until enough of it is cached.
CATALOGS = [
    {
        "id": "imdb-top-rated",
        "name": "IMDb Top Rated",
        "kind": "imdb",
        "min_votes": 10_000,
        "max_votes": None,
        "weight_votes": 25_000,
        "min_rating": 0.0,
        "limit": 1000,
    },
    {
        "id": "imdb-hidden-gems",
        "name": "IMDb Hidden Gems",
        "kind": "imdb",
        "min_votes": 5_000,
        "max_votes": 25_000,
        "weight_votes": 5_000,
        "min_rating": 7.5,
        "limit": 500,
    },
    {
        # Letterboxd's film-enthusiast audience rates international, older and arthouse films
        # more fairly than IMDb and is hard to brigade.
        "id": "letterboxd-top-rated",
        "name": "Letterboxd Top Rated",
        "kind": "letterboxd",
        "min_votes": 10_000,          # IMDb votes, so the pool matches IMDb Top Rated
        "min_lb_votes": 1_000,
        "weight_votes": 10_000,       # Letterboxd ratings
        "limit": 1000,
    },
    {
        # Much better liked on Letterboxd than on IMDb, relative to everything else.
        "id": "underrated-on-imdb",
        "name": "Underrated on IMDb",
        "kind": "underrated",
        "min_votes": 5_000,
        "min_lb_votes": 1_000,
        "weight_votes": 10_000,
        "min_lb_percentile": 0.80,    # top 20% on Letterboxd...
        "min_gap": 0.20,              # ...and at least 20 percentile points lower on IMDb
        "min_imdb_rating": 6.0,       # keeps out fan-brigaded Letterboxd scores (e.g. concert films)
        "limit": 500,
    },
    {
        # Loved by professional critics, seen by relatively few IMDb users.
        "id": "critics-darlings",
        "name": "Critics' Darlings",
        "kind": "critics",
        "min_votes": 5_000,
        "max_votes": 100_000,
        "min_metacritic": 75,
        "min_critics": 10,            # Metacritic reviews, so one rave can't carry a film
        "limit": 500,
    },
]
MIN_CATALOG_ITEMS = 50       # don't publish a ratings-based row until it has this many movies

MANIFEST = {
    "id": "personal.imdb.top-rated",
    "version": "1.2.0",
    "name": "IMDb Top Rated",
    "description": "Movies ranked by IMDb, Letterboxd and critics, filterable by genre and year, "
                   "rebuilt daily. Streams come from your other addons.",
    "resources": ["catalog"],
    "types": ["movie"],
    "idPrefixes": ["tt"],
    "behaviorHints": {"configurable": False},
}


def log(msg: str) -> None:
    print(msg, flush=True)


def download(data_dir: Path, force: bool) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in DATASETS:
        target = data_dir / name
        fresh = target.exists() and time.time() - target.stat().st_mtime < MAX_AGE_HOURS * 3600
        if fresh and not force:
            log(f"  {name}: cached")
            continue
        log(f"  {name}: downloading...")
        req = urllib.request.Request(DATASET_URL + name, headers={"User-Agent": "imdb-stremio-catalog"})
        tmp = target.with_suffix(".part")
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out, 1 << 20)
        tmp.replace(target)
        log(f"  {name}: {target.stat().st_size / 1e6:.0f} MB")


def rows(path: Path):
    """Yield split TSV rows. IMDb's TSVs are unquoted, so plain split is correct."""
    with gzip.open(path, "rt", encoding="utf-8", newline="\n") as fh:
        next(fh)  # header
        for line in fh:
            yield line.rstrip("\n").split("\t")


def load_movies(data_dir: Path) -> list[dict]:
    floor = min(c["min_votes"] for c in CATALOGS)
    ratings: dict[str, tuple[float, int]] = {}
    for tconst, rating, votes in rows(data_dir / "title.ratings.tsv.gz"):
        v = int(votes)
        if v >= floor:
            ratings[tconst] = (float(rating), v)

    movies = []
    for r in rows(data_dir / "title.basics.tsv.gz"):
        tconst, title_type, title, _orig, is_adult, year, _end, _runtime, genres = r[:9]
        if title_type != "movie" or is_adult != "0" or tconst not in ratings:
            continue
        rating, votes = ratings[tconst]
        movies.append({
            "id": tconst,
            "name": title,
            "year": "" if year == "\\N" else year,
            "genres": [] if genres == "\\N" else genres.split(","),
            "rating": rating,
            "votes": votes,
        })
    return movies


def short_count(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def meta(m: dict) -> dict:
    parts = [f"IMDb {m['rating']:.1f} ({short_count(m['votes'])} votes)"]
    if "lb" in m:
        parts.append(f"Letterboxd {m['lb']:.1f}/5")
    if "mc" in m:
        parts.append(f"Metacritic {m['mc']}")
    return {
        "id": m["id"],
        "type": "movie",
        "name": m["name"],
        "poster": POSTER.format(m["id"]),
        "posterShape": "poster",
        "releaseInfo": m["year"],
        "imdbRating": f"{m['rating']:.1f}",
        "genres": m["genres"],
        "description": " · ".join(parts),
    }


def weighted_sort(pool: list[dict], score: str, votes: str, weight: int, mean: float) -> None:
    """Sort best first by IMDb's Top 250 style weighted rating.

    weighted = (v / (v + m)) * R + (m / (v + m)) * C, where R is the movie's score, v its votes,
    m the catalog's weight_votes and C the mean score. A 9.8 from 5,000 votes is pulled toward
    the mean much harder than a 9.3 from 3 million, which stops vote-brigaded titles from
    topping the list. The displayed rating is still the movie's real rating.
    """
    pool.sort(key=lambda m: -(m[votes] * m[score] + weight * mean) / (m[votes] + weight))


def imdb_pool(movies: list[dict], cat: dict) -> list[dict]:
    in_range = [m for m in movies
                if m["votes"] >= cat["min_votes"]
                and (cat["max_votes"] is None or m["votes"] < cat["max_votes"])]
    if not in_range:
        return []
    # Mean over every movie in the vote range, before the rating cut, so it stays a true average.
    mean = sum(m["rating"] for m in in_range) / len(in_range)
    pool = [m for m in in_range if m["rating"] >= cat["min_rating"]]
    weighted_sort(pool, "rating", "votes", cat["weight_votes"], mean)
    return pool


def letterboxd_candidates(movies: list[dict], cat: dict) -> list[dict]:
    return [m for m in movies
            if m["votes"] >= cat["min_votes"] and m.get("lbv", 0) >= cat["min_lb_votes"]]


def letterboxd_pool(movies: list[dict], cat: dict) -> list[dict]:
    pool = letterboxd_candidates(movies, cat)
    if not pool:
        return []
    weighted_sort(pool, "lb", "lbv", cat["weight_votes"], sum(m["lb"] for m in pool) / len(pool))
    return pool


def percentiles(pool: list[dict], key: str) -> dict[str, float]:
    """Each movie's share of the pool scoring at or below it (0 = worst, 1 = best)."""
    ordered = sorted(pool, key=lambda m: m[key])
    n = len(ordered)
    out: dict[str, float] = {}
    i = 0
    while i < n:                       # equal scores share the same percentile
        j = i
        while j < n and ordered[j][key] == ordered[i][key]:
            j += 1
        for m in ordered[i:j]:
            out[m["id"]] = j / n
        i = j
    return out


def underrated_pool(movies: list[dict], cat: dict) -> list[dict]:
    """Top-tier on Letterboxd but ranked much lower on IMDb, compared within the same pool.

    Percentiles rather than raw scores, because the sites use different scales and audiences.
    """
    pool = letterboxd_candidates(movies, cat)
    if not pool:
        return []
    lb_pct, imdb_pct = percentiles(pool, "lb"), percentiles(pool, "rating")
    mean = sum(m["lb"] for m in pool) / len(pool)
    picked = [m for m in pool
              if lb_pct[m["id"]] >= cat["min_lb_percentile"]
              and lb_pct[m["id"]] - imdb_pct[m["id"]] >= cat["min_gap"]
              and m["rating"] >= cat["min_imdb_rating"]]
    weighted_sort(picked, "lb", "lbv", cat["weight_votes"], mean)
    return picked


def critics_pool(movies: list[dict], cat: dict) -> list[dict]:
    pool = [m for m in movies
            if cat["min_votes"] <= m["votes"] < cat["max_votes"]
            and m.get("mc", 0) >= cat["min_metacritic"] and m.get("mcv", 0) >= cat["min_critics"]]
    pool.sort(key=lambda m: (-m["mc"], -m["mcv"], -m["rating"]))
    return pool


POOLS = {"imdb": imdb_pool, "letterboxd": letterboxd_pool,
         "underrated": underrated_pool, "critics": critics_pool}


def year_matches(year: str, option: str) -> bool:
    """`option` is a single year ("2024") or a decade ("2020s")."""
    if option.endswith("s"):
        return year[:3] == option[:3]
    return year == option


def year_options(pool: list[dict]) -> list[str]:
    years = sorted({m["year"] for m in pool if m["year"]}, reverse=True)
    decades = sorted({y[:3] + "0s" for y in years}, reverse=True)
    return decades + years


def write_json(path: Path, obj) -> None:
    if sys.platform == "win32":
        # Genre+year file names can push deep folders past Windows' 260-character path limit.
        path = Path("\\\\?\\" + str(path.resolve()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def write_list(folder: Path, cat_id: str, items: list[dict], filters: dict[str, str]) -> int:
    """Write every file name Stremio may request for one filtered list.

    Stremio requests /catalog/movie/<id>.json for the unfiltered first page and
    /catalog/movie/<id>/<extra>.json otherwise. Whichever filter was picked last comes first
    (genre=X&year=Y or year=Y&genre=X), and later pages put skip in front: skip=100&genre=X.
    Stremio keeps asking for more until it gets an empty page, using skip=<movies loaded so
    far>, so a 17-movie list is followed by a request for skip=17. That final empty page is
    written too, so the list ends cleanly instead of with a 404.
    """
    parts = [f"{k}={v}" for k, v in filters.items()]
    orders = ["&".join(p) for p in itertools.permutations(parts)] if parts else [""]
    starts = list(range(0, len(items), PAGE_SIZE)) + [len(items)] if items else [0]
    count = 0
    for start in starts:
        body = {"metas": [meta(m) for m in items[start:start + PAGE_SIZE]]}
        if start == 0:
            if parts:
                names = orders
            else:
                write_json(folder / f"{cat_id}.json", body)
                count += 1
                continue
        else:
            names = [f"skip={start}" + (f"&{o}" if o else "") for o in orders]
        for name in names:
            write_json(folder / cat_id / f"{name}.json", body)
            count += 1
    return count


def build(movies: list[dict], out: Path, base_url: str | None) -> None:
    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    folder = tmp / "catalog" / "movie"
    manifest = dict(MANIFEST, catalogs=[])
    files = 0

    for cat in CATALOGS:
        pool = POOLS[cat["kind"]](movies, cat)
        if cat["kind"] != "imdb" and len(pool) < MIN_CATALOG_ITEMS:
            log(f"  {cat['name']}: skipped, only {len(pool)} movies have the ratings it needs")
            continue
        limit = cat["limit"]
        row = pool[:limit]
        files += write_list(folder, cat["id"], row, {})

        genre_counts: dict[str, int] = {}
        for m in pool:
            for g in m["genres"]:
                genre_counts[g] = genre_counts.get(g, 0) + 1
        genres = sorted(g for g, n in genre_counts.items() if n >= MIN_GENRE_ITEMS)
        years = year_options(pool)

        by_genre = {g: [m for m in pool if g in m["genres"]] for g in genres}
        for g in genres:
            files += write_list(folder, cat["id"], by_genre[g][:limit], {"genre": g})
        for y in years:
            in_year = [m for m in pool if year_matches(m["year"], y)]
            files += write_list(folder, cat["id"], in_year, {"year": y})
            for g in genres:
                both = [m for m in by_genre[g] if year_matches(m["year"], y)]
                files += write_list(folder, cat["id"], both, {"genre": g, "year": y})

        manifest["catalogs"].append({
            "type": "movie",
            "id": cat["id"],
            "name": cat["name"],
            "extra": [
                {"name": "genre", "options": genres},
                {"name": "year", "options": years},
                {"name": "skip"},
            ],
        })
        top = row[0] if row else None
        log(f"  {cat['name']}: {len(row)} in row, {len(pool)} total, {len(genres)} genres, "
            f"{len(years)} year options" + (f", #1 = {top['name']} ({top['year']})" if top else ""))

    write_json(tmp / "manifest.json", manifest)
    (tmp / ".nojekyll").write_text("", encoding="utf-8")
    url = (base_url.rstrip("/") + "/manifest.json") if base_url else "manifest.json"
    (tmp / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>IMDb Top Rated for Stremio</title>"
        "<body style='font-family:sans-serif;max-width:40em;margin:3em auto'>"
        "<h1>IMDb Top Rated</h1><p>Personal Stremio catalog addon. In Stremio, open "
        f"Addons and paste this URL into the search box:</p><p><code>{url}</code></p>"
        f"<p>Built {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}.</p>",
        encoding="utf-8")

    shutil.rmtree(out, ignore_errors=True)
    tmp.replace(out)
    log(f"  wrote {files} catalog files to {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=Path("data"), help="where IMDb files are cached")
    ap.add_argument("--out", type=Path, default=Path("dist"), help="output folder to host")
    ap.add_argument("--base-url", help="public URL the dist folder is served from")
    ap.add_argument("--force-download", action="store_true")
    args = ap.parse_args()

    log("Fetching IMDb datasets")
    download(args.data, args.force_download)
    log("Ranking movies")
    movies = load_movies(args.data)
    log(f"  {len(movies)} movies with enough votes")
    log("Updating Letterboxd/Metacritic ratings")
    cache = ratings.update(movies, log)
    for m in movies:
        r = cache.get(m["id"])
        if r:
            m.update({k: v for k, v in r.items() if k in ("lb", "lbv", "mc", "mcv")})
    log("Writing addon")
    build(movies, args.out, args.base_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
