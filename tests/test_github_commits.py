"""CommitsScanner 升级测试: since 窗口 + 多平台 key + 轮换 query。"""
from scanners.github_commits import CommitsScanner


def test_key_pattern_multiplatform():
    pat = CommitsScanner.KEY_PATTERN
    assert pat.search("sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456789") is not None
    assert pat.search("sk-kimi-abcdefghijklmnopqrstuvwxyz123456") is not None
    assert pat.search("sk-sp-abcdefghijklmnopqrstuvwxyz123456") is not None
    assert pat.search("sk-proj-abcdefghijklmnopqrstuvwxyz") is not None
    assert pat.search("sk-4677b153277a40b0" "96c6716686c29ac4") is not None  # 纯 sk-
    assert pat.search("ghp_abcdefghijklmnopqrstuvwxyz") is None  # 非 sk- 不应误报


def test_since_param_build():
    s = CommitsScanner(token="t", since_hours=2)
    assert s.since_hours == 2


def test_repo_queries_uses_rotation_term():
    """轮换 query 词进入 repo 搜索;code-search 风格串截断到首词;保底固定查询。

    回归(I3): CommitsScanner.search(query) 曾忽略 query,固定 4 条 deepseek 查询。
    """
    s = CommitsScanner(token="t")
    assert s._repo_queries(None) == ["deepseek in:readme", "deepseek-ai",
                                     "deepseek language:python", "deepseek-api"]
    qs = s._repo_queries("kimi")
    assert qs[0] == "kimi" and len(qs) == 5
    qs2 = s._repo_queries("moonshot sk-kimi-")
    assert qs2[0] == "moonshot"  # code-search 风格串 → 截断到第一个空格前的词
    qs3 = s._repo_queries("filename:env deepseek")
    assert "filename:env" not in qs3  # 限定符串跳过 → 保底固定查询
    assert qs3[0] == "deepseek in:readme"
    qs4 = s._repo_queries("sk-")
    assert qs4[0] == "sk-" and len(qs4) == 5


def test_since_url_param_built():
    """since_hours>0 时 commits 请求 URL 带 since ISO 时间参数。"""
    import asyncio
    s = CommitsScanner(token="t", since_hours=2)
    captured = {}

    class FakeResp:
        status = 200

        async def json(self):
            return []  # 空提交列表 → 不再发 diff 请求

    class FakeCtx:
        """支持 `async with session.get(...) as resp` 协议的返回对象。"""

        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def get(self, url, **kw):  # 同步返回 ctx(模拟 aiohttp session.get 的上下文管理器)
            captured["url"] = url
            return FakeCtx(FakeResp())

    sem = asyncio.Semaphore(4)
    asyncio.run(s._scan_repo_commits(FakeSession(), sem, "owner/repo"))
    assert captured["url"].startswith(
        "https://api.github.com/repos/owner/repo/commits?per_page=30&since=")
    assert "since=" in captured["url"]


def test_registry_github_commits_alias_with_since():
    """registry 别名 github_commits 同款 CommitsScanner,且带 since_hours 窗口。

    回归(I3): 别名曾无 since_hours → watch 模式新鲜度窗口失效(全量历史扫描)。
    """
    from config_loader import config
    from scanner_engine import ScannerEngine
    engine = object.__new__(ScannerEngine)
    engine.proxy = None
    reg = engine._get_scanner_registry()
    assert reg["github_commits"][0] is reg["commits"][0]  # 同款 scanner 类
    assert reg["github_commits"][2]["since_hours"] == config.watch_commits_since_hours
    assert "since_hours" not in reg["commits"][2]  # 一次性 commits 源不带窗口(全量)
    # engine 显式覆盖优先(CLI --commits-since-hours)
    engine.commits_since_hours = 5
    reg2 = engine._get_scanner_registry()
    assert reg2["github_commits"][2]["since_hours"] == 5


def test_since_uses_z_suffix_no_plus():
    """v2.5.4: since 的 isoformat "+00:00" 中 + 会被服务端按 form 规则
    解码成空格——时间窗失效或 422。必须用 ...Z 后缀。"""
    import asyncio

    captured = []

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def json(self):
            return []

    class _Sess:
        def get(self, url, **kw):
            captured.append(url)
            return _Resp()

    s = CommitsScanner(since_hours=168)
    asyncio.run(s._scan_repo_commits(_Sess(), asyncio.Semaphore(1), "a/b"))
    assert captured, "应发出请求"
    since_value = captured[0].split("since=")[1]
    assert "+" not in since_value, "since 值不得含未编码的 +"
    assert since_value.endswith("Z")
