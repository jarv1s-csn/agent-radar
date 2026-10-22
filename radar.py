#!/usr/bin/env python3
"""agent-radar: find fast-rising feature requests in AI agent repositories.

Searches open GitHub issues, counts the 👍 each issue gained in the last few
days from the timestamp of every reaction, and ranks them. Feature requests
and bugs are ranked separately. Writes a bilingual (中文 / English) markdown
report plus JSON data for reuse.

If any part of the collection fails, the run exits non-zero and writes
nothing, so a partial scan never replaces a good report.

Zero dependencies: Python 3.11+ standard library only. Needs a GitHub token
(any token that can read public repositories), because GitHub's GraphQL API
does not allow anonymous access.

Usage:
    GITHUB_TOKEN=... python radar.py --config radar.toml
    python radar.py --fixture tests/fixture.json --today 2026-10-22   # offline
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

GRAPHQL_API = "https://api.github.com/graphql"
REQUEST_INTERVAL_S = 1.0    # spacing between live API calls (secondary rate limits)
SEARCH_PAGE = 50            # issues per search page
REACTIONS_FIRST = 30        # newest 👍 timestamps fetched together with each issue
REACTIONS_PAGE = 100        # 👍 timestamps per follow-up page
MAX_ATTEMPTS = 4            # per API call, for transient failures and rate limits
TITLE_MAX = 90
REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

SEARCH_QUERY = """
query($q: String!, $first: Int!, $after: String, $reactionsFirst: Int!) {
  search(query: $q, type: ISSUE, first: $first, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Issue {
        id number title url createdAt updatedAt
        comments { totalCount }
        labels(first: 30) { nodes { name } }
        reactions(content: THUMBS_UP, first: $reactionsFirst,
                  orderBy: {field: CREATED_AT, direction: DESC}) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes { createdAt }
        }
      }
    }
  }
}
"""

MORE_REACTIONS_QUERY = """
query($id: ID!, $first: Int!, $after: String) {
  node(id: $id) {
    ... on Issue {
      reactions(content: THUMBS_UP, first: $first, after: $after,
                orderBy: {field: CREATED_AT, direction: DESC}) {
        pageInfo { hasNextPage endCursor }
        nodes { createdAt }
      }
    }
  }
}
"""


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
    gain: int                    # 👍 added within the gain window

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass
class Coverage:
    """How much of one search window made it into the candidate pool."""
    repo: str
    window: str                  # "new" | "mid" | "top"
    total: int                   # open issues GitHub reports for the window
    fetched: int
    cutoff: int | None           # lowest 👍 in the pool if truncated; None = complete


@dataclass
class Row:
    issue: Issue
    kind: str                    # "request" | "unlabeled" | "bug"
    age_days: float
    velocity: float              # lifetime 👍 per day


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ------------------------------------------------------------------- errors

class FetchError(Exception):
    """Collection failed. The run stops without writing anything."""


class HttpError(Exception):
    """A failed HTTP exchange. status 0 means the request never got a response."""

    def __init__(self, status: int, body: str = "", headers: dict | None = None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}

    def describe(self) -> str:
        if self.status == 0:
            return f"network error: {self.body[:300]}"
        return f"HTTP {self.status}: {self.body[:300]}"


def retry_delay(error: HttpError, attempt: int) -> float | None:
    """Seconds to wait before retrying, or None if the error is not retryable."""
    if error.status in (403, 429):
        limited = ("retry-after" in error.headers
                   or error.headers.get("x-ratelimit-remaining") == "0"
                   or "rate limit" in error.body.lower())
        if not limited:
            return None
        if "retry-after" in error.headers:
            delay = float(error.headers["retry-after"])
        elif "x-ratelimit-reset" in error.headers:
            delay = int(error.headers["x-ratelimit-reset"]) - time.time() + 1
        else:
            delay = 60
        return min(max(delay, 5), 120)
    if error.status in (0, 500, 502, 503, 504):
        return (5, 15, 45)[min(attempt, 2)]
    return None


# ------------------------------------------------------------------ fetching

Transport = Callable[[dict], dict]


def urllib_transport(token: str) -> Transport:
    """POSTs a GraphQL payload to GitHub and returns the decoded JSON body."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "agent-radar",
    }

    def post(payload: dict) -> dict:
        request = urllib.request.Request(
            GRAPHQL_API, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            raise HttpError(error.code, body, dict(error.headers.items())) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
            raise HttpError(0, str(error)) from None

    return post


class GitHubClient:
    """Runs GraphQL queries with pacing and retries. Raises FetchError on failure."""

    def __init__(self, transport: Transport, interval: float = 0.0,
                 sleep: Callable[[float], None] | None = None):
        self.transport = transport
        self.interval = interval
        self.sleep = sleep or time.sleep
        self.calls = 0
        self._last_call: float | None = None

    def query(self, query: str, variables: dict | None = None) -> dict:
        problem = ""
        for attempt in range(MAX_ATTEMPTS):
            self._pace()
            self.calls += 1
            try:
                body = self.transport({"query": query, "variables": variables or {}})
            except HttpError as error:
                problem = error.describe()
                delay = retry_delay(error, attempt)
                if delay is None:
                    raise FetchError(problem) from None
            else:
                errors = body.get("errors") or []
                if not errors:
                    return body.get("data") or {}
                messages = "; ".join(e.get("message", "?") for e in errors)
                problem = f"GitHub GraphQL error: {messages}"
                transient = any(
                    e.get("type") == "RATE_LIMITED"
                    or "timeout" in e.get("message", "").lower()
                    or "something went wrong" in e.get("message", "").lower()
                    for e in errors)
                if not transient:
                    raise FetchError(problem)
                delay = 60 if any(e.get("type") == "RATE_LIMITED" for e in errors) else 15
            if attempt + 1 < MAX_ATTEMPTS:
                print(f"retrying in {delay:.0f}s after: {problem}", file=sys.stderr)
                self.sleep(delay)
        raise FetchError(f"gave up after {MAX_ATTEMPTS} attempts: {problem}")

    def _pace(self) -> None:
        if self.interval and self._last_call is not None:
            wait = self.interval - (time.monotonic() - self._last_call)
            if wait > 0:
                self.sleep(wait)
        self._last_call = time.monotonic()


class FixtureTransport:
    """Answers the radar's GraphQL queries from a JSON file, for offline runs.

    File shape: {"<owner/name>": {"new" | "mid" | "top": {"issueCount": int,
    "nodes": [issue, ...]}}}. Each issue is a search node, except that 👍 are
    given compactly: "thumbsUp" is the total and "recentThumbsUp" lists recent
    timestamps; the remaining reactions are dated at the issue's creation.
    """

    def __init__(self, path: Path):
        self.data = json.loads(path.read_text(encoding="utf-8"))
        self.issues = {node["id"]: node
                       for windows in self.data.values()
                       for window in windows.values()
                       for node in window["nodes"]}

    def __call__(self, payload: dict) -> dict:
        query, variables = payload["query"], payload.get("variables") or {}
        if "search(" in query:
            return {"data": {"search": self._search(variables)}}
        if "node(" in query:
            node = self.issues[variables["id"]]
            reactions = self._reactions(node, variables["first"], variables.get("after"))
            return {"data": {"node": {"reactions": reactions}}}
        if "repository(" in query:
            return self._repositories(query)
        raise ValueError("fixture cannot answer this query")

    def _repositories(self, query: str) -> dict:
        data, errors = {}, []
        for alias, owner, name in re.findall(
                r'(r\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)', query):
            repo = f"{owner}/{name}"
            if repo in self.data:
                data[alias] = {"nameWithOwner": repo, "hasIssuesEnabled": True}
            else:
                data[alias] = None
                errors.append({"type": "NOT_FOUND", "message":
                               f"Could not resolve to a Repository with the name '{repo}'."})
        return {"data": data, "errors": errors} if errors else {"data": data}

    def _search(self, variables: dict) -> dict:
        q = variables["q"]
        repo = re.search(r"repo:(\S+)", q).group(1)
        window = "new" if "created:>=" in q else "mid" if "created:" in q else "top"
        found = self.data.get(repo, {}).get(window, {"nodes": []})
        nodes = found["nodes"]
        start, first = int(variables.get("after") or 0), variables["first"]
        page = []
        for node in nodes[start:start + first]:
            item = {k: v for k, v in node.items() if k not in ("thumbsUp", "recentThumbsUp")}
            item["reactions"] = self._reactions(node, variables["reactionsFirst"], None)
            page.append(item)
        return {
            "issueCount": found.get("issueCount", len(nodes)),
            "pageInfo": {"hasNextPage": start + first < len(nodes),
                         "endCursor": str(start + first)},
            "nodes": page,
        }

    @staticmethod
    def _reactions(node: dict, first: int, after: str | None) -> dict:
        recent = sorted(node.get("recentThumbsUp", []), reverse=True)
        times = recent + [node["createdAt"]] * (node["thumbsUp"] - len(recent))
        start = int(after or 0)
        return {
            "totalCount": len(times),
            "pageInfo": {"hasNextPage": start + first < len(times),
                         "endCursor": str(start + first)},
            "nodes": [{"createdAt": t} for t in times[start:start + first]],
        }


# --------------------------------------------------------------- collection

def check_repos(client: GitHubClient, repos: list[str]) -> None:
    """Fails fast on a misspelled, missing or issue-less repository."""
    fields = []
    for i, repo in enumerate(repos):
        owner, name = repo.split("/")
        fields.append(f'r{i}: repository(owner: "{owner}", name: "{name}") '
                      "{ nameWithOwner hasIssuesEnabled }")
    data = client.query("query { " + " ".join(fields) + " }")
    for i, repo in enumerate(repos):
        found = data.get(f"r{i}")
        if not found:
            raise FetchError(f"repository {repo} not found")
        if not found.get("hasIssuesEnabled"):
            raise FetchError(f"repository {repo} has issues disabled")


def count_gain(client: GitHubClient, node: dict, since: datetime) -> int:
    """👍 added since `since`, read from reaction timestamps (newest first)."""
    connection = node["reactions"]
    times = [parse_time(r["createdAt"]) for r in connection["nodes"]]
    page_info = connection["pageInfo"]
    while times and times[-1] >= since and page_info["hasNextPage"]:
        data = client.query(MORE_REACTIONS_QUERY, {
            "id": node["id"], "first": REACTIONS_PAGE, "after": page_info["endCursor"]})
        connection = data["node"]["reactions"]
        times += [parse_time(r["createdAt"]) for r in connection["nodes"]]
        page_info = connection["pageInfo"]
    if any(newer < older for newer, older in zip(times, times[1:])):
        raise FetchError(f"reactions on {node['url']} did not arrive newest first; "
                         "cannot count recent 👍")
    return sum(t >= since for t in times)


def to_issue(repo: str, node: dict, gain: int) -> Issue:
    return Issue(
        repo=repo,
        number=node["number"],
        title=" ".join(node["title"].split()),
        url=node["url"],
        created_at=node["createdAt"],
        updated_at=node["updatedAt"],
        labels=[label["name"] for label in node["labels"]["nodes"]],
        comments=node["comments"]["totalCount"],
        thumbs_up=node["reactions"]["totalCount"],
        gain=gain,
    )


def search_window(client: GitHubClient, repo: str, window: str, qualifier: str,
                  settings: dict, since: datetime, known: dict[str, Issue]
                  ) -> tuple[list[Issue], Coverage]:
    q = " ".join(filter(None, [f"repo:{repo}", "is:issue", "is:open", qualifier,
                               "sort:reactions-+1-desc"]))
    pool = settings["pool_size"]
    nodes: list[dict] = []
    after, total = None, 0
    try:
        while len(nodes) < pool:
            data = client.query(SEARCH_QUERY, {
                "q": q, "first": min(SEARCH_PAGE, pool - len(nodes)),
                "after": after, "reactionsFirst": REACTIONS_FIRST})
            search = data["search"]
            total = search["issueCount"]
            nodes += [n for n in search["nodes"] if n and "number" in n]
            if not search["pageInfo"]["hasNextPage"]:
                break
            after = search["pageInfo"]["endCursor"]
        nodes = nodes[:pool]
        issues = []
        for node in nodes:
            key = f"{repo}#{node['number']}"
            if key in known:          # already collected from another window
                issues.append(known[key])
            else:
                issues.append(to_issue(repo, node, count_gain(client, node, since)))
    except (KeyError, TypeError) as error:
        raise FetchError(f"unexpected response while searching {repo} ({window}): "
                         f"missing {error}") from None
    truncated = total > len(issues)
    cutoff = min((i.thumbs_up for i in issues), default=0) if truncated else None
    return issues, Coverage(repo, window, total, len(issues), cutoff)


def scan(client: GitHubClient, repos: list[str], as_of: datetime, settings: dict
         ) -> tuple[dict[str, Issue], list[Coverage]]:
    check_repos(client, repos)
    new_since = (as_of - timedelta(days=settings["lookback_days"])).date()
    mid_since = (as_of - timedelta(days=settings["mid_days"])).date()
    windows = [
        ("new", f"created:>={new_since}"),
        ("mid", f"created:{mid_since}..{new_since - timedelta(days=1)}"),
        ("top", ""),
    ]
    since = as_of - timedelta(days=settings["gain_days"])
    excluded = {label.lower() for label in settings["exclude_labels"]}
    found: dict[str, Issue] = {}
    seen: dict[str, Issue] = {}
    coverage: list[Coverage] = []
    for repo in repos:
        for window, qualifier in windows:
            issues, cov = search_window(client, repo, window, qualifier, settings,
                                        since, seen)
            coverage.append(cov)
            for issue in issues:
                seen[issue.key] = issue
                if not excluded & {label.lower() for label in issue.labels}:
                    found[issue.key] = issue
            print(f"{repo} [{window}]: {cov.fetched} of {cov.total} open issues",
                  file=sys.stderr)
    return found, coverage


# ------------------------------------------------------------------- ranking

LABEL_WORDS = re.compile(r"[a-z0-9]+")
TITLE_TAG = re.compile(
    r"^\W*(feature[\s_-]*request|feature|feat|fr|enhancement|bug)\b", re.IGNORECASE)


def classify(issue: Issue, settings: dict) -> str:
    """'request', 'bug' or 'unlabeled', from labels first and a title tag second."""
    words = {w for label in issue.labels for w in LABEL_WORDS.findall(label.lower())}
    if words & set(settings["request_label_words"]):
        return "request"
    if words & set(settings["bug_label_words"]):
        return "bug"
    match = TITLE_TAG.match(issue.title)
    if match:
        return "bug" if match.group(1).lower() == "bug" else "request"
    return "unlabeled"


def build_rows(issues: dict[str, Issue], as_of: datetime, settings: dict) -> list[Row]:
    rows = []
    for issue in issues.values():
        age = max((as_of - parse_time(issue.created_at)).total_seconds() / 86400, 1.0)
        rows.append(Row(issue, classify(issue, settings), age, issue.thumbs_up / age))
    return rows


def fastest_rising(rows: list[Row], settings: dict, bugs: bool = False) -> list[Row]:
    picked = [r for r in rows
              if (r.kind == "bug") == bugs and r.issue.gain >= settings["min_gain"]]
    picked.sort(key=lambda r: (r.issue.gain, r.issue.thumbs_up), reverse=True)
    return picked[: settings["top_n_bugs" if bugs else "top_n"]]


def new_and_climbing(rows: list[Row], settings: dict) -> list[Row]:
    fresh = [r for r in rows
             if r.kind != "bug"
             and r.age_days <= settings["lookback_days"]
             and r.issue.thumbs_up >= settings["min_reactions"]]
    fresh.sort(key=lambda r: r.velocity, reverse=True)
    return fresh[: settings["top_n"]]


# -------------------------------------------------------------------- report

KIND_LABEL = {"request": "需求 Request", "unlabeled": "未分类 Unlabeled", "bug": "Bug"}
WINDOW_LABEL = {"new": "近 {lookback} 天 · last {lookback}d",
                "mid": "{lookback}–{mid} 天 · {lookback}–{mid}d",
                "top": "全部时间 · all time"}


def cell(text: str) -> str:
    text = text if len(text) <= TITLE_MAX else text[: TITLE_MAX - 1] + "…"
    return text.replace("|", "\\|")


def link(issue: Issue) -> str:
    return f"[#{issue.number} {cell(issue.title)}]({issue.url})"


def labels(issue: Issue) -> str:
    return cell(", ".join(issue.labels[:3])) or "—"


def coverage_cell(cov: Coverage | None) -> str:
    if cov is None:
        return "—"
    if cov.cutoff is None:
        return f"全部 all ({cov.fetched})"
    return f"≥{cov.cutoff} 👍 ({cov.fetched}/{cov.total:,})"


def render_report(as_of: datetime, repos: list[str], rows: list[Row],
                  coverage: list[Coverage], settings: dict) -> str:
    days = settings["gain_days"]
    lookback, mid = settings["lookback_days"], settings["mid_days"]
    lines = [
        f"# Agent 需求雷达 · Agent Demand Radar — {as_of.date().isoformat()}",
        "",
        f"扫描 {len(repos)} 个仓库的未关闭 issue。需求榜不含标为 bug 的 issue，bug 单独列在后面。  ",
        f"Open issues from {len(repos)} repositories. The demand lists leave out "
        "issues labeled as bugs; bugs get their own list below.",
        "",
    ]

    def table(selected: list[Row], with_velocity: bool) -> list[str]:
        if with_velocity:
            out = [f"| # | Repo | Issue | 类型 Type | 👍 | 👍/天 per day | +👍 {days}d "
                   "| 天数 Age | Labels |",
                   "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | --- |"]
        else:
            out = [f"| # | Repo | Issue | 类型 Type | +👍 {days}d | 👍 | 天数 Age | Labels |",
                   "| ---: | --- | --- | --- | ---: | ---: | ---: | --- |"]
        for rank, r in enumerate(selected, 1):
            i = r.issue
            if with_velocity:
                out.append(f"| {rank} | {i.repo} | {link(i)} | {KIND_LABEL[r.kind]} "
                           f"| {i.thumbs_up} | {r.velocity:.1f} | +{i.gain} "
                           f"| {r.age_days:.0f} | {labels(i)} |")
            else:
                out.append(f"| {rank} | {i.repo} | {link(i)} | {KIND_LABEL[r.kind]} "
                           f"| +{i.gain} | {i.thumbs_up} | {r.age_days:.0f} "
                           f"| {labels(i)} |")
        return out

    rising = fastest_rising(rows, settings)
    lines += [
        "## 涨得最快 · Fastest rising",
        "",
        f"最近 {days} 天新增的 👍，按每个 👍 的时间戳计算。  ",
        f"👍 added in the last {days} days, counted from each reaction's timestamp.",
        "",
    ]
    lines += table(rising, with_velocity=False) if rising else [
        f"没有需求在 {days} 天内新增 {settings['min_gain']} 个以上 👍。 / "
        f"No request gained {settings['min_gain']}+ 👍 in {days} days."]
    lines.append("")

    fresh = new_and_climbing(rows, settings)
    lines += [
        f"## 新冒头 · New and climbing (last {lookback} days)",
        "",
        "按“点赞数 ÷ 创建天数”排序。  ",
        "Ranked by 👍 per day since the issue was opened.",
        "",
    ]
    lines += table(fresh, with_velocity=True) if fresh else ["暂无。 / Nothing yet."]
    lines.append("")

    bugs = fastest_rising(rows, settings, bugs=True)
    lines += [
        "## Bug 涨幅 · Fastest-rising bugs",
        "",
        "外部工具通常修不了 bug，单独列出供参考。  ",
        "Bugs are rarely fixable from outside the product; listed for reference.",
        "",
    ]
    lines += table(bugs, with_velocity=False) if bugs else ["暂无。 / Nothing yet."]
    lines.append("")

    by_repo = {(c.repo, c.window): c for c in coverage}
    headers = [WINDOW_LABEL[w].format(lookback=lookback, mid=mid)
               for w in ("new", "mid", "top")]
    lines += [
        "## 覆盖范围 · Coverage",
        "",
        f"每个仓库按创建时间分三段，各取 👍 最多的前 {settings['pool_size']} 个 issue 进入候选池。"
        "表中是每段的入池门槛：低于门槛的 issue 即使涨得快也看不到。  ",
        f"Each repository is searched in three windows by creation date, keeping the "
        f"{settings['pool_size']} most-👍 issues of each. Cells show the entry threshold: "
        "issues below it are invisible to this report even if they are rising.",
        "",
        f"| Repo | {' | '.join(headers)} |",
        "| --- | ---: | ---: | ---: |",
    ]
    for repo in repos:
        cells = [coverage_cell(by_repo.get((repo, w))) for w in ("new", "mid", "top")]
        lines.append(f"| {repo} | {' | '.join(cells)} |")

    lines += [
        "",
        "## 编者点评 · Notes",
        "",
        "- [ ] 哪些需求能在官方产品之外解决（插件、hook、MCP、CLI）？ / "
        "Which of these can be solved outside the core product?",
        "- [ ] 哪些已经有人做了，还缺什么？ / Which already have a community solution, "
        "and what does it lack?",
        "",
        "<sub>Generated by agent-radar. Counts are 👍 reactions on open issues. "
        "Type comes from labels, or from a [FEATURE]/[BUG] title tag when labels say "
        "neither. Issues labeled duplicate or invalid are kept, because maintainers "
        "often use those labels on consolidated community requests.</sub>",
        "",
    ]
    return "\n".join(lines)


def row_dict(r: Row) -> dict:
    return {**asdict(r.issue), "kind": r.kind, "age_days": round(r.age_days, 1),
            "velocity": round(r.velocity, 2)}


def latest_payload(as_of: datetime, rows: list[Row], coverage: list[Coverage],
                   settings: dict) -> dict:
    return {
        "generated_at": as_of.isoformat(timespec="seconds"),
        "gain_days": settings["gain_days"],
        "fastest_rising": [row_dict(r) for r in fastest_rising(rows, settings)],
        "new_and_climbing": [row_dict(r) for r in new_and_climbing(rows, settings)],
        "fastest_rising_bugs": [row_dict(r) for r in fastest_rising(rows, settings, True)],
        "coverage": [asdict(c) for c in coverage],
    }


def snapshot_payload(as_of: datetime, rows: list[Row], coverage: list[Coverage],
                     settings: dict) -> dict:
    return {
        "taken_at": as_of.isoformat(timespec="seconds"),
        "gain_days": settings["gain_days"],
        "coverage": [asdict(c) for c in coverage],
        "issues": {r.issue.key: {**asdict(r.issue), "kind": r.kind}
                   for r in sorted(rows, key=lambda r: r.issue.key)},
    }


# ---------------------------------------------------------------------- main

DEFAULTS = {
    "lookback_days": 60, "mid_days": 365, "pool_size": 100, "gain_days": 7,
    "top_n": 15, "top_n_bugs": 10, "min_reactions": 5, "min_gain": 3,
    "exclude_labels": ["spam"],
    "request_label_words": ["enhancement", "feature", "request", "proposal"],
    "bug_label_words": ["bug", "regression"],
}


def load_settings(config: dict) -> dict:
    given = config.get("settings", {})
    unknown = sorted(set(given) - set(DEFAULTS))
    if unknown:
        print(f"warning: ignoring unknown settings: {', '.join(unknown)}", file=sys.stderr)
    settings = {**DEFAULTS, **{k: v for k, v in given.items() if k in DEFAULTS}}
    if not 1 <= settings["pool_size"] <= 1000:
        raise ValueError("pool_size must be between 1 and 1000 (GitHub search limit)")
    if settings["mid_days"] <= settings["lookback_days"]:
        raise ValueError("mid_days must be larger than lookback_days")
    return settings


def main(argv: list[str] | None = None, transport: Transport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=Path("radar.toml"))
    parser.add_argument("--out", type=Path, default=Path("."),
                        help="base directory for data/ and reports/")
    parser.add_argument("--fixture", type=Path,
                        help="answer queries from a saved JSON file instead of GitHub")
    parser.add_argument("--today", type=date.fromisoformat, default=None,
                        help="pretend the run happens at 00:00 UTC on this date")
    args = parser.parse_args(argv)

    config = tomllib.loads(args.config.read_text(encoding="utf-8"))
    try:
        settings = load_settings(config)
    except ValueError as error:
        parser.error(str(error))
    repos = [entry["name"] for entry in config.get("repos", [])]
    if not repos:
        parser.error("no [[repos]] in config")
    bad = [r for r in repos if not REPO_NAME.match(r)]
    if bad:
        parser.error(f"not an owner/name repository: {', '.join(bad)}")

    interval = 0.0
    if args.fixture:
        transport = FixtureTransport(args.fixture)
    elif transport is None:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            print("error: set GITHUB_TOKEN; GitHub's GraphQL API needs a token "
                  "(any token that can read public repositories).", file=sys.stderr)
            return 2
        transport = urllib_transport(token)
        interval = REQUEST_INTERVAL_S

    if args.today:
        as_of = datetime(args.today.year, args.today.month, args.today.day,
                         tzinfo=timezone.utc)
    else:
        as_of = datetime.now(timezone.utc).replace(microsecond=0)

    client = GitHubClient(transport, interval)
    try:
        issues, coverage = scan(client, repos, as_of, settings)
    except FetchError as error:
        print(f"error: {error}\nNothing was written; the previous report is unchanged.",
              file=sys.stderr)
        return 1

    # Everything is computed before the first write.
    rows = build_rows(issues, as_of, settings)
    day = as_of.date().isoformat()
    report = render_report(as_of, repos, rows, coverage, settings)
    latest = json.dumps(latest_payload(as_of, rows, coverage, settings),
                        ensure_ascii=False, indent=1)
    snapshot = json.dumps(snapshot_payload(as_of, rows, coverage, settings),
                          ensure_ascii=False, indent=1)

    snapshots = args.out / "data" / "snapshots"
    reports = args.out / "reports"
    snapshots.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    (snapshots / f"{day}.json").write_text(snapshot, encoding="utf-8")
    (reports / f"{day}.md").write_text(report, encoding="utf-8")
    (reports / "latest.md").write_text(report, encoding="utf-8")
    (args.out / "data" / "latest.json").write_text(latest, encoding="utf-8")

    print(f"{len(issues)} issues in the pool, {client.calls} API calls; "
          f"report: {reports / f'{day}.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
