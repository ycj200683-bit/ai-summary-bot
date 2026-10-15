# 测试讲解：`tests/test_main.py` 到底在干什么

> 给俊的 W7 配套讲解。建议左边开 `test_main.py`，右边开这个文件对着看。

---

## 一、先建立心智模型：测试是什么

**你现在是怎么验证代码对不对的？**

改一句 → 跑 `python main.py --file sample.txt` → 肉眼看输出 → "嗯，对了"。

这套流程有三个毛病：

| 毛病 | 具体表现 |
|---|---|
| 累且慢 | 改了 10 次就得手动验 10 次 |
| 会看漏 | 改 A 功能把 B 功能改坏了，你只看 A 的输出，发现不了 |
| 验不了异常分支 | "429 被限流时要重试"这个逻辑，你没法手动造出来（总不能让 DeepSeek 真限流你） |

**测试就是把这句"嗯，对了"写成代码。**

```python
assert result == "摘要内容"      # 这一行就是"嗯，对了"的代码版
```

一旦写成代码，它就变成了**可重复执行、不会看漏、能造任意场景**的验证。

---

## 二、pytest 的工作规则（只有 3 条）

pytest 没有魔法，它只认三件事：

1. **文件名** 以 `test_` 开头（所以叫 `test_main.py`）
2. **函数名** 以 `test_` 开头（所以叫 `test_chat_正常返回`）
3. 函数体里有 `assert`

满足这三条，pytest 就自动把它收集起来、逐个执行。

执行结果的含义：

| 输出 | 含义 |
|---|---|
| `PASSED` | 所有 assert 都成立 |
| `FAILED` | 有某个 assert 不成立（pytest 会指出是第几行、期望什么、实际什么） |
| `SKIPPED` | 被 `@pytest.mark.skip(...)` 主动跳过了 |
| `ERROR` | 测试跑之前就崩了（比如 import 失败） |

**你现在跑 `python -m pytest -v` 看到的就是**：4 个用例被收集，3 个 PASSED，1 个 SKIPPED。

---

## 三、这个文件的 5 层结构

| 行号 | 内容 | 作用 |
|---|---|---|
| 17–20 | `import pytest` / `import main` | 把被测代码（`main.py`）拿进来 |
| 28–45 | **工具箱**：`FakeResponse` / `ok_payload` / `make_client` | 造"假货"，供所有用例复用 |
| 52–60 | 示例 1：纯逻辑测试 | 最简单的一种，不涉及网络 |
| 68–88 | 示例 2：正常返回 | 学会 `monkeypatch` 这一招 |
| 96–122 | 参考实现：401 不重试 | 已跑通，照它的套路写 TODO 2 |
| 134–156 | **TODO 2（你的作业）** | 填空题，5 个空 |

**读的顺序**：工具箱 → 示例 1 → 示例 2 → 参考实现 → 自己填 TODO 2。

---

## 四、三个必须搞懂的概念

### 概念 1：为什么要造 `FakeResponse`？

`main.py` 第 155–179 行用到了 `requests.post(...)` 的返回值的**三个属性**：

```python
resp.status_code      # 第 167 行起，判断 429 / 401 / 402 / 5xx
resp.text             # 第 176 行，错误信息
resp.json()           # 第 179 行，正常时取摘要正文
```

真的 `requests.Response` 对象你手搓不出来（要建立 socket、要有真实的 HTTP 响应）。但**主代码只需要这三个东西**，所以造一个只有这三个的假货就够了：

```python
class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code     # 让主代码能读到状态码
        self._payload = payload or {}
        self.text = text                   # 让主代码能读到错误文本

    def json(self):                        # 让主代码能调 .json()
        return self._payload
```

> 这叫 **"测试替身"（Test Double / Stub）**。核心思想：**只需要实现被用到的那部分**，不需要完整模拟。

### 概念 2：`monkeypatch` 是什么？

"猴子补丁"（monkey patch）—— 在运行时**偷偷把某个东西替换掉**。

```python
monkeypatch.setattr(main.requests, "post", fake_post)
#                   ↑换谁        ↑换它的什么  ↑换成什么
```

这行的意思是：**从此刻起，`main.requests.post` 指向 `fake_post`，而不是真的 `requests.post`。**

为什么必须这么做？三个理由：

1. **不花钱**：跑 20 次测试就真调 20 次 API，烧 20 次钱
2. **结果稳定**：真网络会抖，测试就会"这次过下次不过"（这叫 flaky test，是团队里最讨人厌的东西）
3. **能造异常场景**：想测 429 被限流、401 key 失效 —— 你没法让服务商配合你演这些

**两个细节**：

- 参数名 `monkeypatch` 是 pytest 的**内置 fixture**。你只要在测试函数的参数里写上它，pytest 就自动塞一个进来，测试结束还会**自动把改动还原**，不用你手动改回来。
- 只有 `main.py` 里**通过模块名调用**的地方才能被打补丁。所以主代码写的是 `requests.post(...)`（`import requests` 后用模块名调），而不是 `from requests import post`。**这也是为什么主代码的 import 写法很重要。**

### 概念 3：`pytest.raises` 是什么？

普通的 `assert` 断言"值必须等于什么"。但有些代码**正确的行为就是抛异常**，这时要用：

```python
with pytest.raises(FatalError):
    make_client().chat("系统提示", "用户提示")
```

意思是：**"下面这段代码必须抛出 `FatalError`。"**

- 抛了 → 测试通过 ✅
- 没抛 → 测试失败（说明错误处理没生效） ❌
- 抛了别的类型（比如 `RetryableError`）→ 也失败 ❌

**为什么要用 `with` 包住？** 因为异常一抛出，后面的语句就不执行了。`with` 的作用是"捕获这段代码的异常并检查类型"。所以：

```python
with pytest.raises(FatalError):
    make_client().chat(...)      # 这行会抛异常

assert calls["n"] == 1           # 所以断言必须写在 with 外面
```

---

## 五、`test_chat_401_不重试` 的完整执行链路

这是你最该搞懂的一条链路。测试跑起来时，实际上发生了 5 件事：

```
① pytest 收集到 test_chat_401_不重试
   看到参数有 monkeypatch → 自动注入一个
        ↓
② monkeypatch.setattr 把 requests.post 换成 fake_post
   此刻 main.py 里的 requests.post 已经不是你认识的那个了
        ↓
③ chat() 照常执行到 requests.post(...)
   实际打到了 fake_post → 返回 401 → 计数器变成 1
        ↓
④ chat() 看到 401 → raise FatalError
   @retry 的 wrapper 只 except RetryableError
   → FatalError 不匹配 → 直接向上穿透，一次都不重试
        ↓
⑤ pytest.raises 接住 FatalError → 断言 calls["n"] == 1
   → PASSED（整条链路走完，一次真实网络请求都没发）
```

**第 ④ 步是关键**，回去看 `main.py`：

```python
def retry(max_tries=3, backoff=1.0):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(1, max_tries + 1):
                try:
                    return func(*args, **kwargs)
                except RetryableError as e:      # ← 只捕这一种！
                    ...
        return wrapper
    return decorator
```

`except RetryableError` **只捕 `RetryableError`**。`FatalError` 压根进不了这个分支，所以会直接穿透 —— 这就是"401 不重试"的全部原理。

---

## 六、TODO 2 的 5 个空，怎么推

```python
@pytest.mark.skip(reason="⬜ TODO 2：...")      # ← 记得删掉这一行
def test_chat_429_重试后成功(monkeypatch):
    calls = {"n": 0}
    slept = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] <= ___:                                  # 空 1
            return FakeResponse(429, text="too many requests")
        return FakeResponse(200, ok_payload("重试后拿到的摘要"))

    def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.time, "sleep", ___)                # 空 2

    result = make_client().chat("系统提示", "用户提示")

    assert result == ___                                        # 空 3
    assert calls["n"] == ___                                    # 空 4
    assert slept == ___                                         # 空 5
```

### 空 1：`calls["n"] <= ?`

题目要求"**前两次** 429，第三次 200"。
`calls["n"]` 依次是 1、2、3。要让 1 和 2 走 429 分支，3 走 200 分支。

→ 填 **2**。

### 空 2：`monkeypatch.setattr(main.time, "sleep", ?)`

上面已经定义好了 `fake_sleep` 这个函数（它不真的睡，只把时长记进 `slept`）。
我们要替换成的就是它。

→ 填 **`fake_sleep`**（注意：**不加括号**，是传函数本身，不是调用它）

> 为什么必须换掉 `time.sleep`？因为 `@retry(backoff=1.0)` 真的会 `time.sleep(1)` 和 `time.sleep(2)`，测试会干等 3 秒。测试应该跑得飞快。

### 空 3：`assert result == ?`

`fake_post` 最终返回的是 `FakeResponse(200, ok_payload("重试后拿到的摘要"))`。
而 `ok_payload(content)` 会拼成 `{"choices": [{"message": {"content": content}}]}`。
`chat()` 从里面抠出来的是 `content`。

→ 填 **`"重试后拿到的摘要"`**

### 空 4：`assert calls["n"] == ?`

看 `main.py` 第 133 行：`@retry(max_tries=3, backoff=1.0)`。
`max_tries=3` 意思是**最多执行 3 次（含第一次）**。

链路：第 1 次 429 → 重试；第 2 次 429 → 重试；第 3 次 200 → 成功返回。
总共调用 **3** 次。

→ 填 **3**

### 空 5：`assert slept == ?`

看 `main.py` 第 99 和 109 行：

```python
wait = backoff                          # 初始 1.0
...
time.sleep(wait)                        # 第一次重试前，睡 1.0 秒
wait *= 2                               # 翻倍 → 2.0
...
time.sleep(wait)                        # 第二次重试前，睡 2.0 秒
```

所以 `slept` 里依次记录下来的是 **1.0** 和 **2.0**。

→ 填 **`[1.0, 2.0]`**

这个断言很重要 —— 它验证的不只是"重试了"，而是**"指数退避真的生效了"**。只重试不退避，会把对方打得更堵。

---

## 七、跑起来的正确姿势

```bash
cd ~/Desktop/ai_summary_bot
python -m pytest -v
```

- ✅ `4 passed` → 恭喜，W7 核心完成
- ❌ 某条 FAILED → pytest 会明确指出**哪一行、期望什么、实际什么**，把那段贴给我
- ⚠️ 测试跑了 3 秒才结束 → 说明空 2 没填对，`time.sleep` 没被替换掉

**永远不要**按编辑器右上角的 ▶ 跑测试文件 —— 那是"当脚本执行"，会 `ModuleNotFoundError`。测试永远用 pytest 跑。
