"""
Base class for all scanners.
"""

import hashlib
import os
import re
import time
from abc import ABC, abstractmethod

# Key pattern: sk- followed by optional proj- prefix and 32-64 alphanumeric chars
KEY_PATTERN = re.compile(
    # OpenAI 现行 key(sk-proj-/svcacct-/admin-,总长 ~150-230)必须先于通用 sk- 分支,
    # 否则被 {20,95} 截断成残key 永远验证失败。
    # v2.4.9: {70,200} 封顶会把总长 >208 的真 key 截成残 key(正则不要求吃完整串,
    # 匹配成功即返回),is_bad_key 放行 → 验证 401 静默丢失。改为无上限 + 负向边界
    # (?![A-Za-z0-9_-]) 断言吃完整串,长度交给 is_bad_key 的 250 上限管理。
    # v2.5.1: 通用分支加负向断言——短假 sk-proj-/(len 24-56,DB 实证 269 条
    # unknown 噪声)此前从通用分支(可选组不启用,proj-... 全当 body)溜进 DB;
    # 专有前缀必须走各自分支的长度约束,不够长就提取不到,直接不进来。
    r"sk-proj-[A-Za-z0-9_-]{70,}(?![A-Za-z0-9_-])"       # OpenAI 项目级(现行默认)
    r"|sk-svcacct-[A-Za-z0-9_-]{70,}(?![A-Za-z0-9_-])"   # OpenAI 服务账号
    r"|sk-admin-[A-Za-z0-9_-]{70,}(?![A-Za-z0-9_-])"     # OpenAI 组织管理
    r"|sk-ant-api03-[A-Za-z0-9_-]{80,}(?![A-Za-z0-9_-])"  # Claude(真实总长 ~104-120,须先于通用分支)
    r"|sk-ant-oat01-[A-Za-z0-9_-]{60,}(?![A-Za-z0-9_-])"  # Claude CLI setup token(v2.5.2 识别侧已收,提取链曾断裂)
    r"|sk-or-v1-[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])"      # OpenRouter(真实总长≥39,对齐 providers {30,})
    r"|sk-(?!(?:proj-|svcacct-|admin-|ant-api03-|ant-oat01-|or-v1-))(?:ws-|kimi-|sp-|cp-)?[a-zA-Z0-9_-]{20,95}"  # 通用 sk- 家族(专有前缀已由负向断言排除)
    r"|eyJ[a-zA-Z0-9_.-]{80,}"     # MiniMax JWT (contains dots)
    r"|tp-[a-zA-Z0-9]{20,64}"      # Xiaomi Token Plan
    r"|gsk_[A-Za-z0-9]{40,60}"     # Groq
    r"|r8_[A-Za-z0-9]{30,50}"      # Replicate
    r"|hf_[A-Za-z0-9]{30,40}"      # HuggingFace
    r"|fw_[A-Za-z0-9]{30,50}"      # Fireworks AI
    r"|jina_[A-Za-z0-9]{30,50}"    # Jina AI
    r"|pa-[A-Za-z0-9]{30,50}"      # Voyage AI
    r"|xai-[A-Za-z0-9]{20,90}"     # xAI Grok(官方确认 xai- 前缀)
    r"|nvapi-[A-Za-z0-9_-]{30,90}" # NVIDIA NIM(build.nvidia.com)
    r"|AIza[a-zA-Z0-9_-]{35}(?![a-zA-Z0-9_-])"        # Google Gemini 传统 key(总长恰 39)
    r"|AQ\.Ab[a-zA-Z0-9_-]{30,120}(?![a-zA-Z0-9_-])"  # Google Gemini 2026 新 Auth key(AI Studio 现行签发)
    r"|ms-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # ModelScope 魔搭(UUID 形态)
    r"|bce-v3/ALTAK-[A-Za-z0-9]{14,32}/[A-Za-z0-9]{14,40}"  # 百度千帆 v2(两段带斜杠)
    r"|\b[a-f0-9]{32}\.[A-Za-z0-9]{16}\b"  # 智谱 GLM (hex.secret 两段带点, TruffleHog 官方格式)
)

BAD_PATTERNS = [
    "your", "xxx", "example", "placeholder", "replace", "here",
    "fake", "dummy", "changeme", "insert", "sample",
    "sk-xxxx", "sk-0000", "sk-1111", "sk-aaaa", "sk-bbbb",
    # v2.4.3: DB 实证补充——unknown 平台 1364 个 key 全是占位符/描述短语,
    # 真实 key 是高熵 base62/hex,不可能包含这些可读词组
    "change_me", "change-me", "change me",
    "coloque", "sua_chave", "sua-chave", "aqui",     # 葡语 "把你的 key 放这里"
    "not-a-real", "not_real", "notreal", "not-real",
    "master-key", "master_key", "masterkey",
    "test-key", "test_key", "testkey", "-test-", "_test_",
    "test-deepseek", "deepseek-test", "deepseek_test",
    "not-secure", "not_secure",
    "your_", "your-", "_your", "-your",
    "literal", "malformed", "provider-key", "provider_key",
    "por-prompt", "invalid", "default-key",
    # v2.5.1: DB 实证补充(unknown 池抽样)——CSS/JS minified 变量、代码字面量
    # 混进提取(sk-lineHei/sk-fontSiz/sk-colors-DEFAULT/sk-None/sk-sensitive 等);
    # 真 key 是高熵 base62,含这些连续可读词根的概率可忽略(同 v2.4.3 论证)
    "linehei", "fontsiz", "colors-", "sensiti", "interna",
    "none-", "nano-", "ckpt",
]
_BAD_PATTERNS_LOWER = frozenset(b.lower() for b in BAD_PATTERNS)

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
    """True if key is a placeholder/test/example key.

    多层过滤:
    1. 已知占位符前缀(sk-proj- 是 OpenAI 专属)
    2. 子串匹配(占位符关键词)
    3. body 字符集/熵检查
    4. 连续递增字符(123456/abcdef)
    5. 长度过滤:超长 key(>100 字符)大概率是 JWT/代码变量/长字符串,不是真 API key
    """
    # sk-proj- 曾被整体拒绝(当时 OpenAI 不在目标平台);接入 OpenAI 后它是正式
    # 提取目标,改为按前缀放宽长度(现行 OpenAI key 总长 ~150-230)。
    # 前缀感知长度上限:eyJ(MiniMax JWT 三段,最小总长 83)与 sk-ant-api03-(Claude,
    # 真实总长 ~104-120)天然超长,统一 >80 会把它们全杀(回归 27f2496);
    # 普通 sk-/tp-/gsk_ 等家族仍限 80(fresh-repo env 长内容误匹配仍需拦截)。
    # 验证端已有 >256 拒验护栏兜底,提取端按前缀放宽上限与之对齐。
    if key.startswith("eyJ"):
        if len(key) > 250:
            return True
    elif key.startswith("sk-ant-api03-"):
        if len(key) > 150:
            return True
    elif key.startswith("sk-ant-oat01-"):
        # Claude CLI setup token(识别侧 providers.py:498 同款口径):
        # 无独立上限会被通用 80 门限当残串丢弃,oat01 提取链曾因此断裂
        if len(key) > 150:
            return True
    elif key.startswith(("sk-proj-", "sk-svcacct-", "sk-admin-")):
        # OpenAI 现行三类前缀:总长 80-170+,上限 250 与验证端护栏对齐
        if len(key) > 250:
            return True
    elif key.startswith("AQ.Ab"):
        # Google Gemini 2026 Auth key(长度未公开文档化,观测 ~40-130)
        if len(key) > 200:
            return True
    elif key.startswith(("xai-", "nvapi-")):
        # v2.4.3: 新平台 pattern 自身允许到 ~94/96 字符,80 门限会静默丢弃
        # 提取正则放行的长 key(实证:85 位 xai key 被 KEY_PATTERN 命中却被此处丢弃)
        if len(key) > 120:
            return True
    elif key.startswith("bce-v3/"):
        # 千帆 v2 pattern 最长 7+6+32+1+40 = 86
        if len(key) > 110:
            return True
    elif key.startswith("sk-or-v1-"):
        # v2.4.9: OpenRouter pattern 允许 body 30-73 → 总长 39-82,
        # 通用 80 门限会杀掉全部真 key(实测 pattern 自身允许 82)
        if len(key) > 120:
            return True
    elif len(key) > 80:
        return True
    lower = key.lower()
    if any(b in lower for b in _BAD_PATTERNS_LOWER):
        return True
    if extra_bad and any(b.lower() in lower for b in extra_bad):
        return True
    # Body check: strip known prefixes before entropy test
    body = key
    for pfx in ("sk-proj-", "sk-ant-", "sk-svcacct-", "sk-admin-", "sk-ws-",
                "sk-kimi-", "sk-sp-", "sk-cp-", "sk-or-v1-", "sk-", "tp-",
                "gsk_", "r8_", "hf_", "nvapi-", "xai-", "ms-", "AIza"):
        if body.startswith(pfx):
            body = body[len(pfx):]
            break
    if body and (body.isdigit() or len(set(body)) < 4):
        return True
    # 占位符/示例 key 检测:连续递增字符(123456/abcdef 等 6 连)。
    # 只用顺序检测,不用字符频率——随机 32 位 hex key 约 30% 概率
    # 某字符出现 6+ 次,频率检测会误杀真 key。
    if len(body) >= 6:
        for seq in ("0123456789", "abcdefghijklmnopqrstuvwxyz", "zyxwvutsrqponmlkjihgfedcba"):
            for i in range(len(seq) - 5):
                if seq[i:i + 6] in body:
                    return True
    # v2.4.3: 描述性短语 slug 检测——body 全小写(数字)且为连字符/下划线连接的
    # 多段词,含 3 段以上纯字母词(如 change_me_get_from_www_com / test-not-real)。
    # 真实 key body 是高熵混合大小写 base62/hex;唯一带分隔符的例外是
    # ms-UUID(段为 hex,非纯字母)与 sk-ant(段含大写),都不会误伤。
    low = body.lower()
    if low == body and low and re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)+", low):
        words = re.split(r"[-_]", low)
        if sum(1 for w in words if len(w) >= 3 and w.isalpha()) >= 3:
            return True
    return False
def extract_keys(text: str, extra_bad: list = None) -> list[str]:
    keys = KEY_PATTERN.findall(text)
    return [k for k in keys if not is_bad_key(k, extra_bad)]


def dedup_results(results: list) -> list:
    """去重：按 (source|provider, key, url) 三元组的 MD5 去重。

    兼容 dict 与对象（如 providers.KeyResult）；identity 取 source 或 provider
    中存在者。这是全项目唯一的去重实现，providers.py 仅 re-export。
    """
    seen = set()
    out = []
    for r in results:
        if isinstance(r, dict):
            src = r.get("source") or r.get("provider") or ""
            key = r.get("key", "")
            url = r.get("url", "")
        else:
            src = getattr(r, "source", "") or getattr(r, "provider", "") or ""
            key = getattr(r, "key", "")
            url = getattr(r, "url", "")
        h = hashlib.md5(f"{src}:{key}:{url}".encode()).hexdigest()
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

    def log(self, msg: str, level: str = "info"):  # noqa: B027
        """Optional override hook for logging; default no-op (not all scanners log)."""
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
