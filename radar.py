#!/usr/bin/env python3
"""agent-radar: find fast-rising feature requests in AI agent repositories.

Scans open GitHub issues, saves a snapshot of their 👍 counts, and ranks
issues by how fast they gain reactions. Writes a bilingual (中文 / English)
markdown report plus JSON data for reuse.

Zero dependencies: Python 3.11+ standard library only.

Usage:
    GITHUB_TOKEN=... python radar.py --config radar.toml
    python radar.py --fixture tests/fixture.json --today 2026-10-22   # offline
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SEARCH_API = "https://api.github.com/search/issues"
# Authenticated search allows 30 requests/minute; stay safely under it.
SEARCH_INTERVAL_S = 2.2
TITLE_MAX = 90


# ---------------------------------------------------------------- data model

@dataclass
class Issue:
    repo: str
    number: int
    title: str
    url: str
    created_at: str
    updated_at: str
    labels: list[str]
    comments: int
    thumbs_up: int

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass
class Row:
    issue: Issue
    age_days: float
    velocity: float              # lifetime 👍 per day
    gain: int | None             # 👍 gained since the previous snapshot
    gain_per_day: float | None


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def to_issue(repo: str, item: dict) -> Issue:
    return Issue(
        repo=repo,
        number=item["number"],
        title=" ".join(item["title"].split()),
        url=item["html_url"],
        created_at=item["created_at"],
        updated_at=item["updated_at"],
        labels=[label["name"] for label in item.get("labels", [])],
        comments=item.get("comments", 0),
        thumbs_up=item.get("reactions", {}).get("+1", 0),
    )


# ------------------------------------------------------------------ fetching

class GitHubSource:
    """Live GitHub search API."""

    def __init__(self, token: str | None):
        self.token = token
        self._last_call = 0.0
        if not token:
            print("warning: GITHUB_TOKEN not set; unauthenticated search allows "
                  "only 10 requests/minute", file=sys.stderr)

    def fetch(self, repo: str, kind: str, query: str, per_page: int) -> list[dict]:
        params = urllib.parse.urlencode({
            "q": query, "sort": "reactions-+1", "order": "desc", "per_page": per_page,
        })
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-radar",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(f"{SEARCH_API}?{params}", headers=headers)

        for attempt in range(3):
            wait = SEARCH_INTERVAL_S - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    self._last_call = time.monotonic()
                    return json.load(response).get("items", [])
            except urllib.error.HTTPError as error:
                self._last_call = time.monotonic()
                rate_limited = error.code == 429 or (
                    error.code == 403 and error.headers.get("X-RateLimit-Remaining") == "0")
                if error.code == 403 and not rate_limited:
                    body = error.read().decode("utf-8", "replace")[:300]
                    raise SystemExit(f"GitHub refused the search for {repo} (403): {body}")
                if rate_limited and attempt < 2:
                    reset = error.headers.get("X-RateLimit-Reset")
                    delay = int(reset) - int(time.time()) + 1 if reset else 30
                    delay = min(max(delay, 5), 120)
                    print(f"rate limited on {repo}, sleeping {delay}s", file=sys.stderr)
                    time.sleep(delay)
                    continue
                if error.code == 422:
                    print(f"skip {repo}: repository not found or not searchable",
                          file=sys.stderr)
                    return []
                raise
        return []


class FixtureSource:
    """Replays saved API items: {"<repo>|<kind>": [item, ...]}. For offline tests."""

    def __init__(self, path: Path):
        self.data = json.loads(path.read_text(encoding="utf-8"))

    def fetch(self, repo: str, kind: str, query: str, per_page: int) -> list[dict]:
        return self.data.get(f"{repo}|{kind}", [])[:per_page]


def scan(source, repos: list[str], today: date, settings: dict) -> dict[str, Issue]:
    since = (today - timedelta(days=settings["lookback_days"])).isoformat()
    excluded = {label.lower() for label in settings["exclude_labels"]}
    found: dict[str, Issue] = {}
    for repo in repos:
        queries = {
            "new": f"repo:{repo} is:issue is:open created:>={since}",
            "top": f"repo:{repo} is:issue is:open",
        }
        count = 0
        for kind, query in queries.items():
            for item in source.fetch(repo, kind, query, settings["per_page"]):
                if "pull_request" in item:
                    continue
                issue = to_issue(repo, item)
                if excluded & {label.lower() for label in issue.labels}:
                    continue
                found[issue.key] = issue
                count += 1
        print(f"{repo}: {count} issues", file=sys.stderr)
    return found


# ----------------------------------------------------------------- snapshots

def save_snapshot(directory: Path, today: date, issues: dict[str, Issue]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{today.isoformat()}.json"
    payload = {
        "taken_on": today.isoformat(),
        "issues": {key: asdict(issue) for key, issue in sorted(issues.items())},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_previous_snapshot(directory: Path, today: date) -> tuple[date, dict] | None:
    if not directory.exists():
        return None
    earlier = sorted(p for p in directory.glob("*.json") if p.stem < today.isoformat())
    if not earlier:
        return None
    latest = earlier[-1]
    data = json.loads(latest.read_text(encoding="utf-8"))
    return date.fromisoformat(data["taken_on"]), data["issues"]


# ------------------------------------------------------------------- ranking

def build_rows(issues: dict[str, Issue], previous: tuple[date, dict] | None,
               today: date) -> list[Row]:
    now = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    rows = []
    for key, issue in issues.items():
        age = max((now - parse_time(issue.created_at)).total_seconds() / 86400, 1.0)
        gain = gain_per_day = None
        if previous is not None:
            prev_date, prev_issues = previous
            if key in prev_issues:
                gain = issue.thumbs_up - prev_issues[key]["thumbs_up"]
                span = max((today - prev_date).days, 1)
                gain_per_day = gain / span
        rows.append(Row(issue, age, issue.thumbs_up / age, gain, gain_per_day))
    return rows


def fastest_rising(rows: list[Row], settings: dict) -> list[Row]:
    rising = [r for r in rows if r.gain is not None and r.gain >= settings["min_gain"]]
    rising.sort(key=lambda r: (r.gain, r.issue.thumbs_up), reverse=True)
    return rising[: settings["top_n"]]


def new_and_climbing(rows: list[Row], settings: dict) -> list[Row]:
    fresh = [r for r in rows
             if r.age_days <= settings["lookback_days"]
             and r.issue.thumbs_up >= settings["min_reactions"]]
    fresh.sort(key=lambda r: r.velocity, reverse=True)
    return fresh[: settings["top_n"]]


# -------------------------------------------------------------------- report

def cell(text: str) -> str:
    text = text if len(text) <= TITLE_MAX else text[: TITLE_MAX - 1] + "…"
    return text.replace("|", "\\|")


def link(issue: Issue) -> str:
    return f"[#{issue.number} {cell(issue.title)}]({issue.url})"


def labels(issue: Issue) -> str:
    return cell(", ".join(issue.labels[:3])) or "—"


def render_report(today: date, repos: list[str], rows: list[Row],
                  previous: tuple[date, dict] | None, settings: dict) -> str:
    lines = [
        f"# Agent 需求雷达 · Agent Demand Radar — {today.isoformat()}",
        "",
        f"扫描 {len(repos)} 个仓库的未关闭 issue，按点赞增速排序。  ",
        f"Open issues from {len(repos)} repositories, ranked by how fast they gain 👍.",
        "",
    ]

    lines += ["## 涨得最快 · Fastest rising", ""]
    if previous is None:
        lines += [
            "首次运行：下次运行起显示与上次快照相比的点赞涨幅。  ",
            "First run: gains since the previous snapshot appear from the next run.",
            "",
        ]
    else:
        prev_date = previous[0]
        span = (today - prev_date).days
        rising = fastest_rising(rows, settings)
        lines += [
            f"对比 {prev_date.isoformat()} 的快照（{span} 天）。  ",
            f"Compared with the snapshot from {prev_date.isoformat()} ({span} days).",
            "",
        ]
        if rising:
            lines += [
                f"| # | Repo | Issue | 👍 | +👍 ({span}d) | 天数 Age | Labels |",
                "| ---: | --- | --- | ---: | ---: | ---: | --- |",
            ]
            for rank, r in enumerate(rising, 1):
                lines.append(
                    f"| {rank} | {r.issue.repo} | {link(r.issue)} | {r.issue.thumbs_up} "
                    f"| +{r.gain} | {r.age_days:.0f} | {labels(r.issue)} |")
        else:
            lines.append(f"没有 issue 涨幅达到 {settings['min_gain']}。 / "
                         f"No issue gained {settings['min_gain']}+ 👍.")
        lines.append("")

    fresh = new_and_climbing(rows, settings)
    lines += [
        f"## 新冒头 · New and climbing (last {settings['lookback_days']} days)",
        "",
        "按“点赞数 ÷ 创建天数”排序。  ",
        "Ranked by 👍 per day since the issue was opened.",
        "",
    ]
    if fresh:
        lines += [
            "| # | Repo | Issue | 👍 | 👍/天 per day | 天数 Age | Labels |",
            "| ---: | --- | --- | ---: | ---: | ---: | --- |",
        ]
        for rank, r in enumerate(fresh, 1):
            lines.append(
                f"| {rank} | {r.issue.repo} | {link(r.issue)} | {r.issue.thumbs_up} "
                f"| {r.velocity:.1f} | {r.age_days:.0f} | {labels(r.issue)} |")
    else:
        lines.append("暂无。 / Nothing yet.")

    lines += [
        "",
        "## 编者点评 · Notes",
        "",
        "- [ ] 哪些需求能在官方产品之外解决（插件、hook、MCP、CLI）？ / "
        "Which of these can be solved outside the core product?",
        "- [ ] 哪些已经有人做了？ / Which already have a community solution?",
        "",
        "<sub>Generated by agent-radar. Counts are 👍 reactions on open issues. "
        "Issues labeled duplicate or invalid are kept, because maintainers often "
        "use those labels on consolidated community requests.</sub>",
        "",
    ]
    return "\n".join(lines)


def write_latest(path: Path, today: date, rows: list[Row], settings: dict) -> None:
    def as_dict(r: Row) -> dict:
        return {**asdict(r.issue), "age_days": round(r.age_days, 1),
                "velocity": round(r.velocity, 2), "gain": r.gain,
                "gain_per_day": None if r.gain_per_day is None else round(r.gain_per_day, 2)}

    payload = {
        "generated_on": today.isoformat(),
        "fastest_rising": [as_dict(r) for r in fastest_rising(rows, settings)],
        "new_and_climbing": [as_dict(r) for r in new_and_climbing(rows, settings)],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------- main

DEFAULTS = {
    "lookback_days": 60, "top_n": 15, "min_reactions": 5, "min_gain": 3,
    "per_page": 50, "exclude_labels": ["spam"],
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=Path("radar.toml"))
    parser.add_argument("--out", type=Path, default=Path("."),
                        help="base directory for data/ and reports/")
    parser.add_argument("--fixture", type=Path,
                        help="replay saved API items instead of calling GitHub")
    parser.add_argument("--today", type=date.fromisoformat, default=None,
                        help="override today's date (YYYY-MM-DD)")
    args = parser.parse_args(argv)

    config = tomllib.loads(args.config.read_text(encoding="utf-8"))
    settings = {**DEFAULTS, **config.get("settings", {})}
    repos = [entry["name"] for entry in config.get("repos", [])]
    if not repos:
        parser.error("no [[repos]] in config")
    today = args.today or datetime.now(timezone.utc).date()

    source = FixtureSource(args.fixture) if args.fixture else GitHubSource(
        os.environ.get("GITHUB_TOKEN"))
    issues = scan(source, repos, today, settings)

    snapshots = args.out / "data" / "snapshots"
    previous = load_previous_snapshot(snapshots, today)
    rows = build_rows(issues, previous, today)

    save_snapshot(snapshots, today, issues)
    reports = args.out / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f"{today.isoformat()}.md"
    report = render_report(today, repos, rows, previous, settings)
    report_path.write_text(report, encoding="utf-8")
    (reports / "latest.md").write_text(report, encoding="utf-8")
    write_latest(args.out / "data" / "latest.json", today, rows, settings)

    print(f"{len(issues)} issues scanned; report: {report_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
