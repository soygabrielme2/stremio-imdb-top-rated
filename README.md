# IMDb Top Rated — personal Stremio catalog

Adds two rows to Stremio, ranked by **IMDb rating** using IMDb's own daily data files:

| Row | What's in it |
|---|---|
| **IMDb Top Rated** | Top 1,000 movies with at least 10,000 votes |
| **IMDb Hidden Gems** | Up to 500 movies rated 7.5+ with 5,000–25,000 votes |
| **Letterboxd Top Rated** | Same pool as IMDb Top Rated, ranked by Letterboxd |
| **Underrated on IMDb** | Top 20% on Letterboxd but at least 20 percentile points lower on IMDb |
| **Critics' Darlings** | Metacritic 75+ (10+ reviews) with fewer than 100,000 IMDb votes |

The last three need Letterboxd and Metacritic ratings, which come from the
[MDBList](https://mdblist.com) API and are cached in `ratings/mdblist.json`. They only appear
once the `MDBLIST_API_KEY` repository secret is set (see below).

In Stremio's **Discover** tab, both can be filtered by **Genre** and **Year** (a single year
like 2024, or a decade like 1990s), alone or together, e.g. History + 2024.

Movies are ranked by a weighted rating like IMDb's own Top 250: titles with few votes are
pulled toward the average so a 9.8 from 5,000 votes can't outrank a 9.3 from 3 million.
The rating shown is the movie's real IMDb rating. Clicking a movie works as usual:
Stremio shows its details page and asks your Real-Debrid addon (Torrentio, etc.) for streams.
This addon only supplies the lists.

Change the thresholds in `CATALOGS` at the top of `build.py`.

## Use it on this PC only

```
python build.py      # downloads ~250 MB of IMDb data the first time, builds ./dist
python serve.py      # keep this running
```

In Stremio: **Addons** → paste `http://127.0.0.1:7000/manifest.json` into the search box → **Install**.
Rerun `build.py` whenever you want fresh ratings. This only works while `serve.py` is running,
and only in Stremio on this PC.

## Use it everywhere (TV, phone), updated daily — GitHub Pages

1. Create a new **public** repository on GitHub (free Pages needs public) and push these files.
2. In the repo: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. **Actions → Build catalog → Run workflow** (it also runs daily at 09:17 UTC).
4. When it finishes, open `https://<your-username>.github.io/<repo>/` — it shows the exact
   manifest URL. Paste that into Stremio's addon search box on each device.

## Letterboxd and Metacritic ratings (MDBList)

1. Create a free account at [mdblist.com](https://mdblist.com) and copy your API key from
   **Preferences → API**.
2. Save it as a repository secret (it is never stored in the code):
   `gh secret set MDBLIST_API_KEY -R <your-username>/<repo>` and paste the key when asked.
3. Run the workflow. Each run looks up new movies and re-checks entries older than 30 days,
   in batches of 100, and commits the updated `ratings/mdblist.json` back to the repo.
   The free plan allows 1,000 requests a day; a run uses at most 400 (`MDBLIST_MAX_REQUESTS`).

## Notes

- IMDb's datasets are licensed for personal, non-commercial use. Keep this for yourself;
  don't submit it to Stremio's community addon list.
- Posters come from Stremio's own image service (`images.metahub.space`), so no API key is needed.
