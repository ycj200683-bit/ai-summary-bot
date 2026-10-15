"""
pytest 测试 —— 项目① 大模型文本摘要器（W7）

为什么测试里"不能真的调 API"：
  1. 每次调用都花钱、都联网，跑 20 次测试就烧 20 次
  2. 网络会抖，测试结果就不稳定（这次过下次不过）
  3. 想测"429 被限流"这种场景，总不能真去把对方惹毛

所以核心手法是 monkeypatch（猴子补丁）：把 requests.post 临时换成一个假的函数，
      由我们自己决定它返回什么，从而随心所欲地造出 200 / 401 / 429 各种情况。

本文件里有：
  ✅ 2 个已经写好的示例（读它们，学会套路）
  ⬜ 2 个 TODO 待你补（跑 pytest 会显示为 skipped）
"""

import pytest

import main          # 被测对象：项目根目录的 main.py
from main import Config, LLMClient, Summarizer, RetryableError, FatalError


# ============================================================
# 工具箱：一个"假的响应对象"
#   requests.post 正常会返回 requests.Response，它需要 .status_code / .json() / .text
#   我们自己造一个只有这三样东西的假货，就够用了
# ============================================================
class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


def ok_payload(content: str = "这是一条摘要。") -> dict:
    """拼出 DeepSeek 正常返回时的 JSON 结构（照抄真实响应的形状）。"""
    return {"choices": [{"message": {"content": content}}]}


def make_client() -> LLMClient:
    """造一个不连网的客户端：key 是假的，因为我们根本不发真请求。"""
    return LLMClient(Config(api_key="test-key-123"))


# ============================================================
# 示例 1：不涉及网络的纯逻辑测试
#   套路 = 准备输入 → 执行 → assert 断言结果
# ============================================================
def test_build_prompt_把句数写进系统提示词():
    bot = Summarizer(make_client(), max_sentences=5)

    system, user = bot._build_prompt("原始文本")

    # 换个 --length 参数，系统提示词里的句数也该跟着变
    assert "5 句" in system
    # 原文要原样出现在用户提示词里
    assert "原始文本" in user


# ============================================================
# 示例 2：给 requests.post 打猴子补丁（最核心的一招）
#   monkeypatch.setattr(对象, "属性名", 替换成什么)
#   → 测试结束后 pytest 会自动还原，不用你手动改回来
# ============================================================
def test_chat_正常返回(monkeypatch):
    captured = {}                        # 用来"偷看"真实代码到底发了什么

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json          # 参数名撞上了 json 模块，但在这个函数内它只是变量
        return FakeResponse(200, ok_payload("摘要内容"))

    monkeypatch.setattr(main.requests, "post", fake_post)

    result = make_client().chat("系统提示", "用户提示")

    # ① 返回的是从 JSON 里抠出来的正文
    assert result == "摘要内容"
    # ② 请求确实发到了正确的地址
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    # ③ key 被正确拼进了请求头
    assert captured["headers"]["Authorization"] == "Bearer test-key-123"
    # ④ model 在请求体里
    assert captured["json"]["model"] == "deepseek-chat"


# ============================================================
# 参考实现 1：401 → 立刻抛 FatalError，且【只调用 1 次】（不许重试）
#
#   逐行读注释，然后照着同样的套路自己写 TODO 2。
# ============================================================
def test_chat_401_不重试(monkeypatch):
    # ① 计数器：用来证明"到底调用了几次"。
    #    为什么用 {"n": 0} 这种字典，而不是 count = 0 ？
    #    → 因为下面内层函数 fake_post 要【修改】外层变量。
    #      如果用 count = 0，fake_post 里写 count += 1 会报 UnboundLocalError
    #      （Python 认为你在内层新建了一个局部变量），除非加 nonlocal count。
    #      字典是"可变对象"，改里面的元素不算重新赋值，所以不用 nonlocal。
    #      —— 这就是 P53「nonlocal 关键字」那一集在解决的问题。
    calls = {"n": 0}

    # ② 假的 requests.post：签名要和真的一致（url 位置参数，其余关键字参数）
    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1                                  # 每被调一次就 +1
        return FakeResponse(401, text="unauthorized")    # 永远返回 401

    # ③ 把真的 requests.post 换掉。测试结束 pytest 会自动换回来。
    monkeypatch.setattr(main.requests, "post", fake_post)

    # ④ pytest.raises(...)：断言"下面这段代码【必须】抛出这个异常"。
    #    如果它没抛、或者抛了别的类型，这条测试就失败。
    #    用 with 是因为 pytest 需要"包住"这段代码来捕获异常。
    with pytest.raises(FatalError):
        make_client().chat("系统提示", "用户提示")

    # ⑤ 关键断言：401 是 FatalError，@retry 只捕 RetryableError，
    #    所以会直接穿透、一次都不重试 → 只调用 1 次。
    assert calls["n"] == 1


# ============================================================
# ⬜ TODO 2（轮到你了）：前两次 429，第三次 200 → 应重试成功，共调用 3 次
#
#   套路和参考实现 1 完全一样，只是多了两个新东西：
#     ① fake_post 要"看情况返回不同结果"（前两次 429，第三次 200）
#     ② 必须把 time.sleep 换掉 —— 否则测试会真的干等 1 + 2 = 3 秒
#
#   照着下面的骨架填空（把 ___ 和 …… 补上），然后删掉 @pytest.mark.skip 那一行。
# ============================================================

def test_chat_429_重试后成功(monkeypatch):
    calls = {"n": 0}
    slept = []                              # 用来记录"每次到底等了多久"

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] <= 2:               # ← 填空：前几次返回 429？
            return FakeResponse(429, text="too many requests")
        return FakeResponse(200, ok_payload("重试后拿到的摘要"))   # ← 之后返回 200，带正常 payload

    def fake_sleep(seconds):
        slept.append(seconds)               # 不真的睡，只记下时长

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.time, "sleep", fake_sleep )   # ← 填空：这里换成 fake_sleep

    # 注意：这次【不需要】pytest.raises，因为最终是成功的、不会抛异常
    result = make_client().chat("系统提示", "用户提示")

    assert result ==  "重试后拿到的摘要"                  # ← 填空：应该返回什么？
    assert calls["n"] == 3                # ← 填空：总共调用几次？
    assert slept == [1.0,2.0]                    # ← 填空：退避等待应该是哪两个数字？
