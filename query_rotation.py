"""查询轮转与变异引擎 — 让 24 小时持续扫描不断产出新 key。

核心策略：
1. 新鲜度查询簇轮转：每轮不同文件类型组合 + sort=indexed 抓最新索引结果
2. 查询分桶：把查询分成 N 个 bucket，每轮只跑 1 个，避免重复
3. 查询变异：基于高产模式自动生成变体，扩展查询空间
"""

from __future__ import annotations

import json
import os
import re

# ══════════════════════════════════════════════════════════════════
#  查询分桶
# ══════════════════════════════════════════════════════════════════

def load_queries(filepath: str = "queries_optimized.txt") -> list[str]:
    """从文件加载查询列表。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filepath)
    if not os.path.exists(path):
        return []
    queries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "pushed:" not in line:
                queries.append(line)
    return queries


def bucketize_queries(queries: list[str], num_buckets: int = 4) -> list[list[str]]:
    """把查询分成 N 个 bucket（按模式分组，保证每个 bucket 覆盖不同文件类型/语言）。"""
    # 按查询模式分组
    buckets: list[list[str]] = [[] for _ in range(num_buckets)]
    for i, q in enumerate(queries):
        buckets[i % num_buckets].append(q)
    return buckets


# ══════════════════════════════════════════════════════════════════
#  新鲜度查询簇生成器
# ══════════════════════════════════════════════════════════════════

# 高产出文件类型（基于历史数据分析）
HOT_EXTS = ["env", "java", "py", "js", "yml", "json", "ts", "kt", "php", "go"]

# v2.5.1: 删除 TIME_SLICE_TEMPLATES——全仓库零引用的死代码(grep 实证),
# deepseek 查询实际内联在 generate_fresh_queries 里。

# 兼容层查询：大量项目用 OpenAI/Anthropic 接口但配置第三方 key
# （只改 base_url + key，代码里写 api.openai.com / OPENAI_API_KEY）。
# 只搜平台名会漏掉这批"换皮"项目——按 OpenAI/Anthropic 标识搜，
# 提取到的 sk- key 再由 UnifiedKeyMatcher 前缀路由到正确平台。
# 实测：api.openai.com sk- 命中 100 条（含 OPENAI_API_KEY=sk-*** 的 .env）。
COMPAT_QUERIES = [
    # v2.6: Multi-platform compat queries based on actual key yield
    "openrouter sk-or-v1- filename:php",
    "openrouter sk-or-v1- filename:java",
    "siliconflow sk- filename:java",
    "dashscope sk- filename:java",
    "openrouter sk-or-v1- filename:dart",
    "api.siliconflow.cn sk- filename:py",
    "openrouter sk-or-v1- filename:properties",
    "api.openai.com sk-",
    "api.openai.com sk- filename:env",
    "openai sk- filename:env",
    "OPENAI_API_KEY filename:env",
    "OPENAI_API_KEY filename:py",
    "OPENAI_API_KEY filename:js",
    "OPENAI_BASE_URL sk- filename:env",
    "base_url sk- filename:env",
    "base_url sk- filename:py",
    "base_url sk- filename:json",
    "api.anthropic.com sk-",
    "api.anthropic.com sk- filename:env",
    "ANTHROPIC_API_KEY filename:env",
    "ANTHROPIC_API_KEY filename:py",
    "ANTHROPIC_BASE_URL sk- filename:env",
]


# v2.4.1: 第二批平台新鲜查询(轮换池)。专有前缀直接做词(sk-proj-/bce-v3/nvapi-);
# 噪声前缀(ms-/xai/AIza)配语境词。覆盖 8 家: OpenAI/Gemini/xAI/混元/千帆/魔搭/NVIDIA/LongCat。
# v2.5.1: nvapi- 裸前缀噪声大 → 配 build.nvidia.com 语境(研究实证);
# +MODELSCOPE_TOKEN(SQL DB 已证 10/3v 但不在任何轮换池)、
#  +SILICONFLOW_API_KEY(env 变量是最大空白面)。
NEW_PLATFORM_FRESH = [
    "sk-proj-",
    "GEMINI_API_KEY AIza",
    "build.nvidia.com nvapi-",
    "bce-v3",
    "XAI_API_KEY xai-",
    "api.hunyuan.cloud.tencent.com sk-",
    "MODELSCOPE_TOKEN ms-",
    "SILICONFLOW_API_KEY filename:env",
    "api.longcat.chat sk-",
    "MODELSCOPE_API_KEY ms-",
    # v2.5.1: claude 新鲜位 0 条专查(54 条全场最薄)+ gemini AIza 2026-09
    # 起被拒收,AQ.Ab 是唯一活口——两家稀缺平台补新鲜位吃 sort=indexed 红利
    "sk-ant-api03-",
    "AQ.Ab filename:env",
]


# v2.4.9: 高产平台新鲜查询——此前新鲜簇 95% 是 deepseek,kimi/zhipu/minimax/
# groq/openrouter 等高产平台拿不到 sort=indexed 的新鲜度红利,只能靠静态
# bucket 轮次碰运气。每轮带 2 条轮换,8 轮覆盖全部。
HIGH_YIELD_PLATFORM_FRESH = [
    "sk-kimi- filename:env",                       # Kimi 专有前缀
    "gsk_ filename:env",                           # Groq 专有前缀
    "sk-or-v1- filename:env",                      # OpenRouter 专有前缀
    "api.minimaxi.com eyJ",                        # MiniMax JWT + 语境
    "api.siliconflow.cn sk- filename:env",         # SiliconFlow
    # v2.5.1: 修智谱槽位错误形态——hex.secret key 不含 sk-,旧
    # "bigmodel.cn sk-" 要求共现永远漏掉纯 hex 泄露(研究实证:DB
    # hex.key 文件常无 sk-,裸 open.bigmodel.cn 7 发 5 中)
    "open.bigmodel.cn filename:env",               # 智谱 .env 泄露面
    "ZHIPUAI_API_KEY",                             # 官方 SDK 默认 env 词(optimized 现 0 条)
    "MINIMAX_API_KEY filename:py",                 # MiniMax 环境变量
    "api.moonshot.cn sk- filename:env",            # Kimi 域名
    "open.bigmodel.cn filename:php",               # 智谱 PHP 建站面(DB 3/3 全中)
]


def generate_fresh_queries(pattern: int = 0) -> list[str]:
    """生成一轮"新鲜度"查询（无日期过滤，靠 sort=indexed 抓最新索引结果）。

    pattern: 轮转模式索引——不同模式覆盖不同文件类型组合，
    多轮轮转后覆盖全部高产出类型（等价于原时间切片的轮转作用）。
    """
    # 按模式轮转的高产出扩展（env 永远在——最高产出）
    # v2.6: Expanded from 5 groups to 8, based on server data
    groups = [
        ["env", "java", "py"],
        ["env", "js", "yml"],
        ["env", "json", "ts"],
        ["env", "kt", "php"],
        ["env", "go", "rs"],
        ["env", "dart", "cs"],       # v2.6: OpenRouter hotspot (dart:50, cs:31)
        ["env", "properties", "yml"], # v2.6: 191 properties files in DB
        ["env", "sql", "sh"],         # v2.6: sql(14h/r), sh growing
    ]
    exts = groups[pattern % len(groups)]
    queries = ["deepseek sk-"]
    for ext in exts:
        queries.append(f"deepseek sk- filename:{ext}")
    queries.append("DEEPSEEK_API_KEY sk-")
    queries.append("api.deepseek.com sk-")
    # 兼容层查询：OpenAI/Anthropic 接口 + 第三方 key（换皮项目）。
    # 每轮带 2 条。修复:旧 pattern%len 只覆盖前 11 条——用
    # (pattern*2)%len 递进取对,多轮覆盖全部 23 条。
    cq = COMPAT_QUERIES
    queries.append(cq[(pattern * 2) % len(cq)])
    queries.append(cq[(pattern * 2 + 1) % len(cq)])
    # v2.4.1 第二批平台新鲜查询:每轮带 2 条,专有前缀优先(sk-proj- 泄露量最大)。
    # 与 deepseek 新鲜簇同待遇——排在 bucket 之前,靠 sort=indexed 抓最新索引。
    npq = NEW_PLATFORM_FRESH
    queries.append(npq[(pattern * 2) % len(npq)])
    queries.append(npq[(pattern * 2 + 1) % len(npq)])
    # v2.4.9 高产平台新鲜查询:每轮 2 条,8 轮覆盖 kimi/groq/openrouter/
    # minimax/siliconflow/zhipu(此前新鲜簇 95% deepseek,这些平台无新鲜度红利)
    hq = HIGH_YIELD_PLATFORM_FRESH
    queries.append(hq[(pattern * 2) % len(hq)])
    queries.append(hq[(pattern * 2 + 1) % len(hq)])
    return queries


# ══════════════════════════════════════════════════════════════════
#  查询轮转器
# ══════════════════════════════════════════════════════════════════

class QueryRotator:
    """管理查询轮转：每轮返回不同的查询子集，保证 24h 持续覆盖。

    轮转策略：
    - 每轮 = 1 个 bucket（静态查询）+ 新鲜度查询簇（抓最新索引结果）
    - bucket 按轮次循环
    - 新鲜度查询簇按文件类型组合轮转（5 组循环）
    - 每 N 轮插入一次历史深度补扫（无 pushed: 版本）
    """

    # 新鲜度查询簇轮转（pattern 索引序列，循环）。
    # 修复历史:旧序列 [0,1,2,3,4,0,1,2] 只用前 5 组(v2.6 修过 groups);
    # v2.5.1 再修:range(8) 对 COMPAT_QUERIES 22 条只覆盖前 16 条
    # ((p*2)%22, p=0..7 → 0..15),16-21 位 6 条查询是永远轮不到的死位置
    # ——实测脚本确认。range(11) 时 (p*2)%22 恰好遍历全部偶数位+奇数位。
    FRESH_PATTERNS = list(range(11))

    def __init__(self, num_buckets: int = 4, queries: list[str] = None,
                 state_file: str | None = None):
        self.queries = queries or load_queries()
        self.buckets = bucketize_queries(self.queries, num_buckets)
        self.num_buckets = num_buckets
        self._round = 0
        # 零命中清理:记录每个查询连续 0 命中的轮次,连续 N 轮后剔除
        # _zero_streak[query] = 连续 0 命中轮次数
        self._zero_streak: dict[str, int] = {}
        self._ZERO_HIT_THRESHOLD = 3  # 连续 3 轮 0 命中即剔除
        self._pruned: set[str] = set()  # 已剔除的查询(不参与轮转)
        # 可选持久化：重启后从上次轮次继续（--fresh 可从头）
        self.state_file = state_file
        self._restore_state()

    def _restore_state(self):
        """从 state 文件恢复轮次（若存在）。"""
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
            self._round = int(data.get("round", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    def save_state(self):
        """保存当前轮次到 state 文件（原子写）。"""
        if not self.state_file:
            return
        try:
            os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
            tmp = f"{self.state_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"round": self._round}, f)
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    def reset(self):
        """从头开始：轮次清零并清除 state 文件（--fresh）。"""
        self._round = 0
        if self.state_file and os.path.exists(self.state_file):
            try:
                os.remove(self.state_file)
            except OSError:
                pass

    def next_round(self) -> list[str]:
        """获取下一轮的查询列表。"""
        self._round += 1
        round_idx = self._round

        # 1. 当前 bucket 的静态查询(跳过已剔除的)
        bucket_idx = (round_idx - 1) % self.num_buckets
        bucket_queries = [q for q in self.buckets[bucket_idx] if q not in self._pruned]

        # 2. 新鲜度查询簇（每轮不同文件类型组合，抓最新索引结果）
        pattern_idx = (round_idx - 1) % len(self.FRESH_PATTERNS)
        fresh_queries = generate_fresh_queries(self.FRESH_PATTERNS[pattern_idx])

        # 3. 每 10 轮插入一次历史深度补扫
        history_queries = []
        if round_idx % 10 == 0:
            history_queries = self._generate_history_queries()

        # 合并 + 去重
        seen = set()
        result = []
        for q in fresh_queries + bucket_queries + history_queries:
            if q not in seen and q not in self._pruned:
                seen.add(q)
                result.append(q)

        return result

    def report_query_result(self, query: str, hit_count: int) -> None:
        """报告本轮查询的命中数,用于零命中清理。

        在 watch_tui.py 的 _run_bucket/_scan_github_serial 每轮结束后调用。
        hit_count > 0 → 重置该查询的零命中计数;hit_count == 0 → +1。
        """
        if query in self._pruned:
            return
        if hit_count > 0:
            # 有命中 → 重置
            if query in self._zero_streak:
                del self._zero_streak[query]
        else:
            # 0 命中 +1
            streak = self._zero_streak.get(query, 0) + 1
            if streak >= self._ZERO_HIT_THRESHOLD:
                # 连续 N 轮 0 命中 → 剔除
                self._pruned.add(query)
                del self._zero_streak[query]
            else:
                self._zero_streak[query] = streak

    @property
    def pruned_count(self) -> int:
        """已剔除的 0 命中查询数(用于日志/TUI 展示)。"""
        return len(self._pruned)

    def _generate_history_queries(self) -> list[str]:
        """历史深度补扫查询（抓老泄露；Code Search 不支持日期过滤，用文件类型/路径覆盖）。"""
        return [
            "deepseek sk- filename:env NOT example",
            "deepseek sk- filename:application.yml",
            "deepseek sk- path:src/main/resources",
            "DEEPSEEK_API_KEY sk- NOT example",
            # 兼容层历史补扫：openai/anthropic 接口 + 第三方 key（换皮老项目）
            "api.openai.com sk- NOT example",
            "ANTHROPIC_API_KEY filename:env NOT example",
        ]

    @property
    def round_num(self) -> int:
        return self._round


# ══════════════════════════════════════════════════════════════════
#  查询变异引擎 —— 从历史高收益查询自动派生变体，扩展查询空间
# ══════════════════════════════════════════════════════════════════

# 文件名近邻簇：同一生态/同类文件，高产查询换近邻往往也高产
# v2.6: Extended based on 4774-key server data
# OpenRouter #1: php(87) > dart(50) > properties(39) > cs(31) > java(90 total)
# SiliconFlow: java(25.5h/r) > cs(12h/r) > py
# Top file types by key count: py(920) > java(468) > js(457) > yml(422) > json(325) > properties(191)
_FILENAME_CLUSTER: dict[str, list[str]] = {
    "java": ["kt", "scala", "php"],
    "kt": ["java", "scala", "php"],
    "scala": ["java", "kt"],
    "py": ["ipynb", "java"],
    "js": ["ts", "dart"],
    "ts": ["js", "dart"],
    "dart": ["ts", "js"],
    "yml": ["yaml", "properties"],
    "yaml": ["yml", "properties"],
    "properties": ["yml", "yaml"],
    "php": ["java", "kt"],
    "cs": ["java", "go"],
    "go": ["cs", "java"],
    "env": ["yml", "properties"],
    "json": ["yml", "properties"],
}

# 变量名后缀互换
_SUFFIX_CLUSTER: dict[str, list[str]] = {
    "API_KEY": ["KEY", "TOKEN"],
    "KEY": ["API_KEY", "TOKEN"],
    "TOKEN": ["API_KEY", "KEY"],
}


# ══════════════════════════════════════════════════════════════════
#  生成查询池 —— 从 query_enumerator.py 产出的 queries_generated.txt
#  每轮抽取一小批新查询试水，探索平台盲区，barren 机制自然淘汰空查询。
# ══════════════════════════════════════════════════════════════════

class GeneratedPool:
    """管理扩展查询池：每轮抽出 batch 条未试过的查询，循环遍历。

    不一次性全量投喂（会淹没配额），而是像探索一样每轮试一小批：
    - 命中 → 被 tracker 记为高产，之后走正常轮转保留
    - 空 → 本轮跑完，之后由 is_barren 跳过
    池子轮转完一遍后从头再扫（持续覆盖新索引的结果）。
    """

    def __init__(self, filepath: str = "queries_generated.txt",
                 batch: int | None = None, batch_size: int | None = None,
                 root: str | None = None):
        self.root = root or os.path.dirname(os.path.abspath(__file__))
        self.filepath = filepath
        # 批大小：新名 batch_size；兼容旧关键字 batch（load_generated_pool/watch_tui 仍用 batch=）
        self.batch_size = batch_size if batch_size is not None \
            else (batch if batch is not None else 12)
        self._queries: list[str] = []
        self._pos = 0
        self._load()

    def _load(self):
        path = self.filepath if os.path.isabs(self.filepath) \
            else os.path.join(self.root, self.filepath)
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    self._queries.append(line)

    def __len__(self):
        return len(self._queries)

    def __iter__(self):
        return iter(self._queries)

    def next_batch(self, size: int | None = None) -> list[str]:
        """取下一批新查询(池耗尽自动重置轮转)。size 覆盖默认 batch_size。"""
        n = size if size is not None else self.batch_size
        if not self._queries:
            return []
        if self._pos >= len(self._queries):
            self._pos = 0
            return []  # 本轮已耗尽：返回空并自动重置，下轮从头轮转
        batch = self._queries[self._pos:self._pos + n]
        self._pos += n
        return batch


def load_generated_pool(filepath: str = "queries_generated.txt",
                        batch: int = 12) -> GeneratedPool:
    """便捷构造。"""
    return GeneratedPool(filepath=filepath, batch=batch)


def mutate_query(q: str) -> list[str]:
    """对一个查询生成若干变体（扩展高产查询空间）。

    策略（任一适用即产生变体）：
      - 加 `NOT test` / `NOT example` 降噪，露出不同文件
      - filename: 同近邻簇互换（java↔kt↔scala, py→ipynb, js↔ts, yml↔yaml）
      - 变量名后缀互换（_API_KEY ↔ _KEY ↔ _TOKEN）
    不含原查询、无内部重复。"""
    out: list[str] = []
    if "sk-" in q:
        if "NOT test" not in q:
            out.append(f"{q} NOT test")
        if "NOT example" not in q:
            out.append(f"{q} NOT example")
    m = re.search(r"filename:(\w+)", q)
    if m:
        for nb in _FILENAME_CLUSTER.get(m.group(1), []):
            out.append(re.sub(r"filename:\w+", f"filename:{nb}", q))
    for suf, alts in _SUFFIX_CLUSTER.items():
        if suf in q:
            for a in alts:
                out.append(q.replace(suf, a, 1))
            break
    seen, res = set(), []
    for v in out:
        if v != q and v not in seen:
            seen.add(v)
            res.append(v)
    return res


def generate_mutants(top_queries: list[tuple[str, float]],
                     existing: set[str] | None = None,
                     max_mutants: int = 5) -> list[str]:
    """从高收益查询（(query, yield) 列表）派生变体。

    - 排除已在 `existing`（本轮已有查询）与已生成变体
    - 封顶 max_mutants，控制每轮 API 预算
    变体作为新查询进入轮转；命中即被 tracker 记为高产、长期 0 命中则被 is_barren 跳过，
    形成自调节的查询空间扩展。"""
    existing = existing or set()
    out: list[str] = []
    seen: set[str] = set(existing)
    for q, _yield in top_queries:
        for m in mutate_query(q):
            if m not in seen:
                seen.add(m)
                out.append(m)
                if len(out) >= max_mutants:
                    return out
    return out
