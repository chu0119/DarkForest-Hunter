"""
智能代理解析器 — 直连优先 + 代理回退 + 结果缓存。

服务器部署时外网可直连，不需要代理；但国内访问部分 API（GitHub/HuggingFace）
可能仍需代理。SmartProxy 自动探测并缓存结果，避免每次请求都探测。
"""

import base64
import json
import logging
import os
import time
import urllib.parse

import requests

_logger = logging.getLogger("darkforest.proxy")

# 探测目标：只测根域名可达性，不消耗搜索配额
_CANARY_URLS = [
    "https://github.com",     # 根域名，非 API
    "https://huggingface.co",
]


class SmartProxy:
    """智能代理解析器：探测直连可用性，不可用时回退到配置代理。

    用法：
        sp = SmartProxy("http://127.0.0.1:7897")
        proxies = sp.get_proxies_dict()  # {"http": ..., "https": ...} 或 None
        proxy_str = sp.get_proxy()       # "http://..." 或 None
    """

    def __init__(self, config_proxy: str = None, test_interval: float = 120.0):
        self.config_proxy = config_proxy
        self._direct_ok: bool | None = None  # None=未测试, True=直连可用, False=需代理
        self._last_test: float = 0.0
        self._test_interval = test_interval  # 缓存有效期（秒）
        self._lock = __import__("threading").Lock()

    def _test_direct(self) -> bool:
        """探测直连可用性（缓存结果，避免频繁探测）。"""
        now = time.time()
        if self._direct_ok is not None and (now - self._last_test < self._test_interval):
            return self._direct_ok

        with self._lock:
            # 双重检查
            if self._direct_ok is not None and (now - self._last_test < self._test_interval):
                return self._direct_ok

            for url in _CANARY_URLS:
                try:
                    requests.get(url, timeout=3.0)
                    # 任一可达 = 直连可用
                    self._direct_ok = True
                    self._last_test = time.time()
                    _logger.info("直连可用（%s 探测成功）", url)
                    return True
                except Exception:
                    continue

            # 全部不可达 = 需要代理
            self._direct_ok = False
            self._last_test = time.time()
            _logger.info("直连不可达，将使用代理: %s", self.config_proxy or "无")
            return False

    def get_proxy(self) -> str | None:
        """返回代理 URL 字符串，或 None（直连）。"""
        if self._test_direct():
            return None  # 直连可用，不需要代理
        return self.config_proxy  # 直连不可用，使用配置代理

    def get_proxies_dict(self) -> dict | None:
        """返回 requests 兼容的 proxies 字典。"""
        proxy = self.get_proxy()
        if proxy:
            return {"http": proxy, "https": proxy}
        return None

    def is_direct(self) -> bool:
        """当前是否使用直连。"""
        return self._test_direct()

    def reset(self):
        """强制重新探测（下次调用 get_proxy 时触发）。"""
        with self._lock:
            self._direct_ok = None
            self._last_test = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
#  v2.4.7: 多代理 IP 级路由器 — 每个端点独立出口 IP、独立 pacing
# ═══════════════════════════════════════════════════════════════════════════════


def parse_subscription_links(subscription_url: str) -> list[dict]:
    """从订阅 URL 解析代理端点列表(支持 VMess/Trojan/Hysteria2/SS 直链和 base64 订阅)。

    返回: [{"name": "xjp-01", "protocol": "trojan", "host": "x.gfw.com",
            "port": 7768, "url": "trojan://..."}, ...]
    """
    endpoints = []
    # ── 订阅 URL 校验(仅允许 https/http 公网地址,阻断私网/环回) ──
    u = urllib.parse.urlparse(subscription_url)
    if u.scheme not in ("http", "https"):
        _logger.warning("订阅链接协议不允许: %s", u.scheme)
        return endpoints
    host = u.hostname or ""
    try:
        import ipaddress
        import socket as _socket
        for inf in _socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(inf[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast):
                _logger.warning("订阅域名 %s 解析到非公网地址(%s),已拒绝", host, ip)
                return endpoints
    except (OSError, ValueError) as e:
        _logger.warning("订阅域名解析失败(%s): %s", host, e)
        return endpoints

    # ── 拉订阅:代理优先,失败回退直连 ──
    # 订阅域名与被墙目标常同域,直连拉取失败曾让 per-IP 多代理特性静默回退。
    # 代理解析与 mihomo_manager.download_mihomo 同口径(env → config.ini → 本地端口)。
    proxy_candidates: list = []
    env_proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "")
    if not env_proxy:
        try:
            from config_loader import config as _cfg
            env_proxy = _cfg.proxy_url or ""
        except Exception:
            env_proxy = ""
    if env_proxy:
        proxy_candidates.append({"http": env_proxy, "https": env_proxy})
    for port in (7897, 10809, 7890):
        proxy_candidates.append({"http": f"http://127.0.0.1:{port}",
                                 "https": f"http://127.0.0.1:{port}"})
    proxy_candidates.append(None)  # 最后回退直连

    content = ""
    last_err = None
    for proxies in proxy_candidates:
        try:
            resp = requests.get(subscription_url, timeout=15, proxies=proxies)
            resp.raise_for_status()
            content = resp.text.strip()
            break
        except Exception as e:
            last_err = e
    if not content:
        _logger.warning("订阅链接拉取失败(代理+直连均试): %s", last_err)
        return endpoints

    # 尝试 base64 订阅体
    try:
        decoded = base64.b64decode(content + "==").decode("utf-8", errors="replace")
        if "://" in decoded:
            content = decoded
    except Exception:
        pass

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if line.startswith("vmess://"):
                info = _parse_vmess(line)
            elif line.startswith("trojan://"):
                info = _parse_trojan(line)
            elif line.startswith("hysteria2://") or line.startswith("hy2://"):
                info = _parse_hysteria2(line)
            elif line.startswith("ss://"):
                info = _parse_shadowsocks(line)
            elif line.startswith("http://") or line.startswith("https://"):
                parsed = urllib.parse.urlparse(line)
                info = {"name": parsed.hostname or "http", "protocol": "http",
                        "host": parsed.hostname, "port": parsed.port or 80, "url": line}
            else:
                continue
            if info:
                endpoints.append(info)
        except Exception as e:
            _logger.debug("解析代理链接失败: %s", e)
            continue

    _logger.info("从订阅解析出 %d 个代理端点: %s",
                 len(endpoints), [e.get("name", "?") for e in endpoints])
    return endpoints


def _parse_vmess(link: str) -> dict | None:
    payload = link[len("vmess://"):]
    try:
        data = json.loads(base64.b64decode(payload + "=="))
    except Exception:
        return None
    return {"name": data.get("ps", data.get("add", "vmess")),
            "protocol": "vmess", "host": data.get("add"),
            "port": int(data.get("port", 0)), "url": link}


def _parse_trojan(link: str) -> dict | None:
    parsed = urllib.parse.urlparse(link)
    if not parsed.hostname:
        return None
    name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else parsed.hostname
    return {"name": name, "protocol": "trojan", "host": parsed.hostname,
            "port": parsed.port or 443, "url": link}


def _parse_hysteria2(link: str) -> dict | None:
    parsed = urllib.parse.urlparse(link)
    if not parsed.hostname:
        return None
    name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else parsed.hostname
    return {"name": name, "protocol": "hysteria2", "host": parsed.hostname,
            "port": parsed.port or 443, "url": link}


def _parse_shadowsocks(link: str) -> dict | None:
    parsed = urllib.parse.urlparse(link)
    if not parsed.hostname:
        return None
    name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else parsed.hostname
    return {"name": name, "protocol": "ss", "host": parsed.hostname,
            "port": parsed.port or 443, "url": link}


class MultiProxyRouter:
    """多代理端点路由器 — 每个端点独立出口 IP、独立 pacing 状态。

    将 N 个 token 分散到 M 个代理 IP:
    - token[i] → proxies[i % M]
    - 每个代理 IP 有独立的 SmartProxy 实例和独立的 pacing key
    - 某个 IP 被 429 → 只影响该 IP 的 pacing,其他 IP 全速运行
    """

    def __init__(self, proxy_urls: list[str]):
        self._proxy_urls = [u for u in proxy_urls if u] if proxy_urls else []
        self._proxies = [SmartProxy(u) for u in self._proxy_urls]
        self._token_to_proxy: dict[str, int] = {}
        self._proxy_cooldowns: dict[int, float] = {}
        self._lock = __import__("threading").Lock()
        self._rr = 0  # 无 token 场景的轮转计数(v2.5.3)

    def get_any_proxy(self) -> dict | None:
        """无 token 场景(回退下载等)的轮转出口——把 raw.githubusercontent
        流量(30 文件/查询)打散到多 IP,而非全部压在单代理出口上。

        v2.5.3: 长跑实测 429×3 均为 IP 级,回退下载走单代理 SmartProxy
        是唯一未分摊的大流量源。轮转跳过冷却中端口。"""
        with self._lock:
            n = len(self._proxy_urls)
            if n == 0:
                return None
            now = time.time()
            for _ in range(n):
                idx = self._rr % n
                self._rr = (idx + 1) % n
                if self._proxy_cooldowns.get(idx, 0) <= now:
                    url = self._proxy_urls[idx]
                    return {"http": url, "https": url}
            # 全部冷却:仍返回下一个(总比单点好,冷却只是概率降权)
            idx = self._rr % n
            self._rr = (idx + 1) % n
            url = self._proxy_urls[idx]
            return {"http": url, "https": url}

    @property
    def num_proxies(self) -> int:
        return len(self._proxy_urls)

    def get_proxies_for_token(self, token: str) -> dict | None:
        """返回该 token 绑定的代理 proxies 字典。

        v2.4.9: 多代理模式下端点是必选项——SmartProxy 的"直连可达则返回
        None"是单代理语义,复用在这里会让所有 token 静默从本机 IP 出去,
        而 pacing 仍按代理 IP 分桶(出口 IP 与限速桶错位,正是滥用检测
        的触发条件)。本机直连探测失败时应直接返回 None 由调用方兜底。
        """
        if not self._proxy_urls:
            return None
        idx = self._assign_proxy_for_token(token)
        url = self._proxy_urls[idx]
        return {"http": url, "https": url}

    def get_pacing_key(self, token: str) -> str:
        """pacing key = 代理出口 IP:port(不同 IP → 独立 pacing 桶)。"""
        if not self._proxy_urls:
            return token[:20] if token else "__unauth__"
        idx = self._assign_proxy_for_token(token)
        url = self._proxy_urls[idx]
        parsed = urllib.parse.urlparse(url)
        return f"{parsed.hostname}:{parsed.port}" if parsed.hostname else f"proxy{idx}"

    def on_ip_rate_limit(self, token: str):
        """某 token 触发 429 → 仅冷却该 token 对应的代理 IP。"""
        with self._lock:
            idx = self._token_to_proxy.get(token[:20] if token else "__unauth__", 0)
            self._proxy_cooldowns[idx] = time.time() + 300
            _logger.warning("代理 IP %s 进入 5min 冷却(429)", self._proxy_urls[idx])

    def on_transport_failure(self, token: str, cooldown: float = 60.0):
        """传输层失败(TLS RST/连接重置)→ 短冷却当前端口并重绑 token 到其他端口。

        与 429 的 on_ip_rate_limit(5min) 区分:传输故障是出口节点质量问题
        (2026-09-22 实测:SSL EOF 随时间 escalate,105 条错误 23 查询 3 败
        被丢——同端口重试基本无效),60s 短冷却换端口即可恢复。
        防呆:没有其他健康端口可去时不冷却(否则全体冷却无路可走)。
        """
        with self._lock:
            n = len(self._proxy_urls)
            if n <= 1:
                return
            rk = token[:20] if token else "__unauth__"
            idx = self._token_to_proxy.get(rk)
            if idx is None:
                return
            now = time.time()
            if self._proxy_cooldowns.get(idx, 0) <= now:
                healthy_others = sum(
                    1 for i in range(n)
                    if i != idx and self._proxy_cooldowns.get(i, 0) <= now)
                if healthy_others == 0:
                    return  # 无路可去,不冷却
                self._proxy_cooldowns[idx] = now + cooldown
                _logger.info("传输故障:端口 %s 冷却 %.0fs,token 重绑其他节点",
                             self._proxy_urls[idx], cooldown)
            # pop 强制下次 _assign_proxy_for_token 重新负载均衡(跳过冷却口)
            self._token_to_proxy.pop(rk, None)

    def is_proxy_in_cooldown(self, token: str) -> bool:
        with self._lock:
            idx = self._token_to_proxy.get(token[:20] if token else "__unauth__", 0)
            return self._proxy_cooldowns.get(idx, 0) > time.time()

    def _assign_proxy_for_token(self, token: str) -> int:
        route_key = token[:20] if token else "__unauth__"
        with self._lock:
            current = self._token_to_proxy.get(route_key)
            if current is not None and self._proxy_cooldowns.get(current, 0) < time.time():
                return current

            n = len(self._proxy_urls)
            if n == 0:
                return 0
            usage = [0] * n
            for idx in self._token_to_proxy.values():
                if 0 <= idx < n:
                    usage[idx] += 1
            best, best_usage = 0, float("inf")
            for i in range(n):
                if self._proxy_cooldowns.get(i, 0) > time.time():
                    continue
                if usage[i] < best_usage:
                    best_usage, best = usage[i], i
            self._token_to_proxy[route_key] = best
            return best
