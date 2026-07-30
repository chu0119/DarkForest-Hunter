# USAGE.md — DarkForest Hunter 使用手册

本手册详细介绍 DarkForest Hunter 的安装、命令参数、使用场景、输出文件与常见问题。

> 项目简介与设计理念请参阅 [README_CN.md](README_CN.md)；二次开发请参阅 [DEVELOPER.md](DEVELOPER.md)。

---

## 一、环境准备

### 1. Python 环境
要求 **Python 3.10 及以上**（代码使用了 `match` 语法、`list[dict]` 类型注解、`|` 联合类型等新特性）。

```bash
# 检查版本
python --version

# 如版本过低，建议用 pyenv 或官方安装包升级
```

### 2. 安装依赖
项目仅依赖两个库：

```bash
pip install -r requirements.txt
```

依赖清单（`requirements.txt`）：
| 依赖 | 版本 | 用途 |
|------|------|------|
| `aiohttp` | ≥3.8.0 | 异步 HTTP 客户端（扫描器并发请求） |
| `requests` | ≥2.28.0 | 同步 HTTP 客户端（GitHub API / DeepSeek 验证） |

### 3. 配置 GitHub Token（强烈推荐）
GitHub Code Search REST API 限流为 **10 次/分钟（已认证）**，未认证更低且部分接口会直接拒绝。两种方式提供 token：

**方式 A：用 GitHub CLI（推荐）**
```bash
# 安装 GitHub CLI: https://cli.github.com/
gh auth login
```
工具会自动读取 `gh auth` 配置的 token。

**方式 B：用环境变量**
```bash
# Linux / macOS
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx

# Windows PowerShell
$env:GITHUB_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"

# Windows CMD
set GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
```

### 4. 准备代理（可选）
GitHub、GitLab 等海外数据源及海外 AI 平台验证建议走代理；国内 AI 平台会自动直连，无需代理。代理优先级：`--proxy` 参数 > `HTTP_PROXY` 环境变量 > 不使用代理。

---

## 二、三个子命令完整参数说明

所有命令的根形式为 `python run.py <子命令> [参数]`。公共参数三个子命令都支持。

### 公共参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `--proxy` | 字符串 | 读 `HTTP_PROXY` | HTTP 代理地址，如 `http://127.0.0.1:7897` |
| `--concurrency` | 整数 | `15` | 并发数（验证阶段并发抓取/验证的线程或协程数） |

### 根命令参数

| 参数 | 说明 |
|------|------|
| `--list-sources` | 列出所有可用数据源（用于 `source` 子命令），列出后直接退出 |

### 2.1 `deepseek` 子命令（单平台 DeepSeek 扫描）

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `--proxy` | 字符串 | 见公共参数 | HTTP 代理地址 |
| `--concurrency` | 整数 | `15` | 并发数 |
| `--pages` | 整数 | `5` | 每条查询的 GitHub 搜索页数（每页 100 条） |
| `--max-duration` | 整数 | `0` | 最大运行秒数，`0` 表示不限时 |
| `--max-keys` | 整数 | `0` | 累计发现 N 个有效 key 即停，`0` 表示不限 |

行为：使用 `build_active_queries()` 构造的 228 条查询（含动态滚动时间窗口），先跑 GitHub Code Search，再用 DeepSeek 接口验证 + 查余额，结果写入 `results/`。

### 2.2 `multi` 子命令（多平台 AI Key 扫描）

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `--proxy` | 字符串 | 见公共参数 | HTTP 代理地址 |
| `--concurrency` | 整数 | `15` | 验证阶段并发数 |
| `--providers` | 多值 | 见下 | 平台 ID 列表，空则用默认 9 家国内厂商 |
| `--max-pages` | 整数 | `2` | 每条查询的 GitHub 搜索页数 |
| `--list-providers` | 开关 | — | 列出所有支持的平台及其启用状态 |

默认 providers（9 家）：`deepseek kimi zhipu qwen minimax doubao baichuan yi stepfun`。
完整 12 家还包括：`xiaomi`、`sensernova`、`claude`。

### 2.3 `source` 子命令（单数据源扫描）

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `--source` | 字符串 | **必填** | 数据源名（见下表） |
| `--proxy` | 字符串 | 见公共参数 | HTTP 代理地址 |
| `--concurrency` | 整数 | `15` | 并发数 |
| `--pages` | 整数 | `3` | 搜索页数 |
| `--max-duration` | 整数 | `0` | 最大运行秒数，`0` 表示不限时 |

可用数据源（`--list-sources` 同样输出）：

| 数据源名 | 说明 |
|----------|------|
| `gist` | GitHub Gists |
| `issues` | GitHub Issues / PRs |
| `commits` | GitHub 提交历史 |
| `gitlab` | GitLab |
| `gitee` | Gitee（码云） |
| `huggingface` | HuggingFace |
| `pypi` | PyPI 注册表 |
| `npm` | npm 注册表 |
| `stackoverflow` | Stack Overflow |
| `docker` | Docker Hub |
| `wayback` | Wayback Machine |
| `commoncrawl` | Common Crawl |
| `github_raw` | GitHub 宽泛 `sk-` 搜索 |
| `pastebin` | Pastebin |
| `reddit` | Reddit |
| `google_dork` | Google Dork |

---

## 三、使用场景示例

所有示例均可直接复制运行（请先按"环境准备"装好依赖并配好 token/代理）。

### 场景 1：快速测试（15 分钟，验证环境是否通）
```bash
# 限时 15 分钟，先跑动态时间窗口的最新提交
python run.py deepseek --max-duration 900
```

### 场景 2：全量 DeepSeek 扫描（带代理）
```bash
# 单平台 DeepSeek 全量，走 Clash/V2Ray 本地代理
python run.py deepseek --proxy http://127.0.0.1:7897
```

### 场景 3：多平台扫描（国内 AI 厂商）
```bash
# 扫 4 家，免费额度多的平台更可能有余额
python run.py multi --providers deepseek kimi qwen zhipu --proxy http://127.0.0.1:7897
```

### 场景 4：单数据源扫描（只跑 HuggingFace）
```bash
# 定向猎取 HuggingFace 上的泄露
python run.py source --source huggingface --proxy http://127.0.0.1:7897
```

### 场景 5：用环境变量代理
```bash
# Linux / macOS
export HTTP_PROXY=http://127.0.0.1:7897
python run.py deepseek

# Windows PowerShell
$env:HTTP_PROXY="http://127.0.0.1:7897"
python run.py deepseek
```

### 场景 6：限时扫描 + 达到 N 个有效 key 即停
```bash
# 跑单数据源 Wayback，最多跑 30 分钟
python run.py source --source wayback --max-duration 1800
# 找到 20 个有效 key 就停
python run.py deepseek --max-keys 20
```

### 场景 7：调整并发与页数
```bash
# 提高并发、增加每查询页数（注意 GitHub 限流，过高无益）
python run.py deepseek --concurrency 25 --pages 8 --proxy http://127.0.0.1:7897
```

### 场景 8：便携 exe 运行（打包后）
```bash
# 打包见 DEVELOPER.md 第七章
./dist/DarkForestHunter.exe deepseek --proxy http://127.0.0.1:7897
./dist/DarkForestHunter.exe multi --providers deepseek kimi
```

---

## 四、输出文件说明

扫描结果统一写入项目根目录的 `results/` 文件夹，提供 **JSON / CSV / Markdown** 三种格式（同时输出）。文件名带时间戳，便于多次扫描归档。

### 4.1 字段含义

| 字段 | 说明 |
|------|------|
| `key` | 完整 API Key（明文，注意保管） |
| `key_preview` | 预览形式 `sk-xxxxxx...xxxx`（脱敏，便于报告） |
| `provider` / `platform` | AI 平台 ID（deepseek / kimi / zhipu / ...） |
| `valid` | 是否通过有效性验证（true / false） |
| `balance` | 账户余额（原始币种，仅支持的平台有值） |
| `balance_usd` | 折算成美元的余额（按内置汇率换算） |
| `source` | 发现该 key 的数据源（gist / huggingface / github / ...） |
| `repo` | 所在仓库（如 `owner/repo`） |
| `file` | 文件路径 |
| `url` | 命中内容的原始 URL |

### 4.2 三种格式

- **JSON**（`results/*.json`）：完整结构化数据，含所有字段，适合程序读取和后续分析。
- **CSV**（`results/*.csv`）：表格形式，每行一个 key，适合用 Excel / 数据库导入。
- **Markdown**（`results/*.md`）：人类可读报告，含统计摘要（有效数、正余额数、总价值 USD），适合直接贴到 issue 或文档。

### 4.3 结果解读
- `valid == true` 表示 key 当前可用；`valid == false` 表示已失效（被吊销、余额耗尽或请求被拒）。
- `balance_usd > 0` 的条目是"高价值泄露"，应优先处理（如果你是 key 所有者请立即轮换）。
- 增量结果会在扫描过程中周期性写入（`_save_incremental`），即使中途中断也能保留已验证的部分。

---

## 五、常见问题 FAQ

### Q1：GitHub 限流（10 次/分钟）怎么办？
工具已内置处理：`ScannerEngine._gh_search` 会读取响应头 `X-RateLimit-Remaining`，接近 0 时自动 sleep 到 `X-RateLimit-Reset` 指向的时间点再继续。你能做的优化：
1. **务必配置 GitHub Token**（未认证限流更低，且部分接口直接 403）。
2. 不要盲目调大 `--concurrency` / `--pages`，GitHub 的限制是按"搜索请求数"而非并发数，调大无益反而更快触发限流。
3. 全量扫描自然会花约 100 分钟，因为 228 条查询 ÷ 10 次/分钟 ≈ 23 分钟纯搜索 + 验证 + 退避等待。

### Q2：没有 GitHub Token 能用吗？
能用 `multi` 和 `source` 子命令的部分数据源（如 HuggingFace、PyPI、Docker Hub 等不依赖 GitHub Code Search 的），但 `deepseek` 子命令依赖 GitHub Code Search，**强烈不建议无 token 运行**——限流会极其严格，甚至直接被拒。配置 token 是性价比最高的优化。

### Q3：代理怎么配？
三种方式，优先级从高到低：
1. 命令行参数：`--proxy http://127.0.0.1:7897`
2. 环境变量：`HTTP_PROXY`（或小写 `http_proxy`）
3. 不使用代理

注意：**国内 AI 平台（deepseek/zhipu/qwen/...）验证时会自动直连**（由 `DIRECT_PROVIDERS` 控制），不走代理，避免代理 IP 被国内厂商风控封锁。海外平台（如 claude）走代理。

### Q4：扫不到 key 是为什么？
常见原因：
- **GitHub Token 未配置或已过期**：搜索请求被拒，日志里看是否有 403/401。
- **限流触发后等待时间长**：日志会显示 `X-RateLimit-Remaining`，耐心等待即可。
- **代理不通**：用 `curl -x http://127.0.0.1:7897 https://api.github.com` 验证代理可用性。
- **数据源本身无新泄露**：尝试用动态时间窗口（默认已开启），或换数据源（如 `huggingface`、`gist`）。
- **坏 key 过滤**：`is_bad_key` 会过滤 `your`/`xxx`/`example`/全数字等明显占位符，这些不会出现在结果里。

### Q5：验证阶段被平台封锁怎么办？
- 国内平台：确保没有给它们配代理（已自动直连）。若你的出口 IP 被封，换网络或降低 `--concurrency`。
- 海外平台：换代理节点，或降低 `--concurrency`（默认 15，可降到 5-8）。
- `_get_with_retry` 已内置 429/503 指数退避，一般会自动恢复。

### Q6：如何只扫描最新提交？
默认的 `deepseek` 子命令已经通过 `generate_rolling_time_queries()` 自动注入"最近 7 天 / 30 天"的 `pushed:>` 查询，每次运行都指向最新提交。你也可以用：
```bash
# 单源 + 限时，快速扫最新
python run.py source --source commits --max-duration 600
```

### Q7：如何扩展到新的 AI 平台？
详见 [DEVELOPER.md 第六章](DEVELOPER.md)。简要步骤：在 `providers.py` 新增一个 `AIProvider`（填 `key_patterns` / `verify_endpoint` / `balance_endpoint`），加入 `ALL_PROVIDERS`，国内平台加入 `DIRECT_PROVIDERS`。无需改其他文件，`UnifiedKeyMatcher` 和 `UnifiedKeyVerifier` 会自动识别。

### Q8：如何扩展到新的数据源？
详见 [DEVELOPER.md 第五章](DEVELOPER.md)。简要步骤：在 `scanners/` 新建文件继承 `BaseScanner`，实现 `source_name` 和 `search()`，注册到 `scanners/__init__.py` 和 `scanner_engine._get_scanner_registry()`，打包时加入 `DarkForestHunter.spec` 的 `hiddenimports`。

### Q9：便携包（exe）怎么用？
1. 按 [DEVELOPER.md 第七章](DEVELOPER.md) 用 `pyinstaller DarkForestHunter.spec` 打包。
2. 产物在 `dist/DarkForestHunter.exe`，命令行用法与 `run.py` 完全一致：
   ```
   DarkForestHunter.exe deepseek --proxy http://127.0.0.1:7897
   ```
3. 结果仍写入运行目录下的 `results/`。

### Q10：扫描中途想停止怎么办？
`Ctrl+C` 中断即可。已验证的结果会通过 `_save_incremental` 增量保存到 `results/`，不会丢失。

---

## 六、性能参考

实际耗时主要受 **GitHub Code Search 10 次/分钟限流**制约，而非本地 CPU/带宽。

| 场景 | 典型耗时 | 说明 |
|------|----------|------|
| 单平台 DeepSeek 全量（228 查询） | 约 100 分钟 | 受 GitHub 限流制约，提速空间有限 |
| 多平台扫描（默认 9 家） | 约 60 分钟 | 每平台查询数较少，验证并发 |
| 单数据源扫描 | 几分钟 ~ 30 分钟 | 取决于数据源规模和 `--pages` |
| 限时扫描 `--max-duration 900` | 恰好 15 分钟 | 到点自动停止并保存 |

提示：
- 调大 `--concurrency` 主要加速**验证阶段**，对 GitHub 搜索阶段的 10 次/分钟硬限制无效。
- 调大 `--pages` 能抓更多结果，但每页都是一次独立搜索请求，会更快消耗配额。
- 多平台扫描的验证阶段在 v2.0 已改为并发（ThreadPoolExecutor），处理 149 个 key 从超时降到约 10 秒。
