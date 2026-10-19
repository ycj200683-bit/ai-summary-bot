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


def _load_dotenv(path: str = ".env") -> None:
    """
    极简 .env 读取（不引入 python-dotenv 这种额外依赖）。

    规则只有三条：
      ① 一行一个 KEY=VALUE；空行和 # 开头的行跳过
      ② 已经存在的环境变量不覆盖（setdefault）——真实环境变量优先级高于文件
      ③ 值两端的引号剥掉

    ★ 为什么不把 key 写死在代码里：
      .env 已经在 .gitignore 里，进不了仓库；代码里硬编码则一推就公开了。
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


# 必须在任何 Config.from_env() 之前执行
_load_dotenv()


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


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


# ============================================================
# 5) 前端页面（单文件，不依赖任何 CDN，离线也能开）
# ============================================================
PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 摘要服务</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Microsoft YaHei", sans-serif;
    background: #f6f7f9; color: #1a1a1a; line-height: 1.6;
    min-height: 100vh; padding: 32px 16px 64px;
  }
  .wrap { max-width: 760px; margin: 0 auto; }
  header { margin-bottom: 24px; }
  h1 { font-size: 26px; font-weight: 700; letter-spacing: -0.3px; }
  h1 span { color: #4f46e5; }
  .sub { color: #6b7280; font-size: 14px; margin-top: 6px; }
  .card {
    background: #fff; border: 1px solid #e5e7eb; border-radius: 14px;
    padding: 20px; box-shadow: 0 1px 2px rgba(0,0,0,.04);
  }
  textarea {
    width: 100%; min-height: 180px; padding: 14px; font-size: 15px;
    font-family: inherit; line-height: 1.7; color: #111827;
    border: 1px solid #d1d5db; border-radius: 10px; resize: vertical;
    outline: none; transition: border-color .15s;
  }
  textarea:focus { border-color: #4f46e5; box-shadow: 0 0 0 3px rgba(79,70,229,.1); }
  .row { display: flex; align-items: center; gap: 12px; margin-top: 14px; flex-wrap: wrap; }
  .count { font-size: 13px; color: #9ca3af; font-variant-numeric: tabular-nums; }
  .count.over { color: #dc2626; font-weight: 600; }
  select {
    padding: 8px 10px; border: 1px solid #d1d5db; border-radius: 8px;
    background: #fff; font-size: 14px; font-family: inherit; color: #374151;
  }
  button {
    margin-left: auto; padding: 10px 22px; font-size: 15px; font-weight: 600;
    color: #fff; background: #4f46e5; border: none; border-radius: 9px;
    cursor: pointer; transition: background .15s, transform .05s;
  }
  button:hover:not(:disabled) { background: #4338ca; }
  button:active:not(:disabled) { transform: translateY(1px); }
  button:disabled { background: #a5b4fc; cursor: not-allowed; }
  .out { margin-top: 20px; display: none; }
  .out.show { display: block; }
  .out-head {
    display: flex; align-items: center; gap: 10px; margin-bottom: 10px;
    font-size: 13px; color: #6b7280;
  }
  .badge {
    background: #eef2ff; color: #4338ca; padding: 3px 9px; border-radius: 6px;
    font-weight: 600; font-variant-numeric: tabular-nums;
  }
  .body {
    background: #fafafa; border: 1px solid #eee; border-radius: 10px;
    padding: 16px; font-size: 15px; white-space: pre-wrap; color: #1f2937;
  }
  .err { background: #fef2f2; border-color: #fecaca; color: #b91c1c; }
  footer { margin-top: 28px; font-size: 13px; color: #9ca3af; text-align: center; }
  footer a { color: #6b7280; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>AI <span>摘要</span>服务</h1>
    <p class="sub">贴一段中文，返回三句话以内的要点。单次上限 5000 字，每人每天 20 次。</p>
  </header>

  <div class="card">
    <textarea id="text" placeholder="把要摘要的正文粘在这里，例如一篇新闻报道、一段会议记录、一份论文摘要……"></textarea>
    <div class="row">
      <span class="count" id="count">0 / 5000</span>
      <select id="len">
        <option value="2">2 句话</option>
        <option value="3" selected>3 句话</option>
        <option value="5">5 句话</option>
      </select>
      <button id="go">生成摘要</button>
    </div>
  </div>

  <div class="out" id="out">
    <div class="out-head">
      <span id="status"></span>
      <span class="badge" id="latency"></span>
      <span class="badge" id="model"></span>
    </div>
    <div class="body" id="body"></div>
  </div>

  <footer>
    后端 FastAPI · 前端原生 JS · 模型接口 OpenAI 兼容协议<br>
    接口文档见 <a href="/docs">/docs</a> · 健康检查 <a href="/api/health">/api/health</a>
  </footer>
</div>

<script>
const MAX = 5000;
const $ = (id) => document.getElementById(id);
const ta = $('text');

ta.addEventListener('input', () => {
  const n = ta.value.length;
  $('count').textContent = n + ' / ' + MAX;
  $('count').classList.toggle('over', n > MAX);
});

$('go').addEventListener('click', async () => {
  const text = ta.value.trim();
  const out = $('out'), body = $('body'), status = $('status');

  if (!text) { alert('先贴点内容'); return; }
  if (text.length > MAX) { alert('超过 5000 字了，删短一点'); return; }

  const btn = $('go');
  btn.disabled = true;
  btn.textContent = '生成中…';

  try {
    const resp = await fetch('/api/summarize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text, length: parseInt($('len').value, 10) })
    });
    const data = await resp.json();

    out.classList.add('show');
    body.classList.remove('err');

    if (!resp.ok) {
      status.textContent = '请求失败';
      body.classList.add('err');
      body.textContent = (data && data.detail) ? data.detail : ('HTTP ' + resp.status);
      $('latency').textContent = '';
      $('model').textContent = '';
      return;
    }

    status.textContent = '摘要结果';
    body.textContent = data.summary;
    $('latency').textContent = data.latency_ms + ' ms · ' + data.input_chars + ' 字';
    $('model').textContent = data.model;
  } catch (e) {
    out.classList.add('show');
    body.classList.add('err');
    status.textContent = '出错了';
    body.textContent = String(e);
  } finally {
    btn.disabled = false;
    btn.textContent = '生成摘要';
  }
});
</script>
</body>
</html>"""


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
