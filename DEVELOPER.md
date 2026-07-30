# DEVELOPER.md — DarkForest Hunter 开发者文档

本文档面向希望二次开发、扩展平台或新增扫描器的开发者。阅读前请先浏览 [README_CN.md](README_CN.md) 了解项目定位。

---

## 一、架构总览

DarkForest Hunter 由 4 个核心 Python 文件 + 一个 `scanners/` 包组成。整体数据流如下：

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐    ┌──────────────┐
│  查询库          │ →  │  GitHub Code     │ →  │  text_matches   │ →  │  验证 +      │
│  BUILTIN_QUERIES │    │  Search / 多源    │    │  提取 sk-xxx    │    │  查余额      │
│  + 动态时间窗口  │    │  扫描            │    │  候选 key       │    │              │
└─────────────────┘    └──────────────────┘    └─────────────────┘    └──────┬───────┘
                              │                                                │
                              │                                                ▼
                              │                                       ┌──────────────────┐
                              │                                       │  保存 results/    │
                              │                                       │  JSON/CSV/Markdown│
                              │                                       └──────────────────┘
                              ▼
                    ┌──────────────────┐
                    │  scanners/ 包     │
                    │  17 个扫描器      │
                    │  (Gist/GitLab/    │
                    │   HuggingFace...) │
                    └──────────────────┘
```

**单平台 DeepSeek 路径**（`run.py deepseek` → `scanner_engine.py`）：
1. `build_active_queries()` 合并静态查询 + 动态滚动时间窗口，得到 228 条活跃查询。
2. `ScannerEngine.run()` 逐条调用 `_gh_search()` 走 GitHub Code Search，从 `text_matches` 提取候选 key。
3. 累计一批后调用 `_verify_dict()` 验证有效性并查余额。
4. `_save_final()` / `save_results()` 落盘到 `results/`。

**多平台路径**（`run.py multi` → `multi_provider_scan.py`）：
1. 对每个 provider 用其 `key_patterns` 在 GitHub 搜索候选。
2. `UnifiedKeyMatcher` 用各平台正则匹配文本里的 key。
3. `UnifiedKeyVerifier` 并发（`ThreadPoolExecutor`）验证 + 查余额。

**单数据源路径**（`run.py source` → `scanner_engine.run_multi_source`）：
- 通过 `_get_scanner_registry()` 把数据源名映射到对应 Scanner 类，异步执行 `search()`，再统一验证。

---

## 二、核心模块职责

| 模块 | 职责 | 关键导出 |
|------|------|----------|
| `run.py` | 统一 CLI 入口，解析参数、装配引擎、输出统计 | `cmd_deepseek` / `cmd_multi` / `cmd_source` |
| `scanner_engine.py` | DeepSeek 单平台扫描引擎。维护查询库、执行 GitHub 搜索、text_matches 提取、验证、保存 | `ScannerEngine`、`BUILTIN_QUERIES`、`generate_rolling_time_queries`、`build_active_queries` |
| `providers.py` | 12 家 AI 平台配置 + 统一匹配/验证器 | `AIProvider`、`ALL_PROVIDERS`、`PROVIDER_MAP`、`UnifiedKeyMatcher`、`UnifiedKeyVerifier`、`DIRECT_PROVIDERS` |
| `multi_provider_scan.py` | 多平台扫描器。GitHub 搜索 → 并发验证 → 保存 | `MultiProviderScanner`、`DEFAULT_PROVIDERS` |
| `scanners/base.py` | 所有扫描器的抽象基类。提供 key 正则、坏 key 过滤、`_get_with_retry`（429 退避） | `BaseScanner`、`extract_keys`、`is_bad_key` |
| `scanners/*.py` | 17 个具体扫描器，各管一个数据源 | 见 `scanners/__init__.py` |

### ScannerEngine 核心方法

- `_gh_search(query, ...)` — 调用 GitHub Code Search API，**读取并跟踪 `X-RateLimit-Remaining` 头**，接近 10 次/分钟上限时自动等待到 reset。
- `_extract_keys_from_text_matches(items)` — 从 GitHub 返回的 `text_matches` 字段提取候选 key。
- `_verify_dict(keys_dict)` — 用 DeepSeek `/chat/completions` 验证有效性，`/user/balance` 查余额，折算成 USD。
- `run(queries)` — 主流水线：循环查询 → 提取 → 验证 → 增量保存。
- `run_multi_source(sources)` — 按数据源名调度 `scanners/` 里的扫描器。
- `_get_scanner_registry(...)` — 数据源名 → `(ScannerClass, default_query, kwargs)` 的映射表（见下文"添加新扫描器"）。

---

## 三、查询系统说明

查询分两层：

### 1. 静态查询 `BUILTIN_QUERIES`（239 条）
位于 `scanner_engine.py` 顶部，是手工调优的高产出 GitHub Code Search 语句，例如：
```
deepseek sk- filename:env
DEEPSEEK_API_KEY sk- filename:java
deepseek api_key sk- filename:py
```
覆盖多种文件类型（env / java / py / js / yml / json / kt / ts / go / php 等）和多种命名约定。

### 2. 动态滚动时间窗口 `generate_rolling_time_queries()`
根据**当前日期**动态生成，避免硬编码的过期日期查询失效：
- 取今天往前推 7 天、30 天，生成 `pushed:>{date}` 子句。
- 两个宽口径查询 + 按高产出文件类型细化的窗口查询，约 16 条。

### 3. 合并 `build_active_queries()`
```
活跃查询集 = 过滤掉过期硬编码日期的静态查询 + 动态滚动窗口查询
```
- 静态查询里凡含 `pushed:` 的硬编码日期全部移除（已被动态查询取代）。
- 仅保留 `pushed:>2025-01-01` 这种超宽窗口作为历史回溯兜底。
- 最终得到 **228 条**活跃查询。每次扫描自动指向最近 7/30 天的提交，逆转了"固定日期查询随时间产出下降"的问题。

> 这是 v2.0 关键改动之一（CHANGELOG 第 9 条）：把过期静态日期替换为动态滚动窗口。

---

## 四、限流处理机制

| 来源 | 限制 | 处理方式 |
|------|------|----------|
| GitHub Code Search REST API | 10 次/分钟（已认证）/ 更低（未认证） | `ScannerEngine._gh_search` 读取 `X-RateLimit-Remaining`，接近上限时 sleep 到 `X-RateLimit-Reset` |
| GitHub Events / 外部平台 | 429 / 503 | `BaseScanner._get_with_retry` 统一指数退避，优先读 `Retry-After` 头，最多重试 3 次 |
| 国内 AI 平台验证端点 | 代理 IP 易被封 | `providers.py` 中 `DIRECT_PROVIDERS` 列表里的平台**绕过代理直连**，避免代理 IP 池被风控 |

`_get_with_retry`（v2.0 新增，CHANGELOG 第 7 条）取代了过去各扫描器里散落的 `except Exception: pass` 静默吞 429 写法，所有扫描器统一走它做退避。

> 注意：v2.0 移除了所有扫描器的 `force_close`（CHANGELOG 第 5 条），改为复用上层传入的 `aiohttp.ClientSession` 连接池，减少 TCP 握手开销。因此**新扫描器不要在 `search()` 里自行 `await session.close()`**。

---

## 五、如何添加新扫描器

### 步骤 1：实现扫描器类
在 `scanners/` 下新建文件（如 `scanners/my_source.py`），继承 `BaseScanner`，必须实现两个成员：

```python
from .base import BaseScanner, extract_keys

class MySourceScanner(BaseScanner):
    """示例：新的数据源扫描器"""

    @property
    def source_name(self) -> str:
        return "my_source"          # 数据源唯一标识，与 run.py --source 参数对应

    async def search(self, query: str | None = None) -> list[dict]:
        """
        抓取数据源内容，提取 key，通过 self._add_result() 累积结果。
        必须返回 self.results（或同等结构的 list[dict]）。
        """
        # 推荐用 self._get_with_retry(session, url, ...) 走统一 429 退避
        # 用 self.extract_local(text) 提取并过滤坏 key
        async with aiohttp.ClientSession() as session:
            status, resp, exc = await self._get_with_retry(
                session, "https://api.example.com/items",
                params={"q": query or "deepseek"}, timeout_total=20,
            )
            if status != 200:
                return self.results
            text = await resp.text()
            for key in self.extract_local(text):
                self._add_result(key, url="...", repo="...", file_path="...")
        return self.results
```

约定：
- `source_name` 必须唯一，且与 `run.py source --source <name>` 一致。
- HTTP 请求一律走 `_get_with_retry`，不要自己写 `except: pass`。
- 不要 `await session.close()`，复用上层 session。
- key 提统一走 `extract_local` / `extract_keys`，自动套用坏 key 过滤（`your`/`xxx`/`example`/全数字等）。

### 步骤 2：注册到 `scanners/__init__.py`
```python
from .my_source import MySourceScanner
__all__ = [..., "MySourceScanner"]
```

### 步骤 3：注册到引擎
在 `scanner_engine.py` 的 `_get_scanner_registry()` 中加一行：
```python
"my_source": (MySourceScanner, "deepseek", {"proxy": self.proxy}),
```
三元组含义：`(ScannerClass, default_query_or_None, kwargs_dict)`。

### 步骤 4：暴露给 CLI（可选）
如希望 `python run.py source --source my_source` 可被发现，在 `run.py` 的 `--list-sources` 输出列表里加一项即可。

### 步骤 5：打包声明（如需打包 exe）
在 `DarkForestHunter.spec` 的 `hiddenimports` 列表里加 `'scanners.my_source'`，否则 PyInstaller 动态导入会漏。

---

## 六、如何添加新 AI 平台

所有平台配置集中在 `providers.py`。

### 1. 定义 `AIProvider`
参照现有定义（如 `DEEPSEEK`）新增一个：
```python
MYAI = AIProvider(
    id="myai",
    name="MyAI",
    name_cn="我的AI",
    base_url="https://api.myai.com",
    key_patterns=[
        r"sk-myai-[a-zA-Z0-9]{32,}",   # 该平台 key 的正则
    ],
    auth_type=AuthType.BEARER,
    verify_endpoint="/v1/chat/completions",  # 验证端点（POST 一个最小请求）
    verify_method="POST",
    balance_endpoint="",                      # 没有就留空（只验证有效性）
    enabled=True,
)
```

字段说明：
| 字段 | 必填 | 说明 |
|------|------|------|
| `id` | 是 | 平台唯一标识，用于 CLI `--providers` |
| `key_patterns` | 是 | 正则列表，`UnifiedKeyMatcher` 用它匹配文本 |
| `verify_endpoint` | 是 | 验证 key 是否有效的 API 路径 |
| `balance_endpoint` | 否 | 余额查询路径；留空则只能验证有效性，不能查余额 |
| `auth_type` | 是 | `BEARER` / `API_KEY` / `HEADER`，决定 Authorization 头形式 |

### 2. 注册到 `ALL_PROVIDERS`
```python
ALL_PROVIDERS: list[AIProvider] = [
    DEEPSEEK, KIMI, ..., MYAI,
]
```
`PROVIDER_MAP` 和 `ACTIVE_PROVIDERS` 会自动生成。

### 3. 国内平台走直连（重要）
如果新平台是国内厂商且直连可达，把它的 `id` 加入 `DIRECT_PROVIDERS` 列表，验证时会**绕过代理直连**，避免代理 IP 被风控封锁（v2.0 修复项，CHANGELOG 第 11 条）。海外平台默认走代理。

### 4. 想要余额查询
- 填好 `balance_endpoint`。
- 在 `UnifiedKeyVerifier` 的余额解析逻辑里加上该平台的响应字段解析（不同平台返回结构不同）。
- 目前支持余额查询的平台：`deepseek` / `zhipu` / `qwen` / `minimax`。

---

## 七、打包说明（PyInstaller）

项目根目录提供 `DarkForestHunter.spec`，可打包成单文件便携 exe：

```bash
# 安装 pyinstaller
pip install pyinstaller

# 打包
pyinstaller DarkForestHunter.spec

# 产物
dist/DarkForestHunter.exe
```

打包后的 exe 用法与 `run.py` 完全一致：
```
DarkForestHunter.exe deepseek --proxy http://127.0.0.1:7897
DarkForestHunter.exe multi --providers deepseek kimi
DarkForestHunter.exe source --source huggingface
```

注意事项：
- `spec` 的 `hiddenimports` 已显式列出所有扫描器模块（PyInstaller 无法静态发现动态导入）。**新增扫描器后必须把模块名加进 `hiddenimports`**，否则打包后运行会报 `ModuleNotFoundError`。
- 排除了 `tkinter`/`unittest`/`pydoc` 等不需要的模块以减小体积。
- 启用了 UPX 压缩（`upx=True`），需要 UPX 在 PATH 中；未安装时会自动跳过压缩。

---

## 八、调试小技巧

- **只跑单数据源验证新扫描器**：`python run.py source --source my_source --max-duration 300`，限定 5 分钟快速看效果。
- **查看可用平台**：`python run.py multi --list-providers`。
- **查看可用数据源**：`python run.py --list-sources`。
- **验证逻辑调试**：直接 import `_verify_dict` 或 `UnifiedKeyVerifier.verify_keys()`，传入 `{key: info}` 字典，避免每次都跑全量 GitHub 搜索。
- **限流观察**：`_gh_search` 会在日志打印 `X-RateLimit-Remaining`，接近 0 时自动等待 reset。
- **结果去重**：`BaseScanner` 通过 `source+key+url` 的 MD5 去重，无需手动处理。
