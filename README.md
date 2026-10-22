# agent-radar

找出 AI agent 工具里**涨得最快、官方还没做**的需求。  
Find the fastest-rising feature requests in AI agent tools — the ones users want and maintainers haven't shipped.

每周扫描一组 GitHub 仓库的未关闭 issue，按每个 👍 的时间戳算出最近一周的涨幅，需求和 bug 分开排序，生成中英双语报告。  
Every week it scans open issues in a set of GitHub repositories, counts the 👍 each issue gained in the past week from reaction timestamps, ranks feature requests and bugs separately, and writes a bilingual report.

最新报告 / Latest report: [`reports/latest.md`](reports/latest.md)

## 为什么看增速 · Why growth, not totals

总票数高的需求通常早已有人做了。刚冒头、涨得快的需求才是窗口。  
Issues with the most votes are usually already solved by someone. The window is in issues that are new and climbing fast.

## 报告内容 · What the report contains

- **涨得最快 · Fastest rising**：最近 7 天新增 👍 最多的需求。涨幅来自每个 👍 的时间戳，不依赖上一次快照，第一次运行就有结果，新进入候选池的老 issue 也能算出涨幅。  
  Requests that gained the most 👍 in the last 7 days. Gains come from each reaction's timestamp, so they need no earlier snapshot: the first run has them, and an older issue entering the pool gets a gain right away.
- **新冒头 · New and climbing**：最近 60 天创建的需求，按“👍 ÷ 天数”排序。  
  Requests opened in the last 60 days, ranked by 👍 per day.
- **Bug 涨幅 · Fastest-rising bugs**：bug 单独成榜。外部工具通常修不了 bug，不该挤占需求榜。  
  Bugs get their own list: they are rarely fixable from outside the product, so they stay out of the demand lists.
- **覆盖范围 · Coverage**：每个仓库按创建时间分三段（近 60 天、60–365 天、全部时间），各取 👍 最多的前 100 个进入候选池。表中列出每段的入池门槛，低于门槛的 issue 看不到。  
  Each repository is searched in three windows by creation date (last 60 days, 60–365 days, all time), keeping the 100 most-👍 issues of each. The table shows each window's entry threshold; issues below it are invisible.

类型按 label 判断（拆词匹配，如 `kind/bug`、`type/feature`）；label 没说明时，看标题里的 `[FEATURE]`、`[BUG]` 等标记；都没有则记为“未分类”，留在需求榜里由你判断。  
Type comes from labels (matched by word, so `kind/bug` and `type/feature` work); when labels say neither, from a `[FEATURE]` / `[BUG]` title tag; otherwise the issue is "unlabeled" and stays in the demand lists for you to judge.

## 用法 · Usage

需要 Python 3.11+，无第三方依赖。需要一个能读公开仓库的 GitHub token（GraphQL API 不支持匿名访问）。  
Python 3.11+, no dependencies. Needs a GitHub token that can read public repositories (the GraphQL API does not allow anonymous access).

```bash
export GITHUB_TOKEN=ghp_...        # 任意只读 token 即可 / any read-only token
python radar.py --config radar.toml
```

输出 / Output:

```
data/snapshots/YYYY-MM-DD.json   # 每次运行的候选池和覆盖范围 / the pool and coverage of each run
data/latest.json                 # 本次榜单，供复用 / this run's lists, for reuse
reports/YYYY-MM-DD.md            # 报告 / report
reports/latest.md                # 最新报告 / latest report
```

**采集失败就整体失败。** 任何一次请求最终失败（仓库名写错、HTTP 错误、GraphQL 报错、重试用尽），程序以非 0 退出，不写快照、不覆盖报告。同一天重跑结果一致。  
**A failed scan writes nothing.** If any request ultimately fails (misspelled repository, HTTP error, GraphQL error, retries exhausted), the run exits non-zero without writing a snapshot or touching the report. Re-running on the same day gives the same result.

离线运行和测试 / Offline run and tests:

```bash
python radar.py --fixture tests/fixture.json --today 2026-10-22 --out /tmp/radar-test
python -m unittest discover -s tests
```

## 自动运行 · Run on a schedule

`.github/workflows/radar.yml` 每周一自动运行：先跑测试，再扫描，成功后把快照和报告提交回仓库。用的是 Actions 自带的 `GITHUB_TOKEN`。扫描失败时不会提交，运行记录显示为失败。  
The workflow runs every Monday: tests first, then the scan, then it commits the snapshot and report. It uses the built-in Actions `GITHUB_TOKEN`. A failed scan commits nothing and the run shows as failed.

## 换一个生态 · Point it at another ecosystem

编辑 `radar.toml` 里的 `[[repos]]` 即可，任何 GitHub 项目都适用。窗口大小、候选池大小、label 词表也都在这个文件里。  
Edit the `[[repos]]` entries in `radar.toml`; it works for any GitHub project. Window sizes, pool size and label words live in the same file.

## License

MIT
