# P0 — 外部源超时根治 + 搜索词覆盖

**日期**：2026-08-09
**范围**：仅 P0（外部源：HuggingFace、GitLab、搜索词覆盖）。P1/P2 各自单独 spec。
**成功标准**：HF 稳定 ≤20s、GitLab 稳定 ≤30s；超时后无 daemon 线程残留；换皮兼容词与中文别名进入外部源轮换。
**门禁**：`python -m pytest tests/ -q` 全绿 + `python -m ruff check .` 全绿 + `python run.py watch --once` 实测耗时达标。

---

## 1. 背景与根因

2h20m 实测产出分布：github 873 行（99%），其余 8 个外部源合计 12 行。外部源产出极低 + 频繁超时（HF 11 次×60s、GitLab 16 次×90s）是最大瓶颈。根因（已核实代码）：

1. **无内部硬时限，join 不取消**：`watch_tui._scan_external`（`watch_tui.py:1245-1283`）仅 `thread.join(timeout)`，join 超时后 daemon 线程仍在跑（aiohttp 会话不取消），下一轮又起新线程 → 残留累积。
2. **每轮请求量无上界**：`_scan_external` 把 `_PLATFORM_SEARCH_POOL`（6 词）+ 源补充词（≤3）全塞给 scanner，`_run_one_scanner`（`scanner_engine.py:1169`）对**每个词**跑完整 `search()`。HF 单词可达 ~200 请求、GitLab ~112 请求，6 词即上千请求。
3. **429 无重试上限**：HF `_hf_search`（`huggingface.py:89-97`）按 `Retry-After` 无限 `continue`；GitLab（`gitlab.py:69-70`）睡 10s 进下一页。单个 60s Retry-After 即可击穿预算。
4. **搜索词覆盖窄**：换皮兼容词（`COMPAT_QUERIES`）只进 GitHub `QueryRotator`（`query_rotation.py:65-81`），外部源完全没用；`_PLATFORM_SEARCH_POOL`（`watch_tui.py:787-796`）8 桶全英文，无中文别名。

## 2. 架构：内部 asyncio 硬时限 + 可取消（机制 1）

核心：把硬截止线从"watch 层 join（不取消）"下沉到"scanner 层 asyncio.timeout（真取消）"。

- `HuggingFaceScanner` / `GitLabScanner` 各加 `deadline_s: float` 构造参数（HF 默认 18、GitLab 默认 28）。
- `search()` 主体用 `async with asyncio.timeout(self._deadline)` 包裹（Python 3.12 原生）。到点抛 `TimeoutError` → 自动取消所有在飞 aiohttp 任务 → session 关闭 → 扫描线程自然退出。
- 在每个请求边界（搜索分页、tree、raw 文件下载）加 `self._should_stop()` 协作检查作兜底，防止个别路径绕过 timeout。
- 429 重试上限 = 1：第一次按 `Retry-After` 等待（截断到 `min(wait, deadline剩余)`），第二次仍 429 直接 `break`。
- watch 侧 `_scan_external` 的 join 超时改为 `内部 deadline + 7s`（HF 25s、GitLab 35s），仅作"取消未生效"的最终保险。join 仍超时（不应发生）记 error 日志便于诊断。

**为什么这样**：`asyncio.timeout` 是 3.11+ 对协程（含 shield 外）统一取消的官方原语，与现有 aiohttp 架构一致；join 退化为观察者，真正取消发生在协程层，daemon 残留有界（deadline 后数秒内退出）。

## 3. P0-1：HuggingFace

**文件**：`scanners/huggingface.py`

- `__init__` 加 `deadline_s: float = 18.0`。
- `search()`（`:31-60`）：用 `async with asyncio.timeout(self._deadline)` 包裹"搜索 + 扫描"主体；`asyncio.timeout` 触发的 `TimeoutError` 不当作错误（已累积的部分 results 保留返回）。
- `_hf_search`（`:62-103`）：429 分支改为"重试 1 次后 break"，`wait = min(retry_after, 剩余deadline, 8)`。
- `_scan_repo_files`（`:121-185`）：
  - tree 顶层命中文件（`:163-168`）当前无上限 → 封顶 3 个（`list(... )[:3]`）。
  - 入口文件维持 3 个（space: app.py/main.py/README.md；其它: README.md/config.json/.env）。
- 预算：`max_items` 由 watch 经 registry 传入 **24**（space=12、model=6、dataset=6）。deadline 是硬保证；预算让"通常能在 18s 内跑完且有意义覆盖"。
- registry 改动：`scanner_engine.py:1110` HF 项 kwargs 加 `max_items: 24`（当前 50）+ `deadline_s: 18`。

## 4. P0-2：GitLab

**文件**：`scanners/gitlab.py`

- `__init__` 加 `deadline_s: float = 28.0`。
- `search()`（`:30-46`）：`async with asyncio.timeout(self._deadline)` 包裹；TimeoutError 不当错误，返回已累积 results。
- `_search_projects`（`:48-76`）：429 分支（`:69-70`）改为"重试 1 次后 break"，`wait = min(10, 剩余deadline)`。
- `_scan_project`（`:78-120`）：tree 顶层 + 7 入口文件不变；文件下载已在 semaphore（concurrency=15）下并发。
- 预算：watch 经 registry 传 `max_projects: 4`、`max_files_per_project: 4`（当前 10/10）。
- registry 改动：`scanner_engine.py:1104` GitLab 项 kwargs 改 `max_projects: 4, max_files_per_project: 4` + 加 `deadline_s: 28`。

**诚实声明**：GitLab 匿名 ~10 req/min/IP 限流很严，4 风暴下 P0 只保证"稳定 ≤30s、不残留线程"，单轮仍可能低产。**真正提产需 P2-8 接入 GitLab token**（解锁 code search + 更高配额）。P0 让 GitLab"不拖垮、可挂着"，P2 让它"出量"。

## 5. P0-3：换皮兼容词进外部源

外部源是**元数据搜索**（HF 搜 repo id/tag/description，GitLab 搜项目名/描述），代码型查询（`filename:env`）不适用。换皮项目的特征是"demo/免费中转/free-endpoint"。新增兼容词桶（`docs/HF_SCANNER_ANALYSIS.md §4` 已验证可用）：

```python
_COMPAT_TERMS = [
    "openai-api-key", "api-key", "chatbot", "ai-chat", "gradio",
    "free-endpoint", "free-api", "proxy", "rotator", "litellm",
]
```

扫到的 `sk-` key 由现有 `UnifiedKeyMatcher` 前缀路由到正确平台，与 GitHub 兼容词机制一致。

## 6. P0-4：中文别名

新增中文别名桶（gitee / 国内 GitLab 中文项目命中率最高；HF/GitLab 元数据搜索支持中文）：

```python
_CN_ALIAS_POOL = [
    "deepseek密钥", "deepseek key", "月之暗面", "智谱", "通义千问",
    "豆包", "火山方舟", "百川", "deepseek api key", "api密钥",
]
```

中文词**只进外部源轮换，不进 GitHub 查询**（GitHub Code Search 对中文支持差）。

## 7. 轮换机制：统一单词轮换

替换当前"每轮取一整个平台桶（6 词）+ 源补充"的逻辑（`watch_tui.py:1256-1258`），改为**跨桶单词轮换，每轮 1 词**：

```python
# 全部平台词（展平，非代表词）+ 兼容词 + 中文别名 + 通用泄露特征
_ROTATION = (
    [w for pool in _PLATFORM_SEARCH_POOL for w in pool]   # ~24 平台词
    + _COMPAT_TERMS                                       # 10
    + _CN_ALIAS_POOL                                      # 10
    + ["sk-"]                                             # codeberg 等通用
)   # ~45 词；每轮 1 词，~45 轮全覆盖（24/7 watch 下约 1-3 小时一轮）
term = _ROTATION[source_round % len(_ROTATION)]
terms = [term]   # 每轮 1 词
```

- 保留 `SOURCE_SEARCH_TERMS` 表语义，但其值并入 `_ROTATION`，不再每轮叠加全部。
- `_run_one_scanner` 的 `for term in search_terms` 每轮只迭代 1 次。
- 同步更新 `tests/test_watch_tui.py` 的轮换回归（`:847-889`）：断言从"每轮多词"改为"每轮单词、N 轮全覆盖"，并新增"中文/兼容词出现在序列里"的测试。

## 8. watch 侧改动

**文件**：`watch_tui.py`

- `_scan_external`（`:1245-1283`）：
  - `timeout = 90 if gitlab else 60` → HF/GitLab 用 `deadline+7`（25/35s）；其它源维持 60s（P0 不动）。
  - 词组装（`:1256-1258`）改用段 7 的单词轮换。
  - join 仍 alive 时日志级别保留 warning；新增"理论上不应发生"注释。
- `SOURCE_SEARCH_TERMS` / `_PLATFORM_SEARCH_POOL` 区段（`:772-796`）增补 `_COMPAT_TERMS` / `_CN_ALIAS_POOL` / `_ROTATION`。

## 9. 测试

遵循既有 mock 风格，**零新依赖**（不引 pytest-asyncio，P1 报告确认可行）。

**新增 `tests/test_scanners_external.py`**（用 `asyncio.run(scanner.search(...))` + 自定义 `FakeSession`，仿 `test_verifier.py:11` 的 `FakeResponse`）：
- HF：deadline 到点抛 `TimeoutError` 且已累积部分 results；429 重试 1 次后 break；max_items=24 封顶；tree 命中文件封顶 3。
- GitLab：deadline 取消；429 重试 1 次后 break；max_projects=4 / max_files=4 封顶。

**更新 `tests/test_watch_tui.py`**：
- 轮换断言改为"每轮单词、N 轮全覆盖"+ 中文/兼容词存在性（段 7）。
- 新增 `_scan_external` 测试：每轮单词、join 超时与内部 deadline 对齐（mock `engine._run_one_scanner` 注入慢返回验证不残留）。

**约束**：测试不触网、不读取真实 key/token；用合成 key 字符串。

## 10. 实测门禁（每个 P0 子任务完成后）

1. `python -m pytest tests/ -q` 全绿。
2. `python -m ruff check .` 全绿。
3. `python run.py watch --once --interval 30 --verify-workers 2`（单轮，不触发 5 分钟看门狗）：观察 HF/GitLab 日志耗时 ≤20/30s、无"超时跳过"、无 daemon 残留。
4. 全部 P0 子任务完成后：`python run.py watch` 实测一轮完整外部源轮换（中文/兼容词/平台词依次出现）。

**看门狗安全**：github_search 默认在源列表、rest=0 连续跑、持续产日志 → 心跳不断；外部源每轮有日志。Ctrl+C 优雅退出，绝不 SIGKILL。

## 11. 不在 P0 范围内（明确排除）

- 其它外部源（gitee/pypi/npm/docker/codeberg/paste_sites）的内部 deadline——未被标为超时高发，留待按需扩展。
- GitLab token 接入（P2-8）、Pastebin Scraping API（P2-9）、CommonCrawl/Google Dork 启用（P2-10）。
- 外部源其余 scanner 单元测试、watch_tui.py 拆分、GitHub 多 token（均属 P1，见路线图）。

## 12. 路线图（本次确认）

| 阶段 | 内容 | 触发实测 |
|---|---|---|
| **P0（本 spec）** | 外部源超时根治（HF/GitLab 内部 deadline）+ 搜索词覆盖（换皮兼容词 + 中文别名） | 每个子任务后 `watch --once`；全部后 `watch` |
| **P1-首项** | GitHub 多 token：config.ini 多 token 填法 + TokenHealth 故障切换（401/403 自动隔离坏 token、查询转给其他 token），per-token pacing 原样保留 | 配置多 token 后 `watch`，观察吞吐 ~N× |
| **P1-其余** | 外部源 scanner 单元测试（gitee/pypi/npm/docker/codeberg/paste_sites）+ watch_tui.py 拆分（TUI 渲染→持久化→Broker 三步） | pytest + watch 冒烟 |
| **P2** | GitLab token 接入 / Pastebin Scraping API / CommonCrawl·Google Dork 启用 | 各源 `run.py source` 单源验证 |
