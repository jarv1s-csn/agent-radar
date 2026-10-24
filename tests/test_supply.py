"""Offline tests for supply.py with a fake GitHub search API."""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import radar  # noqa: E402
import supply  # noqa: E402

CONFIG = """
[settings]
min_stars = 100

[categories.multimedia]
label = "Multimedia"
signals_topic = "Multimedia"
topics = ["text-to-image", "comfyui"]

[categories.writing]
label = "Writing"
topics = ["ai-writing"]
"""


def repo(name: str, stars: int, topics: list[str], created: str = "2026-03-01T00:00:00Z"):
    return {"name": name.split("/")[1], "full_name": name, "html_url": f"https://github.com/{name}",
            "description": f"{name} description", "stargazers_count": stars,
            "created_at": created, "pushed_at": "2026-10-01T00:00:00Z",
            "language": "Python", "archived": False, "topics": topics}


class FakeSearch:
    """Behaves like GitHub: sorted by stars, 1,000-result cap, 422 past it."""

    def __init__(self, repos: list[dict]):
        self.repos = repos
        self.calls = []

    def __call__(self, params: dict) -> dict:
        self.calls.append(params)
        topic, lo, hi = re.fullmatch(r"topic:(\S+) stars:(\d+)\.\.(\d+)", params["q"]).groups()
        hits = sorted((r for r in self.repos
                       if topic in r["topics"] and int(lo) <= r["stargazers_count"] <= int(hi)),
                      key=lambda r: -r["stargazers_count"])
        start = (params["page"] - 1) * params["per_page"]
        if start >= supply.MAX_RESULTS:
            raise radar.HttpError(422, "Only the first 1000 search results are available")
        return {"total_count": len(hits), "incomplete_results": False,
                "items": hits[start:start + params["per_page"]]}


def big_catalog() -> list[dict]:
    # 2,500 text-to-image repos: more than one search can return.
    items = [repo(f"img/r{i}", 100 + i, ["text-to-image"]) for i in range(2500)]
    items += [
        repo("comfy/ComfyUI", 90000, ["comfyui", "text-to-image"], "2023-01-01T00:00:00Z"),
        repo("lists/awesome-comfy", 5000, ["comfyui"]),
        repo("pen/writer", 800, ["ai-writing"]),
        repo("pen/tiny", 50, ["ai-writing"]),          # below min_stars
    ]
    return items


class SupplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = self.tmp / "supply.toml"
        self.config.write_text(CONFIG, encoding="utf-8")

    def run_supply(self, transport) -> int:
        with mock.patch("supply.time.sleep"):
            return supply.main(["--config", str(self.config), "--out", str(self.tmp),
                                "--today", "2026-10-24"], transport=transport)

    def data(self) -> dict:
        return json.loads((self.tmp / "data" / "supply" / "2026-10-24.json")
                          .read_text(encoding="utf-8"))

    def test_splits_past_the_result_cap_and_dedupes(self):
        fake = FakeSearch(big_catalog())
        self.assertEqual(self.run_supply(fake), 0)
        data = self.data()
        multimedia = next(c for c in data["categories"] if c["category"] == "multimedia")
        # 2,500 generated + ComfyUI, counted once although it matches both topics.
        self.assertEqual(multimedia["repos"], 2501)
        self.assertEqual(multimedia["top"][0]["full_name"], "comfy/ComfyUI")
        comfy = next(r for r in data["repos"] if r["full_name"] == "comfy/ComfyUI")
        self.assertEqual(comfy["matched_topics"], ["text-to-image", "comfyui"])
        self.assertTrue(all(c["page"] <= 10 for c in fake.calls))

    def test_skips_lists_and_low_star_repos(self):
        self.assertEqual(self.run_supply(FakeSearch(big_catalog())), 0)
        names = {r["full_name"] for r in self.data()["repos"]}
        self.assertNotIn("lists/awesome-comfy", names)
        self.assertNotIn("pen/tiny", names)
        writing = next(c for c in self.data()["categories"] if c["category"] == "writing")
        self.assertEqual((writing["repos"], writing["new_last_365d"]), (1, 1))

    def test_report_written(self):
        self.assertEqual(self.run_supply(FakeSearch(big_catalog())), 0)
        report = (self.tmp / "reports" / "supply-2026-10-24.md").read_text(encoding="utf-8")
        self.assertIn("| Multimedia | 2,501 |", report)
        self.assertIn("| multimedia | `comfyui` | 2 | 1 |", report)

    def test_failure_writes_nothing(self):
        fake = FakeSearch(big_catalog())

        def transport(params):
            if "ai-writing" in params["q"]:
                raise radar.HttpError(422, "Validation Failed")
            return fake(params)

        self.assertEqual(self.run_supply(transport), 1)
        self.assertFalse((self.tmp / "data").exists())
        self.assertFalse((self.tmp / "reports").exists())

    def test_incomplete_results_retried_then_fail(self):
        def transport(params):
            return {"total_count": 1, "incomplete_results": True, "items": []}

        self.assertEqual(self.run_supply(transport), 1)


if __name__ == "__main__":
    unittest.main()
