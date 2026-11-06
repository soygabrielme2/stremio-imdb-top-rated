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
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

DATASET_URL = "https://datasets.imdbws.com/"
DATASETS = ("title.ratings.tsv.gz", "title.basics.tsv.gz")
MAX_AGE_HOURS = 20          # IMDb refreshes the files daily
PAGE_SIZE = 100             # Stremio asks for the next page with skip=<items so far>
MIN_GENRE_ITEMS = 20        # hide genres too small to be worth a filter option
POSTER = "https://images.metahub.space/poster/medium/{}/img"

# Each catalog becomes one row on the Stremio board, filterable by genre in Discover.
CATALOGS = [
    {
        "id": "imdb-top-rated",
        "name": "IMDb Top Rated",
        "min_votes": 25_000,    # keeps 40-vote 9.6s off the list
        "max_votes": None,
        "min_rating": 0.0,
        "limit": 1000,
    },
    {
        "id": "imdb-hidden-gems",
        "name": "IMDb Hidden Gems",
        "min_votes": 5_000,
        "max_votes": 25_000,
        "min_rating": 7.5,
        "limit": 500,
    },
]

MANIFEST = {
    "id": "personal.imdb.top-rated",
    "version": "1.0.0",
    "name": "IMDb Top Rated",
    "description": "Movies ranked by IMDb rating, rebuilt daily from IMDb's datasets. "
                   "Streams come from your other addons.",
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


def meta(m: dict) -> dict:
    return {
        "id": m["id"],
        "type": "movie",
        "name": m["name"],
        "poster": POSTER.format(m["id"]),
        "posterShape": "poster",
        "releaseInfo": m["year"],
        "imdbRating": f"{m['rating']:.1f}",
        "genres": m["genres"],
        "description": f"IMDb {m['rating']:.1f} from {m['votes']:,} votes",
    }


def ranked_pool(movies: list[dict], cat: dict) -> list[dict]:
    """Movies eligible for a catalog, best first by IMDb's Top 250 style weighted rating.

    weighted = (v / (v + m)) * R + (m / (v + m)) * C, where R is the movie's rating, v its votes,
    m the catalog's vote floor and C the pool's mean rating. A 9.8 from 5,000 votes is pulled
    toward the mean much harder than a 9.3 from 3 million, which stops vote-brigaded titles
    from topping the list. The displayed rating is still the movie's real IMDb rating.
    """
    in_range = [m for m in movies
                if m["votes"] >= cat["min_votes"]
                and (cat["max_votes"] is None or m["votes"] < cat["max_votes"])]
    if not in_range:
        return []
    # Mean over every movie in the vote range, before the rating cut, so it stays a true average.
    mean = sum(m["rating"] for m in in_range) / len(in_range)
    pool = [m for m in in_range if m["rating"] >= cat["min_rating"]]
    floor = cat["min_votes"]
    pool.sort(key=lambda m: -(m["votes"] * m["rating"] + floor * mean) / (m["votes"] + floor))
    return pool


def select(pool: list[dict], cat: dict, genre: str | None = None) -> list[dict]:
    items = [m for m in pool if genre is None or genre in m["genres"]]
    return items[:cat["limit"]]


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def write_pages(folder: Path, cat_id: str, items: list[dict], genre: str | None) -> int:
    """Write every file name Stremio may request for this list.

    Stremio requests /catalog/movie/<id>.json for the first page and
    /catalog/movie/<id>/<extra>.json otherwise, e.g. genre=Drama&skip=100.
    """
    count = 0
    for start in range(0, max(len(items), 1), PAGE_SIZE):
        page = {"metas": [meta(m) for m in items[start:start + PAGE_SIZE]]}
        names = []
        if genre is None:
            names.append(f"skip={start}")
            if start == 0:
                write_json(folder / f"{cat_id}.json", page)
                count += 1
        else:
            names.append(f"genre={genre}&skip={start}")
            names.append(f"skip={start}&genre={genre}")
            if start == 0:
                names.append(f"genre={genre}")
        for n in names:
            write_json(folder / cat_id / f"{n}.json", page)
            count += 1
    # A skip past the end must return an empty list, not a 404.
    end = (len(items) + PAGE_SIZE - 1) // PAGE_SIZE * PAGE_SIZE
    tail = [f"skip={end}"] if genre is None else [f"genre={genre}&skip={end}", f"skip={end}&genre={genre}"]
    for n in tail:
        if not (folder / cat_id / f"{n}.json").exists():
            write_json(folder / cat_id / f"{n}.json", {"metas": []})
            count += 1
    return count


def build(movies: list[dict], out: Path, base_url: str | None) -> None:
    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    catalog_dir = tmp / "catalog" / "movie"
    manifest = dict(MANIFEST, catalogs=[])
    files = 0

    for cat in CATALOGS:
        pool = ranked_pool(movies, cat)
        items = select(pool, cat)
        files += write_pages(catalog_dir, cat["id"], items, None)
        genre_counts: dict[str, int] = {}
        for m in items:
            for g in m["genres"]:
                genre_counts[g] = genre_counts.get(g, 0) + 1
        genres = sorted(g for g, n in genre_counts.items() if n >= MIN_GENRE_ITEMS)
        for g in genres:
            # Genre lists are drawn from the whole pool, not just the top N overall.
            files += write_pages(catalog_dir, cat["id"], select(pool, cat, g), g)
        manifest["catalogs"].append({
            "type": "movie",
            "id": cat["id"],
            "name": cat["name"],
            "extra": [{"name": "genre", "options": genres}, {"name": "skip"}],
        })
        top = items[0] if items else None
        log(f"  {cat['name']}: {len(items)} movies, {len(genres)} genres"
            + (f", #1 = {top['name']} ({top['year']}) {top['rating']}" if top else ""))

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
    log("Writing addon")
    build(movies, args.out, args.base_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
