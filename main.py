"""
项目①：大模型文本摘要器 v0.2（W5 OOP 重构版）
作者：俊 ｜ 方向：B（大模型应用 / AI 产品）

v0.2 变化：
  把 v0.1 里散落的函数，按"职责"包成 3 个类：
    Config     —— 只负责"装配置"（key、模型、温度、系统提示词）
    LLMClient  —— 只负责"怎么跟 API 说话"（发请求、处理错误）
    Summarizer —— 只负责"业务"（给一段文本，返回摘要）

  这样拆的好处：以后要换模型 → 只改 Config；
                要换 API 厂商 → 只改 LLMClient；
                Summarizer（业务逻辑）一行都不用动。

用法：
  python main.py --file sample.txt
  python main.py --file sample.txt --length 5   # 指定摘要句数（练手小任务）

依赖：requests
"""

import os
import argparse
from dataclasses import dataclass

import requests


# ============================================================
# 1) Config：只用来"装配置"的类
#    @dataclass 的作用：帮你自动生成 __init__，
#    不用手写 def __init__(self, api_key, model, ...): self.api_key = api_key ...
# ============================================================
@dataclass
class Config:
    """所有可调整的参数集中放在这里。"""

    api_key: str                                  # 从环境变量读进来的密钥
    api_url: str = "https://api.deepseek.com/v1/chat/completions"
    model: str = "deepseek-chat"
    temperature: float = 0.3                      # 越小越稳定、越大越发散
    timeout: int = 30                             # 超时秒数

    @classmethod
    def from_env(cls) -> "Config":
        """
        从环境变量创建 Config。
        注意：这是"类方法"（@classmethod），第一个参数是 cls（类本身），不是 self。
        用法：Config.from_env()  —— 不需要先造对象就能调。
        """
        key = os.getenv("DEEPSEEK_API_KEY")
        if not key:
            raise SystemExit(
                "❌ 没找到环境变量 DEEPSEEK_API_KEY。请先设置：\n"
                "   Git Bash :  export DEEPSEEK_API_KEY=\"你的key\"\n"
                "   PowerShell: $env:DEEPSEEK_API_KEY=\"你的key\""
            )
        # cls(...) 等价于 Config(api_key=key)，其余用上面写的默认值
        return cls(api_key=key)


# ============================================================
# 2) 自定义异常：给错误分类，方便后面区分"能重试 / 不能重试"
#    （W6 写 @retry 装饰器时就要靠它）
# ============================================================
class LLMError(Exception):
    """所有本项目相关错误的父类。"""


class RetryableError(LLMError):
    """可以重试的错误：网络抖动、超时、被限流。"""


class FatalError(LLMError):
    """不该重试的错误：key 错了、余额不足、请求参数有问题。"""


# ============================================================
# 3) LLMClient：只负责"怎么跟大模型 API 说话"
# ============================================================
class LLMClient:
    """
    把 API 调用相关的东西都收在这里：
      - __init__ 里"存"配置（self.config = config）
      - chat() 里"用"配置发请求

    关键理解：self 就是"这个对象自己"。
    __init__ 里存进去的东西（self.config），
    后面任何方法里都能通过 self.config 取出来，不用再当参数传。
    """

    def __init__(self, config: Config):
        # 把传进来的配置"存"到对象身上，之后所有方法都能用
        self.config = config

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        """
        发一次对话请求，返回模型回复的文本。
        出错时不返回 None，而是"抛出"对应的异常，让调用方决定怎么处理。
        """
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.config.temperature,
        }

        try:
            resp = requests.post(
                self.config.api_url,
                headers=headers,
                json=payload,
                timeout=self.config.timeout,
            )
        except requests.exceptions.Timeout:
            raise RetryableError("⏰ 请求超时，可以重试") from None
        except requests.exceptions.RequestException as e:
            raise RetryableError(f"网络出错，可以重试：{e}") from None

        # 到这说明请求发出去了，看返回码
        if resp.status_code == 429:
            raise RetryableError("429 被限流了，等一会儿可以重试")
        if resp.status_code in (401, 403):
            raise FatalError("401/403 key 不对或没权限，别重试，去检查 key")
        if resp.status_code == 402:
            raise FatalError("402 余额不足，去充值")
        if resp.status_code >= 500:
            raise RetryableError(f"{resp.status_code} 服务端出错，可以重试")
        if resp.status_code >= 400:
            raise FatalError(f"{resp.status_code} 请求有问题：{resp.text[:200]}")

        # 一切正常：从 JSON 里把模型说的话取出来
        return resp.json()["choices"][0]["message"]["content"]


# ============================================================
# 4) Summarizer：只负责"业务"——给文本，出摘要
#    它不关心 HTTP 怎么发、key 从哪来，这些 LLMClient 已经包好了
# ============================================================
class Summarizer:
    """组合 LLMClient 完成'摘要'这件事。"""

    def __init__(self, client: LLMClient, max_sentences: int = 3):
        self.client = client        # 存一个"能发请求的工具"
        self.max_sentences = max_sentences

    def _build_prompt(self, text: str) -> tuple[str, str]:
        """拼出 (系统提示词, 用户提示词)。下划线开头 = 内部用的方法。"""
        system = (
            f"你是一个简洁、准确的中文摘要助手，"
            f"用 {self.max_sentences} 句话以内概括要点。"
        )
        user = f"请简要总结以下内容：\n{text}"
        return system, user

    def summarize(self, text: str) -> str:
        """对外唯一入口：丢进文本，吐出摘要。"""
        system, user = self._build_prompt(text)
        return self.client.chat(system, user)


# ============================================================
# 5) 入口：把三个类组装起来
# ============================================================
def read_text_file(path: str) -> str:
    """读取文本文件内容（utf-8，支持中文）。"""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main():
    parser = argparse.ArgumentParser(description="大模型文本摘要器")
    parser.add_argument("--file", help="要摘要的文本文件路径（如 sample.txt）")
    parser.add_argument("--length", type=int, default=3, help="摘要句数，默认 3")
    args = parser.parse_args()

    # 1. 拿到要摘要的文本
    if args.file:
        text = read_text_file(args.file)
        print(f"📄 已读取文件：{args.file}（{len(text)} 字）")
    else:
        print("=== 大模型文本摘要器 ===")
        print("提示：长文本请用 --file 参数（input() 一次只能读一行）")
        text = input("粘贴要摘要的文本（输入后回车）：\n")

    if not text.strip():
        print("没有输入文本，退出。")
        return

    # 2. 组装：配置 → 客户端 → 摘要器（这就是"把东西串起来"）
    config = Config.from_env()
    client = LLMClient(config)
    bot = Summarizer(client, max_sentences=args.length)

    # 3. 调用 + 兜住异常
    try:
        print("\n--- 摘要结果 ---\n")
        print(bot.summarize(text))
    except RetryableError as e:
        print(f"🔁 出了可重试的错误：{e}")
    except FatalError as e:
        print(f"❌ 出了不该重试的错误：{e}")


if __name__ == "__main__":
    main()
