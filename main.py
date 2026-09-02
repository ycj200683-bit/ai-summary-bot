"""
项目①：大模型文本摘要器 v0.1
作者：俊 ｜ 方向：B（大模型应用 / AI 产品）

v0.1 新增：
  1. --file 参数：直接读整个文本文件（解决 input() 一次只能读一行的限制）
  2. API 错误处理：网络/鉴权/限流出错不崩溃，明确告诉你错在哪

用法：
  python main.py                    # 交互式输入（只能一行）
  python main.py --file sample.txt  # 读整个文件（推荐，长文本用这个）

依赖：requests
"""

import os
import argparse
import requests

API_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"


def get_api_key() -> str:
    """从环境变量读取 API Key，没有就明确提示怎么设。"""
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit(
            "❌ 没找到环境变量 DEEPSEEK_API_KEY。请先设置：\n"
            "   Git Bash :  export DEEPSEEK_API_KEY=\"你的key\"\n"
            "   PowerShell: $env:DEEPSEEK_API_KEY=\"你的key\""
        )
    return key


def read_text_file(path: str) -> str:
    """读取文本文件内容（utf-8，支持中文）。"""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def summarize(text: str, api_key: str) -> str:
    """调用大模型 API，返回 text 的中文摘要。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": "你是一个简洁、准确的中文摘要助手，用 3 句话以内概括要点。",
            },
            {"role": "user", "content": f"请简要总结以下内容：\n{text}"},
        ],
        "temperature": 0.3,
    }

    resp = requests.post(API_URL, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def main():
    parser = argparse.ArgumentParser(description="大模型文本摘要器")
    parser.add_argument("--file", help="要摘要的文本文件路径（如 sample.txt）")
    args = parser.parse_args()

    # 1. 拿到要摘要的文本
    if args.file:
        text = read_text_file(args.file)
        print(f"📄 已读取文件：{args.file}（{len(text)} 字）")
    else:
        print("=== 大模型文本摘要器 ===")
        print("提示：input() 一次只能读一行，长文本请用 --file 参数")
        text = input("粘贴要摘要的文本（输入后回车）：\n")

    if not text.strip():
        print("没有输入文本，退出。")
        return

    # 2. 调用 API——各种错误都在这里兜住，不让程序直接崩掉
    api_key = get_api_key()
    try:
        print("\n--- 摘要结果 ---\n")
        print(summarize(text, api_key))
    except requests.exceptions.Timeout:
        print("⏰ 请求超时了，稍后再试。")
    except requests.exceptions.HTTPError as e:
        print(f"❌ API 返回错误：{e}")
        print("   常见原因：401=key 不对 / 402=余额不足 / 429=请求太频繁被限流")
    except requests.exceptions.RequestException as e:
        print(f"❌ 网络出错：{e}")


if __name__ == "__main__":
    main()