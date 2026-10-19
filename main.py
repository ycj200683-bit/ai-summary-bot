"""
项目①：大模型文本摘要器 v1.1（批量模式）
作者：俊 ｜ 方向：B（大模型应用 / AI 产品）

v1.1 变化（M3 / W9）：
  新增批量模式，一条命令处理整张 CSV：
      python main.py --input data.csv --text-col content --output out.csv
      python main.py --input data.csv --text-col content --output out.csv --limit 5

  分工：
      main.py  负责组装（读配置 → 造 bot → 分流到单条或批量）
      batch.py 负责批量的活（读表 → 逐行调用 → 逐行容错 → 写表）

  设计要点：
    · --file 与 --input 用 argparse 的互斥组，同时给会直接报错
    · 批量用 for 循环而不是 pandas 向量化 —— 因为每行都要等一次 HTTP 请求（慢 I/O）
    · 单行失败只记进 error 列，不影响其他行
    · 原表 data.csv 永不覆盖，结果另存 out.csv

v0.5 变化：
  把"连哪家厂商"从代码里搬到了环境变量里，一套代码通吃所有 OpenAI 兼容服务：
    API_KEY       密钥（兼容旧的 DEEPSEEK_API_KEY）
    API_BASE_URL  服务地址，写 base 或写完整 URL 都行：
                     https://api.deepseek.com/v1                    → 自动补 /chat/completions
                     https://dashscope.aliyuncs.com/compatible-mode/v1
                     http://localhost:11434/v1                      （本地 Ollama）
    MODEL_NAME    模型名，如 deepseek-chat / qwen-plus / qwen2.5:7b

  为什么这件事值得做：调用大模型本质就三件事——发给谁、用哪个模型、拿什么钥匙。
  把这三个变量化之后，切换厂商 = 改环境变量，业务代码一行都不用动。
  这也是"依赖倒置"最朴素的样子：代码依赖"OpenAI 兼容协议"这个抽象，
  而不是依赖"DeepSeek 这家公司"。

  顺带修掉三个真实跑出来的小毛病（都是实测才暴露的，光看代码看不出来）：
    ① 启动时打一行日志，说明当前连的是哪个模型、哪个端点
    ② 404 单独加提示：换厂商时只改端点忘了改模型名，是最容易踩的坑
    ③ 失败时退出码改成 1（以前失败也返回 0，脚本和 CI 根本发现不了失败），
       并且把"显示结果"挪到请求成功之后，失败时不会留下一个空标题

v0.4 变化（W8）：
  引入 logging，替代散落的 print。判断标准只有一条：
    - 给【开发者】看的记录（重试、读文件、出错原因）→ logger.info / warning / error
    - 给【用户】看的输出（界面提示、摘要正文）    → 保留 print
  日志同时打到终端和 logs/app.log；
  FileHandler 必须写 encoding="utf-8"，否则 Windows 上中文日志会乱码。

v0.3 变化：
  新增 @retry(max_tries=3, backoff=1.0) 装饰器，套在 LLMClient.chat 上：
    - 遇到 RetryableError（超时 / 网络抖动 / 429 限流 / 5xx）→ 等待后自动重试
    - 遇到 FatalError（401 key 错 / 402 余额不足）→ 立刻抛出，不做无谓等待
    - 等待时间指数退避：1 秒 → 2 秒 → 4 秒……（避免把对方打得更堵）

  装饰器的价值：chat() 一个字的业务代码都没改，就获得了重试能力。
  这就是"横切关注点"（日志、重试、计时、鉴权）该放的地方。

v0.2 变化：
  把散落的函数按职责包成 3 个类：Config / LLMClient / Summarizer。

用法：
  # 先设三个环境变量（以 DeepSeek 为例）
  #   PowerShell: $env:API_KEY="sk-xxx"
  #   Git Bash  : export API_KEY="sk-xxx"
  python main.py --file sample.txt
  python main.py --file sample.txt --length 5   # 指定摘要句数

  # 换厂商？只改环境变量，代码不动（例：阿里云百炼）
  #   $env:API_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
  #   $env:MODEL_NAME="qwen-plus"

依赖：requests
"""

import os
import time
import functools
import argparse
from dataclasses import dataclass

import requests
import logging

os.makedirs("logs", exist_ok=True)          # 没有 logs/ 目录就建一个

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),                                        # 打到终端
        logging.FileHandler("logs/app.log", encoding="utf-8"),          # 存到文件
    ],
)
logger = logging.getLogger(__name__)


def load_dotenv(path: str = ".env") -> None:
    """
    极简 .env 读取（30 行手写，不引 python-dotenv）。

    规则三条：
      ① 一行一个 KEY=VALUE；空行和 # 开头的行跳过
      ② 已经存在的环境变量不覆盖（setdefault）——真实环境变量优先级高于文件
      ③ 值两端的引号剥掉

    ★ 为什么放在 main.py 而不是 app.py：
      同一个 .env 既要在命令行用，也要在 Web 服务用。
      放在这里，`python main.py` 和 `uvicorn app:app` 读的是同一份配置，
      换模型只改一个地方，不会出现"网页换了、命令行还是老模型"。
    ★ 为什么不放在模块顶层执行：
      测试里要验证"没配 key 时的报错"，模块导入时就偷偷塞进去一个 key
      会让那类测试失效。所以只在 main() 里显式调用。
    ★ 为什么不把 key 直接写进代码：
      .env 在 .gitignore 里，推不上仓库；硬编码则一推就公开了。
      密钥只放环境变量或 .env，这是底线，不是风格问题。
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ============================================================
# 1) Config：只用来"装配置"的类
#    @dataclass 的作用：帮你自动生成 __init__，
#    不用手写 def __init__(self, api_key, model, ...): self.api_key = api_key ...
# ============================================================

# ↓ 默认值提成模块级常量，好处有二：
#   ① Config 和 from_env() 引用的是同一个来源，改一处就都改（不会两边打架）
#   ② 名字自带说明，读代码的人一眼知道现状是"默认走 DeepSeek"
DEFAULT_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-chat"

# 环境变量名也提成常量：拼错变量名是最常见的低级 bug，写成常量至少能被 IDE 补全
ENV_API_KEY = "API_KEY"
ENV_API_BASE_URL = "API_BASE_URL"
ENV_MODEL_NAME = "MODEL_NAME"

# 旧的变量名继续认，避免老用户（其实就是你自己）改配置时炸掉
LEGACY_ENV_API_KEY = "DEEPSEEK_API_KEY"

# 各家 OpenAI 兼容服务的"对话补全"路径，全都长这样
CHAT_COMPLETIONS_PATH = "/chat/completions"


def normalize_api_url(raw: str) -> str:
    """
    把用户填的地址统一成"能直接 POST 的完整 URL"。

    为什么要容错：不同厂商文档里给的东西不一样——
      DeepSeek 文档给 https://api.deepseek.com      （要自己拼 /chat/completions）
      百炼     给 https://dashscope.aliyuncs.com/compatible-mode/v1
      OpenAI   给 https://api.openai.com/v1
    让人记住"到底要不要带 /chat/completions"是反人类的，
    所以这里统一处理：带了就不重复拼，没带就补上，尾斜杠一律去掉。

    这就是"防御式解析"：把恶心留在边界（配置入口），
    让核心逻辑（chat 里的 requests.post）永远拿到干净的输入。
    """
    url = raw.strip().rstrip("/")                  # 去掉手滑打出的空格和结尾斜杠
    if url.endswith(CHAT_COMPLETIONS_PATH):
        return url                                 # 已经是完整 URL，原样用
    return url + CHAT_COMPLETIONS_PATH             # 只给了 base，补上路径


@dataclass
class Config:
    """所有可调整的参数集中放在这里。"""

    api_key: str                                  # 从环境变量读进来的密钥
    api_url: str = DEFAULT_API_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.3                      # 越小越稳定、越大越发散
    timeout: int = 30                             # 超时秒数

    @classmethod
    def from_env(cls) -> "Config":
        """
        从环境变量创建 Config。
        注意：这是"类方法"（@classmethod），第一个参数是 cls（类本身），不是 self。
        用法：Config.from_env()  —— 不需要先造对象就能调。

        读三个变量（都可以不设，不设就用 DeepSeek 的默认值）：
          API_KEY        必须设，否则直接退出
          API_BASE_URL   不设 → DeepSeek；设了 → 按 normalize_api_url 处理
          MODEL_NAME     不设 → deepseek-chat
        """
        # "or" 链的写法：前者为空（None 或空字符串）就取后者。
        # 比 os.getenv("API_KEY", os.getenv("DEEPSEEK_API_KEY")) 好在：
        # 环境变量设成空字符串时也能正确回退（那种情况很少有人想到，但确实会出 bug）
        key = os.getenv(ENV_API_KEY) or os.getenv(LEGACY_ENV_API_KEY)
        if not key:
            raise SystemExit(
                f"❌ 没找到环境变量 {ENV_API_KEY}（旧名 {LEGACY_ENV_API_KEY} 也认）。请先设置：\n"
                f'   PowerShell: $env:{ENV_API_KEY}="你的key"\n'
                f'   Git Bash  : export {ENV_API_KEY}="你的key"\n'
                f"   可选：$env:{ENV_API_BASE_URL} / $env:{ENV_MODEL_NAME} 换厂商"
            )

        # 同样用 "or" 兜底：环境变量没设（None）或设成空串时，都退回默认值
        base_url = os.getenv(ENV_API_BASE_URL) or DEFAULT_API_URL
        model = os.getenv(ENV_MODEL_NAME) or DEFAULT_MODEL

        # cls(...) 等价于 Config(...)，没传的参数用上面写的默认值
        return cls(
            api_key=key,
            api_url=normalize_api_url(base_url),
            model=model,
        )


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
# 2.5) @retry 装饰器：给任何函数套上"失败自动重试"的能力
#      三层嵌套：retry(收参数) → decorator(收函数) → wrapper(真正执行)
# ============================================================
def retry(max_tries: int = 3, backoff: float = 1.0):
    """
    失败自动重试的装饰器（指数退避）。

    max_tries : 最多执行几次（含第一次）
    backoff   : 第一次重试等多少秒，之后每次翻倍（1s → 2s → 4s……）

    只对 RetryableError 重试；其它异常原样抛出，立即失败。
    """
    def decorator(func):
        # @functools.wraps 保留原函数的名字和文档，不然会被 wrapper 顶掉
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            wait = backoff
            for attempt in range(1, max_tries + 1):
                try:
                    return func(*args, **kwargs)          # 正常时直接返回，什么都不用管
                except RetryableError as e:
                    if attempt == max_tries:
                        # 次数用完了，把错误抛给上层（main 里会打印提示）
                        raise
                    # ★W8 改动①：重试属于"开发者记录"，用户不用知道，但排障要查 → warning
                    #   用 %s/%.0f 而不是 f-string：日志级别没开启时不做多余的字符串拼接
                    logger.warning("🔁 第 %s 次失败（%s），%.0f 秒后重试……", attempt, e, wait)
                    time.sleep(wait)
                    wait *= 2                             # 指数退避：等待时间翻倍
        return wrapper
    return decorator


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

    # ↓ 装饰器就加在这一行：chat() 内部代码一个字没改，却自动获得了重试能力
    @retry(max_tries=3, backoff=1.0)
    def chat(self, system_prompt: str, user_prompt: str) -> str:
        """
        发一次对话请求，返回模型回复的文本。
        出错时不返回 None，而是"抛出"对应的异常，让调用方决定怎么处理：
          RetryableError → 会被上面的 @retry 自动重试
          FatalError     → 直接抛出，不重试
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
        # ★v0.5 改动②：404 单独拎出来加提示。这不是洁癖，是我自己踩过的坑——
        #   换了 API_BASE_URL 却忘了改 MODEL_NAME，就会拿着 deepseek-chat 去问百炼，
        #   对方回一个干巴巴的 model_not_found，人得愣半天才知道是自己的配置错位了。
        #   报错信息里直接写上"该去检查哪两个变量"，省掉一次排障。
        if resp.status_code == 404:
            raise FatalError(
                f"404 找不到接口或模型。若刚换过厂商，请检查 {ENV_API_BASE_URL} 和 "
                f"{ENV_MODEL_NAME} 是否配套改了。服务端原文：{resp.text[:200]}"
            )
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


# ============================================================
# 5) 批量模式：读 CSV → 逐行摘要 → 写回新 CSV
#    ★v1.1 新增。真正的逻辑在 batch.py，这里只负责"组装 + 串起来"。
# ============================================================
def run_batch(args) -> None:
    """
    --input 模式下走这条路。

    职责边界要说清楚：
      本函数  负责"准备"——读配置、造 bot、把参数传下去
      batch.py 负责"干活"——读表、循环、容错、写表
    """
    # ↓ 延迟导入（lazy import）：只在真的用批量模式时才加载 pandas。
    #   好处：单条模式（--file）启动不用等 pandas 那 1 秒的导入时间。
    #   这也是"按需加载"的典型用法——主流程不该为边角功能付代价。
    import batch

    # 1. 配置 → 客户端 → 摘要器，和单条模式完全一样
    config = Config.from_env()
    bot = Summarizer(LLMClient(config), max_sentences=args.length)

    # 光看日志你还不够放心？这行把"连到哪"和"读的哪个文件"都摊开
    logger.info("🤖 模型：%s ｜ 端点：%s", config.model, config.api_url)

    # 2. 读表（含列校验、--limit 截断）
    df = batch.load_input(args.input, args.text_col, args.limit)
    logger.info("📄 读入 %s 行，正文列 = %s", len(df), args.text_col)

    # 2.5 ★W10：--resume 时先翻出上次的结果，挑出已经成功/跳过的行
    #   注意这里读的是 --output 指向的那个文件，所以"上次跑的是谁"由 --output 决定。
    #   换一个 --output 就等于换了一条进度线——这正是我们想要的：互不干扰。
    done = batch.load_done(args.output) if args.resume else {}
    if args.resume and not done:
        logger.info("♻️  --resume 已开，但 %s 里没有可复用的结果，这次从头跑", args.output)
    elif done:
        remaining = sum(1 for one in df["id"].astype(str) if str(one) not in done)
        logger.info("♻️  复用 %s 行旧结果，本次只需处理 %s 行", len(done), remaining)

    # 3. 逐行摘要（batch.py 里的核心循环）
    #   out_path 传下去 = 允许边跑边落盘；done 传下去 = 允许命中就跳过。
    df = batch.summarize_rows(bot, df, args.text_col, out_path=args.output, done=done)

    # 4. 写新表 + 汇总
    batch.save_output(df, args.output)
    batch.print_summary(df)
    print(f"✅ 已写出 {args.output}")


def main():
    # 先读 .env：这样 `python main.py --file x.txt` 不用每次手动 export 三行环境变量。
    # 真实环境变量优先级更高（load_dotenv 用 setdefault），服务器上覆盖也照样生效。
    load_dotenv()

    parser = argparse.ArgumentParser(description="大模型文本摘要器")
    parser.add_argument("--length", type=int, default=3, help="摘要句数，默认 3")

    # ★v1.1：--file 和 --input 是两条互斥的路，不能同时给。
    #   add_mutually_exclusive_group() 让 argparse 自己检查：
    #   用户同时传两个，会直接报错 "argument --input: not allowed with argument --file"。
    #   不用你写 if 判断——能用工具约束的，就别用代码兜。
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--file", help="单条模式：要摘要的文本文件（如 sample.txt）")
    source.add_argument("--input", help="批量模式：要处理的 CSV（如 data.csv）")

    # 下面三个只在批量模式生效，有默认值所以不带也不会报错
    parser.add_argument("--text-col", default="content",
                        help="批量模式：哪一列是正文，默认 content")
    parser.add_argument("--output", default="out.csv",
                        help="批量模式：结果写到哪个 CSV，默认 out.csv")
    parser.add_argument("--limit", type=int, default=None,
                        help="批量模式：只处理前 N 行（调试用，省钱）")

    # ★W10：action="store_true" 表示这是个【开关】——写了 --resume 就是 True，
    #   不写就是 False，不用跟值（不能写 --resume True）。
    #   凡是"要/不要"这类开关，都用 store_true，别用 type=bool ——
    #   type=bool 会把字符串 "False" 也当成 True，是个经典坑。
    parser.add_argument("--resume", action="store_true",
                        help="批量模式：接着上次的 --output 跑，跳过已经成功的行")

    args = parser.parse_args()

    # 0. 批量模式分流（在拿文本之前就分开，两条路互不干扰）
    if args.input:
        run_batch(args)
        return

    # 1. 拿到要摘要的文本
    if args.file:
        text = read_text_file(args.file)
        # ★W8 改动②：读了哪个文件、多少字 —— 运行信息，给开发者看 → info（保留 print 的只有"给用户看"的）
        logger.info("📄 已读取文件：%s（%s 字）", args.file, len(text))
    else:
        # ↓ 以下两行是【给用户看的界面】，不动
        print("=== 大模型文本摘要器 ===")
        print("提示：长文本请用 --file 参数（input() 一次只能读一行）")
        text = input("粘贴要摘要的文本（输入后回车）：\n")

    if not text.strip():
        # ★W8 改动③：输入为空属于异常输入 → warning
        logger.warning("没有输入文本，退出。")
        return

    # 2. 组装：配置 → 客户端 → 摘要器（这就是"把东西串起来"）
    config = Config.from_env()
    client = LLMClient(config)
    bot = Summarizer(client, max_sentences=args.length)

    # ★v0.5 改动①：把"连哪家、用什么模型"打出来。
    #   为什么值得记：切厂商之后如果结果不对，第一件事就是确认"到底连到哪去了"。
    #   ⚠ 只打 model 和 url，绝对不要打 api_key —— 日志文件是会被提交/分享出去的
    logger.info("🤖 模型：%s ｜ 端点：%s", config.model, config.api_url)

    # 3. 调用 + 兜住异常
    #
    #   ★v0.5 改动③：把"发请求"和"显示结果"拆开，并把失败变成非 0 退出码。
    #
    #   旧写法是先把 "--- 摘要结果 ---" 打出来（print 在 try 里、且排在调用之前），
    #   结果请求一失败，用户就看到一个孤零零的标题底下什么都没有 —— 很懵。
    #   现在改成：先把结果拿到手，成功了才打印标题和正文。
    #   顺序清楚了，也顺手得到一个好处：失败时不会有半截输出。
    try:
        result = bot.summarize(text)
    except RetryableError as e:
        # ★W8 改动④：真正的失败，等级最高 → error
        logger.error("🔁 重试 3 次后仍然失败：%s", e)
        print("   常见原因：网络不稳 / 429 被限流 / 服务端 5xx，稍后再试。")   # 给用户的提示 → 保留
        raise SystemExit(1)
    except FatalError as e:
        # ★W8 改动⑤：key 错 / 余额不足这类不可恢复错误 → error
        logger.error("❌ %s", e)
        raise SystemExit(1)

    # 走到这里说明成功了，再打印【给用户看的最终输出】
    #   ⚠ 必须用 print 而不是 logger：改成 logger 用户就看不到摘要了
    print("\n--- 摘要结果 ---\n")
    print(result)


if __name__ == "__main__":
    main()
