# CHANGELOG — DarkForest Hunter

本项目版本变更记录。日期采用 YYYY-MM-DD 格式。

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
