"""
Agent 层原型（AI 全栈 + Agent 方向的核心模块）

复用你已有的：Config / LLMClient / Summarizer（main.py 里的东西一行不用改）
新增的是：工具注册表 + 多步循环 + trace 记录

跑法：
  python agent.py --fake "这段话有多少字，关键词是什么"   # 假模型演示，不花 API 费
  python agent.py "帮我读 sample.txt，统计字数并摘要"      # 真模型

为什么要单独一个文件而不是塞进 main.py：
  摘要器解决的是"一次输入 → 一次输出"；
  Agent 解决的是"输入 → 模型自己决定调几次工具 → 输出"。
  两者职责不同，混在一起以后都改不动。
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path

import requests

from main import (Config, LLMClient, Summarizer, FatalError, RetryableError,
                  retry, load_dotenv)

# agent.py 自己也要读 .env —— main.py 只在 main() 里调 load_dotenv，
# 单独跑 agent.py 时不会自动加载，会报"没找到 API_KEY"。
load_dotenv(str(Path(__file__).resolve().parent / ".env"))


# ============================================================
# 1) 工具层：每个工具 = 一个普通函数 + 一份 JSON Schema 说明
#    模型看不到你的代码，它只能读 description 和 parameters 来决定调不调、怎么调。
#    ⚠️ 所以 description 写得清不清楚，直接决定 Agent 聪不聪明。
# ============================================================
PROJECT_DIR = Path(__file__).resolve().parent

TOOL_REGISTRY: dict[str, dict] = {}


def tool(name: str, description: str, parameters: dict):
    """注册工具的装饰器：把函数和它的说明书一起存进注册表。"""
    def decorator(func):
        TOOL_REGISTRY[name] = {
            "fn": func,
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            },
        }
        return func
    return decorator


@tool(
    name="count_words",
    description="统计一段文本的字数（中文按字计，英文按空白分词）。当用户问'多少字/多长'时使用。",
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要统计的文本"},
        },
        "required": ["text"],
    },
)
def count_words(text: str) -> int:
    return len(text.strip())


@tool(
    name="extract_keywords",
    description="从文本中提取出现频率最高的关键词。当用户问'关键词/主题是什么'时使用。",
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要分析的文本"},
            "top_k": {"type": "integer", "description": "返回几个关键词，默认 5"},
        },
        "required": ["text"],
    },
)
def extract_keywords(text: str, top_k: int = 5) -> list[str]:
    """
    极简实现：中文用"二字滑窗"切（无词典时的常用兜底），英文按长度≥3 的单词切，再数词频。

    ★ 为什么不用 `[\\u4e00-\\u9fa5]{2,}` 直接切整段：
      那样"人工智能正在改变软件开发方式"会被当成一个词，工具就等于没起作用
      （真实踩到的坑：第一次跑它把整句原样返回，模型还专门解释了一遍）。
      改成滑窗后至少能切出 人工/智能/改变/开发 这类片段。
    ★ 想提升质量只需换这一行（jieba/TextRank），Agent 那层完全不用动——
      这就是"工具要可替换"的价值。
    """
    import re

    tokens: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fa5]{2,}", text):
        if len(run) <= 4:
            tokens.append(run)                       # 短句整体保留，不再切碎
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))   # 长串二字滑窗
    tokens.extend(re.findall(r"[A-Za-z]{3,}", text))

    freq: dict[str, int] = {}
    for w in tokens:
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:top_k]]


@tool(
    name="read_file",
    description="读取项目目录下的文本文件内容。当用户提到某个文件名（如 sample.txt）时使用。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对项目目录的文件路径，例如 sample.txt"},
        },
        "required": ["path"],
    },
)
def read_file(path: str) -> str:
    # 安全护栏：只允许读项目目录内的文件，挡住 ../../ 这种越界读取
    target = (PROJECT_DIR / path).resolve()
    if PROJECT_DIR not in target.parents and target != PROJECT_DIR:
        raise ValueError(f"拒绝访问项目目录之外的路径：{path}")
    if not target.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    return target.read_text(encoding="utf-8")


# summarize_text 需要 LLMClient，所以用"延迟绑定"：运行时把 client 塞进上下文
_CURRENT_CLIENT: dict[str, LLMClient] = {}


@tool(
    name="summarize_text",
    description="把一段长文本摘要成指定句数。当用户要求'总结/摘要/概括'时使用。",
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要摘要的文本"},
            "sentences": {"type": "integer", "description": "要几句话，默认 3"},
        },
        "required": ["text"],
    },
)
def summarize_text(text: str, sentences: int = 3) -> str:
    client = _CURRENT_CLIENT.get("client")
    if client is None:
        raise RuntimeError("summarize_text 需要先绑定 LLMClient")
    return Summarizer(client, max_sentences=sentences).summarize(text)


def execute_tool(name: str, args: dict) -> dict:
    """执行一个工具，把成功/失败都包装成统一结构（不要让它把整个循环炸掉）。"""
    if name not in TOOL_REGISTRY:
        return {"ok": False, "error": f"没有这个工具：{name}"}
    started = time.time()
    try:
        result = TOOL_REGISTRY[name]["fn"](**args)
        return {"ok": True, "result": result, "elapsed_ms": round((time.time() - started) * 1000)}
    except Exception as e:                       # noqa: BLE001 —— 工具失败不该中断整个 Agent
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "elapsed_ms": round((time.time() - started) * 1000)}


def _preview(value, limit: int = 160) -> str:
    """把工具返回值压成一行可读的预览，供 trace 展示用。"""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + " …"


# ============================================================
# 2) 调用模型：要能传 tools，所以不能用 chat()（它只返回纯文本）
# ============================================================
SYSTEM_PROMPT = """你是一个文档处理助手。面对用户请求时：
1. 先判断需不需要调用工具，需要就调，不要猜数字。
2. 一次只解决一个子问题，拿到结果再决定下一步。
3. 工具返回错误时，向用户说明失败原因，不要编造结果。
4. 已经有足够信息就直接给出最终回答，不要为了多调一次工具而调。"""


class ToolCallingClient:
    """在 LLMClient 之外补一个"支持工具调用"的请求方法。"""

    def __init__(self, config: Config, timeout: int = 120):
        self.config = config
        # 带工具的请求比普通摘要慢（模型要先想"调不调、调哪个"），
        # 30 秒是纯文本摘要的经验值，这里单独放一个更宽的值。
        self.timeout = timeout

    @retry(max_tries=2, backoff=2.0)
    def chat_with_tools(self, messages: list[dict]) -> dict:
        """返回 assistant message（可能带 tool_calls，也可能是纯文本）。"""
        payload = {
            "model": self.config.model,
            "messages": messages,
            "tools": [t["schema"] for t in TOOL_REGISTRY.values()],
            "tool_choice": "auto",
            "temperature": self.config.temperature,
        }
        if self.config.disable_thinking:
            payload["enable_thinking"] = False
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(self.config.api_url, headers=headers, json=payload,
                                 timeout=self.timeout)
        except requests.exceptions.Timeout:
            raise RetryableError(
                f"请求超时（{self.timeout}s）——model={self.config.model}，"
                f"带工具的请求通常要 10-40s，可加 --timeout 180 再试"
            ) from None
        except requests.exceptions.RequestException as e:
            raise RetryableError(f"网络错误：{e}") from None

        if resp.status_code >= 400:
            raise FatalError(
                f"{resp.status_code} API 报错（model={self.config.model}）：{resp.text[:300]}"
            )
        return resp.json()["choices"][0]["message"]


# ============================================================
# 3) Agent 主循环：这就是"Agent"三个字的全部秘密
#    模型说要调工具 → 我执行 → 把结果塞回去 → 模型再决定 → 直到它不再调工具
# ============================================================
def run_agent(user_input: str, client_factory, max_steps: int = 6,
              step_timeout: int = 30) -> dict:
    """
    返回 {"answer": 最终回答, "trace": [每一步的记录]}
    trace 是简历上最值钱的东西——它证明你不是"调了个 API"，而是能看见黑盒里发生了什么。
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_input},
    ]
    trace: list[dict] = []
    started_all = time.time()

    for step in range(1, max_steps + 1):
        assistant = client_factory.chat_with_tools(messages)

        tool_calls = assistant.get("tool_calls") or []
        if not tool_calls:
            # 模型不再要工具了 → 结束
            return {
                "answer": assistant.get("content", ""),
                "trace": trace,
                "steps": len(trace),
                "elapsed_ms": round((time.time() - started_all) * 1000),
            }

        # 把 assistant 的"我要调工具"这条消息也存进历史，否则下一轮模型不认账
        messages.append({
            "role": "assistant",
            "content": assistant.get("content") or "",
            "tool_calls": tool_calls,
        })

        for call in tool_calls:
            fn_name = call["function"]["name"]
            raw_args = call["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}
                result = {"ok": False, "error": f"参数不是合法 JSON：{raw_args[:100]}"}
            else:
                step_started = time.time()
                result = execute_tool(fn_name, args)
                # 单步超时保护：工具跑太久也记账（这里示意，真实超时需线程或信号）
                if (time.time() - step_started) > step_timeout:
                    result["warning"] = f"该步超过 {step_timeout}s"

            trace.append({
                "step": step,
                "tool": fn_name,
                "args": args,
                "ok": result.get("ok", False),
                "elapsed_ms": result.get("elapsed_ms"),
                # 结果也留一份预览：只有参数没有返回值，看的人不知道工具到底算出了什么。
                # 截断 160 字是为了 trace 面板不被一整段长文撑爆。
                "result": _preview(result.get("result")) if result.get("ok") else None,
                **({"error": result["error"]} if not result.get("ok") else {}),
            })

            # 工具结果必须以 role=tool 回填，且要带 tool_call_id，否则模型对不上号
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(
                    result.get("result", result.get("error")), ensure_ascii=False
                ),
            })

    # 步数用完了：这是"防死循环"的最后一道闸
    return {
        "answer": f"⚠️ 达到最大步数 {max_steps}，已停止。可能任务描述不清或工具不够用。",
        "trace": trace,
        "steps": len(trace),
        "elapsed_ms": round((time.time() - started_all) * 1000),
        "hit_max_steps": True,
    }


# ============================================================
# 4) 假模型：不用 key、不花钱，专门用来验证"循环和 trace 到底对不对"
#    真实的模型只在"要不要调工具"上有差异，循环本身是一样的。
# ============================================================
class FakeClient:
    """按剧本走：先调 count_words，再调 extract_keywords，最后给答案。"""

    def __init__(self):
        self.turn = 0

    def chat_with_tools(self, messages: list[dict]) -> dict:
        self.turn += 1
        user_text = next((m["content"] for m in messages if m["role"] == "user"), "")
        tool_results = [m for m in messages if m["role"] == "tool"]

        if self.turn == 1:
            return {"content": "", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "count_words",
                             "arguments": json.dumps({"text": user_text}, ensure_ascii=False)},
            }]}
        if self.turn == 2:
            return {"content": "", "tool_calls": [{
                "id": "call_2", "type": "function",
                "function": {"name": "extract_keywords",
                             "arguments": json.dumps({"text": user_text, "top_k": 3},
                                                     ensure_ascii=False)},
            }]}
        return {"content": f"（假模型演示）收到 {len(tool_results)} 个工具结果，"
                           f"统计完成：{tool_results[-1]['content']}"}


def print_trace(trace: list[dict]) -> None:
    """把 trace 打成一张表——以后这玩意要显示在网页上。"""
    if not trace:
        print("（没有调用任何工具）")
        return
    print("  step | 工具               | 参数                       | 结果   | 耗时")
    print("  -----|--------------------|----------------------------|--------|-------")
    for t in trace:
        args = json.dumps(t["args"], ensure_ascii=False)
        if len(args) > 26:
            args = args[:23] + "..."
        mark = "✅" if t["ok"] else "❌"
        print(f"  {t['step']:>4} | {t['tool']:<18} | {args:<26} | {mark:<6} | "
              f"{t['elapsed_ms'] or 0}ms")


def main() -> None:
    parser = argparse.ArgumentParser(description="文档处理 Agent 原型")
    parser.add_argument("query", nargs="?", default="统计这段话有多少字，关键词是什么：人工智能正在改变软件开发方式",
                        help="给 Agent 的指令")
    parser.add_argument("--fake", action="store_true", help="用假模型演示，不调 API、不花钱")
    parser.add_argument("--max-steps", type=int, default=6, help="最大步数（防死循环）")
    parser.add_argument("--timeout", type=int, default=120,
                        help="单次请求超时秒数，默认 120（带工具的请求比普通摘要慢）")
    parser.add_argument("--probe", action="store_true",
                        help="连通性诊断：分别测「无工具」和「带工具」两个请求谁慢/谁失败")

    args = parser.parse_args()

    # 诊断模式：不跑 Agent，只分别打两个请求，定位是"网络慢"还是"模型不支持工具"
    if args.probe:
        config = Config.from_env()
        print(f"🔧 模型 {config.model} ｜ 地址 {config.api_url}")
        real = LLMClient(config)

        print("\n① 无工具请求（普通摘要链路）……")
        t0 = time.time()
        try:
            out = real.chat("你是一个助手。", "只回复两个字：正常")
            print(f"   ✅ 成功 {time.time() - t0:.1f}s → {out.strip()[:40]}")
        except Exception as e:                                  # noqa: BLE001
            print(f"   ❌ 失败 {time.time() - t0:.1f}s → {type(e).__name__}: {e}")

        print("\n② 带 tools 请求（Agent 链路）……")
        t0 = time.time()
        try:
            probe = ToolCallingClient(config, timeout=args.timeout)
            msg = probe.chat_with_tools([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "这段话有多少字：人工智能"},
            ])
            calls = msg.get("tool_calls") or []
            print(f"   ✅ 成功 {time.time() - t0:.1f}s → "
                  + (f"调用了工具 {[c['function']['name'] for c in calls]}" if calls
                     else f"未调用工具，直接回答：{str(msg.get('content'))[:40]}"))
        except Exception as e:                                  # noqa: BLE001
            print(f"   ❌ 失败 {time.time() - t0:.1f}s → {type(e).__name__}: {e}")
        return

    if args.fake:
        client = FakeClient()
        print(f"🤖 [假模型模式] 指令：{args.query}\n")
    else:
        config = Config.from_env()
        print(f"🔧 模型 {config.model} ｜ 地址 {config.api_url} ｜ 超时 {args.timeout}s")
        real = LLMClient(config)
        _CURRENT_CLIENT["client"] = real          # 给 summarize_text 工具绑定 client
        client = ToolCallingClient(config, timeout=args.timeout)
        print(f"🤖 指令：{args.query}\n")

    result = run_agent(args.query, client, max_steps=args.max_steps)

    print("--- 执行轨迹（Trace）---")
    print_trace(result["trace"])
    print()
    print("--- 最终回答 ---")
    print(result["answer"])
    print()
    print(f"共 {result['steps']} 步 / {result['elapsed_ms']} ms"
          + ("　⚠️ 触发了最大步数保护" if result.get("hit_max_steps") else ""))


if __name__ == "__main__":
    try:
        main()
    except FatalError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已中断。")
