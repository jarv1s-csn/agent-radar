#!/usr/bin/env python3
"""supply: count open-source AI projects by the kind of use they serve.

Reads categories from supply.toml, where each category lists GitHub topics that
only AI projects carry. Collects every matching repository above a star floor
(splitting searches by star range to get past GitHub's 1,000-result cap),
removes duplicates, and writes a JSON dataset plus a markdown summary.

Like radar.py: any failed request fails the whole run and nothing is written.

Usage:
    GITHUB_TOKEN=... python supply.py --config supply.toml
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from radar import MAX_ATTEMPTS, FetchError, HttpError, retry_delay

SEARCH_API = "https://api.github.com/search/repositories"
SEARCH_INTERVAL_S = 2.2      # authenticated search allows 30 requests per minute
PER_PAGE = 100
MAX_RESULTS = 1000           # GitHub search never returns more than this per query
STAR_CEILING = 10_000_000
TOPIC = re.compile(r"^[a-z0-9][a-z0-9-]*$")

Transport = Callable[[dict], dict]


# ------------------------------------------------------------------ fetching

def urllib_transport(token: str) -> Transport:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agent-radar-supply",
    }

    def get(params: dict) -> dict:
        url = f"{SEARCH_API}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            raise HttpError(error.code, body, dict(error.headers.items())) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
            raise HttpError(0, str(error)) from None

    return get


class SearchClient:
    """Paced, retried repository search. Raises FetchError on failure."""

    def __init__(self, transport: Transport, interval: float = 0.0,
                 sleep: Callable[[float], None] | None = None):
        self.transport = transport
        self.interval = interval
        self.sleep = sleep or time.sleep
        self.calls = 0
        self._last: float | None = None

    def search(self, q: str, page: int = 1, per_page: int = PER_PAGE) -> dict:
        params = {"q": q, "sort": "stars", "order": "desc",
                  "per_page": per_page, "page": page}
        problem = ""
        for attempt in range(MAX_ATTEMPTS):
            if self.interval and self._last is not None:
                wait = self.interval - (time.monotonic() - self._last)
                if wait > 0:
                    self.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                body = self.transport(params)
            except HttpError as error:
                problem = f"{q!r} page {page}: {error.describe()}"
                delay = retry_delay(error, attempt)
                if delay is None:
                    raise FetchError(problem) from None
            else:
                if not body.get("incomplete_results"):
                    return body
                problem = f"{q!r} page {page}: GitHub returned incomplete results"
                delay = 10
            if attempt + 1 < MAX_ATTEMPTS:
                print(f"retrying in {delay:.0f}s after: {problem}", file=sys.stderr)
                self.sleep(delay)
        raise FetchError(f"gave up after {MAX_ATTEMPTS} attempts: {problem}")


def collect_topic(client: SearchClient, topic: str, lo: int, hi: int) -> list[dict]:
    """All repositories with this topic and lo..hi stars, splitting the star
    range whenever a search matches more than GitHub will return."""
    q = f"topic:{topic} stars:{lo}..{hi}"
    first = client.search(q)
    total = first["total_count"]
    if total > MAX_RESULTS:
        if lo >= hi:
            raise FetchError(f"{q!r} matches {total} repositories with identical "
                             "star counts; cannot split further")
        mid = max(lo, min(hi - 1, int(math.sqrt(lo * hi))))
        return (collect_topic(client, topic, mid + 1, hi)
                + collect_topic(client, topic, lo, mid))
    items = list(first["items"])
    page = 1
    while len(items) < total:
        page += 1
        body = client.search(q, page)
        if not body["items"]:
            break
        items += body["items"]
    if len(items) < total:
        raise FetchError(f"{q!r}: expected {total} repositories, got {len(items)}")
    return items


# ----------------------------------------------------------------- scanning

def keep(item: dict, settings: dict) -> bool:
    name = item["name"].lower()
    if any(name.startswith(p) for p in settings["skip_name_prefixes"]):
        return False
    return not set(item.get("topics") or []) & set(settings["skip_topics"])


def scan(client: SearchClient, config: dict, settings: dict
         ) -> tuple[dict[str, dict], list[dict]]:
    repos: dict[str, dict] = {}
    queries: list[dict] = []
    for category, spec in config["categories"].items():
        for topic in spec["topics"]:
            items = collect_topic(client, topic, settings["min_stars"], STAR_CEILING)
            kept = 0
            for item in items:
                if not keep(item, settings):
                    continue
                kept += 1
                repo = repos.setdefault(item["full_name"], {
                    "full_name": item["full_name"],
                    "url": item["html_url"],
                    "description": " ".join((item.get("description") or "").split())[:200],
                    "stars": item["stargazers_count"],
                    "created_at": item["created_at"],
                    "pushed_at": item["pushed_at"],
                    "language": item.get("language"),
                    "archived": item.get("archived", False),
                    "topics": item.get("topics") or [],
                    "categories": [],
                    "matched_topics": [],
                })
                if category not in repo["categories"]:
                    repo["categories"].append(category)
                if topic not in repo["matched_topics"]:
                    repo["matched_topics"].append(topic)
            queries.append({"category": category, "topic": topic,
                            "matched": len(items), "kept": kept})
            print(f"{category} / {topic}: {kept} kept of {len(items)}", file=sys.stderr)
    return repos, queries


def summarize(repos: dict[str, dict], config: dict, as_of: datetime) -> list[dict]:
    recent = (as_of - timedelta(days=365)).isoformat()
    rows = []
    for category, spec in config["categories"].items():
        members = sorted((r for r in repos.values() if category in r["categories"]),
                         key=lambda r: -r["stars"])
        stars = [r["stars"] for r in members]
        new = [r for r in members if r["created_at"] >= recent]
        rows.append({
            "category": category,
            "label": spec["label"],
            "signals_topic": spec.get("signals_topic"),
            "repos": len(members),
            "stars_total": sum(stars),
            "stars_median": statistics.median(stars) if stars else 0,
            "repos_1000_plus": sum(s >= 1000 for s in stars),
            "new_last_365d": len(new),
            "new_last_365d_stars": sum(r["stars"] for r in new),
            "top": [{"full_name": r["full_name"], "stars": r["stars"],
                     "description": r["description"][:90]} for r in members[:5]],
        })
    return rows


# -------------------------------------------------------------------- report

def render(as_of: datetime, settings: dict, rows: list[dict], queries: list[dict],
           unique: int) -> str:
    day = as_of.date().isoformat()
    lines = [
        f"# 开源 AI 项目供给 · Open-source AI supply — {day}",
        "",
        f"按用途统计 GitHub 上 star ≥ {settings['min_stars']} 的 AI 开源项目，"
        "用途分类对齐 OpenAI Signals 的消息主题。  ",
        f"Open-source AI repositories with at least {settings['min_stars']} stars, "
        "grouped by the kind of use they serve, using OpenAI Signals' topic categories.",
        "",
        f"去重后共 {unique:,} 个仓库；一个仓库可以同时属于多个类别。  ",
        f"{unique:,} unique repositories; a repository can belong to more than one category.",
        "",
        "| 类别 Category | 仓库 Repos | ≥1k⭐ | 总 star Stars | 近一年新建 New (365d) | 代表项目 Top projects |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in rows:
        top = ", ".join(f"{t['full_name']} ({t['stars']:,})" for t in r["top"][:3])
        lines.append(f"| {r['label']} | {r['repos']:,} | {r['repos_1000_plus']:,} "
                     f"| {r['stars_total']:,} | {r['new_last_365d']:,} | {top} |")
    lines += [
        "",
        "## 查询明细 · Queries",
        "",
        "每个类别由一组只有 AI 项目才会打的 GitHub topic 定义，见 `supply.toml`。"
        "没有打这些 topic 的项目不会被统计，所以数字偏低，适合比较类别之间的相对大小。  ",
        "Each category is a set of AI-specific GitHub topics (see `supply.toml`). "
        "Projects without these topics are missed, so counts are lower bounds, "
        "best used to compare categories with each other.",
        "",
        "| Category | Topic | Matched | Kept |",
        "| --- | --- | ---: | ---: |",
    ]
    for q in queries:
        lines.append(f"| {q['category']} | `{q['topic']}` | {q['matched']:,} | {q['kept']:,} |")
    lines += [
        "",
        "<sub>Generated by agent-radar/supply.py. Lists and tutorials (names starting "
        "with \"awesome\", or topics such as awesome-list, tutorial, course) are skipped.</sub>",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------- main

DEFAULTS = {"min_stars": 100, "skip_name_prefixes": ["awesome"],
            "skip_topics": ["awesome", "awesome-list", "tutorial", "course"]}


def main(argv: list[str] | None = None, transport: Transport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=Path("supply.toml"))
    parser.add_argument("--out", type=Path, default=Path("."))
    parser.add_argument("--today", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)

    config = tomllib.loads(args.config.read_text(encoding="utf-8"))
    settings = {**DEFAULTS, **config.get("settings", {})}
    if not config.get("categories"):
        parser.error("no [categories] in config")
    bad = [t for spec in config["categories"].values() for t in spec["topics"]
           if not TOPIC.match(t)]
    if bad:
        parser.error(f"not a GitHub topic: {', '.join(bad)}")

    interval = 0.0
    if transport is None:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            print("error: set GITHUB_TOKEN", file=sys.stderr)
            return 2
        transport = urllib_transport(token)
        interval = SEARCH_INTERVAL_S

    as_of = (datetime(args.today.year, args.today.month, args.today.day, tzinfo=timezone.utc)
             if args.today else datetime.now(timezone.utc).replace(microsecond=0))
    client = SearchClient(transport, interval)
    try:
        repos, queries = scan(client, config, settings)
    except FetchError as error:
        print(f"error: {error}\nNothing was written.", file=sys.stderr)
        return 1

    rows = summarize(repos, config, as_of)
    day = as_of.date().isoformat()
    payload = json.dumps({
        "generated_at": as_of.isoformat(timespec="seconds"),
        "settings": settings,
        "categories": rows,
        "queries": queries,
        "repos": sorted(repos.values(), key=lambda r: -r["stars"]),
    }, ensure_ascii=False, indent=1)
    report = render(as_of, settings, rows, queries, len(repos))

    data_dir = args.out / "data" / "supply"
    reports = args.out / "reports"
    data_dir.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    (data_dir / f"{day}.json").write_text(payload, encoding="utf-8")
    (reports / f"supply-{day}.md").write_text(report, encoding="utf-8")
    print(f"{len(repos)} repositories, {client.calls} API calls", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
