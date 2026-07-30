"""
Base class for all scanners.
"""

import re
import time
import hashlib
import os
from abc import ABC, abstractmethod
from collections import OrderedDict

# Key pattern: sk- followed by optional proj- prefix and 32-64 alphanumeric chars
KEY_PATTERN = re.compile(r"sk-(?:proj-)?[a-zA-Z0-9]{32,64}")

BAD_PATTERNS = [
    "your", "xxx", "example", "placeholder", "replace", "here",
    "fake", "dummy", "changeme", "insert",
    "sk-xxxx", "sk-0000", "sk-1111", "sk-aaaa", "sk-bbbb",
]

# Paths that strongly indicate test/demo keys (low chance of balance)
LOW_VALUE_PATH_KEYWORDS = [
    "/test/", "/tests/", "/test/java/", "/test/kotlin/",
    "test.java", "test.kt", "test.py", "test.js", "test.ts",
    "demo.java", "demo.py", "example.java", "example.py",
    "sample.java", "sample.py",
    "TestMain", "TestDeep", "DeepSeekTest", "ApiTest",
    "/target/site/", "/target/",  # Build artifacts
    "TongYiChatModelTests", "DeepSeekChatModelTests",  # Common test duplicates
]

TARGET_FILE_EXTS = {
    ".py", ".js", ".ts", ".java", ".kt", ".php", ".rb", ".go",
    ".rs", ".cs", ".swift", ".dart", ".cpp", ".c", ".h",
    ".sh", ".bash", ".zsh", ".fish",
    ".env", ".yml", ".yaml", ".json", ".toml", ".cfg", ".ini",
    ".conf", ".config", ".properties", ".gradle",
    ".txt", ".md", ".html", ".xml", ".plist", ".lua",
    ".ipynb", ".dockerfile", ".envrc", ".env.local",
    ".env.production", ".env.development", ".env.example",
    ".env.sample", ".env.backup", ".credentials",
}

TARGET_FILENAMES = {
    "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".env", ".npmrc", ".pypirc", "credentials", "secrets",
    "config.json", "settings.json", "application.properties",
    "application.yml", "application.yaml",
    "gradle.properties", "local.properties",
}


def is_bad_key(key: str, extra_bad: list = None) -> bool:
    lower = key.lower()
    patterns = BAD_PATTERNS + (extra_bad or [])
    if any(b.lower() in lower for b in patterns):
        return True
    body = key[3:]
    if body.isdigit() or len(set(body)) < 4:
        return True
    return False


def extract_keys(text: str, extra_bad: list = None) -> list[str]:
    keys = KEY_PATTERN.findall(text)
    return [k for k in keys if not is_bad_key(k, extra_bad)]


def dedup_results(results: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for r in results:
        h = hashlib.md5(f"{r.get('source','')}:{r.get('key','')}:{r.get('url','')}".encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            out.append(r)
    return out


class BaseScanner(ABC):
    def __init__(self, concurrency: int = 10, timeout: int = 15,
                 min_key_length: int = 32, max_key_length: int = 64,
                 extra_bad_patterns: list = None, session=None, proxy: str = None):
        self.concurrency = concurrency
        self.timeout = timeout
        self.min_key_length = min_key_length
        self.max_key_length = max_key_length
        self.extra_bad = extra_bad_patterns or []
        self._session = session
        self.key_pattern = re.compile(
            rf"sk-(?:proj-)?[a-zA-Z0-9]{{{min_key_length},{max_key_length}}}"
        )
        self._stop_requested = False
        self._seen_urls = set()
        self.results: list[dict] = []
        # Proxy support: use provided proxy or check environment variable
        self._proxy = proxy or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or None

    @abstractmethod
    async def search(self, query: str | None = None) -> list[dict]:
        ...

    @property
    @abstractmethod
    def source_name(self) -> str:
        ...

    def extract_local(self, text: str) -> list[str]:
        keys = self.key_pattern.findall(text)
        return [k for k in keys if not is_bad_key(k, self.extra_bad)]

    def stop(self):
        self._stop_requested = True

    def _add_result(self, key: str, url: str, repo: str = "",
                    file_path: str = "", source: str = ""):
        self.results.append({
            "key": key,
            "key_preview": key[:10] + "..." + key[-4:],
            "source": source or self.source_name,
            "repo": repo,
            "file": file_path,
            "url": url,
        })

    def log(self, msg: str, level: str = "info"):
        """Log a message. Override in subclass or set externally."""
        pass

    def _should_stop(self) -> bool:
        return self._stop_requested

    def _rate_limit_wait(self, delay: float = 1.0):
        time.sleep(delay)

    async def _get_with_retry(self, session, url, *, headers=None, params=None,
                               timeout_total=20, max_retries=3, retry_statuses=(429, 503)):
        """统一的带指数退避 GET 请求。
        处理 429/503（读 Retry-After 或指数退避）。
        返回 (status, body_bytes, error)：body_bytes 为响应体字节（已读出，避免 resp 关闭后无法读取）；
        status 为 None 表示请求失败，此时 error 存放异常。
        替代各扫描器散落的 `except Exception: pass` 静默吞 429。"""
        import aiohttp as _aiohttp
        last_exc = None
        for attempt in range(max_retries):
            if self._should_stop():
                return None, None, None
            try:
                async with session.get(
                    url, headers=headers, params=params,
                    timeout=_aiohttp.ClientTimeout(total=timeout_total),
                    proxy=self._proxy,
                ) as resp:
                    if resp.status in retry_statuses:
                        # 优先用服务端 Retry-After，否则指数退避
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            try:
                                wait = min(float(retry_after), 60.0)
                            except ValueError:
                                wait = min(2 ** attempt * 2, 30.0)
                        else:
                            wait = min(2 ** attempt * 2, 30.0)
                        await __import__("asyncio").sleep(wait)
                        continue
                    # 必须在 async-with 内读完 body，否则 resp 关闭后调用方无法读取
                    body = await resp.read()
                    return resp.status, body, None
            except (_aiohttp.ClientError, __import__("asyncio").TimeoutError, OSError) as e:
                last_exc = e
                if attempt < max_retries - 1:
                    await __import__("asyncio").sleep(2 ** attempt)
                    continue
                return None, None, last_exc
        # 重试耗尽
        return None, None, last_exc
