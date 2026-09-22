# 新鲜度优先优化 — 设计文档

> 日期: 2026-08-11 | 状态: 已确认 | 基线: `25c1a41` (v2.9.0 交接快照)

## 1. 目标

在 **GitHub token 配额不变(3 token × 10 req/min)**、**验证 API 预算充足(每天 1000-2000 次)** 的约束下,实现:

1. **高价值 key 余额感知**(缩水/充值/重新激活从"重启才感知"变为"每天感知")
2. **新 key 发现**通过"新鲜度优先"策略提升命中率(挖最近推送/提交的代码)
3. **新数据源**以实验先行方式探索,验证有效才集成

## 2. 核心策略:新鲜度优先

GitHub 上新推送的代码是 key 泄露概率最高的来源——刚推的代码往往还没被清理,`.env` 还在。**扫描预算优先花在"最近 1-2 小时推送的代码"上,而不是"旧仓库里再搜一遍"。**

## 3. 组件设计

### 3.1 ReverifyScheduler(新增,watch_tui.py)

独立线程,每 60s 调度一次,负责历史有效 key 的持续重验。

```
┌─────────────────────────────────────────────────────────┐
│ ReverifyScheduler (独立线程, 每 60s 调度一次)              │
│  ├─ 令牌桶: budget=1500/天, rate≈1.7次/分, 桶容量10       │
│  ├─ 待重验 key 队列: DB valid=1 全量, 按余额降序           │
│  ├─ 分层轮询:  ≥10元 每6h / ≥5元 每12h / 其余按预算轮询    │
│  └─ 状态: last_verified_at (每 key), 跳过验证中的 key     │
└──────────────┬──────────────────────────────────────────┘
               │ broker.schedule_reverify(key, source="history")
               ▼
┌─────────────────────────────────────────────────────────┐
│ VerificationBroker (改造点)                              │
│  ├─ 新增 schedule_reverify(): 绕过 _seen, 每 key 一次     │
│  ├─ 验证完成 → 从队列移除(允许下轮再排)                   │
│  └─ _store_result 对 source=history 结果 → 变化检测       │
└─────────────────────────────────────────────────────────┘
```

**调度逻辑:**
- **分层轮询**:≥10 元每 6h、≥5 元每 12h、其余按预算均摊(约 2-3 天全轮一遍)
- **不重复入队**:调度器自己管理"上次重验时间",验证完成的 key 才允许下轮再排
- **优先级**:重验走低优先级队列,新扫描发现的 key 永远先验证

### 3.2 余额历史表(store.py 新增)

```sql
CREATE TABLE key_history (
    key_hash    TEXT,
    verified_at TEXT,
    balance_cny REAL,
    status      TEXT,
    valid       INTEGER
);  -- 只记录有效 key 的验证(无效不写,省空间)
```

### 3.3 变化检测(watch_tui.py `_store_result`)

| 变化类型 | 判定 | 通知 |
|---------|------|------|
| 缩水 | 当前 < 上次 × (1-30%) 且 ≥10 元 | 预警邮件 + CSV |
| 充值/上升 | 当前 > 上次 × 1.5 且 ≥5 元 | 邮件 + CSV |
| 重新激活 | 上次无效 → 现在有效且 ≥5 元 | 邮件 + CSV |
| 其余 | 有变化但未达阈值 | 只记 CSV |

**新增 CSV 账本**:`results/balance_changes.csv`(ts, key, balance_cny, prev_balance, delta, change_type)

### 3.4 配置新增 [watch] 段

```ini
reverify_budget_per_day = 1500   # 重验预算/天
hv_email_threshold = 5           # 邮件阈值(元)
hv_top_threshold = 10            # 重高价值阈值(元)
shrink_warn_pct = 30             # 缩水预警百分比
```

## 4. 新鲜度优先扫描(P2)

### 4.1 fresh-repo 精扫升级为核心通道

- 每轮必扫 + 窗口收紧到最近 1-2 小时(`pushed:>1h`)
- 新推送的 repo 是"别人还没发现"的 key,命中率最高

### 4.2 启用已有 CommitsScanner + 升级(新鲜度核心通道)

**关键发现**:`scanners/github_commits.py` 的 `CommitsScanner` **已存在且完整**(扫最近提交 diff、提取新增行 `sk-` key、`source_name=github_commits`),但未在 watch 模式启用(`DEFAULT_WATCH_SOURCES` 只有 `github_search` + `npm`)。**不是新增,是启用 + 升级**。

升级点:
- 加 `since:` 参数控制时间窗口(默认最近 1-2 小时,每轮限定)
- 加多平台 key 模式(`sk-ant-`/`sk-kimi-` 等,当前只匹配 `sk-[a-zA-Z0-9]{32,64}`)
- **配额独立于 code search**(commits API 有自己的速率限制)→ 不抢 10/min
- 接入 watch 的 `_source_worker` 循环,享受降频/休眠/失败跳过机制

### 4.3 Code Search 排序:新鲜度加权

- `sort=indexed` 已默认(按索引时间倒序),确保每轮从最新结果开始翻
- 高收益查询自动生成平台限定变体(`filename:java` → `filename:java kimi sk-kimi-` 等),在新代码里挖盲区平台 key

### 4.4 GeneratedPool 试水批扩大

- 每轮 12 → 24 条,按收益分布抽样(优先从未跑过的、平台盲区的查询)

### 4.5 产出监测

- trend_monitor 增加每轮 top 5 收益查询跟踪,数据落 `trend.jsonl` 扩展字段

### 4.6 词库结构化(查询空间完善)

**现状(三层结构,已比交接文档显示的完整):**
| 层 | 文件 | 数量 | 特点 |
|----|------|------|------|
| 人工精选 | `queries_optimized.txt` | 297 条 | 集中在 deepseek,含 filename:/language:/path: 维度 |
| 自动生成 | `queries_generated.txt` | 2296 条 | query_enumerator 生成,聚焦盲区平台 |
| 运行时派生 | QueryTracker + 变异引擎 | 动态 | 从历史高收益查询自动生成变体 + 收益学习 |

**问题:**
1. 外部源默认词太浅——每个 scanner 默认只配 1 个 `"deepseek"`(registry `default_term`),多平台查询(221 条)没进外部源
2. 词库覆盖不均——297 条人工查询集中在 deepseek 单平台,盲区平台(kimi/minimax/doubao/claude)靠 generated 池撑着

**完善方向:**
- 外部源多词支持:把每个源默认词从 1 个 `"deepseek"` 扩展到该平台高收益查询列表(复用 query_stats 收益数据)
- 按平台分组维护,query_enumerator 已能按平台 × 文件类型生成,可加"按平台自动生成变体"

## 5. 新数据源探索(P3,实验先行)

**候选源(按新鲜度收益排序):**

| 源 | 新鲜度 | 配额 | 说明 |
|----|--------|------|------|
| GitHub Commits Search | ★★★ | 独立 | 最近提交 diff 里的 key |
| GitHub Issues/Comments | ★★★ | 独立 | 最近 issue 里贴的 key |
| GitHub Events API | ★★★ | 独立 | 实时推送事件,第一时间看到新 push |
| GitLab 代码搜索 | ★★ | token 已有 | 验证配额与代理连通 |
| NPM 生态增强 | ★ | 现有 | 162 key 仅 4 有效,降频 |

**流程**:每个源 1-2 小时小实验 → 命中率/有效 key 率 → 达标才写进 `_get_scanner_registry`。

**验证标准**:每 100 次请求 ≥ 3 个有效 key 才保留,不达标降频或停用。**新鲜度通道(commits/events)享受优先验证待遇。**

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| CommitsScanner 扫 diff 有下载成本 | 已有实现,启用后先实测命中率,窗口默认 1-2h 控制下载量 |
| 重验增加验证 API 消耗 | 预算令牌桶 1500/天,平滑分布;无效 key 不计费 |
| 变化检测误报(网络抖动) | 瞬态错误(error/rate_limited)不判变化,保留历史值 |
| 新源实验无效 | 如实放弃,不盲铺 |

## 7. 测试计划

- ReverifyScheduler 调度逻辑(分层轮询、预算令牌桶、不重复入队)
- 余额历史表写入/读取
- 变化检测(缩水/充值/重新激活/未达阈值)
- Commits Search 提取逻辑
- 现有 295+ 测试全量回归

## 8. 实施顺序

1. **P1**:ReverifyScheduler + 余额历史表 + 变化检测 + 通知(核心,收益最直接)
2. **P2**:新鲜度优先扫描(fresh-repo 核心化 + Commits Search + 排序优化)
3. **P3**:新数据源实验(Commits/Issues/Events 先行)
