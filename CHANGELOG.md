# CHANGELOG — DarkForest Hunter

本项目版本变更记录。日期采用 YYYY-MM-DD 格式。

## v2.5.3 (2026-09-22)

长跑监测收尾优化清单执行（6 项，全量测试 + ruff 全绿）：

1. **error/rate_limited 存量复验** — 根因：`ReverifyScheduler` 只选 `valid=1`，瞬态失败行（`status IN ('error','rate_limited')` 且 `valid=0`）永远无人重验形成死存量（实测 **655 条**：error 638 + rate_limited 17，含 claude 10 条真 key、openai 99、gemini 可能藏 valid）。`run_watch` 启动时批量 `enqueue_reverify` 入持续重验队列（绕过单次限制、in-flight 去重、upsert 回写闭环：error→invalid 停掉 / error→valid 挖回来）。顺手修 SQL `AND/OR` 优先级 bug。
2. **回退下载接入多代理** — `MultiProxyRouter.get_any_proxy()` 轮转出口（跳过冷却端口）；`_fetch_raw` 30 文件/查询的流量此前固定走单代理 SmartProxy，是唯一未分摊的大流量源（长跑 429×3 均为 IP 级）。
3. **last_error 落库** — `keys` 表 ALTER 加 `last_error` 列（仿 `query` 列迁移）；`_verify_one` 返回带 `message`（异常只记类型名防 key 泄漏进日志）；upsert 对 error/rate_limited 截断 200 字符写入、复验成功自动清空。此前 error 行无一句原因，断点诊断只能靠重放探测。
4. **currency 防御** — upsert 对 `provider ∈ PERCENT_PROVIDERS` 强制 `PERCENT`（防旁路落库绕过币种推断）；UPDATE 语句补 `currency` 列（旧缺陷：复验后币种不刷新）；历史 1 条 zhipu_coding 错标 CNY→PERCENT 已修。
5. **active@0 口径修复** — probe 放行（ACTIVE）但余额端点**确凿查出 ≤0** → 降 `valid_zero`（`balance=None` 无确证不降，不误杀）；历史 **242 条** active@balance=0 一次性修正。
6. 存量复验候选 655 条（明文可用）、active@0 修后 0 条、zhipu_coding 错标修后 0 条。

## v2.5.2 (2026-09-22)

三巨头（OpenAI/Claude/Gemini）专项修复：

1. **claude oat01 漏路由** — `KEY_PATTERN` 能提取 `sk-ant-oat01-`（走通用分支）但 `CLAUDE.key_patterns` 只有 api03 → identify 返回空 → DB 5 条 oat01（CLI setup-token，订阅额度）全部 unknown 从未验证。补 `sk-ant-oat01-{80,}` pattern。
2. **gemini AQ.Ab 语境 0 分** — `key_context_queries` 4 条全硬编码 AIza，2026 新格式上下文 0 分易被误路由（实证 1 条 AQ.Ab 落到 deepinfra）。补 `GEMINI_API_KEY AQ`、`aq.ab apikey` 两条。倒计时：2026-09 起 Gemini 拒收标准 AIza key，**AQ.Ab 是唯一活口**。
3. **低转化根因判定**（三段结论）— openai 84% invalid = 查询捞的本来就是死 key（非余额管道问题；OpenAI 无官方 balance API 已实证，402→valid_zero 是最优口径勿改）；gemini 98% invalid = AIza 大多无 generativelanguage 权限（403 归 invalid 正确）；claude = 供给不足（0 条专查）+ 10 条真 key 全落 error 待复验。
4. 新鲜簇补 `sk-ant-api03-`、`AQ.Ab filename:env`（claude 新鲜位 0、gemini 转向新格式，两家吃 sort=indexed 红利）。
5. `queries_optimized` 384→402（+18：claude 7 条专查补空白 + openai 6 + gemini 5，覆盖 `.claude`/`.cursor`/`mcp.json`/`google-services.json` 泄露面）。
6. 覆盖数学复验：NEW_PLATFORM(12)/HIGH_YIELD(11)/COMPAT(22) 在 range(11) 下全全覆盖。

## v2.5.1 (2026-09-22)

检索有效性专项——修复批次（7 项）+ 研究驱动的查询扩充：

1. **传输故障换端口** — `MultiProxyRouter.on_transport_failure`：TLS RST/连接重置立即冷却该端口 60s 并重绑 token 到其他节点；`_gh_search` 网络错误分支接入并重算 proxies（实测 105 错误中 23 查询 3 败全丢，同端口重试基本无效）。
2. **主扫并行度 2→3** — tokens[0] 不再固定留给 fresh-repo（fresh-repo 在池完成后串行执行，主扫期间纯闲置；v2.5 均匀铺排下配额隔离顾虑消失）。
3. **专有前缀负向断言** — 通用 `sk-` 分支加 `(?!proj-|svcacct-|admin-|ant-api03-|or-v1-)`：短假 key（24-56 位）不再从可选组溜进 DB——269 条 unknown 噪声源掐死；`sk-or-v1-{30,}` 独立分支。
4. **CSS/代码字面量垃圾过滤** — 8 个 DB 实证词根（linehei/fontsiz/colors-/sensiti/interna/none-/nano-/ckpt）。
5. **nvidia 端点复活三件套** — `verify_model` 的 `deepseek-v4-flash-0731` 于 2026-09-21 08:00Z EOL（410 先于鉴权）→ 219 条全 ERROR；换现役 `v4.1-flash`（活体实测 410/403/401 语义全验证）；403 按 body 区分（`Authorization failed`→INVALID / 其他→ERROR 可重验）；400/404 对 nvidia 不再误判"认证已过"；410 显式告警防模型再下线静默；`_probe_chat` 补 410 分支。
6. **max_candidates 4→8** — 无上下文 siliconflow key 排第 7 永远进不了验证池（DB 115 valid 全部来自带域名上下文的查询）。
7. **deepseek 余额跨 balance_infos 求和** — 旧取 `[0]` 漏多币种条目（576 valid 全 0 疑点）。

**检索逻辑 bug**（研究 agent 发现）：
- `FRESH_PATTERNS` range(8)→(11)：COMPAT 22 条中 6 条（16-21 位）是永远轮不到的死位置（覆盖数学实证 16/22→22/22）。
- `TIME_SLICE_TEMPLATES` 死代码删除（全仓库零引用）。
- 智谱新鲜槽 `bigmodel.cn sk-` 形态错误（hex key 无 sk- 共现）→ `open.bigmodel.cn filename:env` / `filename:php`。
- `nvapi-` 裸前缀→`build.nvidia.com` 语境；补 `ZHIPUAI_API_KEY`/`MODELSCOPE_TOKEN`/`SILICONFLOW_API_KEY` 等 env 词。

**查询扩充**：`queries_optimized` 355→384（+51，研究产出去重入库：Kimi智谱 14 + DeepSeekQwen 16 + 聚合平台 21）。

4 研究 agent（聚合平台 / Kimi智谱 / DeepSeekQwen / OpenAI三巨头）；全量测试 + ruff 全绿。

## v2.5 (2026-09-22)

原生配额调度——用户批评"告警降级=掩耳盗铃"成立：4.0s 固定节奏 = 窗口前 40s 打光 10 格配额 → 睡到重置，每分钟必产 31s 等待是数学必然，降级告警治标不治本：

1. **新模块 `rate_scheduler.py`** — header-driven：每次响应读 `X-RateLimit-Remaining/Reset` 刷新账本，下次请求 `wait = (reset - now) / remaining` 均匀铺到窗口尾。**窗口内永不提前打光 → 永不 sleep-to-reset → 永不主配额 429**，≥40s 告警天然无触发条件（真根除，非降级）；只保留"配额被外部占用"告警（每窗口一次，真异常信号）。
2. **实测关键数据（带 token）** — `/search/code` → resource=code_search **limit=10/min**；`/search/repositories` → resource=search **limit=30/min**——两个独立配额桶，repo search 额度是 30 非此前以为的 10，此前认知错误已纠正。
3. 分桶 (token, endpoint)，数值全来自响应头（GitHub 改限额自动跟随）；429/Retry-After push 进账本全体退让；深度退避 >90s 转 self-quiet（v2.4.5"不死等"语义的配额层等价）。
4. **IP 层简化** — baseline 统一 **6.0s/出口IP**（滥用阈值 15-20 的一半），v2.4.8 的多/单代理模式区分由配额层接管；token stagger 30s→2s（每轮省 60s）。
5. 488 passed + 11 个 rate_scheduler 单测，ruff 全绿。

## v2.4.9 (2026-09-22)

多 agent 全面审查修复（5 agent 分维度：核心引擎/watch/产出/死代码/内容，逐条现场核实裁决）：

1. **`_on_ip_rate_limit` 类内重复定义**（P1）— 旧版覆盖新版，v2.4.7 多代理 per-IP 429 冷却是**死代码**，429 后 token 永远绑死原 IP。教训：Python 类内重复方法定义不报错，后者静默覆盖前者。删旧版恢复多代理分支。
2. **watch 退出从不调 `engine.stop()`**（P1）— mihomo.exe 每次退出成孤儿进程，下次启动端口被占 → "回退单代理"的真正根因。watch 退出路径补 `engine_stop()`。
3. **双引擎双 mihomo**（0cf389d）— run_watch 与 WatchScanner 各建一个引擎、各启动一份 mihomo（一个占端口一个空壳）。WatchScanner 复用 run_watch 共享引擎。
4. 惩罚箱 25s/降档 15s 硬编码未与基线取 max——token≥7 时惩罚反而提速。改 `max(值, 基线)`。
5. mihomo 就绪探测 15s deadline 全端口共享，一个死节点饿死全部 → 每端口独立 + 坏端口剔除（不再整体失败）。
6. matcher 单例与测试 monkeypatch 冲突（合跑必红）→ `reset_matcher_singleton()` 钩子。
7. 无代理配置时假 proxy 路由 IP 冷却自欺 → 恒 direct。

**产出修复（扫到更多 key）**：
- **Y1（高）**：identify 的 query-term 加分要求完整域名出现在 context，而 context 只拼 repo 路径（永不命中）。查询串自带平台域名，拼进 context 后 +30 分生效，hunyuan 0→31 等 9 家通用 sk- 平台不再永远验不到。
- Y2/Y3/Y4：sk-proj `{70,200}` 封顶截断 >208 位真 key；sk-ant-api03- >108 截断；OpenRouter 总长 82 被 >80 门限全杀（该平台此前一个 key 都提不出来，修后 0→27 valid）。正则修复 + 回归测试。
- Y5：首验 error/rate_limited 的 key 被 `_seen` 永久挡住 → 瞬态失败释放。
- Y6：新鲜簇 95% deepseek → 新增 8 条高产平台新鲜查询轮换。

**删除 1129 行**：deploy_ssh/deploy_server（硬编码 root 口令）、ai_platforms.py（6 类扫描器全死）、僵尸 CI darkforest-scan.yml（还调已删的 deepseek 子命令）、supervisor/monitor_run/pack_release/diagnose；bin/ 与 results/*.yaml 解除跟踪 + .gitignore 补漏。误伤纠正：trend_monitor/email_notifier/10 个 scanners/ 全部生产可达不可删。

459 passed（新增 4 回归），ruff 全绿。

## v2.4.8 (2026-09-22)

限速桶跟随出口 IP，基线跟随模式——回退不再带病提速：

1. **pacing key = 出口 IP** — 多代理每 IP 一个桶（4.0s/IP）；单代理所有 token 共享 `__ip_shared__` 桶，基线 = 4.0 × token 数（保持 IP 级 ~15 req/min）。v2.4.7 断链教训：模式切换后速度必须跟着换挡。
2. mihomo `log-level` warn→warning（a6f65cf）—— v1.18.0 只认 `warning`，其 "invalid mode" 报错信息完全误导（三次复现锁定）。
3. WatchScanner `proxy_subscription` 断链修复（72dd5d7）—— run_watch→WatchScanner→ScannerEngine 三层都没传，**多代理模式从未生效过**。
4. 扫描间隔 4.0s→6.0s（4693162）对齐 Code Search 10 req/min/token 限额；验证提速 4 worker→8、间隔 0.5s→0.25s（准确性优先前提下）。

## v2.4.7 (2026-09-22)

内嵌 mihomo 多代理 + per-IP 路由：

1. **内嵌 mihomo 客户端** — 自动下载、YAML 配置（listeners+proxies+mode Rule+sniffer 对象）、子进程管理；每上游代理一个本地 HTTP 端口（17890+）。订阅（VMess/Trojan/Hysteria2/SS/HTTP 直链）→ 每 GitHub token 独享出口 IP。
2. **MultiProxyRouter** — token→代理稳定映射、负载最小分配、`get_pacing_key` 返回 `host:port`（per-IP 独立限速桶）、429 仅冷却该 IP 5min、死端口剔除。
3. **验证队列多 IP 负载均衡**（9c3c369）— 每 worker 线程分配独立代理 IP。
4. 基线提速 8.0→4.0s（5 IP 实测安全上限 3.5s 留余量）；mihomo 配置格式修正 + 实跑验证零 429（c2a1459）。

## v2.4.6 (2026-09-22)

GitHub IP 级滥用检测惩罚箱：

1. **根因** — per-token pacing 挡不住 IP 级滥用检测：3 token 同 IP × 9 req/min = IP 级 27/min > 阈值 15-20/min（凌晨实测触发升级惩罚 121s→134s 后紧急修复）。
2. **惩罚箱** — 任何 token 出现 Retry-After > 60s（明确滥用信号）→ 全局降速 25s/token 持续 10 分钟，期间不再触发升级惩罚。
3. 基线 6.6→8.0s；2min 内 429 → 15s 降档；IP 级 1h ≥3 次 → 惩罚箱。
4. 教训固化：验证防滥用必须跑数小时以上——别在"11 分钟零 429"上押注。

## v2.4.5 (2026-09-21)

GitHub 限流防撞（实跑实证：6.3s 压 95% 限额持续 4.5h 后，17:27–17:39 聚集 5 次 HTTP 429 二级滥用限流——3 token 同 IP 长跑被滥用检测盯上）：

1. **基线回撤一档**：6.3s → **6.6s**（9.1 req/min，限额 91%）——吞吐 −4%，换取持续运行不进滥用检测。
2. **429 分层冷却**：记录滑动 1 小时 429 时刻；2 分钟内有 429 → 12s（原行为）；**1 小时内 ≥3 次 → 15s 深度冷却**（限额 60%），杜绝"短冷却后复发"。
3. **±8% 抖动**：实际等待加随机抖动，打散机器人式固定节奏（滥用检测对等间隔敏感）；字典记录标称基线，抖动只作用实际 sleep。
4. **余量前置**：`X-RateLimit-Remaining ≤ 2` 即 sleep-to-reset（原 ≤1），固定节奏与窗口边界对齐时不再贴脸撞线。
5. Retry-After 照单全收 + >90s 惩罚期跳本轮 + >300s self-quiet（原有行为不变）。

## v2.4.4 (2026-09-21)

智谱假余额 2000 修正 + GLM Coding Plan 周额度：

1. **根因** — 旧实现用社区端点 `/api/monitor/usage/quota/limit` 的 `limits[0].remaining` 当余额：那是**资源包总额度**（注册赠送恒为 2000），既不是余额也不是 Coding 套餐剩余 → "人人 ¥2000、实际调不动"的假象。
2. **按量真余额** — zhipu 改用 `GET /api/paas/v4/users/balance`（401 严格鉴权实测），解析 `balance_infos[].balance` 求和（资源包/赠送/充值按官方扣费顺序均可消费）。
3. **GLM Coding Plan 周额度** — zhipu_coding 接入 monitor 端点的套餐窗口解析：`data.limits[]` 中 `TOKENS_LIMIT/CREDIT_LIMIT` 按 `nextResetTime` 区分 5 小时窗与周窗，取**周窗剩余百分比**（`percentage`=已用、或 `remaining/usage`、或 `currentValue` 三级回退）作为该 key 的"余额"，币种 `PERCENT`。无套餐窗口（按量 key 打过来）→ None 不折算。注意该端点鉴权失败返回 HTTP 200+body code:1000，解析自然得 None，无假阳性路径。
4. **PERCENT 币种语义** — `PERCENT_PROVIDERS`：换算透传（百分点不冒充金额）、CSV 原始余额列显示剩余 %、邮件币种透传；重验剔除豁免（周额度每周重置，0% 也保留记录）。
5. 历史假余额行（zhipu/zhipu_coding valid 行）已回拨 last_seen 强制下轮重验纠正。
6. 新增 5 个测试（余额求和/周窗选择/无套餐 None/200-错误体防线/剔除豁免）；451 passed，ruff 全绿。

## v2.4.3 (2026-09-21)

全链路效率优化（扫描速率 / 验证队列 / 信号纯度 / 监控漏斗），全部由实跑数据驱动：

1. **扫描速率压线** — Code Search per-token pacing 基线 7.5s→6.3s（8→9.5 req/min，限额 10/min 的 95%）。429 自适应降档（12s）与 remaining≤1 sleep-to-reset 双保险不变，压线不触罚时。
2. **验证队列扩容** — 模糊 key 并行验证候选平台 3→4 家（`max_candidates`），同分候选按 `provider.priority` 历史产出排序——无上下文的通用 sk- key 优先试 deepseek/kimi/qwen 等高产平台；首个有效即取消其余候选，增量成本只在前面全 invalid 时发生。
3. **占位符 key 过滤（DB 实证）** — unknown 平台 1364 个 key 全是描述短语/占位符（`change_me_*`、葡语 `COLOQUE_SUA_CHAVE_AQUI`、`test-not-real`）。BAD_PATTERNS 补 18 个实证模式 + 新增 slug 启发式（全小写多段纯字母短语 → 垃圾；ms-UUID hex 段与 sk-ant 大写段不受影响）。
4. **broker 拒收非推理类 token** — `hf_`/`ghp_`/`github_pat_` 等平台凭据不再进验证队列（HuggingFace 是数据源不是验证平台，此前只产生 unknown 垃圾行）。
5. **差异化平台限速** — `ProviderRateLimiter` 支持 per-provider 间隔覆盖；千帆实测 0.25s 全局间隔下出现 429，单列 1.0s。
6. **扩展查询池重生成** — `query_enumerator` 覆盖第二批 8 家平台的 context 查询笛卡尔积：queries_generated.txt 4631 条（此前新平台为 0）。
7. **metrics 全链路漏斗** — 新增近 24h 验证状态分布、平台错误/限流 TOP（≥5 次验证）、unknown key 掩码样本（用于发现未覆盖的新格式）。
8. **Kimi 余额口径修正（官方字段语义）** — `available_balance` = 现金+代金券（欠费时恰等于代金券），代金券不可抵扣现金计费。余额报表口径改取 **`cash_balance`**（现金，可为负=欠费）；旧响应缺该字段时用 `available - voucher` 折算。纯券账户从此归为 valid_zero 而非虚标余额；`balance<=0`（含欠费负值）统一判有效但无钱。
9. 新增 9 个测试锁定：slug 过滤/真实 key 不误伤/候选排序/差异化限速/token 拒收/Kimi 三种余额形态；438 passed，ruff 全绿。

## v2.4.2 (2026-09-21)

入口收敛与默认值调优（运营者决策：微额探测消耗无条件接受，产出收益最大化优先）：

1. **watch 成为唯一扫描入口** — 删除 `deepseek` / `multi` / `source` / `report` 四个一次性扫描子命令及 `multi_provider_scan.py`、`disclosure.py` 模块（watch 已完整覆盖其能力）。裸启动 `python run.py` 等价于 `python run.py watch`。保留 `metrics` / `maintenance` 账本工具。
2. **chat 探测默认开** — `[verification] allow_chat_probe` 默认 true：每 key 通过只读 GET 后补一次 `max_tokens=1` 最小探测确认"实际可用"（实测口径校准：余额接口显示 ≠ 实际可调用，如 kimi 代金券余额）。`--no-allow-chat-probe` 可关回纯只读。
3. 文档全面对齐：README 中英双语、DEVELOPER 架构路径、watch 手册（验证章节重写、速查表更新）。

## v2.4.1 (2026-09-21)

全平台端点鉴权审计（假 key 实测 20+ 端点）+ 不明确平台兜底探测。**修复 3 个现存平台误报/失效 bug**：

1. **修复 OpenRouter 误报（严重）** — 实测 `/models` 是公开目录（无 key 也 200），旧逻辑把任何 `sk-or-v1-` 格式假 key 判成有效。验证端点改 `/auth/key`（严格鉴权，假 key 401），并新增余额解析（`limit - usage` = 剩余 USD）——OpenRouter 从"验证不准"升级为第 7 个可判余额平台。
2. **修复 Jina 误报路径** — `/models` 同样公开（假 key 200），且无 OpenAI 式 chat 端点（探测 404 会被误判"认证已过"）→ `models_unauthenticated=True` + `chat_probe=False`，只读模式判 ERROR 不误报。
3. **零一万物（Yi）停运下线** — 实测 `/v1/models` → 410 `model_service_closed`（API 服务已关停）。`enabled=False` 退出验证轮询，不再每轮白烧请求；保留配置供历史 DB 查询。
4. **不明确平台兜底探测 `probe_unclear`（默认开）** — models_unauthenticated 平台（魔搭/NVIDIA/LongCat）只读模式无法判定，允许发一次 `max_tokens=1` 最小探测判定有效性；`[verification] probe_unclear_platforms` 或 `--no-probe-unclear` 可关（关则判 ERROR）。全平台 chat 探测仍需显式 `--allow-chat-probe`，安全姿态不变。
5. **其余平台鉴权确认** — Groq/Together/Fireworks/DeepInfra/Replicate（国际）与方舟/硅基/阶跃/Moonshot/百川（国内）全部 401 严格鉴权，现有逻辑正确；Voyage 无 `/models` 端点（404，维持 ERROR 语义，不误报）。
6. **新鲜簇接入新平台** — `generate_fresh_queries` 每轮注入 2 条第二批平台查询（`NEW_PLATFORM_FRESH` 轮换池，8 家全覆盖），与 deepseek 新鲜查询同待遇排轮次最前；`_PLAT_DOMAINS` 补齐第二批平台 + 首批国际平台域名（新平台查询此前拿不到首轮优先）。

## v2.4.0 (2026-09-21)

第二批平台接入（27 → 35）：OpenAI、Google Gemini、xAI Grok、腾讯混元、百度千帆、魔搭 ModelScope、NVIDIA NIM、美团 LongCat。全部端点行为与 key 格式经 2026-09-21 假 key 实测确认。

1. **KEY_PATTERN 扩容（scanners/base.py 单一真相源）** — 新增 OpenAI `sk-proj-/sk-svcacct-/sk-admin-`（专用分支置于通用 sk- 之前，防止 {20,95} 截断长 key）、Gemini `AIza`（39 位）与 2026 新 Auth key `AQ.Ab`、`xai-`、`nvapi-`、`ms-`UUID、千帆 `bce-v3/ALTAK-*/*`。
2. **is_bad_key 前缀放宽** — `sk-proj-` 不再被整体拒绝（OpenAI 接入后是正式提取目标，上限 250 与验证端护栏对齐）；`AQ.Ab` 上限 200；普通 sk- 家族 >80 拦截不变。
3. **8 个新 AIProvider 配置** — 含 key_patterns、上下文查询、api_base、验证模型；混元/千帆/魔搭/LongCat 为国内端点（直连），OpenAI/Gemini/xAI/NVIDIA 走代理。
4. **防误报护栏 `models_unauthenticated`** — 实测魔搭/NVIDIA/LongCat 的 `/models` 公开（假 key 也 200），默认只读模式对这些平台判 ERROR 绝不判 valid；显式开启 chat 探测后由探测结果判定（魔搭/LongCat chat 401、NVIDIA chat 403 实测确认鉴权正常）。
5. **平台化状态码映射** — 千帆 `/v2/models` 对无效 key 返回 403 AccessDenied（非 401）→ 403 按 invalid 收敛；Gemini 403（密钥真实但无 Gemini 权限）→ invalid；xAI 无效 key 返回 400 → invalid（既有 400 分支已覆盖）。其余平台 403 仍按 ERROR 可重验（防 WAF/区域拦截误杀）。
6. **Gemini query 认证** — `AuthType.API_KEY_QUERY` 接线：key 走 `?key=` URL 参数；Gemini 非 OpenAI 兼容协议，chat 探测关闭，GET models 严格鉴权已够。
7. **币种口径修正 `USD_PROVIDERS`** — 旧实现只认 claude 为 USD，openrouter/groq 等 10 个国际平台被错标 CNY；现以共享集合统一（新增 4 个 USD 平台一并纳入），broker 与 engine 两个判定点共用。
8. **QueryGenerator 前缀推断重构** — 硬编码 if/elif 链抽为 `_key_prefix()` 单一实现，新增 `sk-proj-/xai-/nvapi-/ms-/AIza/bce-v3` 前缀；`generate_rolling_for_provider` 同步改用（修复非 sk- 平台滚动查询白烧配额）。
9. **测试** — 新增 21 个用例：新格式提取完整性（sk-proj- 长 key 不截断）、格式即路由、各平台状态码映射、models 公开护栏、探测失败不退回"GET 认证"；`python -m pytest tests/` 428 passed。
10. **查询库接入新平台** — queries_optimized.txt 297→355 行：8 家新平台 51 条查询（专有前缀 `sk-proj-/bce-v3/nvapi-` 直接做搜索词，噪声前缀 `ms-/xai/AIza` 强制配平台语境词）；`build_active_queries` 动态注入 6 条新平台新鲜度查询（周级窗口）；删除 10 条 2026-05/07 硬编码日期的过期 deepseek 查询（watch 的 load_queries 路径不过滤日期，一直在白烧配额）。新增 4 个测试锁定覆盖不变量。

## v2.3.1 (2026-08-29)

1. **修复 watch 状态“复活”** — `_save_from_broker` 先构建 JSON 快照、后从 history map 剔除欠费/失效 key，导致 `dict.pop` 不会移除旧列表引用；下次启动 `load_history()` 会优先从 `watch_state.json` 恢复这些 key。现改为剔除后重建快照；全部 key 被剔除时写入空快照，不让旧 state 残留。
2. **chat 生成探测改为显式 opt-in** — `UnifiedKeyVerifier` 默认只发 GET models/balance，不再自动 POST `/chat/completions`。CLI 新增 `--allow-chat-probe`，配置新增 `[verification].allow_chat_probe`；显式开启后仍保持 `max_tokens=1` 最小请求。
3. **验证链路并发治理** — 模糊 key 的候选平台验证复用有界线程池；watch 多 worker 共享 per-provider 限速器，避免同时压同一家 API；候选请求通过 Session 工厂拿线程安全连接，不再共享同一个 `requests.Session`。
4. **修复数据源目录漂移** — 新增 `AVAILABLE_SOURCES` 单一真相源；`--list-sources`、`source --source`、engine registry 和 `run_multi_source` 使用同一目录；移除 Gitee/PyPI/Reddit 等未实现源的虚假宣传。
5. **治理 SQLite invalid 明文** — 确定性 invalid key 落库时只保留 `key_hash`；watch 启动会迁移旧库存量明文；新增 `run.py maintenance` 支持显式脱敏、按天数清理和 VACUUM；清理 key 时同步删除孤立 `key_history`。
6. **消除 watch 空闲写放大** — Broker 增加状态版本号；没有影响 state/CSV 的验证结果变化时，源线程轮询不再全量重写 JSON/CSV。
7. **建立实际产出反馈闭环** — 扫描 key 携带 source/query；`keys` 表自动迁移 `query` 列，验证结果按查询持久化；回调 `QueryTracker` 按 valid 和高价值转化折算收益，让大量候选但零余额的查询自动降权；`source` 单次扫描结果也写入 SQLite；`metrics` 输出 Top 查询转化榜。
8. **增加查询质量熔断** — 验证样本充足、零高价值且 valid 率低于 5% 的查询自动移出 Code Search 预算；未验证的探索查询不受影响，避免反馈机制扼杀新发现。
9. **扩展反馈到单次扫描** — `deepseek` / `source` 验证后也自动把 valid/高价值结果回写 `QueryTracker`；灭绝保护改用质量收益排序，且不会复活已被质量熔断的查询。
10. **全路径应用质量熔断** — `deepseek` 主流水线和 `source github_search` 也会跳过长期零 valid 且无高价值的查询，避免 CLI 单次扫描绕过 watch 的预算保护。
11. **修复 once 模式时序** — `--once` 的外部源立即扫描，不再空转等待轮次；退出时至少等待 120 秒让在途验证完成，真实 npm 复跑确认提交 1 / 验证 1。
12. **区分可判余额平台的查询权重** — 验证反馈记录 provider 是否有余额端点；有余额接口的 Moonshot/DeepSeek 类查询优先于候选量大但无法判余额的 DashScope 类查询，低价值查询继续熔断。
13. **识别下降趋势查询** — 真实验证发现 `application.yml` 一轮 52 候选、下一轮 0 候选；这类查询不再进入变异种子，并在主流水线/watch 排序中降级，但仍保留一次复测机会。
14. **抽取 watch_persistence 模块** — state/history/CSV 读写从 `watch_tui` 拆出，保持既有导入和 monkeypatch 兼容，降低入口文件复杂度。
15. **新增 metrics 产出画像** — `run.py metrics` 聚合各源/平台/查询/状态的候选、valid、高价值和余额转化；`--check-github` 可探测 GitHub token 健康度且不显示凭据。
16. **清理 lint 与版本口径** — `ruff check .` 全绿；`pyproject.toml` 版本对齐变更记录；README/DEPLOY/watch 手册同步默认 GET-only 语义。

## v2.3.0 (2026-08-26)

产出修复与效率优化(batch, 10 项提交):

1. **修复 `scanner_engine.KEY_PATTERN` 缺智谱 hex.secret 模式** — 旧实现本地复制了一份 `KEY_PATTERN`(缺 `\b[a-f0-9]{32}\.[A-Za-z0-9]{16}\b`),却注释声称"复用 scanners/base.py 单一真相源"→ github_search 主源提不出智谱 key,e212343 投入的 456 条智谱查询白扫。改为顶部 `from scanners.base import KEY_PATTERN`。
2. **修复 `is_bad_key` len>80 误杀 MiniMax JWT / Claude 长 key** — 27f2496 为治 fresh-repo env 长内容误匹配把阈值 100→80,但 eyJ(MiniMax JWT 最小总长 83)与 sk-ant-api03-(Claude 真实总长 ~104-120)天然超长 → 全部被杀(DB minimax valid=0 印证)。改按前缀放宽: eyJ≤250 / sk-ant-api03-≤150,普通 sk- 仍 ≤80。
3. **deepseek/qwen hex + kimi alnum 字符集预检** — DB 证据: 有效 deepseek 824/824、qwen 753/753 均为 32 位小写 hex,纯字母占位串(uikoukw 类)在此被预检拦下省一次验证 API(0.5-2s)。
4. **接线 github_events 实时 PushEvent 流** — `EventsMonitor` 加 `deadline_s` 参数(search 用 asyncio.timeout 限时返回),注册进 engine registry + DEFAULT_WATCH_SOURCES + 导出 + 标签。当前存量饱和下唯一还没榨干的新鲜度杠杆。docker(历史产出 1 行)移出默认源。
5. **0 余额 key 重验改周级冷却** — `ReverifyScheduler._tier_interval`: balance<=0 返回 `zero_interval_hours*3600`(默认 168h=7天)。实测 0 余额回充率为 0,旧实现下 2000 个 0 余额 key 每天吃掉几乎全部 1500 预算。CLI `--reverify-zero-hours` + config `reverify_zero_interval_hours` 可调。
6. **验证链路 Session 注入复用** — `UnifiedKeyVerifier(session=...)` + `_http_get/_http_post`,VerificationBroker 每 worker 线程缓存一个 verifier + 复用 per-thread requests.Session,省每次验证的 TCP/TLS 握手。
7. **`_save_from_broker` 历史缓存化** — 启动一次性 `load_history` 进内存 map,后续保存只做内存合并,不再每 15s 反复读盘(1.5MB JSON + 全表 SELECT + CSV);CSV 旧行平台字段从缓存回填。
8. **`run.py deepseek` 验证口径对齐多平台** — `_verify_dict` 改用 `UnifiedKeyVerifier`(对齐 watch broker),sk-* key 不再被 deepseek 余额端点 401 误判 invalid。
9. **校准文档** — README/READCN 余额查询平台更新为 deepseek/kimi/zhipu/stepfun/siliconflow/minimax(cp)。
10. **清理死代码** — 删 `known_keys`/`all_known_keys`/`get_valid_keys`/`shannon_entropy`/`extract_high_entropy`(生产无引用)。

## v2.2.1 (2026-08-08)

Watch 24/7 统计清零根因修复 + 历史闭环（commit 0425739 + ad942cb，188 tests + ruff 全绿）：

1. **修复 SQLite 跨线程写入被静默吞掉（真根因）** — `store.connect()` 在主线程创建连接、验证 worker 线程 `upsert()`，Python sqlite3 默认禁止跨线程使用 → `ProgrammingError` 被 `except Exception: pass` 吞掉 → `darkforest.db` 永远 0 行、跨运行去重种子失效。修复：`check_same_thread=False` + `_store_lock` 串行化 + 失败记日志（`darkforest.store`）。
2. **"提交"卡片改累计计数** — 原显示"各源当轮提交数"（每轮覆盖），轮次交替时骤降闪 0，看着像清零。新增 `WatchState._source_submitted` 累计计数器（`add_source_submitted`/`source_total_submitted`），三条扫描路径（`_source_worker`/`_run_bucket`/`_scan_github_serial`）统一累计，卡片单调递增。
3. **重启历史闭环** — 启动时 `load_watch_state()` → `seed_from_history()`：历史 key 播种 `_seen`（跳过重验，与 SQLite 种子互为备份）+ 高价值 key（>min_balance）回显 TUI 表格，重启不空白、不重验。
4. **修复保存覆盖历史（ad942cb）** — `_save_from_broker` 原用空 broker 结果覆盖 `watch_state.json`，重启后首轮保存即清空上次会话的 key（TUI 两秒内从"有 key"变"暂无"）。改为**保存=合并**（broker 结果 ∪ 磁盘历史，新验证覆盖同名、纯历史保留），TUI 表格用合并后列表。
5. **修复高价值表"验证时间"列** — `verified_at[-8]`（取单字符恒显 "0"）→ `[-8:]`（显示 HH:MM:SS）。纯显示 bug，数据一直完整。
6. **新增 `docs/watch_manual.txt`** — watch 模式完整使用手册（架构/参数/代理/调参/排障）。



Bug 修复、查询库接线与测试套件：

1. **修复 `multi_provider_scan.py` 验证结果字段缺失** — `_verify_one` 中 `return r` 前的 4 行补字段代码（url/repo/file/context）永远不可达，导致结果 JSON 缺失来源信息；交叉验证命中的结果同样未补字段。现已提取 `_enrich()` 统一补全。
2. **修复 `identify_provider` 上下文得分累加缺陷** — 上下文关键词命中只命中单词（如高频的 `sk-`）时按 query 逐条累加，导致 query 条数多的平台（如 Kimi）分数虚高，压过真正命中平台域名的信号（如 `api.deepseek.com`）。改为取"最强单条 query 命中"。
3. **修复格式不匹配平台混入候选** — minimax（`eyJ` JWT 格式）在 key 为 `sk-` 通用格式时，仅凭上下文单词命中即进入候选。上下文加分现仅在格式匹配后生效。
4. **MiniMax 余额声明一致性** — `has_balance_check=True` 但无 `balance_endpoint`，实际查不了余额。改为 `False`，同步 README（余额查询平台：deepseek / zhipu / qwen）。
5. **`load_tiered_queries` 接线 `queries_optimized.txt`** — 默认文件原指向不存在的 `queries_v5.txt` 导致永远返回空；现默认加载 117 条 `queries_optimized.txt`，兼容无 tier 前缀的纯查询行（默认 tier 5），文件缺失时输出 stderr 提示。
6. **`build_active_queries` 去重** — 修复 `BUILTIN_QUERIES` 中重复条目（`deepseek sk- path:.github/workflows` 出现两次）导致的重复搜索请求。
7. **GitHub 搜索认证头统一为 `Bearer`** — `multi_provider_scan._github_search_sync` 原用 `token` 前缀（已弃用）。
8. **`_fetch_raw` 超时优化** — 连接/读取超时分离（5s/12s），移除 `master` 分支重试，避免代理挂起时整批下载阻塞。
9. **修复 HTTP 402 误判为 error** — 实测发现 `chat/completions` 返回 `402 Insufficient Balance` 时 key 认证已通过（仅欠费），原逻辑将其归类为 ERROR 导致漏掉有效泄露。现归类为 VALID_ZERO（有效但余额不足）。
10. **修复 MD 报告负余额计入总价值** — `_save_final`/`save_results` 的 Markdown 汇总把欠费账号（负余额）计入 Total USD/CNY（实测 -$0.42），现与 `run()` 日志口径一致（只统计正余额），并新增 Positive Balance Keys 行。
11. **新增测试套件 `tests/`（78 个单元测试）** — 覆盖 key 提取/过滤、动态查询构建、provider 识别、验证逻辑（mock 网络）、货币转换、结果去重。新增 `requirements-dev.txt`。

## v2.0.0 (2026-07-26)

本次为架构重构版本，聚焦稳定性、产出效率与可维护性。主要改动：

1. **修复 `multi_provider_scan.py` 的 `asyncio.run` 崩溃 bug** — 修正多平台扫描入口在已有事件循环环境下调用 `asyncio.run` 导致的崩溃，多平台扫描现在可稳定运行。
2. **Gist 扫描器加产出熔断** — Gist 扫描单次运行上限从 19 分钟收紧到 15 秒，避免在产出稀疏的数据源上空耗时间。
3. **Docker 扫描器串行改并发** — Docker Hub 扫描由串行抓取改为并发请求，显著缩短该数据源的扫描耗时。
4. **修复 Wayback 域名循环失效 bug** — Wayback Machine 扫描器的域名循环逻辑失效导致只命中单一域名，现已修复，覆盖范围恢复正常。
5. **移除所有扫描器 `force_close`** — 取消各扫描器中 `await session.close()` 的强制关闭，改为复用上层传入的 `aiohttp.ClientSession` 连接池，减少 TCP 握手开销与连接耗尽问题。
6. **PyPI 重复请求合并、StackOverflow 拉取浪费修复** — PyPI 扫描合并了对同一包的重复请求；Stack Overflow 扫描修复了无效分页拉取导致的流量浪费。
7. **`base.py` 新增 `_get_with_retry` 统一 429 退避** — `BaseScanner` 提供统一的带指数退避 GET 请求方法，优先读取服务端 `Retry-After` 头，取代各扫描器散落的 `except Exception: pass` 静默吞 429 写法。
8. **multi_provider 验证改并发** — 多平台 key 验证由串行改为 `ThreadPoolExecutor` 并发，149 个 key 的验证从超时降到约 10 秒。
9. **GitHub 查询动态滚动时间窗口** — 新增 `generate_rolling_time_queries()`，根据当前日期动态生成"最近 7 天 / 30 天"的 `pushed:>` 查询，替代过期的硬编码日期查询，逆转了固定日期查询随时间产出下降的问题。
10. **创建统一入口 `run.py`，归档旧脚本** — 新增统一 CLI 入口 `run.py`（含 `deepseek` / `multi` / `source` 三个子命令），替代旧的 12 个入口脚本；旧脚本归档到 `legacy/` 目录仅作参考。
11. **修复国内平台代理 IP 被封问题** — `providers.py` 引入 `DIRECT_PROVIDERS` 列表，国内 AI 平台验证时绕过代理直连，避免代理 IP 池被国内厂商风控封锁。

## v1.0.0 (2026-05-21)

初始发布版本。

- DarkForest Hunter 首次发布：开源安全研究工具，扫描公开代码仓库泄露的 DeepSeek API Key。
- 覆盖 **14 个平台**，使用 **238 条搜索查询**。
- 包含 Gist / Issues / Commits / GitLab / Gitee / HuggingFace / PyPI / npm / StackOverflow / Docker / Wayback / CommonCrawl 等多源扫描器。
- 支持自动验证 key 有效性并查询余额，输出 JSON / CSV / Markdown 三种格式。
- 保留多个独立入口脚本（`ultimate_scan.py` / `full_scan.py` / `fast_scan.py` 等）。
