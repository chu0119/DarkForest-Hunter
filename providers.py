"""
AI 平台提供商配置模块
支持国内主流 AI 模型的 Key 格式、API 端点、验证方法
"""

import hashlib
import hmac
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum

import aiohttp
import requests

from scanners.base import dedup_results as dedup_results
from scanners.base import is_bad_key

# 单一真相源：is_bad_key / dedup_results 的实现集中在 scanners.base。
# is_bad_key_multi_provider 保留为向后兼容别名（测试与外部调用仍可用）。
is_bad_key_multi_provider = is_bad_key

_logger = logging.getLogger("darkforest.providers")


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


class ProviderRateLimiter:
    """按 provider 串行化请求窗口，避免多 worker 同时压同一家 API。

    intervals: 可选的 per-provider 最低间隔覆盖（秒）。个别平台限速更紧
    （如千帆默认 0.25s 间隔下实测出现 429），在此差异化放宽。
    """

    def __init__(self, min_interval: float = 0.25,
                 clock=time.monotonic, sleeper=time.sleep,
                 intervals: dict[str, float] | None = None):
        self.min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._sleeper = sleeper
        self._intervals = {k: max(0.0, float(v)) for k, v in (intervals or {}).items()}
        self._next_allowed: dict[str, float] = {}
        self._lock = threading.Lock()

    def acquire(self, provider_id: str) -> float:
        """预约下一次请求窗口；返回实际等待秒数（便于测试与诊断）。"""
        with self._lock:
            interval = self._intervals.get(provider_id, self.min_interval)
            now = self._clock()
            previous = self._next_allowed.get(provider_id, now)
            run_at = max(now, previous)
            self._next_allowed[provider_id] = run_at + interval

        wait = run_at - now
        if wait > 0:
            self._sleeper(wait)
        return wait


# 个别平台的差异化最低请求间隔（秒）。0.25s 全局默认下千帆实测出现 429。
PROVIDER_RATE_INTERVALS = {
    "qianfan": 1.0,
}


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
    verify_endpoint: str = ""        # 验证端点（legacy，已改用只读 models GET）
    models_endpoint: str = "/v1/models"  # 只读模型列表端点（验证有效性，不消耗配额）
    balance_endpoint: str = ""       # 余额查询端点（只读 GET，仅 has_balance_check 平台）

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
    chat_probe: bool = True          # 运行时还受 UnifiedKeyVerifier.allow_chat_probe 总开关控制
    # True：GET models 200 后补一次最小 chat 请求确认"有回复"（部分网关对 /models 不校验 key）
    # False：GET models 200 已可靠认证（如 claude，Anthropic 严格鉴权，POST 反而消耗配额）
    models_unauthenticated: bool = False  # True：/models 公开(假 key 也 200,实测 modelscope/nvidia/longcat)
    # 此时 GET 200 不构成认证证据——默认只读模式判 ERROR(绝不误判 valid)，
    # 显式开启 chat 探测后由探测结果判定。

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
    # sk-proj- 是 OpenAI 专属前缀，DeepSeek key 永远不含 proj-；
    # 且 scanners.base.is_bad_key 会拒绝所有 sk-proj-。此处不再匹配。
    key_patterns=[
        r"sk-[a-zA-Z0-9]{32,64}",
    ],
    key_context_queries=[
        "api.deepseek.com sk-",
        "DEEPSEEK_API_KEY sk-",
        "DEEPSEEK_KEY sk-",
    ],
    api_base="https://api.deepseek.com",
    verify_endpoint="/chat/completions",
    models_endpoint="/models",
    balance_endpoint="/user/balance",
    auth_type=AuthType.BEARER,
    verify_model="deepseek-v4-flash",  # 2026-08 官方模型已换代（deepseek-chat/reasoner 下架）
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
    models_endpoint="/v1/models",
    balance_endpoint="/v1/users/me/balance",  # 报表口径取 cash_balance(现金),代金券不计
    auth_type=AuthType.BEARER,
    verify_model="kimi-k3",  # 2026-08 旗舰；moonshot-v1-8k 将于 2026-08-31 全量下线
    has_balance_check=True,  # 官方提供 /v1/users/me/balance 查询
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
        # 官方格式: [a-f0-9]{32}.[A-Za-z0-9]{16} 两段带点(TruffleHog detector 确认)。
        # 旧正则 sk-[a-zA-Z0-9]{32,64} 匹配不到 → 智谱 key 从未被验证。
        r"[a-f0-9]{32}\.[A-Za-z0-9]{16}",
    ],
    key_context_queries=[
        # 智谱 key 是 hex.secret 两段格式,**不含 sk- 前缀**——查询词若带 sk-
        # 会把真实 key 文件全部漏掉(搜索要求同时含两个词)。裸域名/变量名词
        # 才能命中真实智谱 key 文件,提取正则(hex.secret)自动抓 key。
        "open.bigmodel.cn",
        "GLM_API_KEY",
        "ZHIPU_API_KEY",
        "zhipuai",
        "chatglm",
        "bigmodel",
    ],
    api_base="https://open.bigmodel.cn/api/paas",
    verify_endpoint="/v4/chat/completions",
    # 官方 openapi.json 无 models 列表端点；验证走 chat completions 路径的只读探测
    models_endpoint="/v4/chat/completions",
    auth_type=AuthType.BEARER,  # 智谱新版支持直接 Bearer {API Key}，无需 JWT 签名
    verify_model="glm-4-flash-250414",  # 免费模型（裸 glm-4-flash 已由 250414 版替代）
    has_balance_check=True,
    # v2.4.4: 真余额端点(社区证实,401 严格鉴权):balance_infos[].balance 按来源分列
    # (资源包/赠送/充值,官方扣费顺序=先资源包后现金,均可消费)。
    # 旧端点 /api/monitor/usage/quota/limit 的 limits[0].remaining 是资源包**总额度**
    # (注册赠 2000,恒显示 2000)——正是"假余额 2000"的来源,已弃用。
    balance_endpoint="https://open.bigmodel.cn/api/paas/v4/users/balance",
    free_tier="glm-4-flash-250414 免费",
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
        r"sk-ws-[a-zA-Z0-9]{32,64}",  # 2026 安全升级后新 key 前缀
    ],
    key_context_queries=[
        "dashscope.aliyuncs.com sk-",
        "DASHSCOPE_API_KEY sk-",
        "QWEN_API_KEY sk-",
        "tongyi sk-",
        "qwen sk-",
        "dashscope sk-",
        "maas.aliyuncs.com sk-",
    ],
    api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
    verify_endpoint="/chat/completions",
    models_endpoint="/models",
    auth_type=AuthType.BEARER,
    verify_model="qwen-flash",  # qwen-turbo 已标 legacy，官方建议迁 qwen-flash
    has_balance_check=False,  # 无 API 级余额查询（需阿里云 BssOpenApi，超出 scope）
    free_tier="qwen-flash 免费额度",
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
        # 真 MiniMax key 是带点的完整 JWT(base64url 三段)。
        # 旧正则不含点:真 key 永远匹配不上(1/3 有效段都带点),反而把
        # sourcemap 文件头 eyJ2ZXJzaW9uIjozLCJmaWxlIjoi(version:3)当 key 提取
        # ——实测 208 验 0 有效,其中 134 条是 sourcemap 垃圾。
        # 带点 + 负向排除 sourcemap 特征 "version" "sources"。
        r"eyJ(?!2ZXJzaW9u)[a-zA-Z0-9_.-]{80,}",
    ],
    key_context_queries=[
        "api.minimaxi.com sk-",
        "MINIMAX_API_KEY sk-",
        "minimax sk-",
        "MiniMax-M sk-",
        "api.minimax.io sk-",
    ],
    # 2026-08：旧域名 api.minimax.chat 已废弃，现行 api.minimaxi.com（中国）/ api.minimax.io（国际）
    api_base="https://api.minimaxi.com",
    verify_endpoint="/v1/chat/completions",
    models_endpoint="/v1/models",
    auth_type=AuthType.BEARER,
    verify_model="MiniMax-M3",  # M 系列旗舰（Text-01/abab 已过时）
    has_balance_check=False,  # 无公开余额 API（仅控制台）
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
    # /api/v3 是 base URL 的一部分（2026-08 确认）
    api_base="https://ark.cn-beijing.volces.com/api/v3",
    verify_endpoint="/chat/completions",
    models_endpoint="/models",
    auth_type=AuthType.BEARER,
    verify_model="doubao-seed-1-8-251228",  # Seed 系列（doubao-lite-4k 等旧模型已分批下线）
    has_balance_check=False,  # 方舟为后付费（T+1 出账），无 API 级余额端点
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
    models_endpoint="/v1/models",
    auth_type=AuthType.BEARER,
    verify_model="Baichuan4-Turbo",  # 2026-08：M3/M3-Plus 已上线，Baichuan4-Turbo 为旗舰档
    has_balance_check=False,  # 无公开余额 API（仅控制台）
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
    # 2026-09-21 实测:GET /v1/models → 410 "model_service_closed",API 服务已停运。
    # enabled=False 退出验证轮询(不再对 yi key 白白发请求后全员 ERROR);
    # 保留配置供历史 DB key 的显式 provider_id 查询。
    key_patterns=[
        r"sk-[a-zA-Z0-9]{32,64}",
    ],
    key_context_queries=[
        "api.lingyiwanwu.com sk-",
        "YI_API_KEY sk-",
        "01ai sk-",
        "lingyiwanwu sk-",
    ],
    api_base="https://api.lingyiwanwu.com",  # api.01.ai 已 DNS 失效（2024-08 国际站停服），此域唯一正确
    verify_endpoint="/v1/chat/completions",
    models_endpoint="/v1/models",
    auth_type=AuthType.BEARER,
    verify_model="yi-lightning",
    has_balance_check=False,
    free_tier="服务已停运(410 model_service_closed)",
    priority=0,
    enabled=False,
)


# ──────────────────────────────────────────────────────────────────────────────
#  9. Xiaomi MiLM (小米大模型)
# ──────────────────────────────────────────────────────────────────────────────
XIAOMI = AIProvider(
    id="xiaomi",
    name="Xiaomi MiMo",
    name_cn="小米 MiMo",
    key_patterns=[
        r"sk-[a-zA-Z0-9]{32,64}",
        r"tp-[a-zA-Z0-9]{32,64}",  # Token Plan 订阅 key
    ],
    key_context_queries=[
        "api.xiaomimimo.com sk-",
        "XIAOMI_API_KEY sk-",
        "mimo sk-",
        "xiaomi llm sk-",
        "xiaomimimo sk-",
        "MiMo sk-",
    ],
    # 2026-08：MiLM 已被 MiMo 全面取代（mimo.mi.com / platform.xiaomimimo.com）
    api_base="https://api.xiaomimimo.com",
    verify_endpoint="/v1/chat/completions",
    models_endpoint="/v1/models",
    auth_type=AuthType.BEARER,
    verify_model="mimo-v2.5",  # v2 旧模型 2026-06-30 弃用
    has_balance_check=False,  # 无公开余额 API（控制台 usage 页）
    free_tier="个人开放注册，按量付费",
    priority=3,
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
    models_endpoint="/v1/models",
    auth_type=AuthType.BEARER,
    verify_model="step-3.7-flash",  # 2026-08：step-3.x 时代（step-1-flash 已过时）
    has_balance_check=True,
    # 官方公开余额 API：GET /v1/accounts → {balance, total_cash_balance, total_voucher_balance}
    balance_endpoint="/v1/accounts",
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
    # 2026-08：商汤接口体系特殊——OpenAI 兼容模式走 /compatible-mode/v2，
    # 原生 API 为 /v1/llm/*。此处用兼容模式便于统一验证。
    api_base="https://api.sensenova.cn/compatible-mode",
    verify_endpoint="/v2/chat/completions",
    models_endpoint="/v2/models",  # 兼容模式下无 OpenAI 式 /v1/models，用 v2
    auth_type=AuthType.BEARER,
    verify_model="SenseChat-5",  # V5 文本旗舰 128K（nova-3 已过时）
    has_balance_check=False,  # 无公开余额 API（控制台查看）
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
        r"sk-ant-api03-[a-zA-Z0-9_-]{80,}",  # Real Claude keys contain hyphens after api03-
        r"sk-ant-oat01-[a-zA-Z0-9_-]{80,}",  # v2.5.1: CLI setup-token(订阅额度),
                                             # DB 实证 5 条因 identify 缺此 pattern
                                             # 漏路由成 unknown 从未验证
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
    chat_probe=False,  # Anthropic /v1/models 严格鉴权：GET 200 已可靠，POST 消耗配额
    free_tier="需要付费",
    priority=2,
)


# ──────────────────────────────────────────────────────────────────────────────
#  Coding Plan / 订阅套餐方案（2026-08 调研确认）
#  大部分平台"按量计费"与"Coding Plan"是两套独立体系：key 前缀 + api_base
#  双重区分（只有智谱/阶跃 key 统一）。验证时按前缀路由到正确入口——
#  否则像 kimi 一样：sk-kimi- 发到 api.moonshot.cn 直接 401。
# ──────────────────────────────────────────────────────────────────────────────

KIMI_CODING = AIProvider(
    id="kimi_coding",
    name="Kimi Coding Plan",
    name_cn="月之暗面 Kimi Code",
    key_patterns=[
        r"sk-kimi-[a-zA-Z0-9]{20,}",
    ],
    key_context_queries=[
        "api.kimi.com sk-kimi-",
        "KIMI_CODING_API_KEY sk-kimi-",
        "kimi-for-coding sk-kimi-",
    ],
    api_base="https://api.kimi.com/coding",
    models_endpoint="/v1/models",
    verify_endpoint="/v1/chat/completions",
    auth_type=AuthType.BEARER,
    verify_model="kimi-for-coding",
    has_balance_check=False,  # 订阅按配额窗口计，无公开余额 API
    free_tier="Coding Plan 订阅（~$19/月）",
    priority=4,
)

XIAOMI_PLAN = AIProvider(
    id="xiaomi_plan",
    name="小米 MiMo Token Plan",
    name_cn="小米 MiMo Token Plan",
    key_patterns=[
        r"tp-[a-zA-Z0-9]{20,}",
    ],
    key_context_queries=[
        "token-plan-cn.xiaomimimo.com tp-",
        "XIAOMI_TP_KEY tp-",
        "MiMo tp-",
    ],
    api_base="https://token-plan-cn.xiaomimimo.com/v1",
    models_endpoint="/models",
    verify_endpoint="/chat/completions",
    auth_type=AuthType.BEARER,
    verify_model="mimo-v2.5",
    has_balance_check=False,  # 订阅按 Credit 计，无公开余额 API
    free_tier="Token Plan 订阅",
    priority=4,
)

QWEN_CODING = AIProvider(
    id="qwen_coding",
    name="通义 Coding Plan",
    name_cn="通义千问 Coding Plan",
    key_patterns=[
        r"sk-sp-[a-zA-Z0-9]{20,}",
    ],
    key_context_queries=[
        "coding.dashscope.aliyuncs.com sk-sp-",
        "QWEN_CODING_KEY sk-sp-",
        "qwen coding plan sk-sp-",
    ],
    api_base="https://coding.dashscope.aliyuncs.com/v1",
    models_endpoint="/models",
    verify_endpoint="/chat/completions",
    auth_type=AuthType.BEARER,
    verify_model="qwen3.5-plus",
    has_balance_check=False,  # Coding Plan 限个人交互使用，无公开余额 API
    free_tier="Coding Plan 订阅",
    priority=4,
)

# ──────────────────────────────────────────────────────────────────────────────
#  智谱 GLM Coding Plan(端点与按量独立:api/coding/paas/v4)
# ──────────────────────────────────────────────────────────────────────────────
ZHIPU_CODING = AIProvider(
    id="zhipu_coding",
    name="智谱 GLM Coding Plan",
    name_cn="智谱 GLM Coding Plan",
    key_patterns=[
        # Coding Plan key 与按量 key 同格式(hex.secret),靠上下文区分。
        r"[a-f0-9]{32}\.[A-Za-z0-9]{16}",
    ],
    key_context_queries=[
        "open.bigmodel.cn/api/coding",
        "GLM_CODING_KEY",
        "bigmodel coding plan",
        "zhipu coding",
    ],
    api_base="https://open.bigmodel.cn/api/coding/paas",
    models_endpoint="/v4/models",
    verify_endpoint="/v4/chat/completions",
    auth_type=AuthType.BEARER,
    verify_model="glm-4.7-flash",
    # v2.4.4: Coding Plan 周额度查询(监控端点,GET coding/p4/models 401 严格鉴权确认)。
    # 返回 data.limits[] 含 5 小时窗/周窗(TOKENS_LIMIT/CREDIT_LIMIT)的
    # percentage(已用%)/usage/remaining + nextResetTime——取**周窗剩余百分比**
    # 作为该 key 的"余额"(单位 PERCENT,非金额)。注意该端点鉴权失败返回
    # HTTP 200 + body code:1000,解析不到 limits → None(不影响判定)。
    has_balance_check=True,
    balance_endpoint="https://open.bigmodel.cn/api/monitor/usage/quota/limit",
    free_tier="Coding Plan 订阅",
    priority=4,
)


MINIMAX_CP = AIProvider(
    id="minimax_cp",
    name="MiniMax Coding Plan",
    name_cn="稀宇 MiniMax Coding Plan",
    key_patterns=[
        r"sk-cp-[a-zA-Z0-9]{20,}",
    ],
    key_context_queries=[
        "api.minimaxi.com sk-cp-",
        "MINIMAX_CP_KEY sk-cp-",
        "minimax coding plan sk-cp-",
    ],
    api_base="https://api.minimaxi.com",
    models_endpoint="/v1/models",
    verify_endpoint="/v1/chat/completions",
    auth_type=AuthType.BEARER,
    verify_model="MiniMax-M3",
    has_balance_check=True,
    balance_endpoint="/v1/api/openplatform/coding_plan/remains",  # 官方确认的订阅用量接口
    free_tier="MiniMax-Subscription",
    priority=4,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Provider 注册表
# ═══════════════════════════════════════════════════════════════════════════════

# ──────────────────────────────────────────────────────────────────────────────
#  新平台 (2025-2026) — OpenRouter (48x leak growth), Groq, Replicate 等 10 个
# ──────────────────────────────────────────────────────────────────────────────

OPENROUTER = AIProvider(
    id="openrouter", name="OpenRouter", name_cn="OpenRouter",
    key_patterns=[r"sk-or-v1-[a-zA-Z0-9_-]{30,}"],
    key_context_queries=["openrouter.ai sk-or-", "OPENROUTER_API_KEY sk-or-"],
    # 2026-09-21 假 key 实测:/models 是公开目录(无 key 也 200)——用它验证会把
    # 任何格式正确的假 key 误判有效。改用 /auth/key:严格鉴权(假 key 401
    # "User not found")且返回 usage/limit → 可判余额(limit-usage=剩余 USD)。
    api_base="https://openrouter.ai/api/v1", models_endpoint="/auth/key",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="openai/gpt-4o-mini", has_balance_check=True,
    balance_endpoint="/auth/key", chat_probe=True,
    free_tier="按量付费", priority=5,
)

GROQ = AIProvider(
    id="groq", name="Groq", name_cn="Groq",
    key_patterns=[r"gsk_[A-Za-z0-9]{40,60}"],
    key_context_queries=["api.groq.com gsk_", "GROQ_API_KEY gsk_"],
    api_base="https://api.groq.com/openai/v1", models_endpoint="/models",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="llama-3.3-70b-versatile", has_balance_check=False, chat_probe=True,
    free_tier="免费额度", priority=5,
)

REPLICATE_PROVIDER = AIProvider(
    id="replicate", name="Replicate", name_cn="Replicate",
    key_patterns=[r"r8_[A-Za-z0-9]{30,50}"],
    key_context_queries=["api.replicate.com r8_", "REPLICATE_API_TOKEN r8_"],
    api_base="https://api.replicate.com/v1", models_endpoint="/models",
    verify_endpoint="/predictions", auth_type=AuthType.BEARER,
    auth_header="Authorization", verify_model="meta/llama-3.3-70b-instruct",
    has_balance_check=False, chat_probe=False, free_tier="按量付费", priority=3,
)

TOGETHER_AI = AIProvider(
    id="together", name="Together AI", name_cn="Together AI",
    key_patterns=[r"sk-[a-zA-Z0-9]{32,64}"],
    key_context_queries=["api.together.xyz sk-", "TOGETHER_API_KEY sk-"],
    api_base="https://api.together.xyz/v1", models_endpoint="/models",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
    has_balance_check=False, chat_probe=True, free_tier="免费额度", priority=4,
)

FIREWORKS = AIProvider(
    id="fireworks", name="Fireworks AI", name_cn="Fireworks AI",
    key_patterns=[r"fw_[A-Za-z0-9]{30,50}"],
    key_context_queries=["api.fireworks.ai fw_", "FIREWORKS_API_KEY fw_"],
    api_base="https://api.fireworks.ai/inference/v1", models_endpoint="/models",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="accounts/fireworks/models/llama-v3p3-70b-instruct",
    has_balance_check=False, chat_probe=True, free_tier="免费额度", priority=4,
)

SILICONFLOW = AIProvider(
    id="siliconflow", name="SiliconFlow", name_cn="硅基流动",
    key_patterns=[r"sk-[a-zA-Z0-9]{32,64}"],
    key_context_queries=["api.siliconflow.cn sk-", "SILICONFLOW_API_KEY sk-"],
    api_base="https://api.siliconflow.cn/v1", models_endpoint="/models",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="deepseek-ai/DeepSeek-V3", has_balance_check=True,
    balance_endpoint="/user/info", free_tier="注册送 2000 万 token", priority=5, risk_level="medium",
)

NOVITA = AIProvider(
    id="novita", name="Novita AI", name_cn="Novita AI",
    key_patterns=[r"sk-[a-zA-Z0-9]{32,64}"],
    key_context_queries=["api.novita.ai sk-", "NOVITA_API_KEY sk-"],
    api_base="https://api.novita.ai/v3/openai", models_endpoint="/models",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="meta-llama/llama-3.3-70b-instruct",
    has_balance_check=False, chat_probe=True, free_tier="/usr/bin/bash.50 免费额度", priority=3,
)

DEEPINFRA = AIProvider(
    id="deepinfra", name="DeepInfra", name_cn="DeepInfra",
    # 收紧 pattern: 排除 sk-/eyJ(JWT) 前缀,避免误匹配 OpenRouter(sk-or-v1-)、
    # OpenAI(sk-proj-)、Qwen Coding(sk-sp-) 等平台的 key body 与超长 JWT。
    # DeepInfra key 为裸 base62 串(无 sk- 前缀),40-60 位。
    key_patterns=[r"(?<![A-Za-z0-9_-])(?!sk-|eyJ)[A-Za-z0-9]{40,60}(?![A-Za-z0-9])"],
    key_context_queries=["api.deepinfra.com", "DEEPINFRA_API_KEY"],
    api_base="https://api.deepinfra.com/v1/openai", models_endpoint="/models",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="meta-llama/Llama-3.3-70B-Instruct",
    has_balance_check=False, chat_probe=True, free_tier="/usr/bin/bash.50 免费额度", priority=2,
)

JINA = AIProvider(
    id="jina", name="Jina AI", name_cn="Jina AI",
    key_patterns=[r"jina_[A-Za-z0-9]{30,50}"],
    key_context_queries=["api.jina.ai jina_", "JINA_API_KEY jina_"],
    api_base="https://api.jina.ai/v1",
    # 2026-09-21 假 key 实测:/models 公开(假 key 也 200)→ models_unauthenticated;
    # 且 Jina 无 OpenAI 式 chat 端点(探测会 404 误判"认证已过")→ chat_probe=False。
    # 当前无可靠的只读判定端点:默认判 ERROR,不误报 valid。
    models_endpoint="/models",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="jina-embeddings-v3",
    has_balance_check=False, chat_probe=False, models_unauthenticated=True,
    free_tier="100 万免费 token/月", priority=3,
)

VOYAGE = AIProvider(
    id="voyage", name="Voyage AI", name_cn="Voyage AI",
    key_patterns=[r"pa-[A-Za-z0-9]{30,50}"],
    key_context_queries=["api.voyageai.com pa-", "VOYAGE_API_KEY pa-"],
    api_base="https://api.voyageai.com/v1", models_endpoint="/models",
    verify_endpoint="/embeddings", auth_type=AuthType.BEARER,
    verify_model="voyage-3-lite", has_balance_check=False, chat_probe=False,
    free_tier="0 免费额度", priority=3,
)


# ──────────────────────────────────────────────────────────────────────────────
#  第二批平台 (2026-09 接入) — OpenAI / Gemini / xAI / 混元 / 千帆 /
#  ModelScope / NVIDIA / Longcat。全部端点与状态码经 2026-09-21 假 key 实测：
#  openai 401 / gemini 400 / xai 400 / hunyuan 401 / qianfan 403，
#  modelscope·nvidia·longcat 的 /models 公开不鉴权(models_unauthenticated=True)。
# ──────────────────────────────────────────────────────────────────────────────

OPENAI = AIProvider(
    id="openai", name="OpenAI", name_cn="OpenAI",
    # 现行 key 三类前缀(sk-proj-/svcacct-/admin-,总长 80-170+);legacy 裸 sk-+48
    # 与 10+ 平台共用格式,靠上下文路由。提取见 scanners/base.KEY_PATTERN。
    key_patterns=[
        r"sk-proj-[A-Za-z0-9_-]{70,200}",
        r"sk-svcacct-[A-Za-z0-9_-]{70,200}",
        r"sk-admin-[A-Za-z0-9_-]{70,200}",
    ],
    key_context_queries=[
        "api.openai.com sk-proj-",
        "OPENAI_API_KEY sk-proj-",
        "platform.openai.com sk-proj-",
        "openai sk-proj-",
    ],
    api_base="https://api.openai.com/v1", models_endpoint="/models",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="gpt-4o-mini", has_balance_check=False,
    chat_probe=True, free_tier="按量付费", priority=9,
)

GEMINI = AIProvider(
    id="gemini", name="Google Gemini", name_cn="Google Gemini",
    # 传统 AIza(39 位)+ 2026 新 Auth key(AQ.Ab...,AI Studio 现行签发)。
    # AIza 同为 Google 全家桶 key 格式,非 Gemini 专用的会在验证时被
    # 403(API 未开通/受限)拦截,不会误判有效。
    key_patterns=[
        r"AIza[a-zA-Z0-9_-]{35}(?![a-zA-Z0-9_-])",
        r"AQ\.Ab[a-zA-Z0-9_-]{30,120}",
    ],
    key_context_queries=[
        "generativelanguage.googleapis.com AIza",
        "GEMINI_API_KEY AIza",
        "GOOGLE_API_KEY AIza",
        "aistudio.google.com apikey",
        # v2.5.1: 补 AQ.Ab 新格式语境——旧 4 条全硬编码 AIza,新格式 key
        # 上下文 0 分易被误路由(实证 1 条 AQ.Ab 落到 deepinfra);
        # 2026-09 起 Gemini 拒收标准 AIza,AQ.Ab 是唯一活口
        "GEMINI_API_KEY AQ",
        "aq.ab apikey",
    ],
    api_base="https://generativelanguage.googleapis.com/v1beta",
    models_endpoint="/models",
    # Gemini 原生协议(非 OpenAI 兼容),认证走 ?key= 查询参数(API_KEY_QUERY);
    # 无 OpenAI 式 chat/completions → 关闭 chat 探测,GET /models 严格鉴权已够。
    auth_type=AuthType.API_KEY_QUERY,
    verify_endpoint="", has_balance_check=False, chat_probe=False,
    free_tier="AI Studio 免费额度", priority=9,
)

XAI = AIProvider(
    id="xai", name="xAI Grok", name_cn="xAI Grok",
    key_patterns=[r"xai-[A-Za-z0-9]{20,90}"],
    key_context_queries=["api.x.ai xai-", "XAI_API_KEY xai-", "GROK_API_KEY xai-"],
    api_base="https://api.x.ai/v1", models_endpoint="/models",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="grok-3-mini", has_balance_check=False, chat_probe=True,
    free_tier="新账户赠送额度", priority=6,
)

HUNYUAN = AIProvider(
    id="hunyuan", name="Tencent Hunyuan", name_cn="腾讯混元",
    # OpenAI 兼容接口用 sk- key(混元控制台签发),与 10+ 平台共用通用格式,
    # 靠上下文路由;原生腾讯云 SDK 走 SecretID/SK 签名不在本工具范围。
    key_patterns=[r"sk-[a-zA-Z0-9]{32,64}"],
    key_context_queries=[
        "api.hunyuan.cloud.tencent.com sk-",
        "HUNYUAN_API_KEY sk-",
        "hunyuan sk-",
        "混元 sk-",
    ],
    api_base="https://api.hunyuan.cloud.tencent.com/v1", models_endpoint="/models",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="hunyuan-t1", has_balance_check=False, chat_probe=True,
    free_tier="有免费额度", priority=6,
)

QIANFAN = AIProvider(
    id="qianfan", name="Baidu Qianfan", name_cn="百度千帆",
    # 千帆 v2 API Key: bce-v3/ALTAK-xxx/xxx 两段带斜杠(官方示例确认)。
    # 实测 /v2/models 对无效 key 返回 403 AccessDenied(非 401),
    # 验证器已按平台把 403 映射为 invalid。
    key_patterns=[r"bce-v3/ALTAK-[A-Za-z0-9]{14,32}/[A-Za-z0-9]{14,40}"],
    key_context_queries=[
        "qianfan.baidubce.com bce-v3",
        "QIANFAN_API_KEY bce-v3",
        "千帆 API_KEY",
    ],
    api_base="https://qianfan.baidubce.com/v2", models_endpoint="/models",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="ernie-4.0-8k", has_balance_check=False, chat_probe=True,
    free_tier="有免费额度", priority=6,
)

MODELSCOPE = AIProvider(
    id="modelscope", name="ModelScope", name_cn="魔搭 ModelScope",
    # API-KEY 为 ms- + UUID(api-inference 端点实测形态)。
    # 实测 /v1/models 公开(假 key 也 200)→ models_unauthenticated=True:
    # 默认只读模式判 ERROR,显式 chat 探测后由 401/200 判定。
    key_patterns=[r"ms-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"],
    key_context_queries=[
        "api-inference.modelscope.cn ms-",
        "MODELSCOPE_API_KEY ms-",
        "modelscope ms-",
    ],
    api_base="https://api-inference.modelscope.cn/v1", models_endpoint="/models",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="Qwen/Qwen3-32B", has_balance_check=False, chat_probe=True,
    models_unauthenticated=True,
    free_tier="每天 2000 次免费推理", priority=6,
)

NVIDIA = AIProvider(
    id="nvidia", name="NVIDIA NIM", name_cn="NVIDIA NIM",
    # build.nvidia.com 签发 nvapi- 前缀 key。
    # 实测 /v1/models 公开(假 key 也 200)→ 同 modelscope 处理;
    # chat 端点鉴权失败为 403(_probe_chat 已映射 invalid)。
    # v2.5.1: verify_model 于 2026-09-21 08:00Z EOL(410 先于鉴权)→ 219 条全
    # ERROR,换现役模型(活体实测: 假 key 403 / 无头 401,鉴权语义正常)。
    key_patterns=[r"nvapi-[A-Za-z0-9_-]{30,90}"],
    key_context_queries=[
        "integrate.api.nvidia.com nvapi-",
        "NVIDIA_API_KEY nvapi-",
        "build.nvidia.com nvapi-",
    ],
    api_base="https://integrate.api.nvidia.com/v1", models_endpoint="/models",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="deepseek-ai/deepseek-v4.1-flash",
    has_balance_check=False, chat_probe=True, models_unauthenticated=True,
    free_tier="注册赠送 NIM 额度", priority=5,
)

LONGCAT = AIProvider(
    id="longcat", name="LongCat", name_cn="美团 LongCat",
    # 美团 LongCat API 平台(longcat.chat),OpenAI/Anthropic 双协议兼容。
    # 官方未公开 key 固定前缀 → 通用 sk- + 上下文路由(同 baichuan 模式)。
    # 实测 /openai/v1/models 公开(假 key 也 200)→ models_unauthenticated=True。
    key_patterns=[r"sk-[a-zA-Z0-9]{32,64}"],
    key_context_queries=[
        "api.longcat.chat sk-",
        "LONGCAT_API_KEY",
        "longcat sk-",
        "LongCat-Flash",
    ],
    api_base="https://api.longcat.chat/openai/v1", models_endpoint="/models",
    verify_endpoint="/chat/completions", auth_type=AuthType.BEARER,
    verify_model="LongCat-2.0", has_balance_check=False, chat_probe=True,
    models_unauthenticated=True,
    free_tier="每天 10 万 token 免费额度", priority=4,
)


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
    # Coding Plan / 订阅套餐（key 前缀与按量完全隔离，独立验证入口）
    KIMI_CODING,
    XIAOMI_PLAN,
    ZHIPU_CODING,
    QWEN_CODING,
    MINIMAX_CP,
    # New platforms (2025-2026 research)
    OPENROUTER, GROQ, REPLICATE_PROVIDER, TOGETHER_AI, FIREWORKS,
    SILICONFLOW, NOVITA, DEEPINFRA, JINA, VOYAGE,
    # 第二批平台 (2026-09):国际大厂补全 + 国内云厂商 + 新锐
    OPENAI, GEMINI, XAI, HUNYUAN, QIANFAN, MODELSCOPE, NVIDIA, LONGCAT,
]

# Provider ID -> Provider 映射
PROVIDER_MAP: dict[str, AIProvider] = {p.id: p for p in ALL_PROVIDERS}

# 启用的 Provider 列表
ACTIVE_PROVIDERS: list[AIProvider] = [p for p in ALL_PROVIDERS if p.enabled]

# 余额以 USD 计价的平台（海外）。其余平台（国内外）余额默认按 CNY 处理。
# 旧实现只认 claude 为 USD，openrouter/groq 等 10 个国际平台被错标 CNY——
# 这些平台均无余额查询（balance=0/None），仅影响展示口径，此处一并纠正。
USD_PROVIDERS = frozenset({
    "claude", "openai", "gemini", "xai", "nvidia",
    "openrouter", "groq", "replicate", "together", "fireworks",
    "novita", "deepinfra", "jina", "voyage",
})

# "余额"是额度百分比(0-100)而非金额的平台——GLM Coding Plan 周额度。
# 这些 key 的 balance=剩余%,currency="PERCENT";换算函数对 PERCENT 透传,
# 阈值/台账按百分点理解(>1% 留存,0% 也记录,周重置不剔除)。
PERCENT_PROVIDERS = frozenset({
    "zhipu_coding",
})

# 400=确定性无效的平台(活体实测:无效 key 返回 400 "API key not valid" 等)。
# 其余平台的 400 语义未证实,按 ERROR 处理保护凭据明文(见 verify_key)。
_HTTP400_INVALID_PROVIDERS = frozenset({
    "gemini",
    "xai",
})


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
            format_matched = provider.match_key(key)
            if format_matched:
                confidence += 50

            # 检查上下文关键词（仅在格式匹配后生效——否则纯 sk- 噪声
            # 会让格式不相关的平台(如 minimax 的 eyJ JWT)混入候选）
            # 加分取"最强单条 query 命中"，不跨 query 累加——
            # 否则只命中高频词(如 sk-)的 query 会堆叠分数，压过真正命中平台域名的信号
            if format_matched and context:
                context_lower = context.lower()
                # best = (加成, 命中查询长度):同加成下保留更长的查询——
                # zhipu("open.bigmodel.cn")是 zhipu_coding("open.bigmodel.cn/api/coding")
                # 的子串,同 term 双双 +10 时,按 priority 老排序会让 coding key
                # 被并行验证里的按量端点抢先贴错平台标签;更具体的上下文应优先。
                best = (0, 0)
                for query in provider.key_context_queries:
                    query_terms = query.lower().split()
                    matches = sum(1 for term in query_terms if term in context_lower)
                    if matches >= 2:
                        best = max(best, (30, len(query)))
                    elif matches == 1:
                        best = max(best, (10, len(query)))
                best_bonus = best[0]
                confidence += best_bonus
                matched_query_len = best[1]
            else:
                matched_query_len = 0

            if confidence > 0:
                candidates.append((provider.id, confidence, matched_query_len))

        # 按置信度 → 上下文具体度(更长=更specific) → provider.priority 排序——
        # 通用 sk- key 无上下文时 14 家同分,仍按历史产出优先级兜底。
        # getattr 容错:测试/扩展方可能注入未注册进 PROVIDER_MAP 的简化 provider。
        candidates.sort(
            key=lambda x: (x[1], x[2],
                           getattr(PROVIDER_MAP.get(x[0]), "priority", 0)),
            reverse=True)
        return candidates


# ═══════════════════════════════════════════════════════════════════════════════
#  统一 Key 验证器
# ═══════════════════════════════════════════════════════════════════════════════

# 模块级 matcher 单例(懒加载):identify_provider 每 key 调用,重编译正则太贵。
# 懒加载而非类属性立即初始化——测试 monkeypatch UnifiedKeyMatcher 才生效。
_matcher_singleton: UnifiedKeyMatcher | None = None


def _get_matcher() -> UnifiedKeyMatcher:
    """模块级 matcher 单例(懒加载):identify_provider 每 key 调用,重编译 ~35 个正则太贵。

    v2.4.9: 编译结果缓存到 matcher 实例自身,单例缓存到模块变量。
    测试 monkeypatch UnifiedKeyMatcher/PROVIDER_MAP 后必须先调 reset_matcher_singleton(),
    否则拿到的是旧 PROVIDER_MAP 编译出的残留单例(两测试文件合跑必红)。
    """
    global _matcher_singleton
    if _matcher_singleton is None:
        _matcher_singleton = UnifiedKeyMatcher()
    return _matcher_singleton


def reset_matcher_singleton():
    """测试/运行时重置 matcher 单例(如 monkeypatch 替换了 PROVIDER_MAP 之后)。"""
    global _matcher_singleton
    _matcher_singleton = None


class UnifiedKeyVerifier:
    """统一的 Key 验证器，支持多个 Provider"""

    def __init__(self, providers: list[AIProvider] = None, proxy: str = None,
                 session=None, allow_chat_probe: bool = False,
                 session_factory=None,
                 rate_limiter: ProviderRateLimiter | None = None,
                 candidate_workers: int = 3,
                 probe_unclear: bool = True,
                 max_candidates: int = 8):
        self.providers = providers or ACTIVE_PROVIDERS
        self.proxy = proxy
        self._session = None
        # POST /chat/completions 可能消耗额度。默认只做 GET models/balance 验证，
        # 运营者必须显式接受最小生成探测的代价后才启用。
        self.allow_chat_probe = allow_chat_probe
        # 不明确平台的兜底探测:models_unauthenticated 平台(如魔搭/NVIDIA/LongCat,
        # /models 公开无法只读判定)允许发一次 max_tokens=1 最小请求判定——
        # 否则这些平台在只读模式下永远 ERROR,形同虚设。仅此一类平台,不影响
        # "GET 已能判定"的平台的免探测默认。
        self.probe_unclear = probe_unclear
        # 模糊 key(通用 sk- 格式,14 家同分)并行验证的候选平台上限。
        # v2.5.1: 4→8——实证无上下文 siliconflow key 排第 7,4 上限使其永远
        # 进不了验证池(DB 115 条 valid 全部来自带域名上下文的查询)。
        # 前几家有本地格式预检(deepseek hex/kimi alnum)近乎零成本;
        # 首个有效即取消其余候选,增量成本只在前面全 invalid 时发生(便宜 GET)。
        self._max_candidates = max(1, int(max_candidates))
        # 注入复用的 requests.Session(VerificationBroker 每 worker 一个),
        # 省每次验证的 TCP/TLS 握手开销。None 时回退裸 requests.get/post。
        self._http = session
        # 候选并发会在多个线程发请求；工厂让每个调用线程拿到自己的 Session。
        self._session_factory = session_factory
        # provider 通道限速器：watch 的多个 worker 可注入同一实例。
        self.rate_limiter = rate_limiter or ProviderRateLimiter(
            intervals=dict(PROVIDER_RATE_INTERVALS))
        self._candidate_workers = max(1, int(candidate_workers))
        self._candidate_pool_handle: ThreadPoolExecutor | None = None
        self._candidate_pool_lock = threading.Lock()

    async def _get_session(self):
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def close(self):
        """关闭候选验证执行器；外部注入的 Session 由调用方关闭。"""
        with self._candidate_pool_lock:
            pool, self._candidate_pool_handle = self._candidate_pool_handle, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _candidate_pool(self) -> ThreadPoolExecutor:
        """复用候选并发器，避免每个模糊 key 都创建/销毁线程池。"""
        with self._candidate_pool_lock:
            if self._candidate_pool_handle is None:
                self._candidate_pool_handle = ThreadPoolExecutor(
                    max_workers=self._candidate_workers,
                    thread_name_prefix="key-verify",
                )
            return self._candidate_pool_handle

    def verify_key(self, key: str, provider_id: str = None, context: str = "") -> dict:
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
            # 尝试识别。模块级单例:每 key 重编译 ~35 个正则开销可观。
            matcher = _get_matcher()
            candidates = matcher.identify_provider(key, context)
            if not candidates:
                return {
                    "key": key,
                    "provider": "unknown",
                    "status": VerifyResult.UNKNOWN.value,
                    "balance": None,
                    "message": "无法识别 provider",
                    "model": None,
                }
            providers = [PROVIDER_MAP[c[0]] for c in candidates[:self._max_candidates]]

        # 并行尝试候选 provider（sk-* 通用前缀匹配 10+ 平台，串行 3×3s=9s 太慢）。
        # 候选池随 verifier 复用；找到有效结果后取消未完成任务。
        if len(providers) == 1:
            return self._verify_with_provider(key, providers[0])

        from concurrent.futures import CancelledError, as_completed
        pool = self._candidate_pool()
        try:
            futures = {
                pool.submit(self._verify_with_provider, key, p): p
                for p in providers
            }
        except RuntimeError:
            # shutdown 竞态:broker.stop() 已关闭候选池(worker 还在途)。
            # 返回 ERROR 走重验,不得让异常逃逸炸掉 worker 循环。
            return {
                "key": key, "provider": providers[0].id if providers else "unknown",
                "status": VerifyResult.ERROR.value,
                "balance": None, "message": "候选池已关闭(shutdown)", "model": None,
            }
        try:
            first_non_invalid = None
            cancelled_count = 0
            for fut in as_completed(futures):
                # 收集阶段同样可能撞上关停竞态:broker.stop() 对 worker
                # join(10s) 超时后 close(cancel_futures=True),排队中的候选
                # future 被置 CANCELLED 并从 as_completed 吐出。CancelledError
                # 是 BaseException 子类,会穿透 _verify_one/_worker_loop 的
                # except Exception 护栏直接杀死验证线程(submit 阶段有护栏、
                # 收集阶段曾漏)。取消 = 该候选无结论,继续收其余结果。
                try:
                    result = fut.result()
                except CancelledError:
                    # CancelledError 是 BaseException 子类,必须显式捕获——
                    # except Exception 接不住,会穿透 worker 护栏杀死线程。
                    cancelled_count += 1
                    if first_non_invalid is None:
                        first_non_invalid = {
                            "key": key,
                            "provider": providers[0].id if providers else "unknown",
                            "status": VerifyResult.ERROR.value,
                            "balance": None,
                            "message": "候选池已关闭(候选被取消)", "model": None,
                        }
                    continue
                if result["status"] not in (VerifyResult.INVALID.value,
                                            VerifyResult.ERROR.value,
                                            VerifyResult.RATE_LIMITED.value):
                    # 找到有效结果——取消未启动慢路后立即返回。
                    for other in futures:
                        other.cancel()
                    return result
                if first_non_invalid is None and result["status"] in (
                        VerifyResult.ERROR.value, VerifyResult.RATE_LIMITED.value):
                    # 网络错误/限流先记下——若其余都 invalid 用它兜底
                    # (错误结果不抢占正确 provider 的验证机会)
                    first_non_invalid = result
        finally:
            for future in futures:
                future.cancel()
        # 全部完成:优先返回兜底错误(rate_limited 可重验),否则 invalid。
        # 有候选被取消时不得落 INVALID 兜底——"没验完"不等于"确定无效",
        # 否则会触发 store 脱敏链永久抹掉明文。
        if cancelled_count:
            return first_non_invalid or {
                "key": key, "provider": providers[0].id if providers else "unknown",
                "status": VerifyResult.ERROR.value,
                "balance": None, "message": "候选池已关闭(shutdown)", "model": None,
            }
        return first_non_invalid or {
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
                        "baichuan", "yi", "stepfun", "xiaomi", "sensnova", "kimi",
                        # Coding Plan 变体同样是国内端点,直连(国外代理 IP 易被拒)
                        "kimi_coding", "zhipu_coding", "qwen_coding",
                        "xiaomi_plan", "minimax_cp",
                        # 2026-09 第二批:混元/千帆/魔搭/LongCat 均为国内端点
                        "hunyuan", "qianfan", "modelscope", "longcat"}

    def _proxies_for(self, provider: AIProvider) -> dict | None:
        """国内平台直连，海外平台走代理（避免代理 IP 被国内 API 封锁）。"""
        if provider.id in self.DIRECT_PROVIDERS:
            return None
        return {"http": self.proxy, "https": self.proxy} if self.proxy else None

    def _http_get(self, url, *, headers, proxies, timeout):
        """走注入的 Session(复用连接池)或回退裸 requests.get。"""
        if self._session_factory is not None:
            return self._session_factory().get(
                url, headers=headers, proxies=proxies, timeout=timeout)
        if self._http is not None:
            return self._http.get(url, headers=headers, proxies=proxies, timeout=timeout)
        return requests.get(url, headers=headers, proxies=proxies, timeout=timeout)

    def _http_post(self, url, *, headers, proxies, timeout, json):
        """走注入的 Session(复用连接池)或回退裸 requests.post。"""
        if self._session_factory is not None:
            return self._session_factory().post(
                url, json=json, headers=headers, proxies=proxies, timeout=timeout)
        if self._http is not None:
            return self._http.post(url, json=json, headers=headers, proxies=proxies, timeout=timeout)
        return requests.post(url, json=json, headers=headers, proxies=proxies, timeout=timeout)

    def _probe_allowed(self, provider: AIProvider) -> bool:
        """是否允许对该平台发最小 chat 探测。

        - 全局 opt-in(allow_chat_probe):所有 chat_probe 平台
        - 不明确平台兜底(probe_unclear):仅 models_unauthenticated 平台
          (/models 公开无法只读判定,不探测则永远 ERROR)
        """
        if not provider.chat_probe:
            return False
        return self.allow_chat_probe or (self.probe_unclear and provider.models_unauthenticated)

    def _verify_with_provider(self, key: str, provider: AIProvider) -> dict:
        """验证 key：先只读 GET，再最小探测确认（探测默认开，--no-allow-chat-probe 可关）。

        流程：
          1. GET models → 200 有效 / 401·400 无效 / 402 有效但欠费 /
             403 按平台(invalid 或 error) / 429 限流 / 其他 error
          2. 有效且 has_balance_check → GET balance_endpoint 查余额（只读）
          3. _probe_allowed 平台（全平台 opt-in 或 models 公开兜底）→
             一次 max_tokens=1 最小 chat 探测，探测结果为最终判定
        """
        try:
            # 超长 key 护栏: >256 字符的 key 大概率是 JWT/误提取,塞进
            # Authorization 头会触发 nginx "Request Header Or Cookie Too Large" (400),
            # 永久 error 不收敛。直接判 invalid,省一次无效请求。
            if len(key) > 256:
                return {
                    "key": key, "provider": provider.id,
                    "status": VerifyResult.INVALID.value,
                    "balance": None, "message": "key 过长(>256)拒验", "model": None,
                }
            # P2: 平台格式预检 —— 在调 API 前先验证 key 是否符合平台特征
            # 统计:openrouter/groq 高产但低转化(大量无效 key 浪费 API 调用)
            if not self._pre_check_key_format(key, provider):
                return {
                    "key": key, "provider": provider.id,
                    "status": VerifyResult.INVALID.value,
                    "balance": None, "message": "格式预检失败", "model": None,
                }
            url = f"{provider.api_base}{provider.models_endpoint}"
            if provider.auth_type == AuthType.API_KEY_QUERY:
                # Gemini 等 query 认证平台：key 走 URL 参数，不进 header
                # （假 key 实测返回 400 "API key not valid"，由下方 400 分支判 invalid）
                url = f"{url}?key={key}"
            headers = self._build_auth_headers(provider, key)
            self.rate_limiter.acquire(provider.id)
            resp = self._http_get(url, headers=headers,
                                proxies=self._proxies_for(provider), timeout=30)

            if resp.status_code == 401:
                return {
                    "key": key, "provider": provider.id,
                    "status": VerifyResult.INVALID.value,
                    "balance": None, "message": "认证失败", "model": None,
                }

            if resp.status_code == 429:
                return {
                    "key": key, "provider": provider.id,
                    "status": VerifyResult.RATE_LIMITED.value,
                    "balance": None, "message": "被限速", "model": None,
                }

            if resp.status_code == 402:
                # 认证已通过但余额不足（欠费）。key 本身有效，不能归类为 ERROR。
                return {
                    "key": key, "provider": provider.id,
                    "status": VerifyResult.VALID_ZERO.value,
                    "balance": 0.0, "message": "有效但余额不足 (HTTP 402)", "model": None,
                }

            if resp.status_code == 405:
                # 405 = 端点存在但仅支持 POST（如 chat/completions）。
                # GET 探测无法区分认证状态——按平台兜底：
                #   有余额端点 → 查余额确认（余额响应本身即认证证据）
                #   无余额端点 → POST 最小 chat/completions 探测（每家都有 OpenAI 兼容端口）
                if provider.has_balance_check and provider.balance_endpoint:
                    self.rate_limiter.acquire(provider.id)
                    balance = self._check_balance_sync(key, provider)
                    if balance is not None:
                        # 余额>0 才是 ACTIVE;0/负(如 zhipu 配额耗尽)归 ZERO
                        status = (VerifyResult.VALID_ACTIVE.value
                                  if balance > 0 else VerifyResult.VALID_ZERO.value)
                        return {
                            "key": key, "provider": provider.id,
                            "status": status,
                            "balance": balance, "message": "有效（405 探测）",
                            "model": provider.verify_model,
                        }
                    # 余额查询失败(网络错误/404)≠认证失败——回退 chat 探测,
                    # 不要直接判 INVALID(瞬态网络抖动会把有效 key 永久杀)。
                if self._probe_allowed(provider):
                    self.rate_limiter.acquire(provider.id)
                    probe = self._probe_chat(key, provider)
                    if probe is not None:
                        status, bal, msg = probe
                        return {
                            "key": key, "provider": provider.id,
                            "status": status, "balance": bal,
                            "message": msg, "model": provider.verify_model,
                        }
                    # 探测网络失败 → 无认证证据,判 ERROR 走重验,
                    # 不能判 VALID_NO_BALANCE(假阳性污染结果库)。
                    return {
                        "key": key, "provider": provider.id,
                        "status": VerifyResult.ERROR.value,
                        "balance": None, "message": "405 探测失败（余额/chat 均不可判）",
                        "model": provider.verify_model,
                    }
                return {
                    "key": key, "provider": provider.id,
                    "status": VerifyResult.ERROR.value,
                    "balance": None, "message": "405 且无可探测端点",
                    "model": provider.verify_model,
                }

            if resp.status_code == 403:
                # 403 语义因平台而异（2026-09 假 key 实测）：
                # - qianfan /v2/models 对无效 key 返回 403 AccessDenied（鉴权失败）
                # - gemini 403 表示密钥真实但无 Gemini 权限（API 未开通/ referer 受限），
                #   对本工具而言同样"不可用"
                # 这两类按 invalid 收敛（ERROR 会导致重验不收敛）；
                # 其余平台 403 可能是 WAF/区域拦截，维持 ERROR 语义可重验。
                if provider.id in ("gemini", "qianfan"):
                    return {
                        "key": key, "provider": provider.id,
                        "status": VerifyResult.INVALID.value,
                        "balance": None, "message": "HTTP 403 无权限/鉴权失败", "model": None,
                    }

            if resp.status_code == 400:
                # 400 语义按平台区分(gatekeep 脱敏链):gemini/xai 活体实测无效 key
                # 返回 400 "API key not valid"——确定性无效,照旧 INVALID 收敛。
                # 其余平台 400 语义未证实(网关区域拦截/端点改版都可能 400),
                # 判 INVALID 会触发 store 脱敏链把首次验证的明文永久替换——
                # 归 ERROR 走重验,last_error 留痕,最坏多花几次 GET。
                if provider.id in _HTTP400_INVALID_PROVIDERS:
                    return {
                        "key": key, "provider": provider.id,
                        "status": VerifyResult.INVALID.value,
                        "balance": None, "message": f"HTTP 400: {resp.text[:200]}", "model": None,
                    }
                return {
                    "key": key, "provider": provider.id,
                    "status": VerifyResult.ERROR.value,
                    "balance": None, "message": f"HTTP 400: {resp.text[:200]}", "model": None,
                }

            if resp.status_code != 200:
                return {
                    "key": key, "provider": provider.id,
                    "status": VerifyResult.ERROR.value,
                    "balance": None,
                    "message": f"HTTP {resp.status_code}: {resp.text[:200]}", "model": None,
                }

            # 200 → 认证已通过。⚠️ 例外：models_unauthenticated 平台（2026-09 实测
            # modelscope/nvidia/longcat 的 /models 公开，假 key 也 200），
            # GET 200 不构成认证证据——无法探测时判 ERROR，绝不判 valid
            # （否则任何格式正确的假 key 都会被误报成有效泄露）。
            if provider.models_unauthenticated and not self._probe_allowed(provider):
                return {
                    "key": key, "provider": provider.id,
                    "status": VerifyResult.ERROR.value,
                    "balance": None,
                    "message": "models 端点不校验 key，需显式开启 chat 探测才能判定",
                    "model": None,
                }

            # 200 → 认证已通过。查余额**仅作参考**——余额接口显示 ≠ 实际可用
            # （实测 kimi：available_balance=240 全是 voucher_balance 代金券，
            # chat 探测 429 "suspended due to insufficient balance" 实际欠费）。
            self.rate_limiter.acquire(provider.id)
            balance = self._check_balance_sync(key, provider)

            # 最终判定：chat 探测（每家都有 OpenAI 兼容端口，响应"有回复"才算真有效）。
            # 覆盖所有平台（含 has_balance_check）——余额接口不决定状态，探测为准。
            if self._probe_allowed(provider):
                self.rate_limiter.acquire(provider.id)
                probe = self._probe_chat(key, provider)
                if probe is not None:
                    status, bal, msg = probe
                    # 探测判有效但余额接口有数值 → 补充余额信息（仅展示，不影响状态）
                    if bal is None and balance and balance > 0:
                        bal = balance
                    # v2.5.3: "active≠有钱"口径修复——probe 放行(ACTIVE)但
                    # 平台有余额端点且**确凿查出** ≤0 → 降 VALID_ZERO
                    # (DB 实证: zhipu 26 条 active@balance=0,glm-4-flash 免费
                    # 使 0 钱 key 也 probe 通过)。balance=None(无端点/查询
                    # 失败)保持 probe 状态——没有 0 的确证不降级,不误杀。
                    if (status == VerifyResult.VALID_ACTIVE.value
                            and balance is not None and balance <= 0):
                        status = VerifyResult.VALID_ZERO.value
                        bal = float(balance)
                        msg = "probe 放行但现金余额为 0"
                    return {
                        "key": key, "provider": provider.id,
                        "status": status, "balance": bal,
                        "message": msg, "model": provider.verify_model,
                    }
                if provider.models_unauthenticated:
                    # models 公开平台：探测失败时 GET 200 不构成认证证据，
                    # 不能"退回 GET 认证"——那等于把假 key 判成有效。
                    return {
                        "key": key, "provider": provider.id,
                        "status": VerifyResult.ERROR.value,
                        "balance": None,
                        "message": "chat 探测失败且 models 不鉴权，无法判定",
                        "model": provider.verify_model,
                    }
                # 探测失败（网络/超时）→ 退回到 GET 认证通过的事实
                return {
                    "key": key, "provider": provider.id,
                    "status": VerifyResult.VALID_NO_BALANCE.value,
                    "balance": None, "message": "验证成功（chat 探测失败，按 GET 认证计）",
                    "model": provider.verify_model,
                }
            # chat_probe=False（claude：GET /v1/models 严格鉴权）→ GET 认证即有效
            if balance and balance > 0:
                return {
                    "key": key, "provider": provider.id,
                    "status": VerifyResult.VALID_ACTIVE.value,
                    "balance": balance, "message": "验证成功", "model": provider.verify_model,
                }
            if balance is not None and balance <= 0:
                # 0 或负(欠费,cash_balance 口径可为负)→ 有效但无钱
                return {
                    "key": key, "provider": provider.id,
                    "status": VerifyResult.VALID_ZERO.value,
                    "balance": float(balance), "message": "有效但现金余额不足",
                    "model": provider.verify_model,
                }
            return {
                "key": key, "provider": provider.id,
                "status": VerifyResult.VALID_NO_BALANCE.value,
                "balance": None, "message": "验证成功（无余额查询）",
                "model": provider.verify_model,
            }

        except requests.Timeout:
            return {
                "key": key, "provider": provider.id,
                "status": VerifyResult.ERROR.value,
                "balance": None, "message": "超时", "model": None,
            }
        except Exception as e:
            return {
                "key": key, "provider": provider.id,
                "status": VerifyResult.ERROR.value,
                "balance": None, "message": str(e)[:200], "model": None,
            }

    def _pre_check_key_format(self, key: str, provider: AIProvider) -> bool:
        """平台格式预检:在调 API 前快速验证 key 是否符合平台特征。

        统计基础:
        - openrouter 高产但低转化(大量 sk-or-v1- 无效 key)
        - groq 的 gsk_ 前缀有固定格式(gsk_ + base62)
        预检失败的 key 直接判 invalid,省一次 API 调用(0.5-2s)。
        """
        k = key
        pid = provider.id
        if pid == "openrouter":
            # sk-or-v1- 后跟 base62 串(实测长度 30-60)
            if not k.startswith("sk-or-v1-"):
                return False
            body = k[len("sk-or-v1-"):]
            if len(body) < 20 or len(body) > 80:
                return False
            # base62 字符集:字母+数字
            if not all(c.isalnum() or c in "-_" for c in body):
                return False
        elif pid == "groq":
            # gsk_ 后跟 base62 串(40-60 字符)
            if not k.startswith("gsk_"):
                return False
            body = k[len("gsk_"):]
            if len(body) < 35 or len(body) > 70:
                return False
        elif pid in ("deepseek", "qwen", "dashscope"):
            # DB 证据: 有效样本 100% 为 32 位小写 hex(deepseek 824/824, qwen 753/753)。
            # 纯字母拼凑的占位串(uikoukw 类)在此被杀,省一次验证 API(0.5-2s)。
            if not k.startswith("sk-"):
                return False
            body = k[len("sk-"):]
            if body.startswith("ws-"):  # qwen 2026 升级前缀
                body = body[len("ws-"):]
            if not re.fullmatch(r"[0-9a-f]{28,64}", body):
                return False
        elif pid == "kimi":
            # kimi 真 key body 为 48 位 base62 alnum(DB 240/240)
            if not k.startswith("sk-"):
                return False
            body = k[len("sk-"):]
            if len(body) < 28 or len(body) > 70:
                return False
            if not body.isalnum():
                return False
        # 其他平台不做预检(保留原有验证流程)
        return True

    def _build_auth_headers(self, provider: AIProvider, key: str) -> dict:
        """构建认证 headers"""
        headers = {"Content-Type": "application/json"}

        if provider.auth_type == AuthType.BEARER:
            headers[provider.auth_header] = f"Bearer {key}"
        elif provider.auth_type == AuthType.API_KEY_HEADER:
            headers[provider.auth_header] = key
            # Claude Messages/Models API 要求 anthropic-version 头
            if provider.id == "claude":
                headers["anthropic-version"] = "2023-06-01"
        elif provider.auth_type == AuthType.JWT:
            # 智谱 AI 需要 JWT 签名（legacy，当前 zhipu 已用 Bearer）
            jwt_token = self._create_zhipu_jwt(key)
            headers["Authorization"] = f"Bearer {jwt_token}"
        elif provider.auth_type == AuthType.CUSTOM:
            headers[provider.auth_header] = key

        return headers

    def _create_zhipu_jwt(self, api_key: str) -> str:
        """创建智谱 AI 的 JWT token"""
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

    def _check_balance_sync(self, key: str, provider: AIProvider) -> float | None:
        """检查余额 (同步版本)"""
        if not provider.has_balance_check or not provider.balance_endpoint:
            return None

        try:
            # 绝对 URL 直接使用(如智谱配额端点),相对路径拼 api_base
            url = (provider.balance_endpoint if provider.balance_endpoint.startswith("http")
                   else f"{provider.api_base}{provider.balance_endpoint}")
            headers = self._build_auth_headers(provider, key)

            # 国内平台直连，海外平台走代理
            if provider.id in self.DIRECT_PROVIDERS:
                proxies = None
            else:
                proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None

            resp = self._http_get(url, headers=headers, proxies=proxies, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                # 解析余额（不同平台格式不同；2026-08 调研更新）
                if provider.id == "deepseek":
                    # 官方格式：balance_infos[] 多条目(total_balance 字符串)。
                    # v2.5.1: 跨全部条目求和——旧代码只取 [0],DB 实证 576 valid
                    # 全 0.0 疑漏多币种/赠金条目(如 CNY+USD 分列)。
                    # is_available=false 表示账户被停用(余额存在但不可用),
                    # 不参与求和修正——保持金额口径,停用由 chat 探测判定。
                    infos = data.get("balance_infos") or []
                    if not infos:
                        # 字段整体缺失(API 形状变化)≠ 0 余额确证——返回 None
                        # 保持 probe 状态,不把真有钱的 key 降级 VALID_ZERO
                        return None
                    try:
                        total = 0.0
                        for it in infos:
                            if not isinstance(it, dict):
                                continue
                            raw = it.get("total_balance")
                            if raw is None:
                                raw = it.get("balance", 0)
                            total += float(raw or 0)
                        return total
                    except (TypeError, ValueError):
                        return None  # 单条目脏数据:丢弃的是"已求和结果",编 0 会误降级
                elif provider.id == "kimi":
                    # 官方字段语义(platform.kimi.com / Billing API 规范):
                    #   cash_balance     现金余额(元),可为负=欠费;欠费时
                    #                    available_balance == voucher_balance
                    #   voucher_balance  代金券(活动券,不可抵扣现金计费)
                    #   available_balance 可用余额 = cash + voucher(欠费时仅 voucher)
                    # 报表口径取 **cash_balance**——代金券不是钱,纯券账户实际
                    # 不可消费(实测 chat 探测 429 insufficient balance)。
                    # 旧响应缺 cash_balance 时用 available - voucher 折算。
                    d = data.get("data", {}) or {}
                    cash = d.get("cash_balance")
                    if cash is None:
                        if d.get("available_balance") is None:
                            return None  # cash/available 全缺:无 0 的确证,不编造
                        cash = (float(d.get("available_balance") or 0)
                                - float(d.get("voucher_balance") or 0))
                    return float(cash)
                elif provider.id == "zhipu":
                    # GET /api/paas/v4/users/balance → balance_infos[].balance
                    # (按来源分列:资源包/赠送/充值;官方扣费顺序先资源包后现金,
                    # 全部可消费 → 求和为可消费总额)
                    infos = data.get("balance_infos") or []
                    if not infos:
                        return None  # 同 deepseek:缺失 ≠ 0
                    try:
                        return sum(float(it.get("balance") or 0)
                                   for it in infos if isinstance(it, dict))
                    except (TypeError, ValueError):
                        return None
                elif provider.id == "zhipu_coding":
                    # Coding Plan 周额度:monitor 端点 data.limits[] 中
                    # TOKENS_LIMIT/CREDIT_LIMIT 为 5h/周窗口,取 nextResetTime
                    # 最晚者(周窗)的剩余百分比。无套餐窗口(按量 key)→ None。
                    d = data.get("data") or {}
                    windows = []
                    for lim in (d.get("limits") or []):
                        if not isinstance(lim, dict) or lim.get("type") not in (
                                "TOKENS_LIMIT", "CREDIT_LIMIT"):
                            continue
                        rem_pct = None
                        pct = lim.get("percentage")  # 已用百分比(int)
                        if isinstance(pct, (int, float)):
                            rem_pct = 100.0 - float(pct)
                        else:
                            total = float(lim.get("usage") or 0)
                            rem = lim.get("remaining")
                            if rem is not None and total > 0:
                                rem_pct = float(rem) / total * 100.0
                            else:
                                cur = lim.get("currentValue")
                                if cur is not None and total > 0:
                                    rem_pct = 100.0 - float(cur) / total * 100.0
                        if rem_pct is not None:
                            windows.append((float(lim.get("nextResetTime") or 0),
                                            max(0.0, min(100.0, rem_pct))))
                    if not windows:
                        return None
                    windows.sort()  # nextResetTime 升序;最晚重置 = 周窗
                    return windows[-1][1]
                elif provider.id == "qwen":
                    # 通义 DashScope 余额单位为元;字段缺失(API 变化)→ None 不编 0
                    b = data.get("balance")
                    return float(b) if b is not None else None
                elif provider.id == "stepfun":
                    # 官方 GET /v1/accounts → {balance, total_cash_balance, total_voucher_balance}
                    b = data.get("balance")
                    return float(b) if b is not None else None
                elif provider.id == "siliconflow":
                    # GET /v1/user/info → data.balance(现金余额,元)/totalBalance。
                    # 配置了 balance_endpoint 但此前无解析分支——GET 白发、
                    # 余额恒 None(0 余额 key 永远拿不到"0 的确证")。
                    d = data.get("data") or {}
                    raw = d.get("balance")
                    if raw is None:
                        raw = d.get("totalBalance")
                    if raw is None:
                        return None  # 形状变化 → None,不编造
                    return float(raw)
                elif provider.id == "minimax_cp":
                    # coding_plan/remains 官方 schema 未完整公开——只认最直接的
                    # {"data":{"remains":<num>}} / {"remains":<num>} / data 为数值
                    # 三种形态,其余返回 None(宁缺勿错)。
                    d = data.get("data")
                    if isinstance(d, (int, float)):
                        return float(d)
                    if isinstance(d, dict) and isinstance(d.get("remains"), (int, float)):
                        return float(d["remains"])
                    if isinstance(data.get("remains"), (int, float)):
                        return float(data["remains"])
                    return None
                elif provider.id == "openrouter":
                    # GET /auth/key → data.usage 已用(USD) / data.limit 总额度(null=无上限)
                    # 剩余 = limit - usage;limit 为 null(无上限)时无法折算,返回 None
                    d = data.get("data", {})
                    limit = d.get("limit")
                    if limit is None:
                        return None
                    return float(limit) - float(d.get("usage") or 0)
        except Exception:
            pass

        return None

    def _probe_chat(self, key: str, provider: AIProvider) -> tuple | None:
        """OpenAI 兼容 chat/completions 最小探测（显式 opt-in 后才会调用）。

        返回 (status, balance, message)；网络/超时等无法判定时返回 None。
        每家主流平台基本都有 OpenAI 兼容 /chat/completions 端口——
        一次 max_tokens=1 的请求响应即 key 有效性的直接证据。
        """
        if not provider.verify_model:
            return None
        endpoint = provider.verify_endpoint or "/chat/completions"
        url = f"{provider.api_base}{endpoint}"
        payload = {
            "model": provider.verify_model,
            "messages": [{"role": "user", "content": provider.verify_prompt or "Hi"}],
            "max_tokens": 1,
        }
        headers = self._build_auth_headers(provider, key)
        try:
            resp = self._http_post(url, json=payload, headers=headers,
                                 proxies=self._proxies_for(provider), timeout=30)
        except Exception:
            return None

        if resp.status_code == 200:
            return VerifyResult.VALID_ACTIVE.value, None, "chat 探测：有回复"
        if resp.status_code == 401:
            return VerifyResult.INVALID.value, None, "chat 探测：认证失败"
        if resp.status_code == 402:
            return VerifyResult.VALID_ZERO.value, 0.0, "chat 探测：欠费但有效"
        if resp.status_code == 429:
            # 429 分两种：欠费/账户暂停（key 有效但不可用 → VALID_ZERO）vs 纯限流
            # （可恢复 → RATE_LIMITED）。实测 kimi 欠费 key：429 +
            # "suspended due to insufficient balance, please recharge your account
            # or check your plan and billing details"（available_balance 却显示 240）。
            body = (resp.text or "")[:1000].lower()
            _DEBT_MARKERS = (
                "insufficient balance", "insufficient_balance", "suspended",
                "please recharge", "欠费", "余额不足", "exceeded_current_quota",
                "out of quota", "billing", "payment required",
            )
            if any(m in body for m in _DEBT_MARKERS):
                return VerifyResult.VALID_ZERO.value, 0.0, "chat 探测：欠费/账户暂停"
            return VerifyResult.RATE_LIMITED.value, None, "chat 探测：被限速"
        if resp.status_code in (400, 404, 422):
            # 400/404/422：端点/模型名不符，但认证头已被接受（否则是 401）。
            # 视为认证通过——key 本身有效。
            # v2.5.1: nvidia 例外——活体实测其 400/404 **先于鉴权**(任意 model
            # 路径 404/缺字段 400),不能当"认证已过";模型路由被摘时会集体假
            # valid。返回 None → ERROR 走重验(可告警,不误杀)。
            # v2.5.4: models_unauthenticated 平台(modelscope/longcat)与 nvidia
            # 同类——/models 公开说明鉴权时序未经证实,模型校验/端点改版完全
            # 可能先于鉴权返回 4xx。此前只特判 nvidia,同类的 modelscope/longcat
            # 仍走"认证已过"短路,格式正确的假 key 会被批量判 VALID_NO_BALANCE。
            # 统一按 None(未判定→ERROR 重验)处理,不冒假 valid 风险。
            if provider.models_unauthenticated:
                return None
            return VerifyResult.VALID_NO_BALANCE.value, None, f"chat 探测 HTTP {resp.status_code}（认证已过）"
        if resp.status_code == 410:
            # 模型 EOL(如 2026-09-21 nvidia verify_model 下线):410 先于鉴权,
            # 无法判定 key——返回 None 走 ERROR 重验,并大声告警(配置问题,
            # 不告警就是 219 条静默 ERROR 的教训)。
            _logger.warning(
                "chat 探测 410 Gone: provider=%s model=%s 已 EOL,请更新 "
                "verify_model(key 无法判定,走 ERROR 重验)", provider.id,
                provider.verify_model)
            return None
        if resp.status_code == 403:
            # 与 GET 侧 403 策略一致:仅下列平台的 403 是确定性的
            # 鉴权/无权限信号;其余平台 403 可能是 WAF/区域拦截(可恢复),
            # 落到 return None → 上层判 ERROR 走重验,不做永久性误杀。
            # v2.5.1: +nvidia——活体实测假 key 403 body="Authorization failed"、
            # 无鉴权头 401,鉴权失败是确定性的;但按 body 二次区分保 WAF 余地:
            # 明确鉴权失败文 → INVALID,其他 403(真 WAF/区域) → ERROR 可重验。
            if provider.id in ("gemini", "qianfan"):
                return VerifyResult.INVALID.value, None, "chat 探测：无权限"
            if provider.id == "nvidia":
                body403 = (resp.text or "")[:500].lower()
                if "authorization failed" in body403 or "invalid api key" in body403:
                    return VerifyResult.INVALID.value, None, "chat 探测：鉴权失败"
                return None
            return None
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
    def _key_prefix(provider: AIProvider) -> str:
        """从 key_patterns 推断该平台 key 的可搜索前缀（GitHub 查询词用）。

        顺序敏感：特异前缀必须优先于通用 sk-（sk-proj- 若排在 sk- 后会被吞）。
        无前缀平台（智谱 hex.secret / MiniMax JWT / DeepInfra 裸串）返回 ""，
        查询不带 key 词，靠提取正则抓。
        """
        _kp = "".join(provider.key_patterns)
        for marker in ("sk-kimi-", "sk-ant-", "sk-sp-", "sk-cp-",
                       "sk-or-", "sk-proj-"):
            if marker in _kp:
                return marker
        if "tp-" in _kp and "sk-" not in _kp:
            return "tp-"
        for marker in ("gsk_", "r8_", "hf_", "fw_", "jina_", "pa-",
                       "bce-v3", "nvapi-", "xai-", "AIza", "ms-"):
            if marker in _kp:
                return marker
        if "eyJ" in _kp:
            return ""  # JWT 无固定前缀,只用平台词
        if "sk-" in _kp:
            return "sk-"
        return ""

    @staticmethod
    def generate_for_provider(provider: AIProvider, max_queries: int = 30) -> list:
        """为指定 provider 生成丰富搜索查询。
        升级：从原来 ~11 条扩展到覆盖多维度（文件类型×平台域名、环境变量、
        配置文件、时间窗口），参考 deepseek 单平台 239 条策略。"""
        queries = []

        # 1. 直接使用 provider 定义的查询（最精准）
        for q in provider.key_context_queries:
            queries.append(q)

        # 2. 根据 key 格式 + 平台特征生成多维度查询。
        # 前缀按平台 key 格式决定:hex.secret(智谱)/eyJ(MiniMax)/gsk_(Groq) 等
        # 平台硬编码 sk- 会生成永远 0 命中的查询,白烧配额。
        if provider.id not in ("claude",):  # Claude 有特殊 key 格式
            base_domain = ""
            if provider.api_base:
                base_domain = provider.api_base.replace("https://", "").split("/")[0]

            # 该平台 key 前缀:取自 key_patterns(如智谱 hex.secret 无前缀)
            _pfx = QueryGenerator._key_prefix(provider)
            # 无前缀平台(hex.secret 等):查询不带 key 词,靠提取正则抓

            # 2a. 平台域名 × 高价值文件类型
            if base_domain:
                for ft in QueryGenerator.FILE_TYPES[:8]:  # 前8种文件类型
                    queries.append(f"{base_domain} {_pfx} filename:{ft}".strip())

            # 2b. 平台名 + 前缀 × 配置文件关键词
            for kw in QueryGenerator.CONFIG_KEYWORDS[:6]:
                queries.append(f"{provider.name} {_pfx} filename:{kw}".strip())

            # 2c. 环境变量名搜索（多种命名风格）
            env_var_names = [
                f"{provider.id.upper()}_API_KEY",
                f"{provider.id.upper()}_KEY",
                f"{provider.name.upper().replace(' ', '')}_API_KEY",
                f"{provider.name.upper().replace(' ', '')}_KEY",
            ]
            for env_var in env_var_names:
                queries.append(f'{env_var} {_pfx}'.strip())

            # 2d. SDK/客户端调用模式搜索
            for sdk_term in [f"{provider.name} client", f"{provider.name} api_key",
                             f"{provider.name} authorization"]:
                queries.append(f"{sdk_term} {_pfx}".strip())

        # 3. 通用文件类型查询（前缀按平台格式,非硬编码 sk-）
        # 这些查询能覆盖平台名未出现的泄露（key 在通用 .env 里）
        _kp2 = "".join(provider.key_patterns)
        _pfx2 = "sk-" if "sk-" in _kp2 and "eyJ" not in _kp2 else ""
        if "eyJ" in _kp2:
            _pfx2 = ""
        for ft in ["env", "yml", "json", "properties", "config"]:
            queries.append(f"{provider.name} {_pfx2} filename:{ft}".strip())

        # 去重保序
        seen = set()
        unique = []
        for q in queries:
            if q not in seen:
                seen.add(q)
                unique.append(q)
        return unique[:max_queries]

    @staticmethod
    def generate_rolling_for_provider(provider: AIProvider, max_queries: int = 15) -> list:
        """为 provider 生成高产出查询（Code Search 不支持日期过滤，靠 sort=indexed）。
        参考 scanner_engine.generate_rolling_time_queries，应用到多平台。"""
        queries = []
        prefix = "sk-ant-" if provider.id == "claude" else QueryGenerator._key_prefix(provider)

        # 平台特征词（域名或平台名）
        platform_term = ""
        if provider.api_base:
            platform_term = provider.api_base.replace("https://", "").split("/")[0]
        if not platform_term:
            platform_term = provider.name

        queries = [
            f"{platform_term} {prefix}".strip(),
            f"{provider.name} {prefix} filename:env".strip(),
            f"{provider.id.upper()}_API_KEY {prefix}".strip(),
            f"{provider.name} {prefix}".strip(),
        ]
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
    balance: float | None = None
    currency: str = "USD"
    verified_at: str | None = None
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

def get_provider_by_key(key: str) -> AIProvider | None:
    """根据 key 格式猜测 provider"""
    matcher = UnifiedKeyMatcher()
    candidates = matcher.identify_provider(key)
    if candidates:
        return PROVIDER_MAP[candidates[0][0]]
    return None


# is_bad_key_multi_provider / dedup_results 已统一到 scanners.base
# （见文件顶部 import）。两处原本地实现已删除以避免口径分叉。
