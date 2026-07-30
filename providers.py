"""
AI 平台提供商配置模块
支持国内主流 AI 模型的 Key 格式、API 端点、验证方法
"""

import re
import json
import time
import hashlib
import hmac
import base64
import asyncio
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

import requests

import aiohttp


class AuthType(Enum):
    """认证类型"""
    BEARER = "bearer"           # Authorization: Bearer <key>
    API_KEY_HEADER = "header"   # Authorization: <key> (自定义 header)
    API_KEY_QUERY = "query"     # ?key=<key> (query 参数)
    JWT = "jwt"                 # JWT 签名认证
    CUSTOM = "custom"           # 自定义认证


class VerifyResult(Enum):
    """验证结果"""
    VALID_ACTIVE = "valid_active"       # 有效且有余额
    VALID_ZERO = "valid_zero"           # 有效但余额为0
    VALID_NO_BALANCE = "valid_no_balance"  # 有效但无法查余额
    INVALID = "invalid"                 # 无效
    RATE_LIMITED = "rate_limited"       # 被限速
    ERROR = "error"                     # 网络错误
    UNKNOWN = "unknown"                 # 未知状态


@dataclass
class AIProvider:
    """AI 平台提供商配置"""
    id: str                          # 唯一标识符
    name: str                        # 显示名称
    name_cn: str                     # 中文名称

    # Key 模式
    key_patterns: list               # 正则表达式列表
    key_context_queries: list = field(default_factory=list)  # GitHub 搜索查询

    # API 配置
    api_base: str = ""               # API 基础 URL
    verify_endpoint: str = ""        # 验证端点
    models_endpoint: str = ""        # 模型列表端点
    balance_endpoint: str = ""       # 余额查询端点

    # 认证配置
    auth_type: AuthType = AuthType.BEARER
    auth_header: str = "Authorization"  # 自定义认证 header 名
    api_version: str = ""            # API 版本

    # 验证模型
    verify_model: str = ""           # 用于验证的最小模型
    verify_prompt: str = "Hi"        # 验证用的 prompt

    # 免费/额度信息
    free_tier: str = ""              # 免费额度说明
    has_balance_check: bool = False  # 是否支持余额查询
    supports_usage: bool = False     # 是否支持用量查询

    # 优先级和状态
    priority: int = 0               # 优先级（越高越优先扫描）
    enabled: bool = True            # 是否启用
    risk_level: str = "low"         # 风险等级

    def get_key_regex(self) -> re.Pattern:
        """获取合并后的 key 正则表达式"""
        if not hasattr(self, '_compiled_patterns') or not self._compiled_patterns:
            self._compiled_patterns = [re.compile(p) for p in self.key_patterns]
        return self._compiled_patterns

    def match_key(self, text: str) -> list:
        """从文本中匹配所有可能的 key"""
        matches = []
        for pattern in self.get_key_regex():
            matches.extend(pattern.findall(text))
        return list(set(matches))


# ═══════════════════════════════════════════════════════════════════════════════
#  国内主流 AI 平台配置
# ═══════════════════════════════════════════════════════════════════════════════

# ──────────────────────────────────────────────────────────────────────────────
#  1. DeepSeek (深度求索) - 已有支持，作为基线
# ──────────────────────────────────────────────────────────────────────────────
DEEPSEEK = AIProvider(
    id="deepseek",
    name="DeepSeek",
    name_cn="深度求索",
    key_patterns=[
        r"sk-proj-[a-zA-Z0-9]{32,64}",
        r"sk-[a-zA-Z0-9]{32,64}",
    ],
    key_context_queries=[
        "api.deepseek.com sk-",
        "DEEPSEEK_API_KEY sk-",
        "DEEPSEEK_KEY sk-",
    ],
    api_base="https://api.deepseek.com",
    verify_endpoint="/chat/completions",
    balance_endpoint="/user/balance",
    auth_type=AuthType.BEARER,
    verify_model="deepseek-chat",
    has_balance_check=True,
    free_tier="赠送 $0.1000",
    priority=10,
)


# ──────────────────────────────────────────────────────────────────────────────
#  2. Kimi / Moonshot AI (月之暗面)
# ──────────────────────────────────────────────────────────────────────────────
KIMI = AIProvider(
    id="kimi",
    name="Kimi",
    name_cn="月之暗面 Kimi",
    key_patterns=[
        r"sk-[a-zA-Z0-9]{32,64}",
    ],
    key_context_queries=[
        "api.moonshot.cn sk-",
        "moonshot sk-",
        "MOONSHOT_API_KEY sk-",
        "KIMI_API_KEY sk-",
        "moonshot-ai sk-",
        "api.kimi.com sk-",
    ],
    api_base="https://api.moonshot.cn",
    verify_endpoint="/v1/chat/completions",
    auth_type=AuthType.BEARER,
    verify_model="moonshot-v1-8k",
    has_balance_check=False,
    supports_usage=True,
    free_tier="新用户赠送 15 元",
    priority=9,
)


# ──────────────────────────────────────────────────────────────────────────────
#  3. Zhipu AI / GLM (智谱AI)
# ──────────────────────────────────────────────────────────────────────────────
ZHIPU = AIProvider(
    id="zhipu",
    name="Zhipu AI",
    name_cn="智谱AI GLM",
    key_patterns=[
        r"sk-[a-zA-Z0-9]{32,64}",
    ],
    key_context_queries=[
        "open.bigmodel.cn sk-",
        "GLM_API_KEY sk-",
        "ZHIPU_API_KEY sk-",
        "zhipuai sk-",
        "glm-4 sk-",
        "chatglm sk-",
    ],
    api_base="https://open.bigmodel.cn/api/paas",
    verify_endpoint="/v4/chat/completions",
    auth_type=AuthType.BEARER,  # 智谱新版支持直接 Bearer {API Key}，无需 JWT 签名
    verify_model="glm-4-flash",
    has_balance_check=True,
    balance_endpoint="/v4/user/balance",  # 相对 api_base，修复路径重复(/api/paas 出现两次)
    free_tier="glm-4-flash 免费",
    priority=8,
    risk_level="medium",
)


# ──────────────────────────────────────────────────────────────────────────────
#  4. Qwen / DashScope (阿里通义千问)
# ──────────────────────────────────────────────────────────────────────────────
QWEN = AIProvider(
    id="qwen",
    name="Qwen",
    name_cn="通义千问 Qwen",
    key_patterns=[
        r"sk-[a-zA-Z0-9]{32,64}",
    ],
    key_context_queries=[
        "dashscope.aliyuncs.com sk-",
        "DASHSCOPE_API_KEY sk-",
        "QWEN_API_KEY sk-",
        "tongyi sk-",
        "qwen sk-",
        "dashscope sk-",
    ],
    api_base="https://dashscope.aliyuncs.com/compatible-mode",
    verify_endpoint="/v1/chat/completions",
    auth_type=AuthType.BEARER,
    verify_model="qwen-turbo",
    has_balance_check=True,
    balance_endpoint="/api/v1/services/aigc/text-generation/generation",
    free_tier="qwen-turbo 免费额度",
    priority=8,
)


# ──────────────────────────────────────────────────────────────────────────────
#  5. MiniMax (稀宇科技)
# ──────────────────────────────────────────────────────────────────────────────
MINIMAX = AIProvider(
    id="minimax",
    name="MiniMax",
    name_cn="稀宇科技 MiniMax",
    key_patterns=[
        r"eyJ[a-zA-Z0-9_-]{100,}",  # JWT 格式
    ],
    key_context_queries=[
        "api.minimax.chat sk-",
        "MINIMAX_API_KEY sk-",
        "minimax sk-",
        "MiniMax-Text sk-",
    ],
    api_base="https://api.minimax.chat",
    verify_endpoint="/v1/text/chatcompletion_v2",
    auth_type=AuthType.BEARER,
    verify_model="MiniMax-Text-01",
    has_balance_check=True,
    free_tier="新用户赠送 1 万 token",
    priority=6,
)


# ──────────────────────────────────────────────────────────────────────────────
#  6. ByteDance Doubao (字节跳动 豆包)
# ──────────────────────────────────────────────────────────────────────────────
DOUBAO = AIProvider(
    id="doubao",
    name="Doubao",
    name_cn="字节跳动 豆包",
    key_patterns=[
        r"sk-[a-zA-Z0-9]{32,64}",
    ],
    key_context_queries=[
        "ark.cn-beijing.volces.com sk-",
        "DOUBAO_API_KEY sk-",
        "doubao sk-",
        "volcengine sk-",
        "火山引擎 sk-",
    ],
    api_base="https://ark.cn-beijing.volces.com",
    verify_endpoint="/api/v3/chat/completions",
    auth_type=AuthType.BEARER,
    verify_model="doubao-lite-4k",
    has_balance_check=False,
    free_tier="有免费额度",
    priority=6,
)


# ──────────────────────────────────────────────────────────────────────────────
#  7. Baichuan (百川智能)
# ──────────────────────────────────────────────────────────────────────────────
BAICHUAN = AIProvider(
    id="baichuan",
    name="Baichuan",
    name_cn="百川智能",
    key_patterns=[
        r"sk-[a-zA-Z0-9]{32,64}",
    ],
    key_context_queries=[
        "api.baichuan-ai.com sk-",
        "BAICHUAN_API_KEY sk-",
        "baichuan sk-",
    ],
    api_base="https://api.baichuan-ai.com",
    verify_endpoint="/v1/chat/completions",
    auth_type=AuthType.BEARER,
    verify_model="Baichuan4",
    has_balance_check=False,
    free_tier="新用户赠送 token",
    priority=5,
)


# ──────────────────────────────────────────────────────────────────────────────
#  8. 01.AI / Yi (零一万物)
# ──────────────────────────────────────────────────────────────────────────────
YI = AIProvider(
    id="yi",
    name="01.AI",
    name_cn="零一万物 Yi",
    key_patterns=[
        r"sk-[a-zA-Z0-9]{32,64}",
    ],
    key_context_queries=[
        "api.lingyiwanwu.com sk-",
        "YI_API_KEY sk-",
        "01ai sk-",
        "lingyiwanwu sk-",
    ],
    api_base="https://api.lingyiwanwu.com",
    verify_endpoint="/v1/chat/completions",
    auth_type=AuthType.BEARER,
    verify_model="yi-lightning",
    has_balance_check=False,
    free_tier="有免费额度",
    priority=5,
)


# ──────────────────────────────────────────────────────────────────────────────
#  9. Xiaomi MiLM (小米大模型)
# ──────────────────────────────────────────────────────────────────────────────
XIAOMI = AIProvider(
    id="xiaomi",
    name="Xiaomi MiLM",
    name_cn="小米大模型",
    key_patterns=[
        r"sk-[a-zA-Z0-9]{32,64}",
    ],
    key_context_queries=[
        "api.xiaomi.com sk-",
        "XIAOMI_API_KEY sk-",
        "milm sk-",
        "xiaomi llm sk-",
        "XiaoMi sk-",
    ],
    api_base="https://api.xiaomi.com",
    verify_endpoint="/v1/chat/completions",
    auth_type=AuthType.BEARER,
    verify_model="MiLM-6B",
    has_balance_check=False,
    free_tier="需要申请",
    priority=3,
    risk_level="medium",  # 接口可能需要特殊权限
)


# ──────────────────────────────────────────────────────────────────────────────
#  10. StepFun (阶跃星辰)
# ──────────────────────────────────────────────────────────────────────────────
STEPFUN = AIProvider(
    id="stepfun",
    name="StepFun",
    name_cn="阶跃星辰",
    key_patterns=[
        r"sk-[a-zA-Z0-9]{32,64}",
    ],
    key_context_queries=[
        "api.stepfun.com sk-",
        "STEPFUN_API_KEY sk-",
        "step-1 sk-",
        "stepfun sk-",
    ],
    api_base="https://api.stepfun.com",
    verify_endpoint="/v1/chat/completions",
    auth_type=AuthType.BEARER,
    verify_model="step-1-flash",
    has_balance_check=False,
    free_tier="有免费额度",
    priority=4,
)


# ──────────────────────────────────────────────────────────────────────────────
#  11. SenseNova (商汤科技)
# ──────────────────────────────────────────────────────────────────────────────
SENSERNOVA = AIProvider(
    id="sensnova",
    name="SenseNova",
    name_cn="商汤科技 日日新",
    key_patterns=[
        r"sk-[a-zA-Z0-9]{32,64}",
    ],
    key_context_queries=[
        "api.sensenova.cn sk-",
        "SENSERNOVA_API_KEY sk-",
        "sensenova sk-",
        "日日新 sk-",
        "sensechat sk-",
    ],
    api_base="https://api.sensenova.cn",
    verify_endpoint="/v1/chat/completions",
    auth_type=AuthType.BEARER,
    verify_model="nova-3",
    has_balance_check=False,
    free_tier="有免费额度",
    priority=3,
)


# ──────────────────────────────────────────────────────────────────────────────
#  12. Anthropic Claude (Claude API - 通过代理商)
# ──────────────────────────────────────────────────────────────────────────────
CLAUDE = AIProvider(
    id="claude",
    name="Claude",
    name_cn="Anthropic Claude",
    key_patterns=[
        r"sk-ant-[a-zA-Z0-9]{32,64}",
    ],
    key_context_queries=[
        "api.anthropic.com sk-ant-",
        "ANTHROPIC_API_KEY sk-ant-",
        "claude sk-ant-",
        "anthropic sk-ant-",
    ],
    api_base="https://api.anthropic.com",
    verify_endpoint="/v1/messages",
    auth_type=AuthType.API_KEY_HEADER,
    auth_header="x-api-key",
    verify_model="claude-haiku-4-5-20251001",
    has_balance_check=False,
    free_tier="需要付费",
    priority=2,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Provider 注册表
# ═══════════════════════════════════════════════════════════════════════════════

# 按优先级排序的 Provider 列表
ALL_PROVIDERS: list[AIProvider] = [
    DEEPSEEK,
    KIMI,
    ZHIPU,
    QWEN,
    MINIMAX,
    DOUBAO,
    BAICHUAN,
    YI,
    XIAOMI,
    STEPFUN,
    SENSERNOVA,
    CLAUDE,
]

# Provider ID -> Provider 映射
PROVIDER_MAP: dict[str, AIProvider] = {p.id: p for p in ALL_PROVIDERS}

# 启用的 Provider 列表
ACTIVE_PROVIDERS: list[AIProvider] = [p for p in ALL_PROVIDERS if p.enabled]


# ═══════════════════════════════════════════════════════════════════════════════
#  统一 Key 匹配器
# ═══════════════════════════════════════════════════════════════════════════════

class UnifiedKeyMatcher:
    """统一的 Key 匹配器，支持多个 Provider"""

    def __init__(self, providers: list[AIProvider] = None):
        self.providers = providers or ACTIVE_PROVIDERS
        self._build_patterns()

    def _build_patterns(self):
        """构建所有 provider 的匹配模式"""
        self.all_patterns = {}
        for provider in self.providers:
            self.all_patterns[provider.id] = [
                re.compile(p) for p in provider.key_patterns
            ]

    def match_keys(self, text: str, provider_id: str = None) -> dict:
        """
        从文本中匹配所有可能的 key
        返回: {provider_id: [key1, key2, ...]}
        """
        results = {}

        providers = [PROVIDER_MAP[provider_id]] if provider_id else self.providers

        for provider in providers:
            matched = provider.match_key(text)
            if matched:
                results[provider.id] = matched

        return results

    def identify_provider(self, key: str, context: str = "") -> list:
        """
        根据 key 格式和上下文识别可能的 provider
        返回: [(provider_id, confidence), ...]
        """
        candidates = []

        for provider in self.providers:
            confidence = 0

            # 检查 key 格式
            if provider.match_key(key):
                confidence += 50

            # 检查上下文关键词
            if context:
                context_lower = context.lower()
                for query in provider.key_context_queries:
                    query_terms = query.lower().split()
                    matches = sum(1 for term in query_terms if term in context_lower)
                    if matches >= 2:
                        confidence += 30
                        break
                    elif matches == 1:
                        confidence += 10

            if confidence > 0:
                candidates.append((provider.id, confidence))

        # 按置信度排序
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates


# ═══════════════════════════════════════════════════════════════════════════════
#  统一 Key 验证器
# ═══════════════════════════════════════════════════════════════════════════════

class UnifiedKeyVerifier:
    """统一的 Key 验证器，支持多个 Provider"""

    def __init__(self, providers: list[AIProvider] = None, proxy: str = None):
        self.providers = providers or ACTIVE_PROVIDERS
        self.proxy = proxy
        self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def close(self):
        pass  # requests 不需要关闭会话

    def verify_key(self, key: str, provider_id: str = None) -> dict:
        """
        验证单个 key
        返回: {
            "key": str,
            "provider": str,
            "status": VerifyResult,
            "balance": float | None,
            "message": str,
            "model": str,
        }
        """
        # 识别可能的 provider
        if provider_id:
            providers = [PROVIDER_MAP[provider_id]]
        else:
            # 尝试识别
            matcher = UnifiedKeyMatcher()
            candidates = matcher.identify_provider(key)
            if not candidates:
                return {
                    "key": key,
                    "provider": "unknown",
                    "status": VerifyResult.UNKNOWN.value,
                    "balance": None,
                    "message": "无法识别 provider",
                    "model": None,
                }
            providers = [PROVIDER_MAP[c[0]] for c in candidates[:3]]

        # 尝试每个 provider
        for provider in providers:
            result = self._verify_with_provider(key, provider)
            if result["status"] != VerifyResult.INVALID.value:
                return result

        return {
            "key": key,
            "provider": providers[0].id if providers else "unknown",
            "status": VerifyResult.INVALID.value,
            "balance": None,
            "message": "所有 provider 验证失败",
            "model": None,
        }

    # 国内 AI 平台直连（不走代理）——这些 API 国内可直连，走代理反而易因代理 IP 被封
    # 海外平台（claude 等）才走代理
    DIRECT_PROVIDERS = {"deepseek", "zhipu", "qwen", "minimax", "doubao",
                        "baichuan", "yi", "stepfun", "xiaomi", "sensnova", "kimi"}

    def _verify_with_provider(self, key: str, provider: AIProvider) -> dict:
        """使用指定 provider 验证 key (同步版本，使用 requests)"""
        try:
            url = f"{provider.api_base}{provider.verify_endpoint}"
            headers = self._build_auth_headers(provider, key)

            # 构建请求体
            body = self._build_verify_body(provider)

            # 国内平台直连，海外平台走代理（避免代理 IP 被国内 API 封锁）
            if provider.id in self.DIRECT_PROVIDERS:
                proxies = None
            else:
                proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None

            resp = requests.post(url, json=body, headers=headers, proxies=proxies, timeout=30)

            if resp.status_code == 200:
                # 尝试获取余额
                balance = self._check_balance_sync(key, provider)

                return {
                    "key": key,
                    "provider": provider.id,
                    "status": VerifyResult.VALID_ACTIVE.value if balance and balance > 0
                              else VerifyResult.VALID_ZERO.value if balance == 0
                              else VerifyResult.VALID_NO_BALANCE.value,
                    "balance": balance,
                    "message": "验证成功",
                    "model": provider.verify_model,
                }

            elif resp.status_code == 401:
                return {
                    "key": key,
                    "provider": provider.id,
                    "status": VerifyResult.INVALID.value,
                    "balance": None,
                    "message": "认证失败",
                    "model": None,
                }

            elif resp.status_code == 429:
                return {
                    "key": key,
                    "provider": provider.id,
                    "status": VerifyResult.RATE_LIMITED.value,
                    "balance": None,
                    "message": "被限速",
                    "model": None,
                }

            else:
                return {
                    "key": key,
                    "provider": provider.id,
                    "status": VerifyResult.ERROR.value,
                    "balance": None,
                    "message": f"HTTP {resp.status_code}: {resp.text[:200]}",
                    "model": None,
                }

        except requests.Timeout:
            return {
                "key": key,
                "provider": provider.id,
                "status": VerifyResult.ERROR.value,
                "balance": None,
                "message": "超时",
                "model": None,
            }
        except Exception as e:
            return {
                "key": key,
                "provider": provider.id,
                "status": VerifyResult.ERROR.value,
                "balance": None,
                "message": str(e)[:200],
                "model": None,
            }

    def _build_auth_headers(self, provider: AIProvider, key: str) -> dict:
        """构建认证 headers"""
        headers = {"Content-Type": "application/json"}

        if provider.auth_type == AuthType.BEARER:
            headers[provider.auth_header] = f"Bearer {key}"
        elif provider.auth_type == AuthType.API_KEY_HEADER:
            headers[provider.auth_header] = key
        elif provider.auth_type == AuthType.JWT:
            # 智谱 AI 需要 JWT 签名
            jwt_token = self._create_zhipu_jwt(key)
            headers["Authorization"] = f"Bearer {jwt_token}"
        elif provider.auth_type == AuthType.CUSTOM:
            headers[provider.auth_header] = key

        return headers

    def _build_verify_body(self, provider: AIProvider) -> dict:
        """构建验证请求体"""
        if provider.id == "claude":
            return {
                "model": provider.verify_model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": provider.verify_prompt}],
            }
        else:
            return {
                "model": provider.verify_model,
                "messages": [{"role": "user", "content": provider.verify_prompt}],
                "max_tokens": 1,
            }

    def _create_zhipu_jwt(self, api_key: str) -> str:
        """创建智谱 AI 的 JWT token"""
        import struct
        try:
            # 智谱 AI 使用 API Key 作为 secret 进行 HMAC-SHA256 签名
            # 格式: {api_key}.{timestamp}.{sign}
            timestamp = int(time.time() * 1000)
            sign_str = f"{timestamp}"
            sign = hmac.new(
                api_key.encode(),
                sign_str.encode(),
                hashlib.sha256
            ).hexdigest()
            return f"{api_key}.{timestamp}.{sign}"
        except Exception:
            return api_key  # 回退：直接使用 key

    def _check_balance_sync(self, key: str, provider: AIProvider) -> Optional[float]:
        """检查余额 (同步版本)"""
        if not provider.has_balance_check or not provider.balance_endpoint:
            return None

        try:
            url = f"{provider.api_base}{provider.balance_endpoint}"
            headers = self._build_auth_headers(provider, key)

            # 国内平台直连，海外平台走代理
            if provider.id in self.DIRECT_PROVIDERS:
                proxies = None
            else:
                proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None

            resp = requests.get(url, headers=headers, proxies=proxies, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                # 解析余额（不同平台格式不同）
                if provider.id == "deepseek":
                    return data.get("balance_infos", [{}])[0].get("total_balance", 0)
                elif provider.id == "zhipu":
                    # 智谱 /v4/user/balance 返回 balanceInfos 列表，单位为元（非分，去掉错误的 /100）
                    infos = data.get("balanceInfos", [])
                    if infos and isinstance(infos, list):
                        return float(infos[0].get("totalBalance", 0))
                    return float(data.get("balance", 0))
                elif provider.id == "qwen":
                    # 通义 DashScope 余额单位为元
                    return float(data.get("balance", 0))
        except Exception:
            pass

        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  GitHub 搜索查询生成器
# ═══════════════════════════════════════════════════════════════════════════════

class QueryGenerator:
    """为多个 Provider 生成 GitHub 搜索查询"""

    # 文件类型后缀（高价值）
    FILE_TYPES = [
        "py", "js", "ts", "java", "kt", "php", "go", "cs",
        "env", "yml", "yaml", "json", "toml", "cfg", "ini",
        "sh", "bash", "dart", "swift", "rb", "rs",
    ]

    # 配置文件关键词
    CONFIG_KEYWORDS = [
        "config", "settings", "credentials", "secrets", "local",
        "production", "development", "example", "sample", "backup",
    ]

    @staticmethod
    def generate_for_provider(provider: AIProvider, max_queries: int = 30) -> list:
        """为指定 provider 生成搜索查询"""
        queries = []

        # 1. 直接使用 provider 定义的查询
        for q in provider.key_context_queries:
            queries.append(q)

        # 2. 根据 key 格式生成通用查询
        if provider.id not in ("claude",):  # Claude 有特殊 key 格式
            # API 基础 URL 搜索
            if provider.api_base:
                base_domain = provider.api_base.replace("https://", "").split("/")[0]
                for ft in QueryGenerator.FILE_TYPES[:5]:  # 前5种文件类型
                    queries.append(f"{base_domain} sk- filename:{ft}")

            # 环境变量搜索
            env_var_names = [
                f"{provider.id.upper()}_API_KEY",
                f"{provider.id.upper()}_KEY",
                f"{provider.name.upper()}_API_KEY",
            ]
            for env_var in env_var_names:
                queries.append(f'{env_var} sk-')

        # 限制查询数量
        return queries[:max_queries]

    @staticmethod
    def generate_all(max_per_provider: int = 20) -> list:
        """为所有 provider 生成查询"""
        all_queries = []
        seen = set()

        for provider in ACTIVE_PROVIDERS:
            queries = QueryGenerator.generate_for_provider(provider, max_per_provider)
            for q in queries:
                if q not in seen:
                    seen.add(q)
                    all_queries.append({
                        "query": q,
                        "provider": provider.id,
                        "priority": provider.priority,
                    })

        # 按优先级排序
        all_queries.sort(key=lambda x: x["priority"], reverse=True)
        return all_queries


# ═══════════════════════════════════════════════════════════════════════════════
#  结果存储
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class KeyResult:
    """Key 扫描结果"""
    key: str
    provider: str
    source: str = ""
    url: str = ""
    status: str = "unknown"
    balance: Optional[float] = None
    currency: str = "USD"
    verified_at: Optional[str] = None
    context: str = ""
    file_type: str = ""
    repo: str = ""
    owner: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "provider": self.provider,
            "source": self.source,
            "url": self.url,
            "status": self.status,
            "balance": self.balance,
            "currency": self.currency,
            "verified_at": self.verified_at,
            "context": self.context,
            "file_type": self.file_type,
            "repo": self.repo,
            "owner": self.owner,
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════════════════════════

def get_provider_by_key(key: str) -> Optional[AIProvider]:
    """根据 key 格式猜测 provider"""
    matcher = UnifiedKeyMatcher()
    candidates = matcher.identify_provider(key)
    if candidates:
        return PROVIDER_MAP[candidates[0][0]]
    return None


def is_bad_key_multi_provider(key: str) -> bool:
    """检查是否是已知的无效 key（多 provider 版本）"""
    bad_patterns = [
        "your", "xxx", "example", "placeholder", "replace", "here",
        "fake", "dummy", "changeme", "insert",
        "sk-xxxx", "sk-0000", "sk-1111", "sk-aaaa", "sk-bbbb",
        "test", "sample",
    ]
    lower = key.lower()
    return any(b in lower for b in bad_patterns)


def dedup_results(results: list) -> list:
    """去重结果"""
    seen = set()
    out = []
    for r in results:
        if isinstance(r, dict):
            key = r.get("key", "")
            provider = r.get("provider", "")
            url = r.get("url", "")
        else:
            key = r.key
            provider = r.provider
            url = r.url

        h = hashlib.md5(f"{provider}:{key}:{url}".encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            out.append(r)
    return out
