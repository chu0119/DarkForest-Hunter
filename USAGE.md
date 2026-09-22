# USAGE.md — DarkForest Hunter 使用手册

> 项目简介与设计理念请参阅 [README_CN.md](README_CN.md)；架构与二次开发请参阅 [DEVELOPER.md](DEVELOPER.md)；
> watch 模式的深度手册（调参/排障/代理）请参阅 [docs/watch_manual.txt](docs/watch_manual.txt)。

---

## 一、环境准备

### 1. Python 环境
要求 **Python 3.10+**（`python --version` 检查）。

### 2. 安装依赖
```bash
pip install -r requirements.txt
```
仅依赖 aiohttp / requests / rich 三件套。

### 3. 配置
```bash
copy config.ini.example config.ini
```
必填项只有一个：`[github]` 下的 **Token**（多个逗号分隔，每个 token 独立 10 次/分钟配额，是吞吐的最大杠杆）。

可选配置：
- `[proxy] url` — HTTP 代理（留空则自动探测本地 Clash 7897 / V2Ray 10809 等端口）
- `[SMTP]` — 高价值 key 邮件告警（SSL 465 或 STARTTLS 587）
- `[watch]` / `[verification]` — 全部参数都有合理默认，一般不用动

---

## 二、启动（唯一扫描入口）

```bash
# 挂机跑（TUI 面板，Ctrl+C 退出）
python -u run.py watch

# 无头/后台运行（日志写 results/watch_session_*.log）
python -u run.py watch --no-tui

# 裸启动等价于 watch
python run.py
```

一条命令包含全部能力：
- **11 个数据源**（默认启用 github_search / github_commits / github_events / gitlab / npm，其余 `--sources` 添加）
- **35 个 AI 平台**验证（OpenAI/Gemini/xAI/混元/千帆/魔搭/NVIDIA/LongCat + 国内主流 + 国际网关 + Coding Plan）
- **验证策略**：先只读 GET models/balance → 通过后一次 `max_tokens=1` chat 探测确认实际可用（`--no-allow-chat-probe` 可关）；`/models` 公开的平台（魔搭/NVIDIA/LongCat）兜底探测（`--no-probe-unclear` 可关）
- **新鲜度优先**：events 实时流 + commits 周窗口 + fresh-repos 精扫 + 查询轮转的新鲜簇
- **收益自学习**：查询按 valid/高价值转化排序，低产查询自动降权/熔断
- **重验**：每日 1500 预算分层轮询（高价值 6h / 普通 12h / 0 余额 7 天）
- **告警**：高价值发现邮件 + 余额缩水预警

### 常用变体
| 场景 | 命令 |
|------|------|
| 日常挂机 | `python -u run.py watch` |
| 看全部有效 key（含 ¥0） | `python run.py watch --min-balance 0` |
| 榨干单 token | `python run.py watch --github-pages 2` |
| 验证跟不上 | `python run.py watch --verify-workers 6` |
| 排障 | `python run.py watch --verbose --log-file results/diag.log` |
| 只跑一轮（测试） | `python run.py watch --once` |
| 只扫指定源 | `python run.py watch --sources github_search npm` |
| 查看可用数据源 | `python run.py --list-sources` |

---

## 三、账本工具

```bash
# 产出画像：总量 / 按源 / 按平台 / 按查询 Top10 / 验证状态
#   + 近 24h 验证漏斗 / 平台错误率 TOP / unknown 新格式样本
python run.py metrics
python run.py metrics --check-github    # 附带探测 GitHub token 健康度

# 账本维护：脱敏 invalid 明文 / 清理过期记录 / VACUUM 回收空间
python run.py maintenance --db results/darkforest.db --vacuum
python run.py maintenance --prune-invalid-days 90
```

---

## 四、输出文件（results/ 下，已 gitignore）

| 文件 | 内容 |
|------|------|
| `darkforest.db` | SQLite 账本（全部候选/验证状态/余额历史，跨运行去重种子） |
| `watch_state.json` (+`.bak`) | 最新有效 key 视图（原子写，崩溃自恢复） |
| `watch_high_value.csv` | 高价值 key 台账（完整 key + 平台 + 余额，按余额降序） |
| `balance_changes.csv` | 余额变化流水（充值/缩水/重激活） |
| `query_stats.json` | 查询收益学习（跨重启续用） |
| `queries_generated.txt` | 扩展查询池（`python query_enumerator.py` 重新生成） |
| `watch_session_*.log` | 每次会话日志 |
| `email_sent.db` | 邮件去重 |

---

## 五、常见问题

**Q: 扫到的"余额"是真的吗？**
余额口径已按官方文档校准（如 Kimi 取 `cash_balance` 现金字段，代金券不计）；chat 探测默认开启进一步确认"实际可调用"。

**Q: 为什么很多 key 验证是 invalid？**
泄露 key 的死亡率本来就高（被主人轮换/平台风控）。账本会持续挤水分——假有效会被重验翻转。

**Q: 想提高扫描速率？**
加 GitHub Token（`[github]` 逗号分隔）是唯一线性杠杆；pacing 已压在限额 95%。

**Q: 程序中断会丢数据吗？**
不会。验证结果实时落 SQLite，台账原子写 + .bak 备份，查询轮次持久化，重启自动续跑。

