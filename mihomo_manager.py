"""
mihomo 内嵌管理器 — 自动下载、配置、启动 mihomo,暴露多个本地 HTTP 代理端口。

每个上游代理(VMess/Trojan/Hysteria2)对应一个本地 HTTP 端口,实现 per-IP pacing。
"""

import atexit
import json
import logging
import os
import platform
import subprocess
import time
import urllib.request

import requests

from proxy_resolver import parse_subscription_links

_logger = logging.getLogger("darkforest.mihomo")

# mihomo GitHub release 下载模板
_GITHUB_RELEASE_URL = (
    "https://github.com/MetaCubeX/mihomo/releases/download/v1.18.0/"
    "mihomo-windows-amd64-compatible-v1.18.0.zip"
)
_BINARY_NAME = "mihomo.exe" if platform.system() == "Windows" else "mihomo"


def get_binary_path() -> str:
    """返回 mihomo 二进制路径(项目 bin/ 目录)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", _BINARY_NAME)


def is_installed() -> bool:
    """检查 mihomo 是否已下载."""
    return os.path.isfile(get_binary_path())


def download_mihomo(force: bool = False) -> str | None:
    """下载 mihomo 二进制到项目 bin/ 目录。

    返回二进制路径,失败返回 None。
    自动检测并使用系统代理(Clash/V2Ray)。
    """
    bin_path = get_binary_path()
    if is_installed() and not force:
        return bin_path

    _logger.info("正在下载 mihomo...")
    try:
        # 检测系统代理
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
        if not proxy:
            # 尝试从 config.ini 读
            try:
                from config_loader import config
                proxy = config.proxy_url or ""
            except Exception:
                pass
        if not proxy:
            proxy = "http://127.0.0.1:7897"  # Clash 默认端口

        _logger.info("使用代理 %s 下载 mihomo", proxy)
        proxy_handler = urllib.request.ProxyHandler({
            "http": proxy, "https": proxy,
        })
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request(_GITHUB_RELEASE_URL, headers={
            "User-Agent": "DarkForest-Hunter/2.4.7",
        })
        zip_path = bin_path + ".zip"
        with opener.open(req, timeout=180) as resp:
            with open(zip_path, "wb") as f:
                f.write(resp.read())

        # 解压
        import zipfile
        with zipfile.ZipFile(zip_path, "r") as z:
            for name in z.namelist():
                # 匹配 mihomo 可执行文件(可能带平台后缀)
                lower = name.lower()
                if lower.startswith("mihomo") and (lower.endswith(".exe") or "." not in name.split("/")[-1]):
                    with z.open(name) as src, open(bin_path, "wb") as dst:
                        dst.write(src.read())
                    break
        os.remove(zip_path)

        # 确保可执行
        if platform.system() != "Windows":
            os.chmod(bin_path, 0o755)

        _logger.info("mihomo 下载完成: %s", bin_path)
        return bin_path
    except Exception as e:
        _logger.error("mihomo 下载失败: %s", e)
        return None


def _proxy_to_mihomo_node(info: dict) -> dict | None:
    """将 parse_subscription_links 的输出转换为 mihomo 节点格式。"""
    proto = info.get("protocol", "").lower()
    host = info.get("host", "")
    port = info.get("port", 0)
    name = info.get("name", f"{host}:{port}")

    if not host or not port:
        return None

    url = info.get("url", "")
    if proto == "vmess":
        # 从原始链接重新解析(含 UUID 等敏感信息)
        return _vmess_url_to_node(url, name)
    elif proto == "trojan":
        return _trojan_url_to_node(url, name)
    elif proto == "hysteria2" or proto == "hy2":
        return _hysteria2_url_to_node(url, name)
    elif proto == "ss":
        return _ss_url_to_node(url, name)
    return None


def _vmess_url_to_node(url: str, name: str) -> dict | None:
    """解析 VMess 链接为 mihomo 节点。"""
    import base64
    try:
        payload = url[len("vmess://"):]
        data = json.loads(base64.b64decode(payload + "=="))
    except Exception:
        return None
    return {
        "name": data.get("ps", name),
        "type": "vmess",
        "server": data.get("add"),
        "port": int(data.get("port", 0)),
        "uuid": data.get("id"),
        "alterId": int(data.get("aid", 0)),
        "cipher": data.get("scy", "auto"),
        "udp": True,
        "network": data.get("net", "tcp"),
        "ws-opts": {"path": data.get("path", "")} if data.get("net") == "ws" else None,
        "tls": data.get("tls") == "tls",
    }


def _trojan_url_to_node(url: str, name: str) -> dict | None:
    """解析 Trojan 链接为 mihomo 节点。"""
    from urllib.parse import parse_qs, unquote, urlparse
    try:
        parsed = urlparse(url)
        if not parsed.hostname:
            return None
        # password@host:port#name
        password = unquote(parsed.username or "")
        params = parse_qs(parsed.query)
        return {
            "name": unquote(parsed.fragment) if parsed.fragment else name,
            "type": "trojan",
            "server": parsed.hostname,
            "port": parsed.port or 443,
            "password": password,
            "udp": True,
            # v2.5.3 安全审查确认为**有意设计**(勿盲改 False):机场订阅证书
            # 常与 SNI 不匹配,默认严格校验会使绝大多数订阅直连失败;
            # 残余风险(路径第三方 MITM)与收益权衡后接受——节点运营方本就
            # 终止 TLS 看得到明文,此开关只影响你与节点之间的第三方。
            # 证书合规的自建节点可自行改回 False。
            "skip-cert-verify": True,
            "sni": params.get("sni", [parsed.hostname])[0],
        }
    except Exception:
        return None


def _hysteria2_url_to_node(url: str, name: str) -> dict | None:
    """解析 Hysteria2 链接为 mihomo 节点。"""
    from urllib.parse import parse_qs, unquote, urlparse
    try:
        parsed = urlparse(url)
        if not parsed.hostname:
            return None
        password = unquote(parsed.username or "")
        params = parse_qs(parsed.query)
        return {
            "name": unquote(parsed.fragment) if parsed.fragment else name,
            "type": "hysteria2",
            "server": parsed.hostname,
            "port": parsed.port or 443,
            "password": password,
            "skip-cert-verify": True,
            "sni": params.get("sni", [parsed.hostname])[0],
        }
    except Exception:
        return None


def _ss_url_to_node(url: str, name: str) -> dict | None:
    """解析 Shadowsocks 链接为 mihomo 节点。"""
    from urllib.parse import unquote, urlparse
    try:
        parsed = urlparse(url)
        if not parsed.hostname:
            return None
        # method:password@host:port#name
        userinfo = unquote(parsed.username or "")
        method, _, password = userinfo.partition(":")
        return {
            "name": unquote(parsed.fragment) if parsed.fragment else name,
            "type": "ss",
            "server": parsed.hostname,
            "port": parsed.port or 443,
            "cipher": method or "aes-256-gcm",
            "password": password,
            "udp": True,
        }
    except Exception:
        return None


class MihomoManager:
    """管理 mihomo 进程:下载、配置、启动、停止。

    每个上游代理对应一个本地 HTTP 代理端口,实现 per-IP pacing。
    """

    # 本地 HTTP 代理起始端口(每个上游代理一个)
    _BASE_PORT = 17890

    def __init__(self, subscription_url: str, num_listeners: int | None = None):
        self._subscription_url = subscription_url
        self._process: subprocess.Popen | None = None
        self._config_path: str | None = None
        self._proxy_ports: list[int] = []  # 本地 HTTP 代理端口列表

    @property
    def proxy_ports(self) -> list[int]:
        """返回本地 HTTP 代理端口列表。"""
        return list(self._proxy_ports)

    @property
    def proxy_urls(self) -> list[str]:
        """返回本地 HTTP 代理 URL 列表。"""
        return [f"http://127.0.0.1:{p}" for p in self._proxy_ports]

    def start(self) -> bool:
        """启动 mihomo,返回是否成功。"""
        # 1. 确保二进制存在
        bin_path = get_binary_path()
        if not is_installed():
            bin_path = download_mihomo()
            if not bin_path:
                return False

        # 2. 解析订阅
        endpoints = parse_subscription_links(self._subscription_url)
        if not endpoints:
            _logger.error("订阅解析无端点")
            return False

        # 3. 转换为 mihomo 节点
        nodes = []
        seen_hosts = set()
        for info in endpoints:
            node = _proxy_to_mihomo_node(info)
            if node and node.get("server") and node.get("port"):
                host_key = f"{node['server']}:{node['port']}"
                if host_key in seen_hosts:
                    continue  # 去重(同一 IP 不同名称)
                seen_hosts.add(host_key)
                nodes.append(node)

        if not nodes:
            _logger.error("无可用的 mihomo 节点")
            return False

        # 4. 生成配置(每个节点一个 HTTP 代理监听端口)
        self._proxy_ports = []
        listeners = []
        proxies = []
        for i, node in enumerate(nodes):
            port = self._BASE_PORT + i
            self._proxy_ports.append(port)
            listener_name = f"proxy_{i}"
            node_name = f"node_{i}"
            node["name"] = node_name
            listeners.append({
                "name": listener_name,
                "type": "http",
                "port": port,
                "proxy": node_name,  # 直连到该节点
            })
            proxies.append(node)

        config = {
            "port": 0,
            "socks-port": 0,
            "mixed-port": 0,
            "redir-port": 0,
            "tproxy-port": 0,
            "mode": "Rule",
            "listeners": listeners,
            "proxies": proxies,
            "proxy-groups": [],
            "rules": [],
            "geodata-mode": False,
            "allow-lan": False,
            "bind-address": "127.0.0.1",
            "log-level": "warning",  # mihomo v1.18 只认 warning,不认 warn("invalid mode" 误导性报错)
            "find-process-mode": "off",
            "keep-alive-interval": 30,
            "unified-delay": False,
            "sniffer": {"enable": False},
            "profile": {"store-selected": False, "store-fake-ip": False},
        }

        # 5. 写配置
        self._config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "results", "mihomo_config.yaml")
        os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
        import yaml
        with open(self._config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

        # 6. 启动进程
        try:
            self._process = subprocess.Popen(
                [bin_path, "-f", self._config_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
            )
        except Exception as e:
            _logger.error("mihomo 启动失败: %s", e)
            return False
        # 异常退出兜底:Python 崩溃/被强杀时 atexit 不保证执行,但常规未处理
        # 异常路径会走——没有它,每次异常退出都留一个 mihomo 孤儿占着端口,
        # 下次启动的 ready 探测连上僵尸进程,流量静默走旧配置出口。
        if not getattr(MihomoManager, "_atexit_registered", False):
            atexit.register(self.stop)
            MihomoManager._atexit_registered = True

        # 7. 等待端口就绪
        if not self._wait_for_ready(timeout=15):
            _logger.error("mihomo 端口未就绪")
            self.stop()
            return False

        _logger.info("mihomo 已启动: %d 个本地代理端口 %s",
                     len(self._proxy_ports), self._proxy_ports)
        return True

    def _wait_for_ready(self, timeout: float = 15.0) -> bool:
        """等待本地代理端口就绪，剔除不通的端口。

        v2.4.9: 每端口独立 deadline——此前 15s 由所有端口共享,第一个失活
        节点耗尽预算会饿死后续全部健康端口 → 整体 start 失败回退单代理。
        先做本地 TCP connect 就绪检查(端口未监听秒判),再走外网请求验证
        代理连通性;端口未监听 ≠ 节点故障,失败可区分。
        返回 True = 至少有一个端口可用;可用端口写入 _proxy_ports,
        不通的端口剔除(而非整体失败——1 个死节点不该丢掉其余全部 IP)。
        """
        ready_ports: list[int] = []
        for port in self._proxy_ports:
            # 进程已死(配置坏/端口被占秒退)→ 立即失败,不逐端口空等满 deadline
            # (坏配置最坏曾按 25s×N 端口空转)
            if self._process is not None and self._process.poll() is not None:
                _logger.error("mihomo 进程已退出(code=%s),停止等待端口",
                              self._process.returncode)
                return False
            # 1) 本地端口监听检查(无外网依赖,1s 内出结果)
            deadline = time.time() + timeout
            port_ready = False
            while time.time() < deadline:
                if self._process is not None and self._process.poll() is not None:
                    _logger.error("mihomo 进程在等待端口时退出(code=%s)",
                                  self._process.returncode)
                    return False
                try:
                    import socket
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        port_ready = True
                        break
                except Exception:
                    time.sleep(0.2)
            if not port_ready:
                _logger.warning("本地端口 %d 未监听(mihomo listener 未创建),剔除", port)
                continue
            # 2) 外网连通性验证(该端口独立 deadline,10s)
            proxy_dict = {"http": f"http://127.0.0.1:{port}",
                          "https": f"http://127.0.0.1:{port}"}
            ok = False
            deadline = time.time() + 10.0
            while time.time() < deadline and not ok:
                try:
                    r = requests.get("https://api.github.com/zen",
                                     proxies=proxy_dict, timeout=3)
                    if r.status_code == 200:
                        ok = True
                except Exception:
                    time.sleep(0.3)
            if ok:
                ready_ports.append(port)
            else:
                _logger.warning("本地端口 %d 已监听但外网不可达(节点故障?),剔除", port)
        if not ready_ports:
            return False
        if len(ready_ports) < len(self._proxy_ports):
            _logger.warning("多代理端口就绪 %d/%d,剔除不通端口",
                            len(ready_ports), len(self._proxy_ports))
        self._proxy_ports = ready_ports
        return True

    def stop(self):
        """停止 mihomo 进程。"""
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
            _logger.info("mihomo 已停止")
