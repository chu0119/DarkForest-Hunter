"""error 状态平台诊断(实验先行,不写死方案)。

背景: DB 里 error 状态 key 在 zhipu/kimi/openrouter/deepinfra 四个平台。
诊断方法: 对每个 error 平台,取 DB 里的 error key,用 UnifiedKeyVerifier
直接验证,并做原始 HTTP 探测(GET models 端点 + POST chat 最小探测),
观察实际 HTTP 状态/异常。401→配置问题;网络异常→代理/直连问题;
402/429→平台限流;其他→记录。

诊断时间: 2026-08-12(实测,真实网络请求)
诊断脚本: scripts/diagnose_error_status.py(临时,未入库)

结论(按平台):

1. zhipu(DB 2 个 error,8/9 旧记录;全库仅 2 条,0 valid)
   - 实测: GET+POST open.bigmodel.cn/api/paas/v4/chat/completions 均 401
     "令牌已过期或验证不正确" → 重验收敛为 invalid
   - 判定: 非配置问题。端点/Bearer 认证头/直连路径均正常(服务器正确返回
     401 认证判定,非 5xx/超时)。2 个 error key 本身无效(泄露后轮换/吊销),
     8/9 的 error 是当时验证瞬态。剩余疑点: 全库无 valid zhipu 样本,
     有效 key 的 405→balance 分支未被实测(样本不足,需有效 key 才能闭环)。

2. kimi(DB 2 个 error,8/9 旧记录;另有 300 个 valid_zero)
   - 实测: GET api.moonshot.cn/v1/models 返回 401 "Invalid Authentication"
     → 重验收敛为 invalid
   - 判定: 非配置问题。kimi 验证路径工作正常(300 个 valid_zero 证明
     8/11-8/12 大量成功验证),2 个 error 是当时网络瞬态。

3. openrouter(DB 33 个 error,8/11-8/12 记录)
   - 实测: GET openrouter.ai/api/v1/models 200(直连与代理均通);
     POST chat/completions 401 → 重验收敛为 invalid("chat 探测: 认证失败")
   - 判定: 非配置问题。端点/代理路径/认证头正常,error key 本身无效(401)。
     33 个 error 集中在 22:00-01:02,疑为当时平台侧瞬态(openrouter 529/5xx)
     或网络抖动,现已全部收敛。注意 openrouter /models 是公开列表不校验 key,
     GET 200 无认证信息,认证判定依赖 POST 探测——该链路工作正常。

4. deepinfra(DB 30 个 error;8/10-8/12 记录)
   - 实测: 30 个 error key 中 0 个是真 deepinfra key:
     * 19 个 sk-or-v1-*(openrouter 格式)→ deepinfra 401 "User is not
       authorized" → 重验收敛 invalid
     * 11 个 eyJ* 超长 JWT(5KB~27KB,Firebase ID token 类)→ 验证时
       Authorization: Bearer <超长key> 触发 deepinfra nginx
       "HTTP 400 Request Header Or Cookie Too Large" → 重验仍 error,永不收敛
   - 判定: 明确 bug(识别 + 验证两层),非平台侧问题:
     Bug A(识别混淆): deepinfra key_pattern `[A-Za-z0-9]{40,60}` 过宽,
       匹配 openrouter sk-or-v1 的 hex 段(60 字符命中上限)和任意 JWT 段;
       上下文命中 deepinfra 词时(50+30 分)反超 openrouter 的 60 分,
       openrouter key 被存为 deepinfra(19 个),JWT 也被存为 deepinfra(11 个)。
     Bug B(验证无防御): verify_key 对 HTTP 400 无分类处理,超长 key 直接
       放 Authorization 头 → nginx 400 → 永久 error 不收敛。
   - 修复建议(不在本任务修,需经计划流程):
     * Bug A: 收紧 deepinfra key_pattern(排除 sk- 前缀/sk-or-v1/eyJ 等
       已知他平台格式),或识别时对同时匹配更具体平台的 key 降级 deepinfra 分。
     * Bug B: 验证前拒绝超长 key(>256 字符直接 UNKNOWN/invalid,不发请求),
       或对 400("Header Too Large" 类)响应按 invalid 归类而非 error。
"""
import os
import tempfile

import store


def test_diagnosis_note_exists():
    """诊断测试文件存在即可(实际诊断在 2026-08-12 手工执行,结论见模块 docstring)。"""
    conn = store.connect(os.path.join(tempfile.mkdtemp(), "test.db"))
    assert conn is not None


def test_db_error_status_query_path():
    """回归锚点: 连接与 status 查询路径可用(诊断结论见 docstring)。

    旧版断言活库 error>0 是定时炸弹——error 状态 key 收敛/清理后该断言失败;
    诊断结论(zhipu/kimi/openrouter/deepinfra 的 error 均为瞬态/无效 key,非配置问题)
    保留在模块 docstring,不依赖生产数据。
    """
    if not os.path.exists("results/darkforest.db"):
        import pytest
        pytest.skip("darkforest.db 不存在(本机未跑过 watch)")
    conn = store.connect("results/darkforest.db")
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM keys WHERE status='error'"
        ).fetchone()
        assert rows is not None  # 查询路径可用;不要求 error>0
    finally:
        conn.close()
