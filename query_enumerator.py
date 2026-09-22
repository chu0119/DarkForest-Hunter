"""
查询枚举引擎 (query_enumerator.py) — 结构化扩展查询空间，突破人工查询库盲区。

背景：
queries_optimized.txt（297 行）覆盖了 26 个平台中的少数（deepseek/openrouter/kimi/
qwen/siliconflow/dashscope/groq 等），但 providers.py 里每个平台都带一套已校准的
`key_context_queries`（平台名 + key 前缀）。大量平台（zhipu/doubao/baichuan/yi/xiaomi/
stepfun/sensenova/minimax/novita/together/fireworks/voyage/jina/replicate/deepinfra 等）
几乎没有任何专用查询，只能靠 deepseek 查询的"副作用"偶尔命中。

方案：用每平台的 `key_context_queries` × 高频文件类型/语言做**笛卡尔积**，自动生成
覆盖盲区的新查询。产出会写入 queries_generated.txt，供轮转引擎加载；已存在于
queries_optimized.txt 的查询自动去重，不浪费配额。

用法：
  python query_enumerator.py                 # 生成并写入 queries_generated.txt
  python query_enumerator.py --once          # 同上（默认）
  python query_enumerator.py --dry           # 只打印统计不写文件
"""
import argparse
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(ROOT, "queries_generated.txt")
BASE_FILE = os.path.join(ROOT, "queries_optimized.txt")

# ── 文件类型枚举（Code Search filename: 限定符，GitHub 语法）──
# 高产出配置/密钥文件 + 主流程文件，按平台语境词逐一组合
HOT_FILENAMES = [
    ".env", ".env.local", ".env.production", ".env.development", ".env.example",
    "application.yml", "application.yaml", "application.properties",
    "application-dev.yml", "application-prod.yml", "application-test.yml",
    "bootstrap.yml", "config.yml", "config.yaml", "config.json", "config.toml",
    "settings.json", "settings.py", "secrets.json", "secrets.toml",
    "docker-compose.yml", "docker-compose.yaml", "docker-compose.override.yml",
    "mcp.json", ".mcp.json", "claude_desktop_config.json",
    ".npmrc", "pypirc", "gradle.properties",
    "config.py", "config.js", "config.ts", "config.java", "config.go",
    "main.py", "app.py", "server.py", "client.py",
    "llm.py", "llm_client.py",
    "values.yaml", "secret.yaml", "secrets.yaml",
]
# ⚠ 收敛：全平台 × 全文件名 = 五千条级，太多会淹没配额。
# 改为**数据驱动**：只枚举 DB 里有货但查询覆盖不足的平台，
# 且文件名/语言用能真正捞到货的高潜子集。见 enumerate_platform_queries。

HOT_LANGS = [
    "Python", "JavaScript", "TypeScript", "Java", "Go", "Ruby",
    "Kotlin", "PHP", "Swift", "Rust", "C#", "Dart", "Bash", "Shell",
]

# 通用保密/模板文件（不依赖平台语境，配合 sk- 通用前缀——经常装 key）
GENERIC_KEY_FILE_QUERIES = [
    "sk- filename:env",
    "sk- filename:mcp.json",
    "sk- filename:.mcp.json",
    "sk- filename:claude_desktop_config.json",
    "sk- filename:.npmrc",
    "sk- filename:application.yml",
    "sk- filename:application.properties",
    "sk- filename:secrets.json",
    "sk- filename:settings.json",
    "sk- filename:docker-compose.yml",
    "sk- filename:config.json",
    "sk- filename:.envrc",
    "sk- path:.vscode",
    "sk- path:.github/workflows",
]


def load_existing(path: str = BASE_FILE) -> set[str]:
    """加载已有查询，用于去重。"""
    out: set[str] = set()
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and line not in out:
                out.add(line)
    return out


def normalize(q: str) -> str:
    """归一化：小写后去掉空白，用于去重判断（Code Search 不区分大小写文件？谨慎——用原样+小写双判）。"""
    return re.sub(r"\s+", " ", q.strip()).lower()


def enumerate_platform_queries(providers, focus_platforms: set[str]) -> list[str]:
    """仅对 focus_platforms（DB 有货但查询覆盖不足）的平台枚举查询。"""
    out: list[str] = []
    for p in providers:
        pid = getattr(p, "id", "?")
        # 归一化：平台 id 可能带 _coding/_cp 后缀，映射到聚焦平台主 id
        core = pid.split("_")[0]
        if core not in focus_platforms:
            continue
        ctx = getattr(p, "key_context_queries", None) or []
        for base in ctx:
            for fn in HOT_FILENAMES:
                out.append(f"{base} filename:{fn}")
            for lang in HOT_LANGS:
                out.append(f"{base} language:{lang} NOT test NOT example")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只统计不写文件")
    args = ap.parse_args()

    # 加载 providers
    import providers
    providers_list = providers.ALL_PROVIDERS

    # ── 数据驱动：确定聚焦平台 ──
    # 从 DB 读各平台实际 key 数；选择「有货但查询覆盖不足」的平台。
    focus = set()
    try:
        import store
        c = store.connect(os.path.join(ROOT, "results", "darkforest.db"))
        db_counts = {r[0]: r[1] for r in c.execute(
            "select provider, count(*) from keys group by provider")}
        c.close()
    except Exception as e:
        print(f"[warn] 读 DB 失败，退化为全平台: {e}")
        db_counts = {}

    # 有货平台（>5 个 key）——这些是真实盲区，值得枚举
    for pid, cnt in sorted(db_counts.items(), key=lambda x: -x[1]):
        if cnt >= 5:
            focus.add(pid.split("_")[0])
    # 明确排除：deepseek/openrouter 已有大量专用查询且饱和，枚举收益低；
    # 但保留它们在 focus 里也无妨（好查询会被 barren 机制自然淘汰）。
    # 为控制总量，去掉已有深度覆盖的 deepseek（61xx key 已饱和）
    focus.discard("deepseek")
    if db_counts:
        print(f"DB 有货平台: {sorted(db_counts.keys())}")
    print(f"聚焦枚举平台: {sorted(focus)}")

    existing = load_existing()
    # 归一化去重集
    norm_existing = {normalize(q) for q in existing}
    norm_seen: set[str] = set(norm_existing)

    new_queries: list[str] = []
    for q in enumerate_platform_queries(providers_list, focus):
        n = normalize(q)
        if n in norm_seen:
            continue
        norm_seen.add(n)
        new_queries.append(q)
    # 通用前缀查询
    for q in GENERIC_KEY_FILE_QUERIES:
        n = normalize(q)
        if n in norm_seen:
            continue
        norm_seen.add(n)
        new_queries.append(q)

    # 汇总
    by_platform: dict[str, int] = {}
    for q in new_queries:
        # 粗分平台（取查询第一个词）
        first = q.split()[0]
        by_platform[first] = by_platform.get(first, 0) + 1

    print(f"已有查询: {len(existing)} 条")
    print(f"新增查询: {len(new_queries)} 条")
    print("\n按平台分布（新增）:")
    for k, v in sorted(by_platform.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")

    if args.dry:
        return

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("# 由 query_enumerator.py 结构化生成——覆盖平台盲区的扩展查询。\n")
        f.write("# 与 queries_optimized.txt 去重。每平台 key_context_queries × 文件类型/语言。\n")
        for q in new_queries:
            f.write(q + "\n")
    print(f"\n已写入: {OUT_FILE} ({len(new_queries)} 条)")


if __name__ == "__main__":
    main()
