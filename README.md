# agent-radar

找出 AI agent 工具里**涨得最快、官方还没做**的需求。  
Find the fastest-rising feature requests in AI agent tools — the ones users want and maintainers haven't shipped.

每周扫描一组 GitHub 仓库的未关闭 issue，记录点赞快照，按增速排序，生成中英双语报告。  
Every week it scans open issues in a set of GitHub repositories, snapshots their 👍 counts, ranks them by growth, and writes a bilingual report.

最新报告 / Latest report: [`reports/latest.md`](reports/latest.md)

## 为什么看增速 · Why growth, not totals

总票数高的需求通常早已有人做了。刚冒头、涨得快的需求才是窗口。  
Issues with the most votes are usually already solved by someone. The window is in issues that are new and climbing fast.

报告有两个榜单 / The report has two lists:

- **涨得最快 · Fastest rising**：与上次快照相比新增的 👍（第二次运行起出现）。  
  👍 gained since the previous snapshot (appears from the second run).
- **新冒头 · New and climbing**：最近 60 天创建的 issue，按“👍 ÷ 天数”排序。  
  Issues opened in the last 60 days, ranked by 👍 per day.

## 用法 · Usage

需要 Python 3.11+，无第三方依赖。 / Python 3.11+, no dependencies.

```bash
export GITHUB_TOKEN=ghp_...        # 任意只读 token 即可 / any read-only token
python radar.py --config radar.toml
```

输出 / Output:

```
data/snapshots/YYYY-MM-DD.json   # 每次运行的点赞快照 / raw snapshot per run
data/latest.json                 # 本次榜单，供复用 / this run's lists, for reuse
reports/YYYY-MM-DD.md            # 报告 / report
reports/latest.md                # 最新报告 / latest report
```

离线测试 / Offline test:

```bash
python radar.py --fixture tests/fixture.json --today 2026-10-22 --out /tmp/radar-test
```

## 自动运行 · Run on a schedule

`.github/workflows/radar.yml` 每周一自动运行，并把快照和报告提交回仓库。fork 或推送本仓库后即可生效，用的是 Actions 自带的 `GITHUB_TOKEN`。  
The workflow runs every Monday and commits snapshots and reports back to the repository, using the built-in Actions `GITHUB_TOKEN`.

## 换一个生态 · Point it at another ecosystem

编辑 `radar.toml` 里的 `[[repos]]` 即可，任何 GitHub 项目都适用。  
Edit the `[[repos]]` entries in `radar.toml`; it works for any GitHub project.

## License

MIT
