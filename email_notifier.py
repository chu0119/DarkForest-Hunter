"""
邮件通知模块 — 高价值 key 发现时发送告警邮件。
通过 config.ini [SMTP] 配置，支持 SSL (465) 和 STARTTLS (587)。

防止刷屏：同一 key 在去重窗口内（默认 1 小时）只发送一次，去重记录
持久化到 SQLite，重启后依然有效（避免每次启动对历史高价值 key 重验
时重复发送，也避免同一 key 在窗口内被反复验证反复提醒）。
"""

import hashlib
import html
import logging
import os
import smtplib
import sqlite3
import threading
import time
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

_logger = logging.getLogger("darkforest.email")

# 去重窗口默认：1 小时
DEFAULT_DEDUP_SECONDS = 3600
# 去重库默认路径
DEFAULT_DEDUP_DB = os.path.join("results", "email_sent.db")

# 数据来源的规范名称映射（内部 source 值 → 展示名）
SOURCE_LABELS = {
    "github_search": "GitHub 代码搜索",
    "github": "GitHub",
    "gist": "GitHub Gist",
    "issues": "GitHub Issues",
    "commits": "GitHub 提交",
    "github_commits": "GitHub 提交",
    "github_events": "GitHub 实时推送",
    "gitlab": "GitLab",
    "huggingface": "Hugging Face",
    "hf": "Hugging Face",
    "npm": "npm 仓库",
    "docker": "Docker Hub",
    "history": "历史重验",
}


def source_label(source: str) -> str:
    """把内部 source 值转成规范展示名；未知值原样返回。"""
    if not source:
        return "未知来源"
    return SOURCE_LABELS.get(source, source)


class EmailNotifier:
    """异步邮件发送器（独立线程，不阻塞验证流程）。

    带同 key 去重：同一 key 在 dedup_window_seconds 内只发一封，
    避免每次启动重验历史高价值 key 时刷屏。
    """

    def __init__(self, smtp_config: dict, dedup_window_seconds: int = DEFAULT_DEDUP_SECONDS,
                 dedup_db_path: str = DEFAULT_DEDUP_DB):
        self._server = smtp_config.get("server", "")
        self._port = smtp_config.get("port", 465)
        self._user = smtp_config.get("user", "")
        self._password = smtp_config.get("password", "")
        self._from_addr = smtp_config.get("from_addr", "") or self._user
        self._sender_name = smtp_config.get("sender_name", "DarkForest Hunter")
        self._recipients = smtp_config.get("recipients", [])
        # 完整 key 开关（v2.5.3 安全审查修复）：读 smtp_config["include_full_key"]。
        # 缺省 True 保持既有默认行为（本工具用途：把完整 key 推给运营者；
        # 单元测试以无该键的 dict 构造，语义不变）。
        # 此前该开关是死的——config.ini 显式设 false 也照样发完整 key；
        # 现在 watch 路径（config_loader 恒带此键）的配置值真正生效：
        # false 时纯文本与 HTML 均只渲染 key 预览。
        # 显式解析字符串形态(bool("false") is True 的陷阱——本文件历史上
        # 刚出过一次死开关,不留给下一个重构踩):接受 config_loader 同款
        # 词表;非字符串按 bool 直通。
        _fk = smtp_config.get("include_full_key", True)
        if isinstance(_fk, str):
            self._include_full_key = _fk.strip().lower() in ("true", "1", "yes", "on")
        else:
            self._include_full_key = bool(_fk)
        # 同 key 去重窗口（秒）。窗口内不重复发，避免启动重验刷屏。
        self._dedup_window = max(0, int(dedup_window_seconds))
        self._dedup_db_path = dedup_db_path
        self._dedup_conn = None
        self._dedup_lock = threading.Lock()
        self._init_dedup_db()
        self._lock = threading.Lock()
        self._sent_count = 0
        self._fail_count = 0
        # check→mark 原子化:同 key 并发 send_alert 间隙双发的防线
        self._send_lock = threading.Lock()
        # 本进程内最近发送时间缓存（加速去重判断）
        self._recent: dict[str, float] = {}
        self._recent_lock = threading.Lock()

    def _init_dedup_db(self):
        """初始化去重库：建表。失败则退化为仅内存去重。"""
        try:
            if self._dedup_db_path:
                parent = os.path.dirname(os.path.abspath(self._dedup_db_path))
                if parent:
                    os.makedirs(parent, exist_ok=True)
                conn = sqlite3.connect(self._dedup_db_path, check_same_thread=False)
                conn.execute("""CREATE TABLE IF NOT EXISTS email_sent (
                    key_hash TEXT PRIMARY KEY,
                    key TEXT,
                    sent_at REAL
                )""")
                conn.commit()
                self._dedup_conn = conn
        except Exception as e:
            _logger.warning("邮件去重库初始化失败（退化为内存去重）: %s", e)
            self._dedup_conn = None

    def _is_recently_sent(self, key: str) -> bool:
        """True 表示该 key 在窗口内已发过，应跳过。"""
        if self._dedup_window <= 0:
            return False
        kh = hashlib.sha256(key.encode()).hexdigest()
        now = time.time()
        # 1) 内存缓存快速判断
        with self._recent_lock:
            ts = self._recent.get(kh)
            if ts is not None and now - ts < self._dedup_window:
                return True
        # 2) 查 SQLite（跨进程/重启持久）
        with self._dedup_lock:
            try:
                if self._dedup_conn is None:
                    return False
                row = self._dedup_conn.execute(
                    "SELECT sent_at FROM email_sent WHERE key_hash=?", (kh,)).fetchone()
                if row and row[0] and now - float(row[0]) < self._dedup_window:
                    return True
            except Exception:
                pass
        return False

    def _mark_sent(self, key: str):
        """记录该 key 发送时间（进程内 + SQLite）。"""
        kh = hashlib.sha256(key.encode()).hexdigest()
        now = time.time()
        with self._recent_lock:
            self._recent[kh] = now
        with self._dedup_lock:
            try:
                if self._dedup_conn is not None:
                    self._dedup_conn.execute(
                        "INSERT OR REPLACE INTO email_sent(key_hash, key, sent_at) VALUES(?,?,?)",
                        (kh, key, now))
                    self._dedup_conn.commit()
            except Exception:
                pass

    def _unmark_sent(self, key: str):
        """清除 dedup 占坑(v2.4.9):发送失败时回滚,允许去重窗口内重发。"""
        kh = hashlib.sha256(key.encode()).hexdigest()
        with self._recent_lock:
            self._recent.pop(kh, None)
        with self._dedup_lock:
            try:
                if self._dedup_conn is not None:
                    self._dedup_conn.execute(
                        "DELETE FROM email_sent WHERE key_hash=?", (kh,))
                    self._dedup_conn.commit()
            except Exception:
                pass

    @property
    def enabled(self) -> bool:
        return bool(self._server and self._user and self._password and self._recipients)

    @property
    def include_full_key(self) -> bool:
        return self._include_full_key

    def send_alert(self, key: str, key_preview: str, provider: str, balance: float,
                   currency: str, source: str, repos: list = None):
        """发送高价值 key 告警邮件（异步，不阻塞调用方）。

        默认发完整 key（config email_include_full_key=false 时发预览）。
        同一 key 在去重窗口内只发一次（防刷屏）。
        v2.4.9: 发送失败时回滚 dedup 占坑——此前先占坑后异步发送,SMTP 抖动
        会让该 key 整个去重窗口(默认 1h)的告警被吞。
        """
        if not self.enabled:
            return
        # 去重：窗口内已发过则跳过。check→mark 在 _send_lock 内原子完成,
        # 否则同 key 两个并发调用(重验路径 vs 扫描路径)会在间隙里双双通过
        with self._send_lock:
            if self._is_recently_sent(key):
                return
            self._mark_sent(key)  # 先占坑，避免并发重复
        t = threading.Thread(target=self._do_send_with_rollback,
                             args=(key, key_preview, provider, balance, currency, source, repos),
                             daemon=True)
        t.start()

    def _do_send_with_rollback(self, key, key_preview, provider, balance,
                               currency, source, repos):
        """发送失败时回滚 dedup 占坑，允许去重窗口内重发。

        v2.5.4: 用 _do_send 的**返回值**判定成败——旧实现靠全局 fail 计数
        差值,两个 key 的发送线程并发时,另一线程的失败会把本线程的成功
        误判为失败,回滚占坑 → 去重窗口内同一 key 重复发信。
        """
        if not self._do_send(key, key_preview, provider, balance,
                             currency, source, repos):
            self._unmark_sent(key)

    def send_summary(self, items: list[dict]):
        """把多条高价值 key 合并成**一封**汇总邮件（异步）。

        items: 各含 key/key_preview/provider/balance_cny/source/repos。
        用于启动重验历史高价值 key 时，避免逐条发信刷屏。
        """
        if not self.enabled or not items:
            return
        t = threading.Thread(target=self._do_send_summary, args=(list(items),), daemon=True)
        t.start()

    def _do_send_summary(self, items: list[dict]):
        try:
            n = len(items)
            total = sum(float(r.get("balance_cny") or 0) for r in items)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            subject = f"[DarkForest] 🔑 高价值 Key 盘点 | {n} 个 | 合计 {total:.2f} CNY"

            # 纯文本
            lines = []
            lines.append("🔑 高价值 Key 汇总  |  DarkForest Hunter")
            lines.append("=" * 48)
            lines.append(f"  发现时间 : {now}")
            lines.append(f"  key 数量 : {n}")
            lines.append(f"  合计余额 : CNY {total:.2f}")
            lines.append("=" * 48)
            for i, r in enumerate(items, 1):
                bal = r.get("balance_cny", 0)
                prov = r.get("provider", "unknown")
                src = r.get("source", "")
                repo = (r.get("repos") or [{}])[0].get("repo", "") if r.get("repos") else ""
                lines.append("")
                lines.append(f"[{i}] {prov} | {bal:.2f} CNY | {source_label(src)}")
                lines.append(f"    Key: {r.get('key', '')}")
                if repo:
                    lines.append(f"    Repo: {repo}")
                if r.get("key_preview"):
                    lines.append(f"    PV:  {r.get('key_preview')}")
            lines.append("")
            lines.append("=" * 48)
            lines.append("DarkForest Hunter 自动发送 · 完整 key 以本地 DB 为准")
            body = "\n".join(lines)

            # HTML：表格列出所有 key
            rows = []
            for i, r in enumerate(items, 1):
                bal = r.get("balance_cny", 0)
                prov = r.get("provider", "unknown")
                src = r.get("source", "")
                repo = (r.get("repos") or [{}])[0].get("repo", "") if r.get("repos") else ""
                file = (r.get("repos") or [{}])[0].get("file", "") if r.get("repos") else ""
                url = (r.get("repos") or [{}])[0].get("url", "") if r.get("repos") else ""
                # v2.5.3 安全审查: repo/file/url 来自 GitHub 扫描结果(攻击者可通过
                # 恶意文件名/仓库名注入 HTML)——href 仅接受 http(s),全部转义。
                if url and url.startswith(("http://", "https://")):
                    repo_disp = (f"<a href='{html.escape(url, quote=True)}' "
                                 f"style='color:#00e0a8;text-decoration:none;'>"
                                 f"{html.escape(repo)}</a>")
                else:
                    repo_disp = html.escape(repo) or "-"
                key_full = r.get("key", "")
                # 完整 key 开关:关闭时渲染预览(此前开关为死配置)
                disp_key = (key_full if self._include_full_key
                            else (r.get("key_preview")
                                  or (key_full[:10] + "..." + key_full[-4:]
                                      if len(key_full) >= 14 else "***")))
                rows.append(f"""
<tr style="border-bottom:1px solid #2a2a3e;">
  <td style="padding:12px 14px;color:#8b93a7;"><b>{i}</b></td>
  <td style="padding:12px 14px;color:#e8ecf4;font-weight:600;">{html.escape(str(prov))}</td>
  <td style="padding:12px 14px;color:#00e0a8;font-weight:700;">{bal:.2f} CNY</td>
  <td style="padding:12px 14px;color:#5b6478;">{html.escape(source_label(src))}</td>
</tr>
<tr style="border-bottom:1px solid #24304d;">
  <td></td>
  <td colspan="3" style="padding:2px 14px 12px;font-family:'SF Mono',Consolas,monospace;color:#fff;font-size:11.5px;word-break:break-all;">{html.escape(disp_key)}</td>
</tr>
<tr style="border-bottom:1px solid #1c2438;">
  <td></td>
  <td colspan="3" style="padding:2px 14px 12px;color:#8b93a7;font-size:11.5px;">📦 {repo_disp} / {html.escape(file)}</td>
</tr>""")
            html_body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#0b0d17;font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;">
<div style="max-width:640px;margin:0 auto;padding:28px 16px;">
  <div style="text-align:center;padding:30px 24px;background:linear-gradient(135deg,#131a2e 0%,#0d1020 100%);border-radius:14px 14px 0 0;border:1px solid #1f2a44;border-bottom:none;">
    <div style="font-size:36px;line-height:1;">🗂️</div>
    <div style="color:#00e0a8;font-size:19px;font-weight:700;margin-top:8px;">高价值 Key 汇总</div>
    <div style="color:#5b6478;font-size:12px;margin-top:5px;">{now} · 共 {n} 个 · 合计 {total:.2f} CNY</div>
  </div>
  <div style="background:#0f1422;border:1px solid #1f2a44;border-top:none;border-radius:0 0 14px 14px;padding:10px 16px 14px;">
    <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:12.5px;">
      <tr style="color:#5b6478;text-align:left;">
        <th style="padding:10px 14px;">#</th>
        <th style="padding:10px 14px;">平台</th>
        <th style="padding:10px 14px;">余额</th>
        <th style="padding:10px 14px;">来源</th>
      </tr>
      {''.join(rows)}
    </table>
  </div>
  <div style="text-align:center;color:#3d4560;font-size:11px;padding-top:12px;">DarkForest Hunter · 自动发送 · 完整 key 以本地 DB 为准</div>
</div>
</body></html>
"""

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"{Header(self._sender_name, 'utf-8').encode()} <{self._user}>"
            msg["To"] = ", ".join(self._recipients)
            msg.attach(MIMEText(body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            if self._port == 465:
                with smtplib.SMTP_SSL(self._server, self._port, timeout=15) as smtp:
                    smtp.login(self._user, self._password)
                    smtp.sendmail(self._from_addr, self._recipients, msg.as_string())
            else:
                with smtplib.SMTP(self._server, self._port, timeout=15) as smtp:
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.login(self._user, self._password)
                    smtp.sendmail(self._from_addr, self._recipients, msg.as_string())

            with self._lock:
                self._sent_count += 1
            _logger.info("汇总邮件已发送: %d 个高价值 key, 合计 %.2f", n, total)
        except Exception as e:
            with self._lock:
                self._fail_count += 1
            _logger.warning("汇总邮件发送失败: %s", e)


    def _do_send(self, key: str, key_preview: str, provider: str, balance: float,
                 currency: str, source: str, repos: list = None):
        try:
            subject = f"[DarkForest] 🔑 {provider} 高价值 Key | {currency} {balance:.2f}"

            # 构建邮件正文
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            repo_rows = ""
            repo_txt = ""
            if repos:
                for r in repos[:5]:
                    repo = r.get("repo", "?")
                    file = r.get("file", "")
                    url = r.get("url", "")
                    repo_txt += f"  • {repo} / {file}\n"
                    # v2.5.3 安全审查: 同 summary——href 仅 http(s),全部转义
                    if url and url.startswith(("http://", "https://")):
                        repo_disp = (f"<a href='{html.escape(url, quote=True)}' "
                                     f"style='color:#00e0a8;text-decoration:none;'>"
                                     f"{html.escape(repo)}</a>")
                    else:
                        repo_disp = html.escape(str(repo))
                    repo_rows += (
                        f"<tr><td style='padding:10px 16px;border-bottom:1px solid #2a2a3e;font-size:12.5px;'>"
                        f"<span style='color:#8b93a7;'>📦 </span>"
                        f"<span style='color:#e8ecf4;font-weight:600;'>{repo_disp}</span>"
                        f"<span style='color:#8b93a7;'> / </span>"
                        f"<span style='color:#b7c2d8;'>{html.escape(str(file))}</span>"
                        f"</td></tr>")
            # 完整 key 开关:关闭时两段(纯文本+HTML)均只渲染预览
            disp_key = (key if self._include_full_key else key_preview) or "***"
            key_title = ("完整 Key（复制即用）" if self._include_full_key
                         else "Key 预览（include_full_key=false）")

            body = f"""🔑 高价值 Key 告警  |  DarkForest Hunter
{'=' * 48}
  发现时间 : {now}
  平台     : {provider}
  余额     : {currency} {balance:.2f}
  来源     : {source_label(source)}
{'=' * 48}

{key_title}
{'─' * 48}
{disp_key}
{'─' * 48}

关联仓库
{repo_txt if repo_txt else '  (无)'}
{'=' * 48}
DarkForest Hunter 自动发送
"""

            html_body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#0b0d17;font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;">
<div style="max-width:640px;margin:0 auto;padding:28px 16px;">
  <!-- Header -->
  <div style="text-align:center;padding:34px 24px 26px;background:linear-gradient(135deg,#131a2e 0%,#0d1020 100%);border-radius:14px 14px 0 0;border:1px solid #1f2a44;border-bottom:none;">
    <div style="font-size:40px;line-height:1;">🔑</div>
    <div style="color:#00e0a8;font-size:20px;font-weight:700;letter-spacing:.5px;margin-top:10px;">高价值 Key 告警</div>
    <div style="color:#5b6478;font-size:12.5px;margin-top:6px;">DarkForest Hunter · 自动泄露检测</div>
  </div>

  <!-- Body card -->
  <div style="background:#0f1422;border:1px solid #1f2a44;border-top:none;border-radius:0 0 14px 14px;padding:22px 24px 26px;">
    <!-- Meta grid -->
    <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:13px;">
      <tr>
        <td style="padding:9px 12px;color:#8b93a7;width:42%;">平台</td>
        <td style="padding:9px 12px;color:#e8ecf4;font-weight:600;">{html.escape(str(provider))}</td>
      </tr>
      <tr>
        <td style="padding:9px 12px;color:#8b93a7;">发现时间</td>
        <td style="padding:9px 12px;color:#e8ecf4;">{now}</td>
      </tr>
      <tr>
        <td style="padding:9px 12px;color:#8b93a7;">来源</td>
        <td style="padding:9px 12px;color:#e8ecf4;">{html.escape(source_label(source))}</td>
      </tr>
      <tr>
        <td style="padding:9px 12px;color:#8b93a7;">余额</td>
        <td style="padding:9px 12px;"><span style="color:#00e0a8;font-size:18px;font-weight:700;">{currency} {balance:.2f}</span></td>
      </tr>
    </table>

    <!-- Key block -->
    <div style="margin-top:18px;background:#0a0d18;border:1px solid #24304d;border-radius:10px;padding:14px 16px;">
      <div style="color:#00e0a8;font-size:11.5px;font-weight:700;letter-spacing:1px;margin-bottom:8px;">{key_title}</div>
      <div style="font-family:'SF Mono',Consolas,'Courier New',monospace;color:#fff;font-size:13.5px;line-height:1.6;word-break:break-all;user-select:all;">{html.escape(disp_key)}</div>
    </div>

    <!-- Repos -->
    <div style="margin-top:20px;">
      <div style="color:#8b93a7;font-size:11.5px;font-weight:700;letter-spacing:1px;margin-bottom:6px;">关联仓库</div>
      <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:12.5px;">
        {repo_rows or "<tr><td style='padding:8px 4px;color:#5b6478;'>（无）</td></tr>"}
      </table>
    </div>
  </div>

  <div style="text-align:center;color:#3d4560;font-size:11px;padding-top:14px;">
    DarkForest Hunter · 自动发送 · 请勿外泄
  </div>
</div>
</body></html>
"""

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"{Header(self._sender_name, 'utf-8').encode()} <{self._user}>"
            msg["To"] = ", ".join(self._recipients)
            msg.attach(MIMEText(body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            # SSL (465) 或 STARTTLS (587)
            if self._port == 465:
                with smtplib.SMTP_SSL(self._server, self._port, timeout=15) as smtp:
                    smtp.login(self._user, self._password)
                    smtp.sendmail(self._from_addr, self._recipients, msg.as_string())
            else:
                with smtplib.SMTP(self._server, self._port, timeout=15) as smtp:
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.login(self._user, self._password)
                    smtp.sendmail(self._from_addr, self._recipients, msg.as_string())

            with self._lock:
                self._sent_count += 1
            _logger.info("邮件已发送: %s %s %s %.2f", provider, currency, balance, key_preview)
            return True

        except Exception as e:
            with self._lock:
                self._fail_count += 1
            _logger.warning("邮件发送失败: %s", e)
            return False

    def stats(self) -> dict:
        with self._lock:
            return {"sent": self._sent_count, "failed": self._fail_count}
