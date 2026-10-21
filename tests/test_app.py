"""
tests/test_app.py —— Web 层（app.py）的测试：限流、IP 解析、请求校验。

为什么单独一个文件：
  test_main.py 测的是「摘要内核」（纯逻辑、不碰网络），
  这里测的是「HTTP 壳」——限流、扣费时机、响应字段。

为什么现在才补：
  这两个漏洞上线时都没被发现，因为没有测试盯着它们——
    ① 伪造 X-Forwarded-For 换个假 IP 就能绕过单 IP 限额
    ② 纯空白正文（" "）也能通过校验，白扣一次额度
  都是"手动点一下点不出来、写个用例就露馅"的问题类型。

跑法：pytest tests/test_app.py -v
"""

import os

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

# ★ 这个 import 有副作用：app.py 在模块顶层调 load_dotenv()，会把 .env 里的键
#   塞进 os.environ。如果不还原，同一个 pytest 进程里后面的 test_main 用例
#   会读到 DISABLE_THINKING=1 之类的外部配置，"默认值"类断言就会莫名失败。
#   load_dotenv 只 setdefault、不覆盖，所以只需要删掉它新增的键即可还原。
_ENV_BEFORE = set(os.environ)

import app as app_module  # noqa: E402  （必须在快照之后）

for _k in set(os.environ) - _ENV_BEFORE:
    del os.environ[_k]


# ============================================================
# 测试替身：不碰网络，固定返回两句话
# ============================================================
class FakeSummarizer:
    """站在真 Summarizer 的位置上，但不发任何请求。"""

    def __init__(self, client, max_sentences=3):
        # 和真实现一致：短文本会被收窄，这里固定返回 2 句
        self.sentences_used = min(max_sentences, 2)

    def summarize(self, text: str) -> str:
        return "第一句。第二句。"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """
    每个用例开始前把限流账本清空、把网络调用换掉。

    注意 _global / _swept_on 是模块级 list，用切片赋值原地清空——
    重新绑定会换掉对象，别的模块里 import 到的旧引用就不同步了。
    """
    app_module._usage.clear()
    app_module._global[:] = ["", 0]
    app_module._swept_on[:] = [""]
    monkeypatch.setattr(app_module, "Summarizer", FakeSummarizer)
    # 连 client 一起换掉：否则 get_client() 会去读 API_KEY，没配就抛 503，
    # 这层测试就变成"在测环境配置"而不是"在测限流逻辑"。
    monkeypatch.setattr(app_module, "get_client", lambda: object())
    monkeypatch.setattr(app_module, "TRUST_PROXY", False)
    yield
    app_module._usage.clear()
    app_module._global[:] = ["", 0]
    app_module._swept_on[:] = [""]


@pytest.fixture
def client():
    return TestClient(app_module.app)


def make_request(peer: str, xff: str | None = None) -> Request:
    """手工造一个 Request，只带 client_ip() 需要的那几个 scope 字段。"""
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": headers,
        "client": (peer, 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    })


# ============================================================
# 1) 基础可用性
# ============================================================
def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["max_chars"] == app_module.MAX_CHARS
    # 计数口径要暴露出来，部署时能一眼看到"这是进程内计数"
    assert body["quota_scope"] == "process-memory"


def test_index_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "AI" in r.text


def test_summarize_returns_expected_fields(client):
    r = client.post("/api/summarize", json={"text": "一段用来测试的正文。", "length": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]
    assert body["input_chars"] == len("一段用来测试的正文。")
    # 新增字段：前端靠它显示"今天还剩几次"
    assert body["remaining"] == app_module.MAX_CALLS_PER_DAY - 1


# ============================================================
# 2) 漏洞 ①：空白正文不该扣额度
# ============================================================
def test_blank_text_does_not_consume_quota(client):
    before = client.get("/api/health").json()["used_today_global"]

    r = client.post("/api/summarize", json={"text": "   ", "length": 3})
    assert r.status_code == 400

    after = client.get("/api/health").json()["used_today_global"]
    assert after == before, "空白正文被拒绝了，却还是扣了一次额度"


def test_empty_text_is_rejected_by_schema(client):
    # min_length=1 这一档由 Pydantic 拦截，根本进不到业务代码
    r = client.post("/api/summarize", json={"text": "", "length": 3})
    assert r.status_code == 422


def test_too_long_text_is_rejected(client):
    r = client.post("/api/summarize", json={"text": "字" * (app_module.MAX_CHARS + 1), "length": 3})
    assert r.status_code == 422


# ============================================================
# 3) 漏洞 ②：伪造 X-Forwarded-For 不能绕过单 IP 限额
# ============================================================
def test_forged_xff_cannot_bypass_ip_limit(client):
    """
    以前：每次换一个假 IP → 永远 20 次不封顶（实测 25 次全过）。
    现在：直连来源是 testclient（非内网 IP）→ XFF 一律不采信，
         20 次之后必须 429。
    """
    payload = {"text": "一段用来测试的正文。", "length": 3}
    codes = []
    for i in range(app_module.MAX_CALLS_PER_DAY + 1):
        headers = {"X-Forwarded-For": f"10.0.0.{i}"}
        codes.append(client.post("/api/summarize", json=payload, headers=headers).status_code)

    assert codes[:app_module.MAX_CALLS_PER_DAY] == [200] * app_module.MAX_CALLS_PER_DAY
    assert codes[-1] == 429, "换 IP 仍然能继续用，限流被绕过了"


def test_same_ip_still_gets_blocked(client):
    payload = {"text": "一段用来测试的正文。", "length": 3}
    for _ in range(app_module.MAX_CALLS_PER_DAY):
        assert client.post("/api/summarize", json=payload).status_code == 200
    assert client.post("/api/summarize", json=payload).status_code == 429


# ============================================================
# 4) client_ip()：什么情况下才信 XFF
# ============================================================
def test_public_peer_ignores_xff():
    # 直连来源就是公网 IP → 客户端塞什么 XFF 都不看
    req = make_request("203.0.113.9", xff="1.2.3.4")
    assert app_module.client_ip(req) == "203.0.113.9"


def test_private_peer_uses_last_xff_hop():
    # 前面有反代（来源是内网）→ 取最后一段，左边客户端自己塞的丢弃
    req = make_request("10.1.1.1", xff="1.2.3.4, 198.51.100.7")
    assert app_module.client_ip(req) == "198.51.100.7"


def test_private_peer_without_xff_falls_back_to_peer():
    req = make_request("127.0.0.1")
    assert app_module.client_ip(req) == "127.0.0.1"


def test_trust_proxy_forces_xff(monkeypatch):
    # 代理在另一台机器上时，显式开 TRUST_PROXY
    monkeypatch.setattr(app_module, "TRUST_PROXY", True)
    req = make_request("203.0.113.9", xff="198.51.100.7")
    assert app_module.client_ip(req) == "198.51.100.7"


# ============================================================
# 5) 限流账本本身
# ============================================================
def test_daily_sweep_drops_stale_ips():
    app_module._usage["1.1.1.1"] = ["1999-01-01", 5]
    app_module._usage["2.2.2.2"] = ["1999-01-01", 3]
    app_module.check_quota("3.3.3.3")
    assert "1.1.1.1" not in app_module._usage
    assert "2.2.2.2" not in app_module._usage
    assert "3.3.3.3" in app_module._usage


def test_remaining_counts_down(client):
    payload = {"text": "一段用来测试的正文。", "length": 3}
    first = client.post("/api/summarize", json=payload).json()["remaining"]
    second = client.post("/api/summarize", json=payload).json()["remaining"]
    assert first - second == 1
