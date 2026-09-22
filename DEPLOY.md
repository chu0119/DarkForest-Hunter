# DarkForest Hunter — 部署说明

> 版本: v2.4.3 | 用途: 多平台 AI Key 泄露扫描与验证 | 语言: Python 3.10+

## 1. 包内容

```
run.py                    # 唯一扫描入口（watch；+ metrics/maintenance 工具）
scanner_engine.py         # 核心扫描引擎（GitHub Code Search + 35 平台验证）
watch_tui.py              # Watch 模式 TUI 仪表盘（安全监控面板）
watch_persistence.py      # Watch state / 历史 / CSV 台账读写
providers.py              # 35 个 AI 平台配置 + 统一 key 识别/验证
query_rotation.py         # 查询轮转器（持久化轮次，重启续跑）
query_enumerator.py       # 扩展查询池生成器（覆盖全平台盲区）
queries_optimized.txt     # 355 条数据驱动查询
scanners/                 # 11 个已调度数据源（GitHub 家族/npm/HF/GitLab 等）
store.py                  # SQLite 持久化（跨运行去重 + 历史）
proxy_resolver.py         # 智能代理（直连优先 + 代理回退 + 限流自动切路由）
config_loader.py          # 配置加载
config.ini.example        # ★ 配置模板（复制为 config.ini 使用）
email_notifier.py         # 高价值 key 邮件通知
scripts/redact_results.py # 结果脱敏
tests/                    # 440 个测试
README.md / README_CN.md / USAGE.md / DEVELOPER.md / CHANGELOG.md
requirements.txt          # 运行依赖
```

**注意**：本包不含 `config.ini`（敏感凭据已剔除）。使用前必须先复制模板并填入你的值。

## 2. 快速开始

### 2.1 安装依赖

```bash
pip install -r requirements.txt
# 可选开发依赖: pip install -r requirements-dev.txt
```

### 2.2 配置

```bash
cp config.ini.example config.ini
```

编辑 `config.ini`：

```ini
[proxy]
url = http://127.0.0.1:7897    # 代理（国内必填；留空=直连）

[github]
token = ghp_xxx,ghp_yyy        # ★ 必填。GitHub PAT，支持多个（逗号分隔）
                               #   每个 token 独立 10次/分钟 Code Search 配额
                               #   获取: https://github.com/settings/tokens
                               #   注意: 不同账号的 token 才能叠加配额

[watch]
interval = 300                  # 每轮间隔秒数
min_balance = 1.0               # 高价值 key 余额阈值（¥）
verify_interval = 0.5           # 验证请求间隔
verify_workers = 4              # 验证并发
concurrency = 15                # 扫描并发
include_github = false          # 是否纳入慢速 GitHub Code Search
```

其他可选 token（GitLab/HF/Docker）按需填写，不填自动使用保守匿名预算。

### 2.3 运行

```bash
# 方式一：Watch 模式（推荐，24/7 监控 + TUI 仪表盘）
python run.py watch

# 方式二：只看 GitHub Code Search
python run.py watch --sources github_search

# 方式三：无 TUI（服务器 / systemd / nohup 后台）
python run.py watch --no-tui

# 方式四：跑一轮就退出（配合 cron）
python run.py watch --once

# 测试
python -m pytest tests/ -q
```

## 3. 部署到服务器（systemd 示例）

### 3.1 上传

```bash
scp darkforest-hunter-v2.8.3-release.zip user@server:/opt/
ssh user@server
cd /opt && unzip darkforest-hunter-v2.8.3-release.zip -d darkforest
cd darkforest
cp config.ini.example config.ini && nano config.ini   # 填 token/代理
pip install -r requirements.txt
```

### 3.2 systemd 服务

`/etc/systemd/system/darkforest.service`:

```ini
[Unit]
Description=DarkForest Hunter Watch
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/darkforest
ExecStart=/usr/bin/python3 run.py watch --no-tui
Restart=always
RestartSec=30
# 日志轮换由 watch_tui 自动处理（50MB 上限，保留 5 个备份）

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now darkforest
journalctl -u darkforest -f    # 查看日志
```

> ⚠️ 若 SQLite 报 `readonly database`，检查 `results/` 目录权限：
> `chown -R <user>:<user> /opt/darkforest/results`

### 3.3 服务器直连可用性

服务器部署时 SmartProxy 会自动探测：
- 直连可用（如海外服务器）→ 自动直连，无需代理
- 直连不可用（如国内）→ 自动回退到 `config.ini [proxy] url`
- 遇到 429/503 次级限流 → 自动切换 直连↔代理 路由（v2.8.3 新增）

## 4. 运行行为（v2.8.3）

| 特性 | 说明 |
|------|------|
| 平台覆盖 | 35 个 AI 平台（DeepSeek/Kimi/Zhipu/Qwen/Claude/OpenAI/Gemini/xAI/混元/千帆/魔搭/NVIDIA/LongCat/OpenRouter 等） |
| Key 识别 | 20+ 种前缀/格式：`sk-` 家族 / `sk-proj-` / `AIza` / `AQ.Ab` / `xai-` / `nvapi-` / `ms-`UUID / `bce-v3/` / `eyJ` / `gsk_` / `hf_` 等 |
| 验证方式 | 先只读 GET /models + 余额查询，通过后一次 `max_tokens=1` 最小探测确认实际可用（`--no-allow-chat-probe` 可关）；`/models` 公开的平台兜底探测（`--no-probe-unclear` 可关） |
| 查询轮转 | 355 条查询分 4 桶轮换 + 4631 条扩展池试水，持久化轮次重启续跑 |
| **Fresh Repo** | **v2.8.4 新增**：Repo Search(pushed:>DATE) 抓最近推送项目 → repo: 限定符精扫（窗口逐日推进，tokens[0] 独立配额） |
| Pacing | 每 token 独立 9s 基线间隔，429 后自适应降档 12s |
| 限流保护 | 次级限流 5min self-quiet；IP 级限流自动切路由（直连↔代理） |
| 查询枯竭 | 渐进恢复 top-15，避免瞬间打满配额 |
| 饱和检测 | 连续 6 轮 0key 自动跳过 3 轮 |
| 持久化 | SQLite（全量）+ watch_state.json（最新视图）+ CSV（高价值账本） |
| 重启续跑 | 高价值 key 回显 + 重验（瞬态错误不误删，v2.8.3 修复） |
| 日志 | 50MB 上限自动轮换，保留 5 份 |
| 看门狗 | 5 分钟无活动自动重启进程（从 rotator_state 续跑） |

## 5. 输出文件（results/ 目录）

| 文件 | 内容 |
|------|------|
| `darkforest.db` | SQLite 全量账本（key/余额/状态/时间戳） |
| `watch_state.json` | 最新验证结果视图（重启回显用） |
| `watch_high_value.csv` | 高价值 key 账本（余额 > 阈值） |
| `watch_session_*.log` | 每次运行日志 |
| `rotator_state.json` | 查询轮次持久化 |

## 6. 安全须知

- `config.ini` 含真实凭据，**不要**提交到 git / 发送给他人
- `results/` 含扫描到的真实 key，**不要**公开分享

## 7. 常见问题

**Q: GitHub 一直 429/503?**
A: 检查是否多个 token 同属一个账号（同账号配额共享）；跨账号 token 才能叠加。另确认代理已配置——本地多 token 并发容易触发 IP 级限流，v2.8.3 会自动切路由缓解。

**Q: 高价值 Key 表格余额不更新？**
A: v2.8.3 已修复：TUI 主循环定期拉取验证结果，reverify 2-3s 即刷新；瞬态错误不再误删 key。

**Q: 怎么加新平台？**
A: 在 `providers.py` 添加 Provider 配置（key 正则 + 验证端点 + 查询词），参考现有条目。

**Q: 测试怎么跑？**
A: `python -m pytest tests/ -q`（268 个测试，无需网络）。
