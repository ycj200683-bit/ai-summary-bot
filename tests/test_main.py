"""
pytest 测试 —— 项目① 大模型文本摘要器（W7）

为什么测试里"不能真的调 API"：
  1. 每次调用都花钱、都联网，跑 20 次测试就烧 20 次
  2. 网络会抖，测试结果就不稳定（这次过下次不过）
  3. 想测"429 被限流"这种场景，总不能真去把对方惹毛

所以核心手法是 monkeypatch（猴子补丁）：把 requests.post 临时换成一个假的函数，
      由我们自己决定它返回什么，从而随心所欲地造出 200 / 401 / 429 各种情况。

本文件里有：
  ✅ 4 个 W7 用例（正常返回 / 401 不重试 / 429 重试成功 / prompt 拼装）
  ✅ v0.5 新增：配置解析用例（环境变量、默认值、URL 归一化）
"""

import pytest

import main          # 被测对象：项目根目录的 main.py
from main import (
    Config,
    LLMClient,
    Summarizer,
    RetryableError,
    FatalError,
    normalize_api_url,
)


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
# 练习题 2（俊已独立完成 ✅）：前两次 429，第三次 200 → 应重试成功，共调用 3 次
#
#   新增的两个套路（值得记住）：
#     ① fake_post 要"看情况返回不同结果"（前两次 429，第三次 200）
#     ② 必须把 time.sleep 换掉 —— 否则测试会真的干等 1 + 2 = 3 秒
#       （注：那次下划线占位符把你坑了，正确写法是起个名字叫 fake_sleep）
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


# ============================================================
# v0.5 新增：配置解析测试
#
#   为什么这段配置代码特别需要测试？
#     Config.from_env() 读的是【环境变量】—— 也就是"代码之外的状态"。
#     这类代码最容易出 bug：key 没设、变量名打错、厂商给的 URL 少一段路径……
#     而且这些 bug 在你本机跑得好好的（因为你本机设了），一到别人电脑上就炸。
#     所以必须靠测试 pretend 出各种环境，把每条分支都走一遍。
#
#   新工具：monkeypatch.setenv("名", "值") / monkeypatch.delenv("名")
#     —— 临时改环境变量，测试结束自动还原（和替换 requests.post 是同一招）
# ============================================================

@pytest.fixture
def clean_env(monkeypatch):
    """
    把所有相关环境变量先清空，保证测试结果只取决于本用例自己设了什么，
    不受"本机恰好设过 DEEPSEEK_API_KEY"这类外部因素干扰。

    raising=False 的意思是：变量本来就不存在时也别报错（我们就是要它不存在）。
    """
    for name in ("API_KEY", "API_BASE_URL", "MODEL_NAME", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch          # 返回出去，用例里接着用它 setenv


# ------------------------------------------------------------
# URL 归一化：纯函数，最好测 —— 给输入、断言输出，没有任何副作用
# ------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        # 只给 base → 自动补上 /chat/completions
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/chat/completions"),
        ("http://localhost:11434/v1", "http://localhost:11434/v1/chat/completions"),
        # 已经是完整 URL → 不能重复拼成 .../chat/completions/chat/completions
        (
            "https://api.deepseek.com/v1/chat/completions",
            "https://api.deepseek.com/v1/chat/completions",
        ),
        # 结尾多打一个斜杠（最常见的复制粘贴残留）→ 也要处理干净
        ("https://api.deepseek.com/v1/", "https://api.deepseek.com/v1/chat/completions"),
        # 前后不小心带上空格 → strip 掉
        ("  https://api.openai.com/v1  ", "https://api.openai.com/v1/chat/completions"),
    ],
)
def test_normalize_api_url(raw, expected):
    assert normalize_api_url(raw) == expected


# ------------------------------------------------------------
# 分支 1：什么都不设，只有 key → 走 DeepSeek 默认值
# ------------------------------------------------------------
def test_from_env_默认走_deepseek(clean_env):
    clean_env.setenv("DEEPSEEK_API_KEY", "sk-legacy")      # 故意用【旧】变量名

    cfg = Config.from_env()

    assert cfg.api_key == "sk-legacy"                      # 旧名字仍然认
    assert cfg.api_url == "https://api.deepseek.com/v1/chat/completions"
    assert cfg.model == "deepseek-chat"


# ------------------------------------------------------------
# 分支 2：设了新的三个变量 → 完整切到另一家厂商（这就是 v0.5 的核心卖点）
# ------------------------------------------------------------
def test_from_env_可切到其他厂商(clean_env):
    clean_env.setenv("API_KEY", "sk-new")
    clean_env.setenv("API_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    clean_env.setenv("MODEL_NAME", "qwen-plus")

    cfg = Config.from_env()

    assert cfg.api_key == "sk-new"
    # 注意：这里只给了 base，断言的是【补全之后】的完整地址
    assert cfg.api_url == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )
    assert cfg.model == "qwen-plus"
    # 没碰的字段仍用默认值（@dataclass 的好处）
    assert cfg.temperature == 0.3
    assert cfg.timeout == 30


# ------------------------------------------------------------
# 分支 3：新旧变量名同时存在 → 新的赢（不然改了没生效会很迷惑）
# ------------------------------------------------------------
def test_from_env_新变量名优先于旧变量名(clean_env):
    clean_env.setenv("DEEPSEEK_API_KEY", "旧key")
    clean_env.setenv("API_KEY", "新key")

    assert Config.from_env().api_key == "新key"


# ------------------------------------------------------------
# 分支 4：一个 key 都没有 → 必须直接退出，而不是带着空 key 去发请求
#   （带着空 key 也不是不行，但会拿到 401，还白烧一次重试等待，不如早死早超生）
# ------------------------------------------------------------
def test_from_env_没key就退出(clean_env):
    with pytest.raises(SystemExit):
        Config.from_env()


# ------------------------------------------------------------
# 分支 5：环境变量被设成【空字符串】→ 也要回退到默认值
#   这就是 from_env 里用 `os.getenv(...) or 默认值` 而不是
#   `os.getenv(名, 默认值)` 的原因：后者在"变量存在但为空串"时会返回空串。
#   在 Windows 上 `$env:API_BASE_URL=""` 恰好就是这个行为，很容易踩。
# ------------------------------------------------------------
def test_from_env_空字符串也回退默认值(clean_env):
    clean_env.setenv("API_KEY", "sk-x")
    clean_env.setenv("API_BASE_URL", "")
    clean_env.setenv("MODEL_NAME", "")

    cfg = Config.from_env()

    assert cfg.api_url == "https://api.deepseek.com/v1/chat/completions"
    assert cfg.model == "deepseek-chat"


# ------------------------------------------------------------
# 404 的报错信息必须带上"去检查哪个变量"
#
#   这条测试的由来很有意思：真的去跑"只换端点、忘换模型名"时，
#   百炼回了 model_not_found，而原来的报错只说"404 请求有问题"，
#   人根本不知道是自己配置错位了。所以把提示写进代码，再用测试钉住它。
#
#   另外这也验证了错误分级：404 属于 FatalError（重试没用），不是 RetryableError。
# ------------------------------------------------------------
def test_chat_404_报错提示要指明该检查配置(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return FakeResponse(404, text='{"error":{"code":"model_not_found"}}')

    monkeypatch.setattr(main.requests, "post", fake_post)

    with pytest.raises(FatalError) as exc_info:
        make_client().chat("系统提示", "用户提示")

    # str(e) 拿到异常对象的文字说明；要提醒用户去查这两个变量
    message = str(exc_info.value)
    assert "API_BASE_URL" in message
    assert "MODEL_NAME" in message


# ============================================================
# 句数校验（v1.3）：提示词要求"正好 N 句"，模型未必照做
# ============================================================
def test_count_sentences_中英文标点都算一句():
    assert main.count_sentences("甲。乙！丙？丁!") == 4
    assert main.count_sentences("这句话没有标点") == 0
    assert main.count_sentences("") == 0


def test_summarize_句数不符时只重写一次():
    class FakeClient:
        """第一次故意给 3 句（要求 5 句），第二次给对。"""

        def __init__(self):
            self.calls = 0

        def chat(self, system, user):
            self.calls += 1
            if self.calls == 1:
                return "第一句。第二句。第三句。"
            return "一。二。三。四。五。"

    fake = FakeClient()
    bot = Summarizer(fake, max_sentences=5)

    # 用 2000 字的长文本：句数上限才是 5（短文本会被收窄，测不到"要 5 句"这条路）
    assert bot.summarize("字" * 2000) == "一。二。三。四。五。"
    assert fake.calls == 2          # 只补一次，不会无限重试烧额度


def test_summarize_句数正确时不重复调用():
    class FakeClient:
        def __init__(self):
            self.calls = 0

        def chat(self, system, user):
            self.calls += 1
            return "一。二。三。四。五。"

    fake = FakeClient()
    bot = Summarizer(fake, max_sentences=5)

    bot.summarize("字" * 2000)
    assert fake.calls == 1          # 一次就够，不做多余请求


# ============================================================
# 句数自适应（v1.4）：短文本硬凑 5 句会把一句劈成两句
# ============================================================
def test_max_reasonable_sentences_按字数分档():
    assert main.max_reasonable_sentences(69) == 2
    assert main.max_reasonable_sentences(199) == 2
    assert main.max_reasonable_sentences(200) == 3
    assert main.max_reasonable_sentences(600) == 4
    assert main.max_reasonable_sentences(1500) == 5
    assert main.max_reasonable_sentences(99999) == 5


def test_summarize_短文本自动收窄句数并给出提示():
    class FakeClient:
        def __init__(self):
            self.calls = 0
            self.system = ""

        def chat(self, system, user):
            self.calls += 1
            self.system = system
            return "一。二。"

    fake = FakeClient()
    bot = Summarizer(fake, max_sentences=5)

    bot.summarize("很短的一段话。")      # 7 字 → 上限 2 句

    assert bot.sentences_used == 2       # 实际只用 2 句
    assert "正好 2 句" in fake.system     # 提示词跟着收窄，不会再去凑 5 句
    assert fake.calls == 1               # 第一次就符合，不触发重写


# ============================================================
# 关思考开关（v1.4）：摘要是"提炼"不是"推理"，让模型先想一大段纯属白等
#   实测同一篇 2320 字文章、同样要 5 句：
#     开思考 15.9 s（reasoning_tokens 1274） / 关思考 2.2 s（无推理）
# ============================================================
def test_from_env_关思考开关默认关(clean_env):
    clean_env.setenv("API_KEY", "sk-test")
    assert Config.from_env().disable_thinking is False


def test_from_env_关思考开关认多种写法(clean_env):
    clean_env.setenv("API_KEY", "sk-test")
    for value in ("1", "true", "TRUE", "yes", "on"):
        clean_env.setenv("DISABLE_THINKING", value)
        assert Config.from_env().disable_thinking is True, value

    # 这些不该被当成"开"——尤其 "false" 是最容易踩的坑
    for value in ("0", "false", "", "no"):
        clean_env.setenv("DISABLE_THINKING", value)
        assert Config.from_env().disable_thinking is False, value


def test_chat_关思考时才带_enable_thinking_字段(clean_env, monkeypatch):
    """不开开关就别往请求里塞 enable_thinking——有的服务商见到陌生字段直接 400。"""
    seen = {}

    class FakeResponse:
        status_code = 200
        text = '{"choices":[{"message":{"content":"摘要"}}]}'

        def json(self):
            return {"choices": [{"message": {"content": "摘要"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["payload"] = json
        return FakeResponse()

    monkeypatch.setattr(main.requests, "post", fake_post)

    # 关思考 → 带字段
    LLMClient(Config(api_key="k", disable_thinking=True)).chat("sys", "user")
    assert seen["payload"]["enable_thinking"] is False

    # 默认 → 不带字段
    LLMClient(Config(api_key="k")).chat("sys", "user")
    assert "enable_thinking" not in seen["payload"]
