"""
agent_store.py —— Agent 执行轨迹的落库与查询

为什么要有这个模块（面试会问"为什么不用现成的"）：

1) **为什么不把 steps 塞成 JSON 存一列**
   因为真正想回答的问题是"哪个工具最常失败""平均耗时多少"。
   塞成 JSON 之后每个这类问题都要把全表读出来在 Python 里算；
   拆成两张表，一句 `GROUP BY tool` 就出来了。表的形状应该跟着问题走。

2) **为什么单独一个 sqlite 文件，不塞进 kb.sqlite3**
   两类数据的读写模式完全不同：trace 是"每次请求写一批、频率高"，
   知识库是"偶尔批量写、大量读"。放一个库里，trace 的写锁会跟检索抢；
   分开之后互不干扰，备份/清理也可以各管各的。

3) **为什么要有保留上限**
   trace 会无限增长，几周就能把磁盘吃满。默认只留最近 500 次运行，
   每次写入后顺手淘汰更早的——用一个自增主键排序就够了，不用清理定时器。

4) **写入失败绝不能影响响应**
   Agent 已经跑完了、答案已经在手上了，这时候落库失败只是"少了一份存档"。
   让用户看到一个 500 是荒谬的。所以调用方必须包 try/except，这里也保证
   save_run 要么完整写入、要么整体回滚。

表结构：
    runs(id, query, answer, n_steps, elapsed_ms, model, hit_max_steps, ok, created_at)
    steps(id, run_id → runs(id) ON DELETE CASCADE, step, tool, args, result, error, ok, elapsed_ms)
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

# 和知识库共用同一个数据目录：容器里就是那个 /data 卷，重新部署不会丢。
KB_DIR = Path(os.getenv("KB_DIR") or (Path(__file__).resolve().parent / "data"))
AGENT_DB = Path(os.getenv("AGENT_DB") or (KB_DIR / "agent.sqlite3"))

# 只保留最近 N 次运行。1000 条 trace 大约几 MB，足够演示与排查。
MAX_RUNS = int(os.getenv("AGENT_MAX_RUNS", "500"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    query         TEXT NOT NULL,
    answer        TEXT NOT NULL,
    n_steps       INTEGER NOT NULL,
    elapsed_ms    INTEGER NOT NULL,
    model         TEXT,
    hit_max_steps INTEGER NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step       INTEGER NOT NULL,
    tool       TEXT NOT NULL,
    args       TEXT,
    result     TEXT,
    error      TEXT,
    ok         INTEGER NOT NULL,
    elapsed_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_id_desc ON runs(id DESC);
"""


def connect(db_path: Path | str = AGENT_DB) -> sqlite3.Connection:
    """打开连接并建表。外键要显式打开，否则级联删除不生效。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def save_run(conn: sqlite3.Connection, query: str, answer: str, trace: list[dict],
             elapsed_ms: int, model: str | None = None,
             hit_max_steps: bool = False) -> int:
    """
    存一次完整运行。runs + steps 写在同一个事务里，要么全成、要么全不成。

    返回 run_id。
    """
    ok_all = all(bool(s.get("ok", True)) for s in trace) if trace else True

    try:
        cur = conn.execute(
            "INSERT INTO runs (query, answer, n_steps, elapsed_ms, model, hit_max_steps, ok, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (query, answer, len(trace), elapsed_ms, model,
             1 if hit_max_steps else 0, 1 if ok_all else 0,
             time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        run_id = cur.lastrowid

        rows = []
        for s in trace:
            args = s.get("args")
            rows.append((
                run_id,
                int(s.get("step", 0)),
                str(s.get("tool") or ""),
                # args 是 dict，统一序列化；ensure_ascii=False 让中文在库里可读，
                # 排查时能直接 SQL 查出来看，不用写脚本反序列化
                json.dumps(args, ensure_ascii=False) if args is not None else None,
                s.get("result"),
                s.get("error"),
                1 if s.get("ok") else 0,
                int(s.get("elapsed_ms") or 0),
            ))
        if rows:
            conn.executemany(
                "INSERT INTO steps (run_id, step, tool, args, result, error, ok, elapsed_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

        # 顺手淘汰旧记录。放在同一个事务里，避免"写进去了但没清理"。
        conn.execute(
            "DELETE FROM runs WHERE id <= (SELECT MAX(id) - ? FROM runs)", (MAX_RUNS,)
        )
        conn.commit()
        return run_id
    except Exception:
        conn.rollback()
        raise


def list_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """
    最近若干次运行。**不返回 steps**——列表页只要摘要，
    把每条的 steps 都带出来会让这个接口随数据量线性变慢。
    """
    rows = conn.execute(
        "SELECT id, query, n_steps, elapsed_ms, model, hit_max_steps, ok, created_at "
        "FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "id": r[0],
            # 指令截断：列表里放 60 字够认出来是哪次，也不必传整段长文本
            "query": (r[1][:60] + "…") if len(r[1]) > 60 else r[1],
            "n_steps": r[2],
            "elapsed_ms": r[3],
            "model": r[4],
            "hit_max_steps": bool(r[5]),
            "ok": bool(r[6]),
            "created_at": r[7],
        }
        for r in rows
    ]


def get_run(conn: sqlite3.Connection, run_id: int) -> dict | None:
    """取一次运行的完整记录（含每一步）。找不到返回 None，由路由层翻成 404。"""
    row = conn.execute(
        "SELECT id, query, answer, n_steps, elapsed_ms, model, hit_max_steps, ok, created_at "
        "FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return None

    steps = conn.execute(
        "SELECT step, tool, args, result, error, ok, elapsed_ms "
        "FROM steps WHERE run_id = ? ORDER BY step, id", (run_id,)
    ).fetchall()

    return {
        "id": row[0],
        "query": row[1],
        "answer": row[2],
        "n_steps": row[3],
        "elapsed_ms": row[4],
        "model": row[5],
        "hit_max_steps": bool(row[6]),
        "ok": bool(row[7]),
        "created_at": row[8],
        "trace": [
            {
                "step": s[0],
                "tool": s[1],
                # 存的时候是 JSON 字符串，取出来还原成对象，前端才不用二次解析
                "args": json.loads(s[2]) if s[2] else {},
                "result": s[3],
                "error": s[4],
                "ok": bool(s[5]),
                "elapsed_ms": s[6],
            }
            for s in steps
        ],
    }


def tool_stats(conn: sqlite3.Connection) -> list[dict]:
    """
    按工具聚合：调用次数、失败次数、平均耗时。

    ★ 这个接口是"落库"这件事的价值所在——不落库就只能说"我有 trace"，
      落库之后能说"我知道每个工具的失败率是多少"。一句话的差别。
    """
    rows = conn.execute(
        "SELECT tool, COUNT(*) AS calls, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS fails, "
        "CAST(AVG(elapsed_ms) AS INTEGER) AS avg_ms "
        "FROM steps GROUP BY tool ORDER BY calls DESC"
    ).fetchall()
    return [
        {"tool": r[0], "calls": r[1], "fails": r[2], "avg_ms": r[3]}
        for r in rows
    ]
