#!/usr/bin/env python3
"""
Collects download stats for one username across npm, PyPI, Docker Hub and GHCR,
writes them into the index.html next to this script, and opens it.

Usage (keep index.html in the same folder as this script):
    python fetch_stats.py                     # uses "iloveitaly"
    python fetch_stats.py someuser
    python fetch_stats.py iloveitaly --pypi extra-pkg,another-pkg
    python fetch_stats.py iloveitaly --bq-key my-key.json   # PyPI numbers from BigQuery

BigQuery needs one install:  pip install google-cloud-bigquery

Everything else needs no installs (standard library only).
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

START, END = "<!-- STATS:START -->", "<!-- STATS:END -->"
UA ="Mozilla/5.0 (oss-download-dashboard)"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def get(url, timeout=30, retries=4):
    """Download a URL. Waits and retries if the site says "slow down" (429) or has a hiccup (5xx)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if attempt < retries and (e.code == 429 or e.code >= 500):
                time.sleep(2 ** attempt * 2)  # 2s, 4s, 8s, 16s
                continue
            raise


def get_json(url):
    return json.loads(get(url))


def q(s):
    return urllib.parse.quote(s, safe="")


def normalize(name):
    """PyPI names: lowercase, runs of - _ . become '-'."""
    return re.sub(r"[-_.]+", "-", name).lower()


def result(packages, source, **extra):
    packages = sorted(packages, key=lambda p: p["downloads"], reverse=True)
    return {
        "ok": True,
        "source": source,
        "total": sum(p["downloads"] for p in packages),
        "packages": packages,
        **extra,
    }


def failed(error, **extra):
    return {"ok": False, "error": str(error), "total": 0, "packages": [], **extra}


# --------------------------------------------------------------------------
# npm (all time + last 30 days)
# --------------------------------------------------------------------------
NPM_FIRST_DAY = datetime.date(2015, 1, 10)  # npm has no download data before this


def npm_all_time(name):
    """npm only returns ~18 months per request, so add up one year at a time."""
    try:
        created = get_json(f"https://registry.npmjs.org/{name.replace('/', '%2F')}")["time"]["created"][:10]
        start = max(NPM_FIRST_DAY, datetime.date.fromisoformat(created))
    except Exception:
        start = NPM_FIRST_DAY
    today = datetime.date.today()
    total = 0
    while start <= today:
        end = min(start + datetime.timedelta(days=364), today)
        try:
            total += get_json(f"https://api.npmjs.org/downloads/point/{start}:{end}/{name}").get("downloads", 0)
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404 = no downloads in that year
                raise
        start = end + datetime.timedelta(days=1)
        time.sleep(0.2)  # be polite
    return total


def fetch_npm(user):
    data = get_json(f"https://registry.npmjs.org/-/v1/search?text=maintainer:{q(user)}&size=250")
    objects = data.get("objects", [])
    packages = []
    for i, obj in enumerate(objects, 1):
        name = obj["package"]["name"]
        print(f"   npm {i}/{len(objects)}: {name}")
        try:
            last_30 = get_json(f"https://api.npmjs.org/downloads/point/last-month/{name}").get("downloads", 0)
        except Exception:
            last_30 = (obj.get("downloads") or {}).get("monthly", 0)
        packages.append({"name": name, "downloads": npm_all_time(name), "last_30": last_30})
    return result(packages, "npm registry", period="All Time",
                  last_30=sum(p["last_30"] for p in packages))


# --------------------------------------------------------------------------
# PyPI
# --------------------------------------------------------------------------
CLICKPY_SERVERS = [
    ("https://sql-clickhouse.clickhouse.com", "demo"),
    ("https://sql-clickhouse.clickhouse.com:8443", "demo"),
    ("https://clickpy-clickhouse.clickhouse.com", "play"),  # older address
]

# Last-resort list, used only if PyPI can't be asked (taken from pypi.org/user/iloveitaly, Oct 2026).
SAVED_PYPI_LISTS = {
    "iloveitaly": [
        "activemodel", "aiautocommit", "alembic-squawk", "amazon-lunchmoney", "apple-maps-api",
        "beautiful-traceback", "celery-healthcheck", "clean-workspace", "cleanup-orphans",
        "cloudflare-analytics", "datasette-enrichments-shell", "detect-shadowed-modules",
        "docker-hosts", "docker-image-cleanup", "fastapi-ipware", "funcy-pipe",
        "generate-fastapi-typed-routes", "git-history-extraction", "github-overlord",
        "gmail-digest", "gmail-draft-creator", "gohighlevel-api", "ics-to-datasette",
        "ipython-copy", "ipython-ctrlr-fzf", "ipython-playground", "llm-ide-rules", "llm-sql-prompt",
        "lunchmoney-transaction-enhancer", "lunchmoney-venmo-track", "netsuite",
        "playwright-trace-analyzer", "pytest-celery-utils", "pytest-line-runner",
        "pytest-playwright-artifacts", "pytest-playwright-visual-snapshot", "pytest-plugin-utils",
        "radar-mapping-api", "react-router-routes", "readwise-to-datasette", "rpdb",
        "sqlalchemy-postgres-point", "structlog-config", "todoist-digest", "todoist-scheduler",
        "todoist-to-md", "uv-development-toggle",
    ],
}


def pypi_user_packages(user):
    """Find the user's PyPI packages. Returns (names, how)."""
    errors = []

    # 1. PyPI's XML-RPC API (not behind the bot check that blocks the web pages)
    try:
        import xmlrpc.client
        server = xmlrpc.client.ServerProxy("https://pypi.org/pypi", headers=[("User-Agent", UA)])
        names = {normalize(name) for _role, name in server.user_packages(user)}
        if names:
            return sorted(names), "PyPI API"
        errors.append("PyPI API returned no packages")
    except Exception as e:
        errors.append(f"PyPI API: {e}")

    # 2. The profile page (often blocked by PyPI's bot check)
    try:
        html = get(f"https://pypi.org/user/{q(user)}/")
        names = {normalize(urllib.parse.unquote(n)) for n in re.findall(r'/project/([^/"\s]+)/', html)}
        if names:
            return sorted(names), "PyPI profile page"
        errors.append("profile page had no package list (PyPI bot check)")
    except Exception as e:
        errors.append(f"profile page: {e}")

    # 3. Saved list
    if user.lower() in SAVED_PYPI_LISTS:
        print("   Couldn't ask PyPI, using the saved package list (may miss new packages):", "; ".join(errors))
        return SAVED_PYPI_LISTS[user.lower()], "saved list"

    raise RuntimeError("; ".join(errors) + ". Tip: pass package names with --pypi")


def bigquery_downloads(names, key_path, days, max_gb):
    """Official PyPI data from Google BigQuery, using your service account key."""
    try:
        from google.cloud import bigquery
        from google.oauth2 import service_account
    except ImportError:
        raise RuntimeError("BigQuery library missing. Run: pip install google-cloud-bigquery")

    creds = service_account.Credentials.from_service_account_file(key_path)
    client = bigquery.Client(credentials=creds, project=creds.project_id)

    now = datetime.datetime.now(datetime.timezone.utc)
    sql = """
        SELECT project,
               COUNT(*) AS total,
               COUNTIF(timestamp >= @last30) AS last_30
        FROM `bigquery-public-data.pypi.file_downloads`
        WHERE timestamp >= @since
          AND project IN UNNEST(@names)
        GROUP BY project
    """
    params = [
        bigquery.ScalarQueryParameter("since", "TIMESTAMP", now - datetime.timedelta(days=days)),
        bigquery.ScalarQueryParameter("last30", "TIMESTAMP", now - datetime.timedelta(days=30)),
        bigquery.ArrayQueryParameter("names", "STRING", list(names)),
    ]

    # Check the size first so we never blow through the free 1 TB/month
    dry = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=params, dry_run=True, use_query_cache=False))
    est_gb = dry.total_bytes_processed / 1e9
    print(f"   BigQuery: this query scans at most {est_gb:,.1f} GB (your limit: {max_gb:,} GB)")
    if est_gb > max_gb:
        raise RuntimeError(
            f"BigQuery query would scan up to {est_gb:,.0f} GB, over your {max_gb:,} GB limit. "
            f"Use a smaller --days, or raise --max-gb.")

    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    rows = {normalize(r.project): {"total": r.total, "last_30": r.last_30} for r in job.result()}
    billed = (job.total_bytes_billed or 0) / 1e9
    print(f"   BigQuery: actually billed {billed:,.1f} GB")
    return rows, f"BigQuery (official PyPI data)", f"Last {days} Days"


def clickpy(names):
    """ClickPy: free public copy of PyPI's BigQuery data. All time."""
    in_list = ",".join("'" + n.replace("'", "") + "'" for n in names)
    sql = (
        "SELECT project, sum(count) AS total, "
        "sumIf(count, date >= today() - 30) AS last_30 "
        "FROM pypi.pypi_downloads_per_day "
        f"WHERE project IN ({in_list}) GROUP BY project FORMAT JSON"
    )
    errors = []
    for host, user in CLICKPY_SERVERS:
        try:
            data = get_json(f"{host}/?user={user}&query={q(sql)}")
            rows = {normalize(r["project"]): r for r in data["data"]}
            return rows, "ClickPy", "All Time"
        except Exception as e:
            errors.append(f"{host}: {e}")
    raise RuntimeError(" | ".join(errors))


def pypistats(names):
    """Last resort: pypistats.org, last month only."""
    rows = {}
    for n in names:
        try:
            month = get_json(f"https://pypistats.org/api/packages/{n}/recent")["data"]["last_month"]
            rows[n] = {"total": month, "last_30": month}
        except Exception as e:
            print(f"   pypistats skipped {n}: {e}")
        time.sleep(0.5)  # be polite, avoids rate limits
    if not rows:
        raise RuntimeError("pypistats.org returned nothing")
    return rows, "pypistats.org", "Last Month"


def fetch_pypi(user, extra, bq_key=None, days=30, max_gb=250):
    names, found_via = pypi_user_packages(user)
    names = sorted(set(names) | {normalize(n) for n in extra})
    print(f"   Found {len(names)} PyPI packages via {found_via}")

    # Try each download source in order until one works
    sources = []
    if bq_key:
        sources.append(("BigQuery", lambda: bigquery_downloads(names, bq_key, days, max_gb)))
    sources += [("ClickPy", lambda: clickpy(names)), ("pypistats", lambda: pypistats(names))]

    errors = []
    for label, fn in sources:
        try:
            rows, source, period = fn()
            break
        except Exception as e:
            print(f"   {label} failed: {e}")
            errors.append(f"{label}: {e}")
    else:
        raise RuntimeError(" | ".join(errors))

    packages = [{
        "name": n,
        "downloads": int(rows.get(n, {}).get("total", 0)),
        "last_30": int(rows.get(n, {}).get("last_30", 0)),
    } for n in names]
    return result(packages, f"{source} · packages via {found_via}",
                  period=period, last_30=sum(p["last_30"] for p in packages))


# --------------------------------------------------------------------------
# Docker Hub (all-time pulls)
# --------------------------------------------------------------------------
def fetch_docker(user):
    url = f"https://hub.docker.com/v2/repositories/{q(user)}/?page_size=100"
    packages = []
    while url:
        data = get_json(url)
        packages += [{"name": r["name"], "downloads": r.get("pull_count", 0)} for r in data.get("results", [])]
        url = data.get("next")
    return result(packages, "Docker Hub API")


# --------------------------------------------------------------------------
# GHCR (all-time downloads, read from GitHub's package pages: no public API)
# --------------------------------------------------------------------------
def parse_count(s):
    s = s.replace(",", "").strip()
    mult = 1000 if s[-1:].lower() == "k" else 1_000_000 if s[-1:].lower() == "m" else 1
    try:
        return int(float(s.rstrip("kKmM")) * mult)
    except ValueError:
        return 0


def fetch_ghcr(user):
    u = re.escape(user)
    link_patterns = [
        rf'/users/{u}/packages/container/package/([^"?#\s]+)',
        rf'/{u}/[^/"\s]+/pkgs/container/([^"?#\s]+)',
    ]
    names = set()
    for page in range(1, 11):
        html = get(f"https://github.com/{q(user)}?tab=packages&ecosystem=container&page={page}")
        found = set()
        for pat in link_patterns:
            found |= set(re.findall(pat, html, re.I))
        if not found - names:
            break
        names |= found

    packages = []
    for raw in sorted(names):
        html = get(f"https://github.com/users/{q(user)}/packages/container/package/{raw}")
        m = (re.search(r'Total downloads[\s\S]{0,800}?title="([\d,]+)"', html, re.I)
             or re.search(r'Total downloads[\s\S]{0,800}?>\s*([\d.,]+[kKmM]?)\s*<', html, re.I))
        packages.append({"name": urllib.parse.unquote(raw), "downloads": parse_count(m.group(1)) if m else 0})
        time.sleep(0.3)
    return result(packages, "GitHub package pages")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("username", nargs="?", default="iloveitaly")
    ap.add_argument("--pypi", default="", help="extra PyPI packages, comma separated")
    ap.add_argument("--bq-key", default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
                    help="path to your BigQuery service account key (.json) to get PyPI numbers from BigQuery")
    ap.add_argument("--days", type=int, default=30, help="BigQuery only: how many days back to count (default 30)")
    ap.add_argument("--max-gb", type=int, default=250, help="BigQuery only: refuse queries bigger than this (default 250)")
    ap.add_argument("--html", default=None, help="dashboard file to fill in (default: index.html next to this script)")
    ap.add_argument("--no-open", action="store_true", help="don't open the dashboard when done")
    args = ap.parse_args()

    # Always use the index.html that sits next to this script, wherever the terminal is
    html_path = Path(args.html) if args.html else Path(__file__).resolve().parent / "index.html"
    if not html_path.exists():
        sys.exit(f"Can't find {html_path}\nPut index.html in the same folder as fetch_stats.py and try again.")
    html = html_path.read_text(encoding="utf-8")
    if START not in html or END not in html:
        sys.exit(f"{html_path} is missing the {START} / {END} markers. Use the latest index.html.")

    user = args.username
    extra = [s.strip() for s in args.pypi.split(",") if s.strip()]

    sources = {
        "npm": lambda: fetch_npm(user),
        "pypi": lambda: fetch_pypi(user, extra, args.bq_key, args.days, args.max_gb),
        "docker": lambda: fetch_docker(user),
        "ghcr": lambda: fetch_ghcr(user),
    }

    stats = {
        "username": user,
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    for key, fn in sources.items():
        print(f"Fetching {key}...")
        try:
            stats[key] = fn()
        except Exception as e:
            stats[key] = failed(e)
        s = stats[key]
        if s["ok"]:
            print(f"   OK  {s['total']:,} downloads from {len(s['packages'])} packages ({s['source']})")
        else:
            print(f"   FAILED: {s['error']}")

    stats["grand_total"] = sum(stats[k]["total"] for k in sources)

    # Write the numbers straight into index.html, between the markers
    data = json.dumps(stats, indent=2).replace("</", "<\\/")  # keep it safe inside <script>
    before, rest = html.split(START, 1)
    _, after = rest.split(END, 1)
    html = f"{before}{START}\n    <script>window.STATS = {data};</script>\n    {END}{after}"
    html_path.write_text(html, encoding="utf-8")

    print(f"\nCombined total: {stats['grand_total']:,}")
    print(f"Dashboard updated: {html_path}")
    if not args.no_open:
        webbrowser.open(html_path.as_uri())


if __name__ == "__main__":
    main()
