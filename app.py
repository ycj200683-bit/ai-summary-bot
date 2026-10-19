"""
app.py —— 把 ai_summary_bot 变成一个 Web 服务（FastAPI）
作者：俊

【这一层的职责】
  已有：main.py（调模型的客户端 + 摘要器）、batch.py（批量 + 断点续跑）
  现在：把上面那套能力**通过 HTTP 暴露出去**，让别人不用装 Python 也能用。

  一句话区别：
    以前  你得在我电脑前敲命令
    现在  给你一个链接，手机也能用

【为什么只加一层壳，不重写逻辑】
  摘要的核心是 Summarizer，批量的核心是 batch.summarize_rows。
  这两块已经测过了，Web 层只负责「收请求 → 调它们 → 返回结果」。
  这就是分层的意义：换入口（CLI → HTTP）不用动内核。

【启动】
  PowerShell:
    $env:API_KEY = $env:DASHSCOPE_API_KEY
    $env:API_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    $env:MODEL_NAME = "qwen-plus"
    python -m uvicorn app:app --reload --port 8000

  浏览器打开 http://127.0.0.1:8000        网页
                 http://127.0.0.1:8000/docs 自动生成的 API 文档
"""

import os
import time
from datetime import date

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from main import Config, FatalError, LLMClient, RetryableError, Summarizer
from main import DEFAULT_MODEL, ENV_MODEL_NAME
from main import load_dotenv


# 必须在任何 Config.from_env() 之前执行。
# 这里直接复用 main.py 里的实现：CLI 和 Web 服务读同一份 .env，不各写一份。
load_dotenv()


# ============================================================
# 1) 可调参数
# ============================================================
MAX_CHARS = 5000          # 单次最多接受多少字（防止有人塞进来一本小说）
MAX_CALLS_PER_DAY = 20    # 每个 IP 每天最多几次
                          # 公开部署必须限流：这背后是真金白银的 API 账单，
                          # 不设上限，一个脚本就能把你的额度刷干净。
MAX_CALLS_PER_DAY_GLOBAL = 300   # 全站每天总上限
                          # ★ 只有单 IP 限额是不够的：换个 IP 就绕过去了，
                          #   或者链接被转发到群里，几十个人同时用照样能刷爆。
                          #   加一道全站闸门，最坏情况下的账单就是可预期的。

app = FastAPI(
    title="AI 摘要服务",
    description="基于大模型的中文文本摘要 API。支持单条摘要，批量能力见 /docs 与 CLI。",
    version="2.0",
)

# 客户端和模型名做成"全局只建一次"。
# 每次请求都重新建 LLMClient 会浪费，而且 requests.Session 复用连接更快。
_client: LLMClient | None = None
_model_name: str = "未配置"
_config_error: str = ""


# ============================================================
# 2) 请求 / 响应格式（Pydantic 会替我们做校验）
# ============================================================
class SummarizeIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_CHARS,
                      description=f"要摘要的正文，1~{MAX_CHARS} 字")
    length: int = Field(3, ge=1, le=5, description="摘要句数，1~5")


class SummarizeOut(BaseModel):
    summary: str
    latency_ms: int
    input_chars: int
    model: str


# ============================================================
# 3) 内部小工具
# ============================================================
def get_client() -> LLMClient:
    """
    惰性创建客户端。

    ★ 为什么不在模块顶层直接 Config.from_env()：
      Config.from_env() 在没配 key 时会 raise SystemExit —— 在命令行里那是"程序退出"，
      但在这里会把整个 Web 服务带崩。一个没配 key 的服务应该是
      "能启动、但接口返回 503 并说清原因"，而不是"起不来、日志里一堆看不懂的东西"。
      这就是 CLI 和服务的差别：服务的健壮性要求高一档。
    """
    global _client, _model_name, _config_error
    if _client is not None:
        return _client

    try:
        config = Config.from_env()
    except SystemExit as e:
        _config_error = str(e)
        raise HTTPException(status_code=503, detail="服务端尚未配置模型密钥")

    _client = LLMClient(config)
    _model_name = config.model
    return _client


def client_ip(request: Request) -> str:
    """取客户端 IP。部署到反向代理后面时，真实 IP 在 X-Forwarded-For 里。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# IP -> [日期, 已用次数]。进程内存里的简陋限流，重启即清零。
# 生产环境应该放 Redis —— 但那是另一个阶段的活，现在够用。
_usage: dict[str, list] = {}
_global: list = ["", 0]      # [日期, 全站已用次数]


def check_quota(ip: str) -> int:
    """用掉一次额度，返回剩余次数。超限抛 429。"""
    today = date.today().isoformat()

    # 第一道闸：全站总量
    if _global[0] != today:
        _global[0], _global[1] = today, 0
    if _global[1] >= MAX_CALLS_PER_DAY_GLOBAL:
        raise HTTPException(
            status_code=429,
            detail="今天的全站额度已用完，明天再来（这是个免费演示服务，额度有限）",
        )

    # 第二道闸：单个 IP
    rec = _usage.get(ip)
    if not rec or rec[0] != today:
        _usage[ip] = [today, 1]
        _global[1] += 1
        return MAX_CALLS_PER_DAY - 1

    if rec[1] >= MAX_CALLS_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=f"今天的 {MAX_CALLS_PER_DAY} 次额度已用完，明天再来",
        )
    rec[1] += 1
    _global[1] += 1
    return MAX_CALLS_PER_DAY - rec[1]


# ============================================================
# 4) 接口
# ============================================================
@app.get("/api/health")
def health() -> dict:
    """健康检查：部署平台和监控都靠它判断服务活没活。"""
    # 注意这里不能只看 _model_name：客户端是惰性创建的，第一次请求之前它还是"未配置"。
    # 健康检查要能在"还没接过任何请求"时就说清自己配的是什么模型。
    model = _model_name if _client is not None else (os.getenv(ENV_MODEL_NAME) or DEFAULT_MODEL)
    return {
        "status": "ok",
        "model": model,
        "configured": _client is not None or bool(os.getenv("API_KEY") or os.getenv("DEEPSEEK_API_KEY")),
        "max_chars": MAX_CHARS,
        "max_calls_per_day": MAX_CALLS_PER_DAY,
        "max_calls_per_day_global": MAX_CALLS_PER_DAY_GLOBAL,
        "used_today_global": _global[1] if _global[0] == date.today().isoformat() else 0,
    }


@app.post("/api/summarize", response_model=SummarizeOut)
def summarize(payload: SummarizeIn, request: Request) -> SummarizeOut:
    """单条摘要。核心逻辑全部复用 main.py 里已经测过的 Summarizer。"""
    check_quota(client_ip(request))

    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="正文不能是空白")

    bot = Summarizer(get_client(), max_sentences=payload.length)

    started = time.time()
    try:
        summary = bot.summarize(text)
    except RetryableError as e:
        # 重试 3 次还失败：这不是用户的问题，是上游服务的问题
        raise HTTPException(status_code=502, detail=f"上游模型服务不稳定，请稍后再试：{e}")
    except FatalError as e:
        raise HTTPException(status_code=502, detail=f"模型调用失败：{e}")
    cost_ms = int((time.time() - started) * 1000)

    return SummarizeOut(
        summary=summary.strip(),
        latency_ms=cost_ms,
        input_chars=len(text),
        model=_model_name,
    )


WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
INDEX_FILE = os.path.join(WEB_DIR, "index.html")


@app.post("/api/quota/take")
def quota_take(request: Request) -> dict:
    """
    取一次额度。前端在调云服务大模型之前先来这里。

    ★ 为什么限流留在 Python 后端、不放到前端：
      前端的任何计数都能被用户改（改 JS、清 localStorage）。
      服务端按 IP 记账才拦得住。云服务用的是平台的额度，
      这里这一道闸门是"我们自己的"闸门，两层都要有。
    """
    remaining = check_quota(client_ip(request))
    return {"ok": True, "remaining": remaining, "limit": MAX_CALLS_PER_DAY}


@app.get("/", response_class=HTMLResponse)
def index():
    """
    返回前端页面（web/index.html）。

    ★ 为什么把页面从这里挪出去：
      这个页面现在要加载云服务 SDK、读写云数据库、流式渲染，JS 已经有几百行。
      前后端混在同一个三引号字符串里，动一边容易碰坏另一边
      （前面那几次三引号事故就是教训）。物理分开，各自好改。
    """
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse(
            "<h1>web/index.html 找不到</h1><p>前端文件没有跟着部署包一起上传。</p>",
            status_code=500,
        )


# ============================================================
# 6) 直接运行入口（部署平台走这条路）
# ============================================================
if __name__ == "__main__":
    # ★ 部署平台的硬性要求：端口从 PORT 环境变量读，并绑 0.0.0.0。
    #   绑 127.0.0.1 外面访问不到；端口写死则平台分配的端口和你的对不上。
    #   本地跑没设 PORT，就退回 8000。
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
    )
