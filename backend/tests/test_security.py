"""安全回归测试：CORS 收紧、Host 校验、健康检查不泄露本机路径。

背景：这是个跑在用户本机、持有简历和 API Key 的服务。若 CORS 放开为 *，
任意恶意网页都能读取简历、消耗额度、覆盖简历内容。
"""

EXTENSION_ORIGIN = "chrome-extension://" + "a" * 32
EVIL_ORIGIN = "https://evil.example.com"


def test_evil_origin_cannot_read_resume(client):
    """恶意站点读简历：请求本身能到，但不会拿到跨域许可，浏览器侧读不到内容。"""
    response = client.get("/api/resume", headers={"Origin": EVIL_ORIGIN})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_extension_origin_is_allowed(client):
    """扩展页面必须正常拿到跨域许可，否则功能直接坏掉。"""
    response = client.get("/api/resume", headers={"Origin": EXTENSION_ORIGIN})
    assert response.headers.get("access-control-allow-origin") == EXTENSION_ORIGIN


def test_evil_origin_preflight_is_rejected(client):
    """恶意站点连预检都过不了，POST /api/analyze、/api/resume 无从发起。"""
    response = client.options(
        "/api/analyze",
        headers={
            "Origin": EVIL_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_non_loopback_host_is_rejected(client):
    """DNS rebinding 会把域名解析到 127.0.0.1，Host 头仍是攻击者域名，必须拒绝。"""
    response = client.get("/api/health", headers={"Host": "evil.example.com"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Host header"


def test_health_does_not_leak_absolute_path(client):
    """健康检查不能回绝对路径——里面有 Windows 用户名。"""
    data = client.get("/api/health").json()
    assert data["log_file"] == "jobs.jsonl"
    assert "\\" not in data["log_file"]
    assert ":" not in data["log_file"]
