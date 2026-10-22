"""Offline tests.

The fixture holds real issues captured on 2026-10-22, with synthetic 👍
timestamps for the past week and one synthetic mid-age issue (#999001).

Run: python -m unittest discover -s tests
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import radar  # noqa: E402

FIXTURE = ROOT / "tests" / "fixture.json"
DAY = "2026-10-22"
CONFIG = """
[settings]
top_n = 10

[[repos]]
name = "anthropics/claude-code"

[[repos]]
name = "openai/codex"
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = self.tmp / "radar.toml"
        self.config.write_text(CONFIG, encoding="utf-8")

    def run_radar(self, transport=None, config: Path | None = None) -> int:
        argv = ["--config", str(config or self.config), "--out", str(self.tmp),
                "--today", DAY]
        if transport is None:
            argv += ["--fixture", str(FIXTURE)]
        with mock.patch("radar.time.sleep"):        # retries must not slow tests
            return radar.main(argv, transport=transport)

    def latest(self) -> dict:
        return json.loads((self.tmp / "data" / "latest.json").read_text(encoding="utf-8"))

    def report(self) -> str:
        return (self.tmp / "reports" / "latest.md").read_text(encoding="utf-8")


class RankingTest(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.run_radar(), 0)

    def test_rising_ranks_by_reaction_timestamps(self):
        rising = [(r["number"], r["gain"]) for r in self.latest()["fastest_rising"]]
        self.assertEqual(rising, [(102273, 41), (45596, 40), (91870, 31), (15942, 25),
                                  (999001, 15), (48195, 12)])
        self.assertNotIn(8745, [n for n, _ in rising])   # +2 is below min_gain

    def test_new_issue_has_gain_on_first_sight(self):
        # Created inside the 7-day window: every 👍 counts, no earlier snapshot needed.
        brand_new = next(r for r in self.latest()["new_and_climbing"]
                         if r["number"] == 102273)
        self.assertEqual(brand_new["gain"], 41)

    def test_mid_age_issue_reaches_the_pool(self):
        # Outside both the last-60-days and all-time top lists; found by the mid window.
        self.assertIn(999001, [r["number"] for r in self.latest()["fastest_rising"]])

    def test_bugs_are_listed_separately(self):
        latest = self.latest()
        demand = latest["fastest_rising"] + latest["new_and_climbing"]
        self.assertNotIn("bug", {r["kind"] for r in demand})
        self.assertEqual([(r["number"], r["gain"]) for r in latest["fastest_rising_bugs"]],
                         [(48237, 20)])

    def test_new_and_climbing_ranked_by_velocity(self):
        fresh = self.latest()["new_and_climbing"]
        self.assertEqual(fresh[0]["number"], 48195)       # ~7.1 👍/day
        velocities = [r["velocity"] for r in fresh]
        self.assertEqual(velocities, sorted(velocities, reverse=True))

    def test_duplicate_label_is_kept(self):
        buddy = next(r for r in self.latest()["fastest_rising"] if r["number"] == 45596)
        self.assertIn("duplicate", buddy["labels"])

    def test_report_shows_coverage_thresholds(self):
        report = self.report()
        self.assertIn("## 覆盖范围 · Coverage", report)
        self.assertIn("| anthropics/claude-code | ≥25 👍 (7/2,431) | ≥40 👍 (4/1,874) "
                      "| ≥422 👍 (6/9,120) |", report)

    def test_report_links_and_gains(self):
        report = self.report()
        self.assertIn("[#45596 Bring Back Buddy", report)
        self.assertIn("| 需求 Request | +40 | 1194 |", report)
        self.assertIn("## Bug 涨幅 · Fastest-rising bugs", report)

    def test_same_day_rerun_gives_same_result(self):
        before = self.latest()
        self.assertEqual(self.run_radar(), 0)
        self.assertEqual(self.latest(), before)


class FailureTest(Base):
    """Any collection failure: non-zero exit, nothing written or overwritten."""

    def setUp(self):
        super().setUp()
        (self.tmp / "reports").mkdir()
        (self.tmp / "reports" / "latest.md").write_text("previous report", encoding="utf-8")
        self.fixture = radar.FixtureTransport(FIXTURE)

    def assert_nothing_written(self):
        self.assertEqual(self.report(), "previous report")
        self.assertFalse((self.tmp / "data").exists())
        self.assertFalse((self.tmp / "reports" / f"{DAY}.md").exists())

    def failing_search(self, error):
        def transport(payload):
            if "search(" in payload["query"] and "openai/codex" in payload["variables"]["q"]:
                if isinstance(error, Exception):
                    raise error
                return error
            return self.fixture(payload)
        return transport

    def test_http_422_fails_the_run(self):
        error = radar.HttpError(422, '{"message":"Validation Failed"}')
        self.assertEqual(self.run_radar(self.failing_search(error)), 1)
        self.assert_nothing_written()

    def test_graphql_error_fails_the_run(self):
        body = {"data": None, "errors": [{"message": "Something is wrong with q"}]}
        self.assertEqual(self.run_radar(self.failing_search(body)), 1)
        self.assert_nothing_written()

    def test_persistent_server_error_fails_after_retries(self):
        calls = []

        def transport(payload):
            calls.append(payload)
            raise radar.HttpError(502, "Bad Gateway")

        self.assertEqual(self.run_radar(transport), 1)
        self.assertEqual(len(calls), radar.MAX_ATTEMPTS)
        self.assert_nothing_written()

    def test_unknown_repo_fails_before_searching(self):
        config = self.tmp / "typo.toml"
        config.write_text(CONFIG + '\n[[repos]]\nname = "openai/codexx"\n', encoding="utf-8")
        searches = []

        def transport(payload):
            if "search(" in payload["query"]:
                searches.append(payload)
            return self.fixture(payload)

        self.assertEqual(self.run_radar(transport, config), 1)
        self.assertEqual(searches, [])
        self.assert_nothing_written()

    def test_reactions_not_newest_first_fails(self):
        def transport(payload):
            body = self.fixture(payload)
            if "search(" in payload["query"]:
                for node in body["data"]["search"]["nodes"]:
                    node["reactions"]["nodes"].reverse()
            return body

        self.assertEqual(self.run_radar(transport), 1)
        self.assert_nothing_written()

    def test_missing_token_fails(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            code = radar.main(["--config", str(self.config), "--out", str(self.tmp)])
        self.assertEqual(code, 2)
        self.assert_nothing_written()


class RetryTest(Base):
    def test_transient_errors_are_retried(self):
        fixture = radar.FixtureTransport(FIXTURE)
        failures = iter([radar.HttpError(502, "Bad Gateway"), radar.HttpError(0, "reset"),
                         radar.HttpError(403, "secondary rate limit",
                                         {"Retry-After": "1"})])

        def transport(payload):
            error = next(failures, None)
            if error:
                raise error
            return fixture(payload)

        self.assertEqual(self.run_radar(transport), 0)
        self.assertEqual(self.latest()["fastest_rising"][0]["number"], 102273)

    def test_plain_403_is_not_retried(self):
        self.assertIsNone(radar.retry_delay(radar.HttpError(403, "Forbidden"), 0))
        self.assertIsNone(radar.retry_delay(radar.HttpError(401, "Bad credentials"), 0))


class ClassifyTest(unittest.TestCase):
    def kind(self, title: str, labels: list[str]) -> str:
        issue = radar.Issue("o/r", 1, title, "", "", "", labels, 0, 0, 0)
        return radar.classify(issue, radar.DEFAULTS)

    def test_labels(self):
        self.assertEqual(self.kind("x", ["enhancement", "area:tui"]), "request")
        self.assertEqual(self.kind("x", ["type/feature"]), "request")
        self.assertEqual(self.kind("x", ["kind/bug", "priority/p2"]), "bug")
        self.assertEqual(self.kind("x", ["regression"]), "bug")
        self.assertEqual(self.kind("x", ["Bug"]), "bug")
        self.assertEqual(self.kind("x", ["debug-logs"]), "unlabeled")

    def test_request_label_wins_over_bug_label(self):
        self.assertEqual(self.kind("x", ["bug", "enhancement"]), "request")

    def test_title_tag_when_labels_say_neither(self):
        self.assertEqual(self.kind("[FEATURE]: Add /goal", []), "request")
        self.assertEqual(self.kind("[FEATURE(app)]: images", ["VS Code"]), "request")
        self.assertEqual(self.kind("feat(tasks): registry", []), "request")
        self.assertEqual(self.kind("[FR] Allow custom args", []), "request")
        self.assertEqual(self.kind("Feature Request: Zen API", []), "request")
        self.assertEqual(self.kind("[Bug]: loop", []), "bug")
        self.assertEqual(self.kind("[FEATURE] Multi-account", ["invalid"]), "request")
        self.assertEqual(self.kind("Free plan shows no models", []), "unlabeled")
        self.assertEqual(self.kind("Keybinds", []), "unlabeled")

    def test_label_beats_title_tag(self):
        self.assertEqual(self.kind("[FEATURE] crashes", ["bug"]), "bug")


if __name__ == "__main__":
    unittest.main()
