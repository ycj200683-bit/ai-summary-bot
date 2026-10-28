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
  python -m uvicorn app:app --reload --port 8000

  密钥和模型名不用在 shell 里 export：同目录的 .env 由 main.load_dotenv() 读，
  CLI 和 Web 共用同一份配置（当前默认 deepseek-v4.1-flash）。
  要临时覆盖就在启动前设环境变量——load_dotenv 用 setdefault，
  真实环境变量优先级高于 .env 文件。

  浏览器打开 http://127.0.0.1:8000        网页
                 http://127.0.0.1:8000/docs 自动生成的 API 文档

【部署：限流的两个坑，别踩】
  ① 反代后取真实 IP：见 client_ip()。默认只在「直连来源是内网/回环」时
     才信 X-Forwarded-For，且只取最后一段（客户端自己塞的左边几段一律丢弃）。
     代理在另一台机器上、直连来源是公网 IP 时，设 TRUST_PROXY=1 显式启用。
  ② 计数存在进程内存里：uvicorn 默认单进程，够用；
     一旦加 --workers N，每个进程各记一份，全站上限就变成 N 倍。
     要多进程就先把 _usage/_global 换成 Redis。
"""

import ipaddress
import json
import os
import threading
import time
from datetime import date

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from main import Config, FatalError, LLMClient, RetryableError, Summarizer, count_sentences
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
    # 实际用了几句。正文太短时句数会被收窄，前端拿它跟用户选的对比，
    # 才能给出"原文只有 69 字，按 2 句给出"这种说明，而不是让用户以为选择被无视。
    sentences_used: int = 0
    # 本次用掉之后这个 IP 今天还剩几次。前端显示出来，
    # 用户才不会在第 20 次被 429 时才突然发现"原来有额度"。
    remaining: int = -1         # -1 = 未知（老客户端/未走限流）


# ---- Agent 接口的请求/响应 ----
class AgentIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000,
                       description="给 Agent 的自然语言指令")
    # 步数上限是 Agent 的生命线：没有它，模型可能在两个工具之间来回调到天荒地老，
    # 一次请求烧掉几十次 API 调用。默认 6 步，上限 10 步。
    max_steps: int = Field(6, ge=1, le=10, description="最大执行步数，1~10")


class AgentStep(BaseModel):
    step: int
    tool: str
    args: dict
    ok: bool
    elapsed_ms: int | None = None
    # 工具返回值的预览。前端把它显示在参数下面，
    # 看的人才知道"这个工具到底算出了什么"，而不只是"它被调用了"。
    result: str | None = None
    error: str | None = None


class AgentOut(BaseModel):
    answer: str
    trace: list[AgentStep]
    steps: int
    elapsed_ms: int
    model: str
    remaining: int = -1
    hit_max_steps: bool = False


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


def connect_kb():
    """
    打开知识库的 sqlite 连接。

    ★ 为什么再包一层而不是直接 from kb import connect：
      知识库依赖 sqlite 文件可写、embedding 接口可用。把它放在函数内导入，
      意味着"即使 kb.py 整个坏掉，摘要和 Agent 接口照样能用"——
      单元测试里可以用 monkeypatch 换掉它，不用起真实数据库。
    """
    from kb import connect

    return connect()


# 强制信任 X-Forwarded-For（代理在另一台机器上、直连来源是公网 IP 时才需要）
TRUST_PROXY = (os.getenv("TRUST_PROXY") or "").strip().lower() in ("1", "true", "yes", "on")

# 视为"前面有反代"的来源地址段。
# ★ 为什么不用 ipaddress 自带的 is_private：
#   它把 203.0.113.0/24、198.51.100.0/24、192.0.2.0/24 这些 IANA 文档/保留段也算 private，
#   用保留段做测试时会得到反直觉的结果。这里只列真正"内网"的段，语义明确。
_INTERNAL_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),        # 回环（同机 nginx 反代）
    ipaddress.ip_network("10.0.0.0/8"),         # 私有网段 / 容器网络
    ipaddress.ip_network("172.16.0.0/12"),      # 私有网段
    ipaddress.ip_network("192.168.0.0/16"),     # 私有网段
    ipaddress.ip_network("169.254.0.0/16"),     # link-local
    ipaddress.ip_network("100.64.0.0/10"),      # 云厂商内网 / 运营商 CGNAT
    ipaddress.ip_network("::1/128"),            # IPv6 回环
    ipaddress.ip_network("fc00::/7"),           # IPv6 唯一本地地址
)


def _is_internal_host(host: str) -> bool:
    """直连来源是不是内网/回环地址。是 → 说明我们前面挂着代理，XFF 才有意义。"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 不是 IP 字面量（比如 TestClient 塞进来的 "testclient"），当作不可信来源
        return False

    # 双栈场景下客户端可能报成 ::ffff:127.0.0.1 这种 IPv4-mapped 地址
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped

    # 版本不同不能直接 in（IPv4 地址对 IPv6 网段比较会抛 TypeError）
    return any(ip.version == net.version and ip in net for net in _INTERNAL_NETS)


def _is_ip(host: str) -> bool:
    """是不是 IP 字面量。用来把伪造的垃圾串挡在 XFF 解析外面。"""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    """
    取客户端 IP。

    ★ 为什么不能直接用 X-Forwarded-For 的第一段（以前的写法，是个真漏洞）：
      XFF 是客户端也能随便发的请求头。以前的代码取第一段，
      攻击者每次换一个假 IP 发一次，单 IP 限额就形同虚设。
      实测：伪造 25 个不同 IP 各发 1 次，全部放行。

    现在的判定顺序：
      ① 直连来源是公网 IP（且没开 TRUST_PROXY）→ 用它，XFF 一律不看
      ② 直连来源是内网/回环（说明前面有反代）→ 从 XFF 右边往左找
         第一个【公网】地址

    ★ 为什么是"从右往左找公网地址"，而不是直接取最后一段：
      XFF 是从客户端往服务端一路追加的，最左边是客户端自称的（可伪造），
      最右边是最近一跳代理追加的。但最近一跳代理追加的可能是它自己的内网地址
      ——Render、部分 nginx 配置都会这么干。直接取最后一段，
      结果就是"所有用户共用一个 IP"，20 次/天的限额被几十个人瞬间分完。
      所以跳过尾部的内网地址，取第一个公网地址；客户端伪造的公网 IP
      在左边，永远排在被代理追加的真实地址前面，取不到。
    """
    peer = request.client.host if request.client else "unknown"

    if TRUST_PROXY or _is_internal_host(peer):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            hops = [h.strip() for h in forwarded.split(",") if h.strip()]

            for hop in reversed(hops):
                if _is_ip(hop) and not _is_internal_host(hop):
                    return hop

            # 整条链路都是内网地址（本机调试、容器内网互访）→ 退回最右一跳
            if hops:
                return hops[-1]

    return peer


# IP -> [日期, 已用次数]。进程内存里的简陋限流，重启即清零。
# 生产环境应该放 Redis —— 但那是另一个阶段的活，现在够用。
_usage: dict[str, list] = {}
_global: list = ["", 0]      # [日期, 全站已用次数]

# ★ 为什么要加锁：FastAPI 的同步端点跑在线程池里，多个请求会真的并发进来。
#   "先读再写"不是原子操作，20 个请求同时到、都读到 19，最后一个都没拦住。
#   一把粗锁就够——临界区只有几行算术，不会成为瓶颈。
_quota_lock = threading.Lock()
_swept_on: list = [""]       # 上次清理过期 IP 记录的日期


def _sweep_locked(today: str) -> None:
    """
    删掉非今天的 IP 记录。

    ★ 不做清会怎样：_usage 只增不减，IP 越攒越多，进程内存慢慢涨上去。
      一天清一次就够，成本是 O(IP 数)，而每个请求都要走这里——
      所以用 _swept_on 记一下"今天清过了"，不要每次都扫。
    """
    stale = [k for k, v in _usage.items() if v[0] != today]
    for k in stale:
        del _usage[k]


def check_quota(ip: str) -> int:
    """用掉一次额度，返回剩余次数。超限抛 429。整个读取-判断-写入都在锁里。"""
    today = date.today().isoformat()

    with _quota_lock:
        if _swept_on[0] != today:
            _sweep_locked(today)
            _swept_on[0] = today

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
        # 限额记在进程内存里：单进程部署才是准的，多 worker 会各记一份（上限 ×N）。
        # 把它显式暴露出来，部署时一眼能看到"我这个实例的计数口径是什么"。
        "quota_scope": "process-memory",
        "trust_proxy": TRUST_PROXY,
    }


@app.post("/api/summarize", response_model=SummarizeOut)
def summarize(payload: SummarizeIn, request: Request) -> SummarizeOut:
    """单条摘要。核心逻辑全部复用 main.py 里已经测过的 Summarizer。"""
    # ★ 顺序很重要：先校验、后扣费。
    #   Pydantic 的 min_length=1 拦不住 " " 这种纯空白，
    #   以前的代码先 check_quota() 再 strip()，导致每次空白请求白扣一次额度
    #   （实测 25 次空白 → 400 ×25，但全站计数 0 → 25）。
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="正文不能是空白")

    remaining = check_quota(client_ip(request))

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
        sentences_used=bot.sentences_used,
        remaining=remaining,
    )


@app.post("/api/summarize/stream")
def summarize_stream(payload: SummarizeIn, request: Request):
    """
    流式摘要：模型吐一段就推一段，用户不用等整段生成完才看到第一个字。

    ★ 流式到底省了多少（实测，不是感觉）：
      被测文本 150 字、要求 3 句，首字 842ms、总耗时 1187ms；同一段走同步接口
      948ms。也就是说省下的是"生成那部分"（约 345ms），省不掉的是上游的
      首 token 延迟（这个跟流式没关系，谁来都要等）。
      **输出越长，省得越多**；输出本来就只有两句话，别再吹"快十倍"。
      30 篇语料压测的均值是 2.39s，那是长文本 + 多次调用的场景，别拿来当这条的背书。

    ★ 为什么用 SSE 而不是 WebSocket：
      这是"服务端单向持续推文本"的场景，没有双向通信需求。SSE 就是一个
      不关闭的 HTTP 响应，浏览器原生支持，不用协议升级、不用自己维护心跳，
      过代理和 CDN 也比 WebSocket 省事。

    ★ 为什么校验和扣额度必须在返回 StreamingResponse 之前做完：
      一旦开始返回流，HTTP 状态码就已经发出去了（默认 200）。这时候才发现
      "额度不足"或"正文空白"，只能塞进事件体里，前端得专门解析一遍——
      错误处理就分裂成两套。放在前面做，错误仍然是标准的 4xx/5xx。
    """
    # 顺序和 /api/summarize 保持一致：先校验、后扣费（空白正文不该吃额度）
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="正文不能是空白")

    remaining = check_quota(client_ip(request))      # 超限直接 429，根本进不到流

    bot = Summarizer(get_client(), max_sentences=payload.length)
    model = _model_name
    started = time.time()

    def event(obj: dict) -> str:
        # SSE 的格式是死的：必须以 "data: " 开头、以空行结尾。
        # ensure_ascii=False 是为了中文在网线上直接是 UTF-8，而不是一堆 \uXXXX。
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    def gen():
        buf: list[str] = []
        first_ms: int | None = None
        try:
            for piece in bot.summarize_stream(text):
                if first_ms is None:
                    first_ms = int((time.time() - started) * 1000)
                buf.append(piece)
                yield event({"delta": piece})
        except (RetryableError, FatalError) as e:
            # 流已经开始了，状态码改不了，只能用事件体把错误送回去，
            # 前端看到 {error} 就停止追加并展示提示。
            yield event({"error": str(e)})
            return

        full = bot.finalize_summary("".join(buf))
        yield event({
            "done": True,
            "summary": full.strip(),
            "sentences_used": bot.sentences_used,
            "sentences_got": count_sentences(full),
            "first_token_ms": first_ms,
            "latency_ms": int((time.time() - started) * 1000),
            "input_chars": len(text),
            "model": model,
            "remaining": remaining,
        })

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        # ★ 这一行不是可有可无：nginx 之类的反代会把响应缓冲起来攒够一批再发，
        #   于是前端等到最后一次性收到全部内容——"流式"名存实亡。
        "X-Accel-Buffering": "no",
    })


WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
INDEX_FILE = os.path.join(WEB_DIR, "index.html")

# 页面按 mtime 缓存：以前每个请求都 open() 一次磁盘（实测 3.4ms），
# 页面又不会变，纯属白读。但也不能读一次就永久缓存——
# 改了 index.html 却要重启服务才生效，调试时很难受。
# 折中：记住上次读到的 mtime，一样就直接用缓存。
_index_cache: dict = {"mtime": None, "body": ""}


def read_index() -> str:
    """读 web/index.html，文件没变就走内存缓存。"""
    mtime = os.path.getmtime(INDEX_FILE)
    if _index_cache["mtime"] != mtime:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            _index_cache["body"] = f.read()
        _index_cache["mtime"] = mtime
    return _index_cache["body"]


# 多页面的通用缓存：{文件名: {"mtime", "body"}}
# 复用同一套"按 mtime 失效"的思路，避免为每个页面各写一份缓存逻辑。
_page_cache: dict[str, dict] = {}


def read_page(filename: str) -> str:
    """读 web/ 下的任意页面，文件没变就用缓存。"""
    path = os.path.join(WEB_DIR, filename)
    mtime = os.path.getmtime(path)
    cached = _page_cache.get(filename)
    if cached is None or cached["mtime"] != mtime:
        with open(path, "r", encoding="utf-8") as f:
            cached = {"mtime": mtime, "body": f.read()}
        _page_cache[filename] = cached
    return cached["body"]


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


@app.post("/api/agent", response_model=AgentOut)
def agent_run(payload: AgentIn, request: Request) -> AgentOut:
    """
    Agent 接口：给一句自然语言指令，模型自己决定调哪些工具、调几次。

    ★ 和 /api/summarize 的本质区别：
      摘要是"一次输入 → 一次输出"，路径固定；
      Agent 是"输入 → 模型决定 → 工具执行 → 再决定 …… → 输出"，路径不固定。
      所以返回里除了 answer，还必须带 trace —— 否则用户看到结果也不知道它干了什么。

    ★ 为什么先扣额度再执行：
      一次 Agent 请求内部可能触发 2-6 次模型调用 + 若干次工具执行，
      成本远高于单次摘要。按"1 次请求 = 1 次额度"计，是刻意让额度口径简单可预期；
      真正上线时更合理的做法是按 token 或按步数计费，这里先用次数。
    """
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="指令不能是空白")

    remaining = check_quota(client_ip(request))

    # 延迟导入：agent.py 依赖 main.py，放在函数里导入可以保证
    # "agent 模块出问题时，/api/summarize 等既有接口照常可用"——
    # 一个功能挂掉不该拖垮整个服务。
    from agent import _CURRENT_CLIENT, ToolCallingClient, run_agent

    client = get_client()
    # summarize_text 工具需要 client，运行前绑定
    _CURRENT_CLIENT["client"] = client
    runner = ToolCallingClient(client.config, timeout=120)

    try:
        result = run_agent(query, runner, max_steps=payload.max_steps)
    except RetryableError as e:
        raise HTTPException(status_code=502, detail=f"上游模型服务不稳定，请稍后再试：{e}")
    except FatalError as e:
        raise HTTPException(status_code=502, detail=f"模型调用失败：{e}")

    return AgentOut(
        answer=result["answer"].strip(),
        trace=[AgentStep(**s) for s in result["trace"]],
        steps=result["steps"],
        elapsed_ms=result["elapsed_ms"],
        model=_model_name,
        remaining=remaining,
        hit_max_steps=bool(result.get("hit_max_steps")),
    )


# ============================================================
# 3.6) 知识库：入库 / 列表 / 检索
# ============================================================
# ★ 为什么这几个接口都不走 Agent：
#   入库和检索是"确定性操作"，返回什么完全由输入决定，中间没有"模型决定做什么"。
#   包成 Agent 只会多花一次模型调用、多几十秒延迟，还引入不确定性。
#   Agent 是给"不知道该用什么工具"的场景用的（/api/agent），不是给固定流程用的。

class KbUploadIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description="文档名，用于检索结果里标来源")
    # 20000 字约等于 50 块、5 批 embedding 请求，成本和时间都可控。
    # 不设上限的话，贴一本小说进来会跑几百批请求，前端早超时了。
    text: str = Field(..., min_length=1, max_length=20000)


class KbDocOut(BaseModel):
    id: int
    name: str
    n_chunks: int
    created_at: str


class KbSearchIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    top_k: int = Field(4, ge=1, le=10)


class KbHit(BaseModel):
    score: float
    doc: str
    doc_id: int
    chunk: int
    text: str


@app.post("/api/kb/upload")
def kb_upload(payload: KbUploadIn, request: Request) -> dict:
    """
    文档入库：切片 → 向量化 → 存 sqlite。

    ★ 扣额度：这一步会真金白银调 embedding 接口，所以和摘要一样算一次额度。
      检索本身也要调一次 embedding（问题向量化），但量太小，不单独扣。
    ★ 幂等：同样的内容重复上传直接返回旧的 doc_id。判断依据是
      内容 sha256，不是文件名——改个名传同一篇不会存两份。
    ★ 顺序很关键：先查重、再扣额度。反过来的话重复上传会白吃一次额度，
      而前端还在显示"没有重复消耗额度"，等于对用户说假话。
    """
    from kb import add_document, find_existing   # 延迟导入：知识库坏了不该拖垮摘要主流程

    conn = connect_kb()
    try:
        existing = find_existing(conn, payload.text)
        if existing is not None:
            # 没花 embedding 请求，就不扣额度；remaining 返回 -1 表示"本次未扣"
            return {
                "ok": True,
                "id": existing["id"],
                "name": existing["name"],
                "n_chunks": existing["n_chunks"],
                "skipped": True,
                "remaining": -1,
            }

        remaining = check_quota(client_ip(request))

        try:
            info = add_document(conn, payload.name.strip(), payload.text)
        except (FatalError, RetryableError) as e:
            raise HTTPException(status_code=502, detail=f"向量化失败：{e}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            # 典型是换了 embedding 模型导致维度不一致，属于冲突而不是入参错误
            raise HTTPException(status_code=409, detail=str(e))
    finally:
        conn.close()

    return {
        "ok": True,
        "id": info["id"],
        "name": info["name"],
        "n_chunks": info["n_chunks"],
        "skipped": info["skipped"],
        "remaining": remaining,
    }


@app.get("/api/kb/docs")
def kb_docs() -> dict:
    """列出知识库里的文档。前端每次入库/删除后都会重新拉一次。"""
    from kb import list_documents

    conn = connect_kb()
    try:
        docs = list_documents(conn)
    finally:
        conn.close()
    return {"ok": True, "docs": docs, "count": len(docs)}


@app.delete("/api/kb/docs/{doc_id}")
def kb_delete(doc_id: int) -> dict:
    """删文档，chunks 靠外键级联一起清掉。"""
    from kb import delete_document

    conn = connect_kb()
    try:
        ok = delete_document(conn, doc_id)
    finally:
        conn.close()
    if not ok:
        raise HTTPException(status_code=404, detail=f"文档 {doc_id} 不存在")
    return {"ok": True, "deleted": doc_id}


@app.post("/api/kb/search", response_model=list[KbHit])
def kb_search(payload: KbSearchIn) -> list[KbHit]:
    """
    纯检索接口（不经过模型生成回答），用来单独验证检索效果。

    ★ 为什么要有这个接口：Agent 的回答是"检索 + 生成"混合的结果，
      答得不好时分不清是检索没找到还是模型没用好。先看这里的前几名分数，
      就能把问题定位到检索层。
    """
    from kb import search

    try:
        conn = connect_kb()
        try:
            hits = search(conn, payload.query, top_k=payload.top_k)
        finally:
            conn.close()
    except (FatalError, RetryableError) as e:
        raise HTTPException(status_code=502, detail=f"向量化失败：{e}")
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return [
        KbHit(score=h["score"], doc=h["doc"], doc_id=h["doc_id"],
              chunk=h["ord"], text=h["text"])
        for h in hits
    ]


@app.get("/kb", response_class=HTMLResponse)
def kb_page():
    """知识库页面：上传文档、看列表、试检索。"""
    try:
        return read_page("kb.html")
    except FileNotFoundError:
        return HTMLResponse(
            "<h1>web/kb.html 找不到</h1><p>页面文件没有跟着部署包一起上传。</p>",
            status_code=500,
        )


@app.get("/agent", response_class=HTMLResponse)
def agent_page():
    """
    Agent 演练场页面。

    ★ 为什么不直接改首页：首页 721 行，承载着摘要主流程、云服务 SDK、
      流式渲染和历史统计。为了加一个功能去改它，风险远大于收益。
      新开一个页面 = 零风险试错，跑顺了再考虑合并。
    """
    try:
        return read_page("agent.html")
    except FileNotFoundError:
        return HTMLResponse(
            "<h1>web/agent.html 找不到</h1><p>页面文件没有跟着部署包一起上传。</p>",
            status_code=500,
        )


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
        return read_index()
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
