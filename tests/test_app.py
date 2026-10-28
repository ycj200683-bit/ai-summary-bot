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
import json
from array import array

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

    # 流式链路要用的两个方法。替身也得有，否则 /api/summarize/stream
    # 一跑就 AttributeError——那是测试替身不完整，不是被测代码有问题。
    def summarize_stream(self, text: str):
        yield "第一句。"
        yield "第二句。"

    def finalize_summary(self, raw: str) -> str:
        return raw.strip()


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


def test_private_peer_skips_trailing_proxy_hop():
    """
    Render / 部分 nginx 会把「代理自己的内网地址」追加在 XFF 末尾。
    直接取最后一段 → 所有用户共用一个 IP → 20 次/天被几十个人分完。
    正确做法：从右往左跳过内网地址，取第一个公网地址。
    """
    req = make_request("10.1.1.1", xff="203.0.113.5, 10.1.1.1")
    assert app_module.client_ip(req) == "203.0.113.5"


def test_private_peer_skips_multiple_proxy_hops():
    # 多级代理：客户端 + 两层内网代理，仍要拿到最前面的公网地址
    req = make_request("127.0.0.1", xff="203.0.113.5, 10.0.0.2, 172.16.0.9")
    assert app_module.client_ip(req) == "203.0.113.5"


def test_all_internal_xff_falls_back_to_rightmost():
    # 整条链路都是内网（本机调试 / 容器互访）→ 退回最右一跳，至少能区分来源
    req = make_request("127.0.0.1", xff="10.0.0.2, 192.168.1.7")
    assert app_module.client_ip(req) == "192.168.1.7"


def test_non_ip_xff_is_ignored():
    # 伪造的不是 IP 字面量时不能被当成"公网地址"返回
    req = make_request("10.1.1.1", xff="not-an-ip, 203.0.113.5")
    assert app_module.client_ip(req) == "203.0.113.5"


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


# ============================================================
# 6) 知识库：扣费时机与入参
# ============================================================
@pytest.fixture
def kb_env(tmp_path, monkeypatch):
    """
    把知识库接到临时文件上，并把 embedding 换成假的——不联网、不花钱、结果稳定。

    ★ 为什么连 connect_kb 一起换：
      真实 connect() 会写项目下的 data/kb.sqlite3，跑测试会污染开发数据，
      也没法在 CI 里并行。
    ★ 为什么返回的是一个"每次新建连接"的工厂，而不是复用同一个连接：
      FastAPI 的同步端点跑在线程池里，sqlite 默认禁止跨线程使用同一个连接
      （sqlite3.ProgrammingError）。生产代码本来就是"一个请求一个连接"，
      测试也应该照着来，而不是给连接加 check_same_thread=False 把这个差异掩盖掉。
    ★ 为什么不换 kb.search / add_document：那是本项目自己的逻辑，正是要测的部分；
      只把"外部世界"（网络、文件位置）挡掉。
    """
    import kb

    db_file = tmp_path / "kb_test.sqlite3"
    kb.connect(db_file).close()                     # 先建表
    monkeypatch.setattr(app_module, "connect_kb", lambda: kb.connect(db_file))

    def fake_embed(texts, config=None, **kwargs):
        # 固定的 4 维假向量：不联网，也不影响"有没有扣额度"这件事
        return [array("f", [0.5, 0.5, 0.5, 0.5]) for _ in texts]

    monkeypatch.setattr(kb, "embed_texts", fake_embed)
    yield db_file


def test_duplicate_upload_does_not_consume_quota(client, kb_env):
    """
    回归测试：内容相同的文档重复上传，不该扣额度。

    ★ 为什么专门测这一条：
      最初的实现是"先 check_quota、再查重"，于是重复上传照样扣一次额度，
      而前端提示写的是"没有重复消耗额度"——功能和文案对不上，
      对用户来说就是"说一套做一套"。这种 bug 不会报错，只会悄悄多扣钱，
      只能靠测试盯住。
    """
    before = client.get("/api/health").json()["used_today_global"]

    first = client.post("/api/kb/upload", json={"name": "青海湖.txt", "text": "青海湖是最大的内陆咸水湖。"})
    assert first.status_code == 200
    assert first.json()["skipped"] is False
    mid = client.get("/api/health").json()["used_today_global"]
    assert mid == before + 1, "首次上传应该扣一次额度"

    # 换个文件名、内容一模一样：靠内容 sha256 判定，照样算重复
    second = client.post("/api/kb/upload", json={"name": "换个名字.txt", "text": "青海湖是最大的内陆咸水湖。"})
    assert second.status_code == 200
    body = second.json()
    assert body["skipped"] is True
    assert body["id"] == first.json()["id"], "重复内容应该指回原来的 doc_id"
    assert body["remaining"] == -1, "未扣额度时 remaining 要明确返回 -1"

    after = client.get("/api/health").json()["used_today_global"]
    assert after == mid, "重复上传又扣了一次额度"


def test_kb_search_topk_is_validated(client, kb_env):
    # top_k 上限 10，越界由 Pydantic 拦在业务代码之外
    r = client.post("/api/kb/search", json={"query": "任意问题", "top_k": 99})
    assert r.status_code == 422


def test_kb_delete_missing_doc_returns_404(client, kb_env):
    r = client.delete("/api/kb/docs/999999")
    assert r.status_code == 404
    assert "999999" in r.json()["detail"]


def test_kb_upload_empty_text_is_rejected(client, kb_env):
    r = client.post("/api/kb/upload", json={"name": "空文档.txt", "text": ""})
    assert r.status_code == 422


# ============================================================
# 7) 流式接口：事件格式、错误前置、扣费时机
# ============================================================
def _sse_events(resp_text: str) -> list[dict]:
    """把 SSE 响应体拆成事件列表，方便断言。"""
    out = []
    for line in resp_text.splitlines():
        if line.startswith("data:"):
            out.append(json.loads(line[5:].strip()))
    return out


def test_stream_接口返回_sse_并能逐个事件读出(client):
    r = client.post("/api/summarize/stream", json={"text": "一段用来测试的正文。", "length": 3})

    assert r.status_code == 200
    # 媒体类型必须是 text/event-stream，否则浏览器不会按流处理
    assert r.headers["content-type"].startswith("text/event-stream")
    # 反代缓冲会让"流式"名存实亡，这个头必须带上
    assert r.headers.get("x-accel-buffering") == "no"

    events = _sse_events(r.text)
    deltas = [e["delta"] for e in events if "delta" in e]

    # 替身吐两段，就应该收到两个 delta 事件，而不是攒成一段
    assert deltas == ["第一句。", "第二句。"]
    assert "".join(deltas) == "第一句。第二句。"

    done = events[-1]
    assert done["done"] is True, "最后一个事件必须是收尾汇总"
    assert done["summary"] == "第一句。第二句。"
    for key in ("latency_ms", "input_chars", "model", "remaining", "sentences_used"):
        assert key in done, f"收尾事件缺字段：{key}"


def test_stream_空白正文返回_400_且不扣额度(client):
    """
    流式最容易踩的坑：错误发生在开始推流之后就改不了状态码。
    所以校验必须前置。这里验证两件事：状态码仍是 400，且没有白扣额度。
    """
    before = client.get("/api/health").json()["used_today_global"]

    r = client.post("/api/summarize/stream", json={"text": "   ", "length": 3})
    assert r.status_code == 400

    after = client.get("/api/health").json()["used_today_global"]
    assert after == before, "空白正文被拒了，却还是扣了一次额度"


def test_stream_只扣一次额度(client):
    before = client.get("/api/health").json()["used_today_global"]

    r = client.post("/api/summarize/stream", json={"text": "一段用来测试的正文。", "length": 3})
    assert r.status_code == 200

    after = client.get("/api/health").json()["used_today_global"]
    assert after == before + 1, "流式一次请求应该只扣一次额度"


def test_stream_入参校验和同步接口一致(client):
    # 超长正文、越界句数都应该在进业务代码之前被拦掉
    r = client.post("/api/summarize/stream", json={"text": "字" * (app_module.MAX_CHARS + 1), "length": 3})
    assert r.status_code == 422

    r = client.post("/api/summarize/stream", json={"text": "正常正文。", "length": 9})
    assert r.status_code == 422
