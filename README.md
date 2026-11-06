# IMDb Top Rated — personal Stremio catalog

Adds two rows to Stremio, ranked by **IMDb rating** using IMDb's own daily data files:

| Row | What's in it |
|---|---|
| **IMDb Top Rated** | Top 1,000 movies by rating with at least 25,000 votes |
| **IMDb Hidden Gems** | Up to 500 movies rated 7.5+ with 5,000–25,000 votes |

Both can be filtered by genre in Stremio's **Discover** tab. Clicking a movie works as usual:
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

## Notes

- IMDb's datasets are licensed for personal, non-commercial use. Keep this for yourself;
  don't submit it to Stremio's community addon list.
- Posters come from Stremio's own image service (`images.metahub.space`), so no API key is needed.
