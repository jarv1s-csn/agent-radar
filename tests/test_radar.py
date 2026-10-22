"""Offline tests: replay real API items captured on 2026-10-22.

Run: python -m unittest discover -s tests
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import radar  # noqa: E402

FIXTURE = ROOT / "tests" / "fixture.json"
CONFIG = """
[settings]
lookback_days = 60
top_n = 10
min_reactions = 5
min_gain = 3
per_page = 50
exclude_labels = ["spam"]

[[repos]]
name = "anthropics/claude-code"

[[repos]]
name = "openai/codex"
"""

# 👍 each issue gained between the two runs (2026-10-15 -> 2026-10-22).
GAINS = {45596: 40, 15942: 25, 91870: 31, 48195: 12, 8745: 2}


def earlier_fixture() -> dict:
    data = copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))
    for items in data.values():
        for item in items:
            item["reactions"]["+1"] -= GAINS.get(item["number"], 0)
    # An issue that did not exist a week earlier.
    data["anthropics/claude-code|new"] = [
        i for i in data["anthropics/claude-code|new"] if i["number"] != 102273]
    return data


class RadarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "radar.toml").write_text(CONFIG, encoding="utf-8")
        earlier = self.tmp / "earlier.json"
        earlier.write_text(json.dumps(earlier_fixture()), encoding="utf-8")
        self.run_radar(earlier, "2026-10-15")
        self.run_radar(FIXTURE, "2026-10-22")

    def run_radar(self, fixture: Path, day: str) -> None:
        radar.main(["--config", str(self.tmp / "radar.toml"), "--out", str(self.tmp),
                    "--fixture", str(fixture), "--today", day])

    def test_first_run_has_no_gains(self):
        first = (self.tmp / "reports" / "2026-10-15.md").read_text(encoding="utf-8")
        self.assertIn("First run", first)

    def test_rising_ranks_by_gain(self):
        latest = json.loads((self.tmp / "data" / "latest.json").read_text(encoding="utf-8"))
        rising = [(r["number"], r["gain"]) for r in latest["fastest_rising"]]
        self.assertEqual(rising[:4], [(45596, 40), (91870, 31), (15942, 25), (48195, 12)])
        # A 2-👍 gain is below min_gain.
        self.assertNotIn(8745, [n for n, _ in rising])

    def test_duplicate_label_is_kept(self):
        latest = json.loads((self.tmp / "data" / "latest.json").read_text(encoding="utf-8"))
        buddy = next(r for r in latest["fastest_rising"] if r["number"] == 45596)
        self.assertIn("duplicate", buddy["labels"])

    def test_new_issue_ranked_by_velocity(self):
        latest = json.loads((self.tmp / "data" / "latest.json").read_text(encoding="utf-8"))
        fresh = latest["new_and_climbing"]
        # #48195: 185 👍 in ~26 days (~7.1/day) beats #102273: 41 👍 in ~7 days (~6.0/day).
        self.assertEqual(fresh[0]["number"], 48195)
        brand_new = next(r for r in fresh if r["number"] == 102273)
        self.assertIsNone(brand_new["gain"])  # absent from the earlier snapshot
        velocities = [r["velocity"] for r in fresh]
        self.assertEqual(velocities, sorted(velocities, reverse=True))

    def test_report_escapes_and_links(self):
        report = (self.tmp / "reports" / "latest.md").read_text(encoding="utf-8")
        self.assertIn("Compared with the snapshot from 2026-10-15 (7 days)", report)
        self.assertIn("[#45596 Bring Back Buddy", report)
        self.assertIn("| +40 |", report)

    def test_snapshots_saved(self):
        names = sorted(p.name for p in (self.tmp / "data" / "snapshots").iterdir())
        self.assertEqual(names, ["2026-10-15.json", "2026-10-22.json"])


if __name__ == "__main__":
    unittest.main()
