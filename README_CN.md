<p align="right"><a href="README.md">English</a></p>

<br>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue?style=flat-square&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/平台-35-orange?style=flat-square" alt="平台">
  <img src="https://img.shields.io/badge/数据源-11-green?style=flat-square" alt="数据源">
  <img src="https://img.shields.io/badge/查询-~5000-red?style=flat-square" alt="查询">
  <img src="https://img.shields.io/badge/测试-479-success?style=flat-square" alt="测试">
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=flat-square" alt="License">
</p>

<h1 align="center">🌲 DarkForest Hunter</h1>

<p align="center">
  <em>"宇宙就是一座黑暗森林，每个文明都是带枪的猎人。"</em><br>
  <sub>— <strong>刘慈欣</strong>，《三体》</sub>
</p>

---

> 开源安全研究工具：扫描公开代码仓库中泄露的 AI API Key（覆盖 **35 个 AI 平台**），逐个验证有效性（先只读 GET models/balance，再补一次 `max_tokens=1` 最小探测确认实际可用；均可关）并支持余额查询（部分平台）。起因是我们震惊地发现大量有余额的 key 就那样躺在公开仓库里，数月无人知晓。

---

## 🌲 黑暗森林

在 GitHub 的代码森林里，数百万开发者每天都在提交代码。每一行 `API_KEY=sk-...` 都是一次**广播** —— 一个文明暴露了自己的坐标。

**我们是这片森林里的猎人。**

这源自刘慈欣《三体》的黑暗森林理论：每个泄露的 key 都是一次坐标广播。只不过在网络安全的森林里，猎人可能是自动化脚本、加密货币矿工、数据窃贼，或者更糟。

## 🔭 为什么存在

AI API 已成为基础设施。每天都有成千上万的开发者把 API Key 硬编码进配置文件、测试脚本、Jupyter Notebook、Docker Compose、GitHub Actions —— 然后不小心推送到公开仓库。

我们造这个工具想回答一个问题：**公开代码里暴露了多少 AI Key？** 答案让我们震惊 —— 不只是 key，很多还带着**可观余额**。这些 key 就那样暴露着，数月无人知晓。

## ✨ 核心特性

| | |
|---|---|
| 🎯 **原生配额调度（v2.5）** | 以 GitHub 的 `X-RateLimit-*` 响应头为唯一真相——把剩余配额均匀铺满整个限流窗口，扫描器**永不提前打光配额**、永不触发 sleep-to-reset、永不撞主配额 429。 |
| 🌐 **开箱即用的多 IP 代理（v2.4.7+）** | 内置 **mihomo 客户端**——填入任意 VMess/Trojan/Hysteria2/SS 订阅，每个 GitHub token 独享一个出口 IP、独立限速节奏；订阅不可用时自动回退单代理。 |
| 🛡️ **分层防滥用体系** | IP 级惩罚箱、429 分层冷却、深度退避静默、传输故障自动换端口——全部基于真实 GitHub 二级限流的实测调校，而非纸上谈兵。 |
| ✅ **准确性优先的验证** | 35 平台逐家实测鉴权语义（部分 `/models` 端点公开——这类平台补一次最小探测而非误判有效）。余额口径严格区分：现金 vs 代金券 vs 周额度百分比，绝不混算。 |
| 📈 **收益学习查询引擎** | 约 5000 条查询 + 实时反馈闭环：烧配额不出转化的查询被质量熔断；高产查询加深翻页；头部查询自动派生变体。 |
| 📬 **24/7 watch 与告警** | 优先级验证队列、预算制重验调度（余额缩水/充值/重新激活检测）、高价值邮件告警、TUI 仪表盘、一键启停脚本。 |

## 🎯 做什么

**24/7 持续 watch 模式**（唯一扫描入口）循环扫描数据源，把发现的 key 送入验证队列，高价值发现邮件告警。所有参数都有合理默认，`python -u run.py watch` 一条命令即包含全部。

### Watch 模式架构

```
数据源 (生产者)  ──▶  VerificationBroker (优先级队列)  ──▶  Worker (消费者)
  github_search           │  新 key: 优先级 0                    ├─ GET /models (只读)
  github_commits          │  重验:   优先级 20                   ├─ 余额查询 (只读)
  github_events (实时)     │                                     └─ chat 探测（默认开启，--no-allow-chat-probe 可关）
  gitlab                  │  ──▶  SQLite 账本 + CSV 台账  ──▶  邮件告警 (高价值)
  npm                     │         └──▶  TUI 仪表盘 (2fps)

GitHub Code Search 请求 ──▶  RateScheduler (header 驱动)     ──▶  配额窗口内均匀铺排,
                           └─▶  MultiProxyRouter / mihomo     ──▶  零打光零 sleep-to-reset
                               (每 token 一个出口 IP)             每 IP 独立限速
```

### 支持平台 (35 个)

**国内外主平台 (12)：** 深度求索 DeepSeek、月之暗面 Kimi、智谱 AI GLM、阿里通义千问 Qwen、稀宇科技 MiniMax、字节跳动豆包 Doubao、百川智能 Baichuan、零一万物 Yi、小米 MiMo、阶跃星辰 StepFun、商汤科技日日新 SenseNova、Anthropic Claude。

**Coding Plan / 订阅套餐 (5)：** Kimi Code、智谱 GLM Coding Plan、通义 Coding Plan、MiniMax Coding Plan、MiMo Token Plan。

**国际平台 (10)：** OpenRouter、Groq、Replicate、Together AI、Fireworks AI、硅基流动 SiliconFlow、Novita AI、DeepInfra、Jina AI、Voyage AI。

**第二批接入 (2026-09, 8)：** OpenAI、Google Gemini、xAI Grok、腾讯混元、百度千帆、魔搭 ModelScope、NVIDIA NIM、美团 LongCat。

> **验证说明：** 魔搭 / NVIDIA NIM / LongCat 三家的 `/models` 端点**公开不鉴权**（假 key 也 200），只读模式判不了——默认各补一次最小探测（`probe_unclear_platforms = true`，`--no-probe-unclear` 可关）。千帆对无效 key 返回 403；Gemini 走 `?key=` 查询参数认证；OpenRouter 用 `/auth/key` 验证（其 `/models` 是公开目录）；NVIDIA 探测模型保持现役（EOL 模型会在鉴权前返回 410）。零一万物（Yi）API 已停运（HTTP 410），已停用。

**支持余额查询：** deepseek / kimi / zhipu / stepfun / siliconflow / minimax(cp) / openrouter（走 `/auth/key`：额度上限 − 已用）。**GLM Coding Plan 显示周额度剩余百分比**（单位 `PERCENT`，0% 也记录，绝不混入 CNY 合计）。Kimi 余额取 **`cash_balance` 现金口径**（代金券不是钱）。其余平台仅验证有效性。

> **探测策略：** 默认只读 GET，每个 key 补一次 `max_tokens=1` 最小确认探测（`--no-allow-chat-probe` 可关）。探测放行但余额端点**确凿查出** ≤0 的 key 记为 `valid_zero`——"active" 不等于"有钱"。

### 数据源 (11 个源，5 个 watch 默认)

| 类别 | 数据源 | watch 默认 |
|------|--------|-----------|
| GitHub 家族 | Code Search、Commits、**Events (实时 PushEvent)**、Gist、Issues、Raw | ✅ Code Search、Commits、Events |
| 代码托管 | GitLab | ✅ GitLab |
| AI 平台 | HuggingFace (Models/Datasets/Spaces) | — |
| 包注册表 | npm | ✅ npm |
| 剪贴板 | Pastebin / Rentry / ControlC | — |
| 容器 | Docker Hub | — |

> 非默认源（Gist/Issues/HF/paste_sites 等）可通过 `--sources` 添加；完整可用目录以 `python run.py --list-sources` 为准。

### 适用场景

- **安全研究** — 量化 API Key 泄露的规模与模式
- **组织审计** — 扫描自有仓库的意外凭据泄露
- **漏洞赏金** — 发现泄露 key 用于赏金项目
- **持续监控** — 24/7 watch + 高价值邮件告警

## 🚀 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# (可选) GitHub 认证以提高速率限制
gh auth login

# 24/7 watch（唯一扫描入口,挂机跑,Ctrl+C 退出）
python -u run.py watch

# 无头/后台运行(日志进 results/watch_session_*.log)
python -u run.py watch --no-tui

# 裸启动等价于 watch
python run.py

# 列出可用数据源
python run.py --list-sources
```

### 本地账本维护

```bash
# 脱敏确定性 invalid 明文；--prune-invalid-days 90 可同时删除过期记录
python run.py maintenance --db results/darkforest.db --vacuum
```

### 产出画像

```bash
# 聚合查看候选/有效/高价值转化，不输出 key
python run.py metrics --min-balance 1

# 同时探测 GitHub token 健康状态
python run.py metrics --min-balance 1 --check-github
```

### Watch 参数

```bash
# 无头模式(后台/服务器) — 日志写入 results/watch_session_*.log
python -u run.py watch --no-tui

# 带 TUI 仪表盘(终端 UI, Ctrl+C 退出)
python run.py watch

# 自定义数据源 + 验证调参
python -u run.py watch --no-tui \
  --sources github_search github_events gitlab npm \
  --verify-workers 8 --verify-interval 0.25 \
  --reverify-budget 1500 --reverify-zero-hours 168

# 仅跑一轮(测试用)
python run.py watch --no-tui --once

# 多代理订阅(VMess/Trojan/Hysteria2/SS → 内嵌 mihomo,每 token 独立出口 IP)
# 也可写进 config.ini 的 [proxy].subscription
python -u run.py watch --no-tui --proxy-subscription "https://你的订阅链接"
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--interval` | 300s | 每轮扫描间隔秒数 |
| `--min-balance` | ¥1.0 | 仅记录余额高于此值的 key |
| `--verify-interval` | 0.25s | 每个 worker 验证间隔(QPS ≈ workers/interval) |
| `--verify-workers` | 8 | 并发验证 worker 数 |
| `--reverify-budget` | 1500/天 | 历史有效 key 每日重验预算 |
| `--reverify-zero-hours` | 168 (7天) | 0 余额 key 重验间隔 |
| `--hv-email-threshold` | ¥5 | 新发现高价值 key 邮件阈值 |
| `--hv-top-threshold` | ¥10 | "顶级" key 重验阈值(6h) |
| `--shrink-warn-pct` | 30% | 余额跌幅触发缩水预警的百分比 |
| `--commits-since-hours` | 168 (7天) | github_commits 源时间窗口 |
| `--github-pages` | 1 | Code Search 每查询页数(越大越慢) |
| `--proxy-subscription` | — | 代理订阅 URL(内嵌 mihomo 多 IP 模式) |
| `--no-allow-chat-probe` | 探测开 | 关闭 `max_tokens=1` 确认探测 |
| `--no-probe-unclear` | 探测开 | 关闭公开 `/models` 平台的兜底探测 |
| `--fresh` | — | 查询轮转从头开始 |

### 编程调用

```python
from scanner_engine import ScannerEngine, build_active_queries

queries = build_active_queries()  # 静态查询 + 动态滚动时间窗口
engine = ScannerEngine(concurrency=15, scan_pages=5, max_duration=3600,
                       output_dir="./results", proxy="http://127.0.0.1:7897")
results = engine.run(queries)
```

## 📁 项目结构

```
DarkForest-Hunter/
├── run.py                    # 统一 CLI (watch 唯一扫描入口 + maintenance/metrics)
├── watch_tui.py              # 24/7 watch: 队列 + 调度器 + TUI + 重验
├── watch_persistence.py      # watch state / SQLite 历史 / CSV 台账读写
├── scanner_engine.py         # 扫描引擎 (GitHub 搜索 + 查询追踪 + 验证)
├── rate_scheduler.py         # 原生配额调度器 (header 驱动均匀铺排)
├── proxy_resolver.py         # SmartProxy + MultiProxyRouter (per-IP token 路由)
├── mihomo_manager.py         # 内嵌 mihomo 生命周期 (下载/配置/启停)
├── providers.py              # 35 平台配置 + UnifiedKeyMatcher/Verifier
├── store.py                  # SQLite key 存储(历史 + 跨运行去重)
├── query_rotation.py         # 查询轮转 + 变异引擎 + 生成池
├── query_enumerator.py       # 离线: 重新生成扩展查询池
├── email_notifier.py         # 异步邮件告警(去重 + HTML 模板)
├── trend_monitor.py          # DB 指标记录(trend.jsonl)
├── config_loader.py          # config.ini 加载器
├── config.ini.example        # 配置模板
├── scanners/                 # 11 个已调度源扫描器(继承 BaseScanner)
│   ├── base.py               # BaseScanner + extract_keys + is_bad_key + 429 退避
│   ├── github_events.py      # 实时 PushEvent 监控(watch 默认源)
│   ├── github_commits.py     # 提交历史 + diff 扫描
│   ├── github_gist.py        # GitHub Gist 扫描
│   ├── github_issues.py      # GitHub Issues/PR 扫描
│   ├── github_raw.py         # 宽泛 sk- 原始内容扫描
│   ├── gitlab.py             # GitLab blob 搜索
│   ├── huggingface.py        # HuggingFace (Models/Datasets/Spaces)
│   ├── npm_registry.py       # npm 注册表扫描(watch 默认源)
│   ├── docker.py             # Docker Hub 镜像层扫描
│   └── paste_sites.py        # Pastebin + Google Dork
├── bin/                      # mihomo 二进制(首次多代理使用时下载, gitignored)
├── results/                  # 运行时输出(gitignored — 含真实 key)
│   ├── darkforest.db         # SQLite key 存储
│   ├── watch_state.json      # 最新验证视图
│   ├── watch_high_value.csv  # 高价值 key 台账
│   ├── query_stats.json      # 查询收益学习
│   └── watch_session_*.log   # 每会话日志
├── tests/                    # 479 个测试(pytest, 全部离线/mock)
├── docs/                     # 手册与分析文档
├── README.md                 # 英文版
├── README_CN.md              # 本文件(中文版)
├── CHANGELOG.md              # 版本变更记录
└── LICENSE                   # MIT License
```

## ⚙️ 配置

复制 `config.ini.example` → `config.ini` 后填写：

- `[proxy]` — HTTP 代理(Clash/V2Ray)，缺省自动探测；`subscription` = 代理订阅 URL(内嵌 mihomo 多 IP 模式)
- `[github]` — Token，逗号分隔多 token 并行(每 token 10 req/min)
- `[SMTP]` — 邮件告警(SSL 465 或 STARTTLS 587)
- `[watch]` — watch 全部调参(预算/阈值/间隔)
- `[verification]` — 探测开关(`allow_chat_probe`、`probe_unclear_platforms`)

## 📜 版本历史

> 完整变更记录见 [CHANGELOG.md](CHANGELOG.md)。每个版本的更新、优化、调整一览：

### v2.5.x — 原生配额调度与检索有效性 (2026-09-22)

| 版本 | 摘要 |
|------|------|
| **v2.5.3** | **执行优化清单**：655 条历史 `error`/`rate_limited` 存量 key 启动时批量入持续重验队列（调度器只选 `valid=1`，它们此前永远无人重验）；回退下载改走全代理轮转出口（此前固定单代理，是唯一未分摊的大流量源）；新增 `last_error` 列记录每个 error 的原因（恢复后自动清空）；currency 防御（PERCENT 平台在 upsert 强制 + 历史错标修正）；探测放行但余额确凿 ≤0 记 `valid_zero`（修正 242 条历史 active@0）。 |
| **v2.5.2** | **三巨头专项修复**：Claude `sk-ant-oat01-`（CLI setup token）能提取但 identify 漏路由——5 条 DB 存量卡 unknown，已补入 Claude patterns；Gemini `AQ.Ab`（2026 新格式）补专属上下文查询（旧 4 条全硬编码 AIza → 新格式 0 分误路由）；+18 条查询（补上 Claude 专查空白，覆盖 `.claude`/`.cursor`/`google-services.json` 泄露面）。 |
| **v2.5.1** | **检索有效性专项（7 项修复 + 查询扩充）**：传输故障（TLS 重置）立即换健康端口，不再同端口重试（实测 23 个查询 3 败全丢）；主扫放开用全部 3 token（此前 1 个闲置留给 fresh-repo）；专有前缀负向断言掐死短假 `sk-proj-`/`sk-ant-` 噪声源（269 条 DB 实证）；CSS/代码字面量垃圾过滤（8 个实证词根）；**NVIDIA 探测模型 EOL 修复**（219 条全 error → 复活：403 按 body 区分 + 410 显式告警）；`max_candidates` 4→8（无上下文 siliconflow 排第 7 永远进不了验证池）；deepseek 余额跨条目求和；**查询轮转死位置修复**（22 条 compat 中 6 条永远轮不到，`FRESH_PATTERNS` 8→11）；+51 条研究驱动查询。 |
| **v2.5** | **原生配额调度** —— 用 header 驱动调度替代固定间隔"爆发-睡眠"节奏：`wait = (reset - now) / remaining` 把每 token 的 10 次请求均匀铺满限流窗口。**根除**（而非降级掩盖）了每分钟一条的"配额将重置等待 31s"告警、主配额 429 与 sleep-to-reset 卡顿；`code_search`(10/min) 与 `search`(30/min) 独立分桶；>90s 等待转静默自冷；IP 地板统一 6.0s（滥用阈值一半）；token 错峰 30s→2s（每轮省 60s）。4.8 小时受监测长跑验证：**0 配额告警、0 网络错误升级、13.7% valid 率**。 |

### v2.4.x — 多代理、平台扩容与防滥用 (2026-09-21 → 09-22)

| 版本 | 摘要 |
|------|------|
| **v2.4.9** | **全链路审查修复**：`_on_ip_rate_limit` 类内重复定义静默覆盖（多代理 429 冷却曾是死代码——Python 类重定义陷阱）；watch 退出从不调 `engine.stop()` → mihomo 孤儿进程（"回退单代理"反复出现的根因）；双 mihomo bug（WatchScanner 自建第二引擎）；惩罚值钳制 `≥ 基线`（防"惩罚反而提速"）；产出修复（查询串拼进平台 identify 让 +30 分信号真正生效；`sk-proj-` 208 位截断；OpenRouter 被 `>80` 门限全杀 0→27 valid；瞬态错误释放 `_seen`）；**清除 1129 行死代码**（含硬编码 root 口令的部署脚本、僵尸 CI、无引用扫描器）。 |
| **v2.4.8** | 限速桶**跟随出口 IP**；基线跟随模式（多代理 4.0s/IP、单代理 4.0×token 数）——回退不再"带病提速"。mihomo `log-level` 修正（v1.18.0 只认 `warning`，其 "invalid mode" 报错信息有误导）。 |
| **v2.4.7** | **内嵌 mihomo**：原生支持 VMess/Trojan/Hysteria2/SS 订阅 → 每节点一个本地端口、每 GitHub token 独立出口 IP、per-IP 限速。验证队列 worker 各自绑定独立代理 IP。基线按实测安全上限（3.5s）调至 4.0s。 |
| **v2.4.6** | **IP 级惩罚箱**：GitHub 滥用检测是按 IP 的——3 token 挤一个 IP（~27 req/min）被阶梯升级惩罚。任一 Retry-After >60s 触发全局冷却（25s/token 持续 10 分钟）。 |
| **v2.4.5** | **限流防撞**（源自一次 4.5 小时实跑积累 5 次二级限流的真实教训）：基线 6.3s→6.6s 回撤一档；429 分层冷却（1 小时 ≥3 次 → 深度冷却）；±8% 抖动打散等间隔节奏；`remaining ≤ 2` 前置 sleep-to-reset。 |
| **v2.4.4** | **智谱假余额修正**：旧端点返回的是注册赠额（恒为 ¥2000）——改用真实 `/users/balance`；**GLM Coding Plan 周额度**以百分比作余额、新币种 `PERCENT`（绝不混入 CNY 合计）；历史假余额行标记强制重验。 |
| **v2.4.3** | **全链路效率**：扫描压线至限额 95%；模糊 key 并行验证 4 个候选平台（按历史产出排序）；占位符过滤（1364 条 DB 实证 → 18 模式 + slug 启发式）；per-provider 差异化限速（千帆 0.25s 撞 429 → 1.0s）；扩展查询池重生成（4631 条）；metrics 24h 验证漏斗；**Kimi 余额取 `cash_balance`**（代金券不是钱）。 |
| **v2.4.2** | **入口收敛**：`watch` 成为唯一扫描入口（删除 `deepseek`/`multi`/`source`/`report` 子命令）；**chat 探测默认开**——一次 `max_tokens=1` 确认 key 真的能用（余额显示 ≠ 可调用）。 |
| **v2.4.1** | **全平台鉴权审计**（假 key 实测 20+ 端点）：OpenRouter 误报修复（`/models` 公开 → 改验 `/auth/key`，顺带支持余额）；Jina 同类修复；零一万物停运（410）停用；公开 `/models` 平台的 `probe_unclear` 兜底探测；第二批平台全部接入新鲜簇。 |
| **v2.4.0** | **第二批平台接入：27 → 35** —— OpenAI、Google Gemini、xAI、腾讯混元、百度千帆、魔搭、NVIDIA NIM、美团 LongCat。 |

### v2.3.x — watch 模式成熟期 (2026-08-26 → 08-29)

| 版本 | 摘要 |
|------|------|
| **v2.3.1** | watch **状态"复活"修复**（陈旧 `watch_state.json` 会让失效 key 死而复生）；chat 探测改显式 opt-in（v2.4.2 又默认打开）；验证并发治理（有界线程池、per-provider 共享限速、线程安全会话）；SQLite invalid 明文脱敏 + `maintenance` 命令；**查询收益反馈闭环** + 质量熔断（零转化查询被移出预算）；`metrics` 产出画像；拆出 `watch_persistence` 模块。 |
| **v2.3.0** | **产出修复批次（10 项）**：`KEY_PATTERN` 单一真相源（引擎私有副本缺智谱 `hex.secret` 提取，456 条查询白扫）；前缀感知长度上限（MiniMax JWT/Claude 长 key 不再被 `>80` 误杀）；hex/alnum 字符集预检（占位 key 死在调 API 之前）；接线实时 `github_events` 源；0 余额 key 改周级重验（预算让给有变化空间的 key）；验证会话复用（省 TLS 握手）；历史缓存化去掉每次保存的线性 I/O。 |

### v2.2.x 及更早 — 地基期 (2026-05 → 08)

| 版本 | 摘要 |
|------|------|
| **v2.2.1** | **watch 统计清零根因修复**：SQLite 跨线程写被静默吞掉（DB 恒 0 行、跨运行去重失效）→ `check_same_thread=False` + 写锁；TUI 累计计数器（轮次切换不再闪 0）；重启历史闭环；保存=合并历史（重启不再清空账本）；**首个真实测试套件**（78 个单测）。 |
| **v2.0.0** | **架构重构**：统一 `run.py` CLI 取代 12 个散落入口脚本；动态滚动时间窗口查询（`pushed:>最近7天`）替代硬编码日期；全扫描器统一 429 退避（读 `Retry-After`）；多平台验证并发化（149 key 约 10 秒）；连接池复用；国内平台绕过代理直连（防厂商风控封 IP）。 |
| **v1.0.0** | **初始发布** (2026-05-21)：泄露 DeepSeek key 扫描器，14 平台、238 条查询，Gist/Issues/Commits/GitLab/Gitee/HF/PyPI/npm/StackOverflow/Docker/Wayback 多源扫描，JSON/CSV/Markdown 报告输出。 |

## 📄 许可证

MIT License — 详见 [LICENSE](LICENSE)

---

<p align="center">
  🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲🌲<br>
  <em>"宇宙就是一座黑暗森林，每个文明都是带枪的猎人。"</em><br>
  <sub>— 刘慈欣，《三体》</sub>
  <br><br>
  <sub>先入林者先得。</sub>
</p>
