# Stock Analysis System v7

US-stock investment-analysis skills — business-quality scoring, investment
theses (valuation / technical / events), portfolio decisions, screening, and
visual dashboards. Runs on **Claude Code, Cowork, Codex, Cursor, and OpenCode**.

美股投资分析 skill 系统 —— 业务质量评分、投资论点(估值 / 技术 / 事件)、组合决策、
选股、可视化看板。可在 **Claude Code、Cowork、Codex、Cursor、OpenCode** 上运行。

**[English](#english) · [中文](#中文)**

---

## English

### Quickstart

```bash
git clone https://github.com/Vibing-Alpha/investment-skill.git && cd investment-skill
python3 -m pip install -r requirements.txt      # yfinance + PyYAML
make setup                                       # guided first-run setup
# no make (e.g. native Windows)? → python3 -m scripts.distribute bootstrap
```

`make setup` walks you through your API key (`.env`), strategy (`strategy.yaml`),
and holdings (`portfolio-state.yaml`) — all personal + gitignored. Then open the
repo in your agent and try `/score-business AAPL`.

> **Run your agent from the repo root** — skills use repo-relative paths and do
> not work installed globally. Each agent reads its own skill layout
> automatically (`.claude/skills/` for Claude Code / Cowork; `.agents/skills/` +
> `AGENTS.md` for Codex / Cursor / OpenCode) — no extra setup.

### Skills

| Command | What it does |
|---------|--------------|
| `/score-business TICKER` | Business-quality analysis |
| `/investment-thesis TICKER` | Valuation + technical timing + catalysts + thesis |
| `/portfolio` | Whole-portfolio review + buy/sell/hold + IBKR orders |
| `/screen-stocks` | Find tickers by price action / sector / watchlist |
| `/research-industry` | Candidate tickers in a sector |
| `/monitor` | Daily triage of holdings + watchlist (routes; never trades) |
| `/write-report TICKER` | Readable Markdown report from an analysis |
| `/generative-ui` | Standalone HTML dashboard from an analysis |

See [`CLAUDE.md`](CLAUDE.md) for the data flow and output conventions (every
number is sourced; units/FX explicit; portfolio limits enforced). Human-facing
reports honor `output_language` in `strategy.yaml` (any language); JSON analysis
is always English.

### Updating

```bash
python3 -m scripts.update check     # newer release? (also auto-checked on session start)
python3 -m scripts.update apply     # fast-forward to it + show the changelog
```

Updates are opt-in and never overwrite your local edits. Release notes:
[`CHANGELOG.md`](CHANGELOG.md).

### Requirements

Python 3.10+, `yfinance` + `PyYAML`. Two data-API keys are required —
`FINANCIAL_DATASETS_API_KEY` (financialdatasets.ai) and `FMP_API_KEY`
(financialmodelingprep.com); `FINNHUB_API_KEY` (finnhub.io) is optional.

---

## 中文

### 快速开始

```bash
git clone https://github.com/Vibing-Alpha/investment-skill.git && cd investment-skill
python3 -m pip install -r requirements.txt      # yfinance + PyYAML
make setup                                       # 引导式首次设置
# 没有 make(如原生 Windows)? → python3 -m scripts.distribute bootstrap
```

`make setup` 会引导你填:API key(`.env`)、投资策略(`strategy.yaml`)、持仓
(`portfolio-state.yaml`)—— 全部是个人配置且 gitignored。然后在你的 agent 里打开本
仓库,试试 `/score-business AAPL`。

> **务必在仓库根目录启动 agent** —— skill 用的是仓库相对路径,全局安装无法工作。每种
> agent 会自动读取各自的 skill 布局(Claude Code / Cowork 读 `.claude/skills/`;
> Codex / Cursor / OpenCode 读 `.agents/skills/` + `AGENTS.md`)—— 无需额外设置。

### Skills(技能)

| 命令 | 作用 |
|------|------|
| `/score-business TICKER` | 业务质量分析 |
| `/investment-thesis TICKER` | 估值 + 技术择时 + 催化事件 + 投资论点 |
| `/portfolio` | 全组合复盘 + 买/卖/持 + IBKR 订单 |
| `/screen-stocks` | 按涨跌幅 / 板块 / 自选筛选股票 |
| `/research-industry` | 某行业的候选标的 |
| `/monitor` | 持仓+自选每日分诊(路由到对应 skill;从不下单)|
| `/write-report TICKER` | 把分析写成可读的 Markdown 报告 |
| `/generative-ui` | 把分析做成独立 HTML 看板 |

数据流与输出约定(每个数字都带来源标签;单位/汇率显式;组合限额强制)详见
[`CLAUDE.md`](CLAUDE.md)。人面向报告用 `strategy.yaml` 的 `output_language`(任意语言);
JSON 分析恒为英文。

### 更新

```bash
python3 -m scripts.update check     # 有新版吗?(会话启动时也会自动检查)
python3 -m scripts.update apply     # 快进到最新版 + 显示更新日志
```

更新是可选的,绝不覆盖你的本地改动。更新日志见 [`CHANGELOG.md`](CHANGELOG.md)。

### 环境要求

Python 3.10+,`yfinance` + `PyYAML`。需要两个数据 API key:`FINANCIAL_DATASETS_API_KEY`
(financialdatasets.ai)和 `FMP_API_KEY`(financialmodelingprep.com);
`FINNHUB_API_KEY`(finnhub.io)可选。
