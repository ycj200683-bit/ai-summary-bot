"""
kb.py —— 知识库检索层：文档切片 → 向量化 → sqlite 存储 → 相似度检索

设计取舍（这些是面试真正会被问到的点，写在代码里而不是事后回忆）：

1) 为什么用 sqlite 而不是向量数据库
   这个项目的知识库规模在"几百到几千块"这个量级。sqlite 单文件、零服务、
   备份就是拷一个文件，Render 免费层也能跑；pgvector 得额外起 Postgres，
   为了几千行向量多一个故障点是净亏。规模真上去了再换，接口不用动。

2) 为什么向量存 BLOB 而不是 JSON
   1024 维 float32 = 4096 字节；写成 JSON 文本大约 20KB，而且每次读都要
   json.loads 一遍（几千块就是几十万次浮点解析）。BLOB 直接 frombytes 成
   array，读出来就能算。

3) 为什么入库时就把向量归一化
   余弦相似度 = 点积 / (|a||b|)。如果两边都是单位向量，分母恒为 1，
   检索时只剩点积。省掉每次查询给全库向量算模长的开销——这是最大头的一笔。

4) 为什么不用 numpy
   本机没装，也不想为了几千次点积给部署加一个二进制依赖。
   纯 Python 用 sum(map(operator.mul, a, b))，循环在 C 层跑，1024 维也就微秒级。

命令行自测（不用起 Web 服务）：
    python kb.py add 某文章.txt
    python kb.py list
    python kb.py search "你关心的问题" -k 5
    python kb.py drop <doc_id>
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from array import array
from collections import Counter
from operator import mul
from pathlib import Path

import requests

from main import Config, FatalError, RetryableError, load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
KB_DIR = Path(os.getenv("KB_DIR") or (PROJECT_DIR / "data"))
DB_PATH = KB_DIR / "kb.sqlite3"

# 百炼兼容模式下的 embedding 模型。和聊天模型不是同一个，要单独配。
# v3 默认 1024 维；换模型会让历史向量全部失效，所以维度要写进 meta 表做校验。
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-v3")
EMBED_BATCH = 10          # 单次请求最多带几条文本（太大容易 400 / 超时）

# 切片参数。chunk 太小 → 检索准但答案缺上下文；太大 → 上下文全但相似度被稀释。
# 中文 400 字约等于 2-3 段，是"一段话能自洽"的常见尺度。
CHUNK_SIZE = int(os.getenv("KB_CHUNK_SIZE", "600"))
CHUNK_OVERLAP = int(os.getenv("KB_CHUNK_OVERLAP", "80"))


# ============================================================
# 1) 切片
# ============================================================
# 切在句子边界上，别把一句话劈开：劈开之后这句话在任何一个块里都读不通，
# 检索命中它也没用。找不到标点才硬切。
_SENT_END = "。！？；!?;\n"


def _find_cut(text: str, limit: int) -> int:
    """在 limit 附近找一个合适的断开位置，优先句末标点。"""
    window_start = max(1, limit - 80)          # 往前 80 字内找标点，找不到就硬切
    for i in range(min(limit, len(text)), window_start, -1):
        if text[i - 1] in _SENT_END:
            return i
    return limit


def split_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    把长文本切成带重叠的块。

    两步走：
      ① 先按空行切段，超长段落再按句末标点切碎 —— 保证没有单块超过 chunk_size；
      ② 再把小段贪心拼接成接近 chunk_size 的块，块之间保留 overlap 字重叠。

    ★ 为什么要重叠：
      答案往往横跨两个块的边界。不重叠的话"上一块结尾 + 下一块开头"这句
      谁都检索不到。80 字重叠是最小代价买到"边界不漏"。
    """
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []

    # ① 切段 + 切碎超长段
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        while len(para) > chunk_size:
            cut = _find_cut(para, chunk_size)
            pieces.append(para[:cut].strip())
            para = para[cut:].strip()
        if para:
            pieces.append(para)

    # ② 贪心聚合
    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        merged = f"{buf}\n{piece}" if buf else piece
        if len(merged) <= chunk_size:
            buf = merged
            continue
        if buf:
            chunks.append(buf)
        tail = buf[-overlap:] if (buf and overlap > 0) else ""
        buf = tail + piece
        # 极端情况：某段本身就比 chunk_size 大（理论上不会，兜底防死循环）
        while len(buf) > chunk_size:
            cut = _find_cut(buf, chunk_size)
            chunks.append(buf[:cut].strip())
            buf = buf[cut:].strip()
    if buf.strip():
        chunks.append(buf.strip())

    return [c for c in chunks if c.strip()]


# ============================================================
# 2) 向量化
# ============================================================
def _embed_url(api_url: str) -> str:
    """从 chat 的地址推出 embedding 的地址（同一套 OpenAI 兼容前缀）。"""
    base = api_url.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base + "/embeddings"


def _normalize(vec: list[float]) -> array:
    """转成 array('f') 并做 L2 归一化。零向量原样返回，避免除零。"""
    arr = array("f", vec)
    norm = sum(map(mul, arr, arr)) ** 0.5
    if norm > 0:
        arr = array("f", (v / norm for v in arr))
    return arr


def embed_texts(texts: list[str], config: Config | None = None,
                batch_size: int = EMBED_BATCH, max_tries: int = 3) -> list[array]:
    """
    批量向量化，返回已归一化的 array('f')。

    ★ 为什么要分批：一次塞几百条会被上游拒掉（400 或超时）。
      分批 + 每批独立重试，坏一批不影响已经成功的批次。
    ★ 为什么重试只针对 RetryableError：401/402 这种重试一百次也一样，
      立刻失败并说清原因，比让用户等 3 轮退避更有用。
    """
    if config is None:
        config = Config.from_env()
    if not texts:
        return []

    url = _embed_url(config.api_url)
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    out: list[array] = []

    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        last_err: Exception | None = None
        for attempt in range(1, max_tries + 1):
            try:
                resp = requests.post(
                    url, headers=headers,
                    json={"model": EMBED_MODEL, "input": batch},
                    timeout=60,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # 上游可能不保证返回顺序，按 index 排回去
                    items = sorted(data["data"], key=lambda d: d.get("index", 0))
                    out.extend(_normalize(it["embedding"]) for it in items)
                    last_err = None
                    break
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_err = RetryableError(f"embedding 上游 {resp.status_code}: {resp.text[:200]}")
                else:
                    raise FatalError(f"embedding 请求被拒（HTTP {resp.status_code}）：{resp.text[:300]}")
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_err = RetryableError(f"embedding 网络异常：{exc}")

            if attempt < max_tries:
                time.sleep(1.5 * attempt)      # 简单线性退避，够用
        if last_err is not None:
            raise last_err

    return out


# ============================================================
# 3) 存储
# ============================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    n_chunks     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord    INTEGER NOT NULL,
    text   TEXT NOT NULL,
    vec    BLOB NOT NULL,
    dim    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
"""


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """打开连接并建表。外键要显式打开，sqlite 默认不级联。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _content_hash(text: str) -> str:
    """内容指纹：同一篇文档重复上传时用它去重，避免库里堆重复块。"""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]


def find_existing(conn: sqlite3.Connection, text: str) -> dict | None:
    """
    按内容指纹查是否已入库。

    ★ 为什么单独暴露这个函数：额度要在"确定会花 embedding 请求"之后才扣。
      如果先扣额度再查重，重复上传会白白吃掉一次额度，而前端还在告诉用户
      "没有重复消耗额度"——那就是对用户说假话。先查后扣才对。
    """
    digest = _content_hash(text)
    row = conn.execute(
        "SELECT id, name, n_chunks FROM documents WHERE content_hash = ?", (digest,)
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "n_chunks": row[2]}


def add_document(conn: sqlite3.Connection, name: str, text: str,
                 config: Config | None = None,
                 chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> dict:
    """
    入库一篇文档。已存在（内容指纹相同）就直接返回旧的，不重复消耗 embedding 额度。

    chunk_size / overlap 暴露成参数是为了能做参数对比（评测脚本要按不同块大小建库）。
    默认值仍是模块常量，线上行为不变。

    返回 {"id", "name", "n_chunks", "skipped"}
    """
    digest = _content_hash(text)
    existing = find_existing(conn, text)
    if existing is not None:
        return {**existing, "skipped": True}

    pieces = split_text(text, chunk_size=chunk_size, overlap=overlap)
    if not pieces:
        raise ValueError("文档内容为空，切不出任何块")

    vectors = embed_texts(pieces, config=config)
    if len(vectors) != len(pieces):
        raise RuntimeError(f"向量数({len(vectors)}) 和块数({len(pieces)}) 对不上")

    dim = len(vectors[0])
    stored = conn.execute("SELECT value FROM meta WHERE key = 'dim'").fetchone()
    if stored and int(stored[0]) != dim:
        raise RuntimeError(
            f"向量维度不一致：库里是 {stored[0]} 维，当前模型 {EMBED_MODEL} 产出 {dim} 维。"
            "换过 embedding 模型的话需要先清空重建（python kb.py reset）。"
        )

    cur = conn.execute(
        "INSERT INTO documents (name, content_hash, n_chunks, created_at) VALUES (?, ?, ?, ?)",
        (name, digest, len(pieces), time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    doc_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO chunks (doc_id, ord, text, vec, dim) VALUES (?, ?, ?, ?, ?)",
        [(doc_id, i, piece, vectors[i].tobytes(), dim) for i, piece in enumerate(pieces)],
    )
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('dim', ?)", (str(dim),))
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('model', ?)", (EMBED_MODEL,))
    conn.commit()
    return {"id": doc_id, "name": name, "n_chunks": len(pieces), "skipped": False}


def list_documents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, n_chunks, created_at FROM documents ORDER BY id DESC"
    ).fetchall()
    return [
        {"id": r[0], "name": r[1], "n_chunks": r[2], "created_at": r[3]}
        for r in rows
    ]


def delete_document(conn: sqlite3.Connection, doc_id: int) -> bool:
    """删文档，靠外键级联把 chunks 一起删掉。"""
    cur = conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    conn.commit()
    return cur.rowcount > 0


# ============================================================
# 4) 检索
# ============================================================
def search(conn: sqlite3.Connection, query: str, top_k: int = 5,
           config: Config | None = None, hybrid: bool = True) -> list[dict]:
    """
    检索：默认走**混合检索**（向量 + 关键词，RRF 秩融合），hybrid=False 退化为纯向量。

    返回 [{"score", "lexical", "fused", "text", "doc", "doc_id", "ord"}]，按 fused 从高到低。
    其中 `score` 仍然是余弦相似度（0~1 口径不变，前端的分档着色继续有效），
    `fused` 才是排序依据——所以可能看到"第 1 名的余弦比第 2 名低"，那不是 bug，
    正是关键词这一路把答案顶上来的结果。

    ★ 为什么加混合检索（这是评测数据逼出来的，不是拍脑袋）：
      20 题实测里，纯向量的 top1 只有 30%（400 字块）。失败最多的一类是
      "只提到关键词但没给答案"的清单式段落——术语密度高、向量相似度高，
      但里面根本没内容（典型：问"换 Redis 怎么保证原子性"，排在第一位的是
      「已知限制」里那句"下一步换 Redis"，只比真答案高 0.007）。
      纯向量对这种段落没有免疫力；关键词那一路则恰好相反——它对术语敏感，
      但会被同义词/换述骗。两路互补，融合才有意义。

    ★ 为什么用 RRF 而不是加权求和：
      余弦是 0~1 的量，BM25 没有上界（几十到几百都可能），
      加权求和必须先把两者归一化，而归一化方式本身就是个要调的参数
      （min-max？除以最大值？对每批结果都一样吗？）。
      RRF 只用**名次**：score = Σ 1/(k+rank)。名次天然可比，不用调权重，
      是工业界融合多路检索的常用默认解。k 取 60 是老经验值：
      它决定"名次差一点"和"名次差很多"的区分度。
    """
    query = (query or "").strip()
    if not query:
        return []

    qvec = embed_texts([query], config=config)[0]
    dim = len(qvec)

    rows = conn.execute(
        "SELECT c.doc_id, c.ord, c.text, c.vec, c.dim, d.name "
        "FROM chunks c JOIN documents d ON d.id = c.doc_id"
    ).fetchall()
    if not rows:
        return []

    if rows[0][4] != dim:
        raise RuntimeError(
            f"库里向量是 {rows[0][4]} 维，当前查询向量是 {dim} 维，无法比较。"
            "换过 embedding 模型的话需要先清空重建（python kb.py reset）。"
        )

    q_tokens = set(_tokenize(query))
    use_lexical = hybrid and bool(q_tokens)

    # ---------- 一遍扫描，同时算余弦和 BM25 ----------
    # 中文没有空格，只能靠 n-gram 兜底分词；先算 df（包含该词的块数）再算 IDF，
    # IDF 的意义是"越少见的词越有区分度"，这正是纯向量缺的那部分信息。
    items: list[dict] = []
    df: Counter = Counter()
    tokenized: list[list[str]] = []
    for _doc_id, _ord, text, _blob, _dim, _name in rows:
        toks = _tokenize(text)
        tokenized.append(toks)
        if use_lexical:
            present = q_tokens & set(toks)
            for t in present:
                df[t] += 1

    total = len(rows)
    avgdl = (sum(len(t) for t in tokenized) / total) if total else 1.0

    for idx, (doc_id, ord_, text, blob, _dim, doc_name) in enumerate(rows):
        vec = array("f")
        vec.frombytes(blob)
        # 两边都已归一化 → 点积就是余弦相似度，不必再除模长
        cosine = sum(map(mul, qvec, vec))

        lex = 0.0
        if use_lexical:
            toks = tokenized[idx]
            dl = len(toks) or 1
            tf_all = Counter(t for t in toks if t in q_tokens)
            if tf_all:
                # BM25：k1 控制词频饱和（同一个词出现 10 次不该比 5 次强一倍），
                # b 控制长度归一化强度（长块不该因为字多就占便宜）
                k1, b = 1.5, 0.75
                for t, tf in tf_all.items():
                    idf = math.log(1 + (total - df[t] + 0.5) / (df[t] + 0.5))
                    lex += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))

        items.append({
            "score": round(cosine, 4),
            "lexical": round(lex, 4),
            "text": text,
            "doc": doc_name,
            "doc_id": doc_id,
            "ord": ord_,
            "_cos": cosine,
            "_lex": lex,
        })

    if not use_lexical:
        items.sort(key=lambda x: x["_cos"], reverse=True)
        for it in items:
            it["fused"] = it["score"]
        return [_strip_internal(it) for it in items[:top_k]]

    # ---------- RRF 融合 ----------
    K = 60
    by_cos = sorted(range(len(items)), key=lambda i: items[i]["_cos"], reverse=True)
    by_lex = sorted((i for i in range(len(items)) if items[i]["_lex"] > 0),
                    key=lambda i: items[i]["_lex"], reverse=True)

    fused: Counter = Counter()
    for rank, i in enumerate(by_cos, 1):
        fused[i] += 1.0 / (K + rank)
    for rank, i in enumerate(by_lex, 1):
        fused[i] += 1.0 / (K + rank)

    for i, it in enumerate(items):
        it["fused"] = round(fused.get(i, 0.0), 6)

    # 只按关键词命中的块也要能进来，所以排序集合是 fused 的并集。
    # ★ 打平时（fused 相同）以关键词分为准：字面精确命中比余弦相似度更可信。
    #   这不是理论洁癖——只有两条候选时 RRF 会出现对称（一条余弦第一、关键词第二，
    #   另一条正好相反，两者 fused 完全相等），没有裁决规则结果就是随机顺序。
    items.sort(key=lambda x: (x["fused"], x["_lex"], x["_cos"]), reverse=True)
    return [_strip_internal(it) for it in items[:top_k]]


def _tokenize(text: str) -> list[str]:
    """
    极简分词：中文按二字滑窗，英文/数字/下划线按词切。

    ★ 为什么不引 jieba：这个项目的检索层刻意保持零额外依赖（连 numpy 都没引）。
      二字滑窗的代价是会切出一些无意义片段，但对 BM25 影响有限——
      IDF 会自动把高频无意义片段（"的是""怎么"）的权重压到接近 0。
    ★ 为什么英文要连下划线：代码标识符是整体概念，
      `max_steps` 拆成 max 和 steps 就丢掉了它作为一个符号的辨识度。
    """
    tokens: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fa5]+", text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    tokens.extend(w.lower() for w in re.findall(r"[A-Za-z0-9_]{2,}", text))
    return tokens


def _strip_internal(item: dict) -> dict:
    """把内部用于排序的字段去掉，别让它们在接口响应里露出来。"""
    return {k: v for k, v in item.items() if not k.startswith("_")}


# ============================================================
# 5) 命令行自测
# ============================================================
def _cli() -> int:
    load_dotenv(str(PROJECT_DIR / ".env"))
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1

    cmd, rest = args[0], args[1:]
    conn = connect()

    if cmd == "add":
        if not rest:
            print("用法：python kb.py add <文件路径>")
            return 1
        path = Path(rest[0])
        text = path.read_text(encoding="utf-8")
        info = add_document(conn, path.name, text)
        flag = "（内容相同，跳过）" if info["skipped"] else ""
        print(f"已入库 doc_id={info['id']}  {info['name']}  {info['n_chunks']} 块 {flag}")
        return 0

    if cmd == "list":
        docs = list_documents(conn)
        if not docs:
            print("知识库还是空的")
            return 0
        print(f"{'id':>4}  {'块数':>4}  {'创建时间':<20}  名称")
        for d in docs:
            print(f"{d['id']:>4}  {d['n_chunks']:>4}  {d['created_at']:<20}  {d['name']}")
        return 0

    if cmd == "search":
        top_k = 5
        if "-k" in rest:
            idx = rest.index("-k")
            top_k = int(rest[idx + 1])
            rest = rest[:idx] + rest[idx + 2:]
        query = " ".join(rest)
        hits = search(conn, query, top_k=top_k)
        if not hits:
            print("没检索到内容（知识库是空的？）")
            return 0
        for i, h in enumerate(hits, 1):
            print(f"\n[{i}] score={h['score']}  来源={h['doc']} 第{h['ord']}块")
            print("    " + h["text"][:200].replace("\n", " "))
        return 0

    if cmd == "drop":
        if not rest:
            print("用法：python kb.py drop <doc_id>")
            return 1
        ok = delete_document(conn, int(rest[0]))
        print("已删除" if ok else "没找到这个 doc_id")
        return 0 if ok else 1

    if cmd == "reset":
        for table in ("chunks", "documents", "meta"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        print("知识库已清空")
        return 0

    print(f"不认识的命令：{cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
