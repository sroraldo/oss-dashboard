# OSS Download Dashboard

Total downloads of `iloveitaly`'s open source packages across npm, PyPI, Docker Hub and GHCR.

- `fetch_stats.py` collects the numbers and writes them into `index.html`.
- `.github/workflows/update-stats.yml` runs it every day and saves the result.

## Run it on your computer

    python fetch_stats.py iloveitaly

The dashboard opens when it's done.

## Where the numbers come from

| Source     | How                                              | Period    |
|------------|--------------------------------------------------|-----------|
| npm        | npm downloads API, added up one year at a time   | All time  |
| PyPI       | Package list from PyPI's API, downloads from ClickPy | All time |
| Docker Hub | Docker Hub API                                   | All time  |
| GHCR       | GitHub package pages (no API exists)             | All time  |

Optional: `--bq-key key.json` gets PyPI numbers from Google BigQuery instead (last 30 days by default).
