# 项目① AI 摘要服务 —— 生产镜像
#
# 为什么这么写，逐条记在下面注释里（面试问"为什么不用多阶段构建"也能答上）。
#
# ⚠️ 状态：本镜像在本机**没有 build 过**（开发机上没装 Docker）。
#    已在等价环境下验证过的是"应用行为"：PORT 从外部注入、KB_DIR 指向独立目录、
#    重启后知识库仍在（对应 compose 里的 volume）。build 这一步待首次有 Docker 的机器补验。

FROM python:3.12-slim

# · PYTHONDONTWRITEBYTECODE：容器里不需要 .pyc，少写一堆文件
# · PYTHONUNBUFFERED：不加的话 stdout 会被缓冲，docker logs 看不到实时日志，
#   出问题时只能等进程退出才刷出来，排查基本靠猜
# · PIP_NO_CACHE_DIR：pip 缓存留在镜像里纯占体积
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    KB_DIR=/data

WORKDIR /app

# ★ 先只拷依赖清单再 install，最后才拷代码。
#   Docker 的层缓存是按"这一层的输入有没有变"判断的：代码天天改、依赖很少改，
#   这样改代码时不会重新装一遍依赖（否则每次 build 都要多等几十秒）。
COPY requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

# ★ 只拷 Web 服务真正需要的文件。
#   故意不拷 batch.py：它 import pandas，而 Web 镜像装的是 requirements-web.txt
#   （不含 pandas，体积小很多）。把用不了的脚本塞进镜像只会误导后面维护的人。
COPY main.py app.py agent.py kb.py ./
COPY web ./web

# ★★ 这一步是整个 Dockerfile 最容易漏、也最容易导致"容器起不来"的地方：
#   main.py 第 84 行是 os.makedirs("logs") + FileHandler("logs/app.log")，路径相对 CWD。
#   如果以非 root 用户运行、而 /app 归 root，这一行会在 import 阶段抛 PermissionError，
#   容器直接退出，日志里只看得到一行 Traceback。
#   所以：先建好 logs/ 和 /data（知识库卷的挂载点），再把属主交给运行用户。
#
#   顺便，为什么用非 root：容器里以 root 跑，一旦应用被打穿就等于把 root 递出去。
#   uid 固定成 10001 是为了和宿主机映射时好排查。
RUN useradd -m -u 10001 appuser \
 && mkdir -p /app/logs /data \
 && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8000

# 健康检查不额外装 curl（slim 镜像里没有），直接用 python 打自己的接口。
# ${PORT} 用 shell 形式才能展开，所以写 CMD 而不是 CMD []
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os,sys,urllib.request; \
port=os.getenv('PORT','8000'); \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/api/health' % port, timeout=2).status == 200 else 1)"

# 用 sh -c 形式启动，才能吃到 PORT 环境变量（exec 形式不会展开变量）
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
