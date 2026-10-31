"""
eval_retrieval.py —— 检索评测：不同切片参数下，同一批问题的召回表现

为什么要有这个脚本（面试会问"你怎么知道检索变好了"）：

在此之前，判断检索好坏的唯一依据是"手工试了几个 query，看起来还行"。
这不是评测，这是印象。有了这个脚本，改切片参数、换 embedding 模型、
加不加混合检索，都能拿同一把尺子量一次。

评测设计：

1) **语料**：用项目自己的 README（约 1000 行、2.8 万字）。选它的理由不是方便，
   而是它同时包含表格、代码块、长短段落、中英混排——比纯散文更接近真实文档。

2) **标准答案用"锚点字符串"而不是"块编号"**。
   这是这个脚本最关键的设计：块编号依赖切片参数（改 chunk_size 后第 33 块
   就变成别的内容了），锚点字符串则跨参数稳定——只要命中的块里包含那段特征文本，
   就算命中。否则换参数后基准全废，评测就变成自欺欺人。

3) **只变 chunk_size，overlap 固定 80**。
   两个变量一起动，结果无法归因。代价是不同块大小下的"重叠比例"不同
   （300 字块是 27%，600 字块是 13%），这一点在结论里必须写明，不能装作没这回事。

4) **三个指标**：
   - top1：第一名命中 → 用户直接看到正确答案
   - top3：前三名里有 → 让模型自己挑也能挑到
   - MRR：第一次命中的倒数排名，反映"排在多靠前"，比 top1/top3 更细腻

跑法：
    python eval_retrieval.py                 # 默认 300 / 400 / 600
    python eval_retrieval.py --sizes 400     # 只跑一组

产物：docs/retrieval-eval.md（markdown 表格，可直接贴进 README）
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import time
from pathlib import Path

from main import Config, load_dotenv
from kb import add_document, connect, search

PROJECT = Path(__file__).resolve().parent
# ★ 语料是 README 的一份**冻结快照**，不是 README 本身。
#   README 既是项目文档又是评测语料，改了文档语料就变了，
#   上一轮的数字和文档里写的块数立刻对不上——评测就成了"每次都有理"。
#   快照一次之后，README 随便改，基准始终可复现。
CORPUS = PROJECT / "docs" / "eval-corpus.md"
OUT = PROJECT / "docs" / "retrieval-eval.md"

# 20 条问题 + 标准答案锚点。
# ★ 锚点必须是"只可能出现在正确答案附近"的特征串，且已逐条 grep 确认存在于语料中。
#   锚点写错会让评测变成假绿——比没有评测更危险。
QA: list[tuple[str, str]] = [
    ("限流里「先扣费再校验」那个坑是怎么修的？", "先扣费再校验"),
    ("流式输出为什么要关掉反向代理的缓冲？", "X-Accel-Buffering"),
    ("失败重试的退避间隔是多少？", "1s → 2s → 4s"),
    ("Agent 怎么把工具结果按 id 回填给模型？", "tool_call_id"),
    ("Agent 怎么防止在两个工具之间无限循环？", "max_steps"),
    ("容器里为什么不用 root 运行？", "appuser"),
    ("检索实测里第一名的相似度是多少？", "0.6737"),
    ("怎么证明检索是语义匹配而不是关键词匹配？", "0.5827"),
    ("知识库怎么判断同一篇文档被重复上传？", "content_hash"),
    ("向量在 sqlite 里用什么类型存？", "float32"),
    ("知识库用的 embedding 模型是哪个？", "text-embedding-v3"),
    ("怎么给别人一个固定域名访问服务？", "Cloudflare Tunnel"),
    ("限流账本换成 Redis 后怎么保证原子性？", "INCR"),
    ("没配 API Key 时接口返回什么？", "503"),
    ("检索要返回几条，是哪个参数控制的？", "top_k"),
    ("切片为什么要保留重叠的部分？", "重叠 80"),
    ("删掉文档时它下面的切片怎么清理？", "ON DELETE CASCADE"),
    ("Redis 里过期的 IP 计数怎么自动消失？", "EXPIRE"),
    ("流式输出首字延迟实测多少毫秒？", "842"),
    ("30 篇语料压测平均每篇耗时多久？", "2.39"),
]


def build_db(chunk_size: int, overlap: int, db_path: Path, text: str,
             config: Config) -> tuple[sqlite3.Connection, int, float, float]:
    """
    按指定参数建库，返回 (连接, 块数, 平均块长, 建库耗时秒)。

    ★ 已存在就直接复用：重跑评测时不必再花一次 embedding 费用、也不用多等二十秒。
      参数改了要重建时才删库（判断依据是块数是否为 0，简单但够用）。
    """
    from kb import connect as kb_connect

    db_path.parent.mkdir(parents=True, exist_ok=True)

    if db_path.exists():
        conn = kb_connect(db_path)
        n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if n:
            lengths = [r[0] for r in conn.execute("SELECT LENGTH(text) FROM chunks").fetchall()]
            return conn, n, statistics.mean(lengths), 0.0
        conn.close()

    conn = connect(db_path)
    started = time.time()
    info = add_document(conn, CORPUS.name, text, config=config,
                        chunk_size=chunk_size, overlap=overlap)
    elapsed = time.time() - started

    lengths = [r[0] for r in conn.execute("SELECT LENGTH(text) FROM chunks").fetchall()]
    return conn, info["n_chunks"], (statistics.mean(lengths) if lengths else 0), elapsed


def evaluate(conn: sqlite3.Connection, config: Config, top_k: int = 3,
             hybrid: bool = True) -> dict:
    """
    跑完 20 条问题，算 top1 / top3 / MRR 与平均检索耗时。

    ★ 未命中时额外记录"正确块在哪、top1 是谁"，用来区分三类失败：
        - 相邻块（块边界把答案切到隔壁）→ 粒度问题，调 chunk_size 或加重叠
        - 同主题但无关段落 → 语义区分度不够
        - 只提到关键词、不含答案的段落（如"已知限制"清单）→ 词面命中骗过向量
      没有这个分类，看到 35% 只会得出"检索不行"，然后无从下手。
    """
    # 先扫一遍：每个锚点出现在哪些块里。落库一次，后面比对用。
    all_rows = conn.execute("SELECT ord, text FROM chunks").fetchall()
    truth: dict[str, list[int]] = {}
    for _q, anchor in QA:
        truth[anchor] = [ord_ for ord_, text in all_rows if anchor in text]

    hits1 = hits3 = 0
    rr_sum = 0.0
    latencies: list[int] = []
    misses: list[dict] = []

    for question, anchor in QA:
        t0 = time.time()
        results = search(conn, question, top_k=top_k, config=config, hybrid=hybrid)
        latencies.append(int((time.time() - t0) * 1000))

        rank = None
        for i, r in enumerate(results, 1):
            if anchor in r["text"]:
                rank = i
                break

        if rank == 1:
            hits1 += 1
        if rank is not None:
            hits3 += 1
            rr_sum += 1.0 / rank
        else:
            top1 = results[0] if results else {"ord": -1, "score": 0.0}
            truth_ords = truth[anchor]
            if truth_ords and abs(top1["ord"] - truth_ords[0]) <= 2:
                kind = "相邻块（答案被块边界切走）"
            elif not truth_ords:
                kind = "锚点在库里不存在（评测用例有问题）"
            else:
                kind = "跑偏（词面相似或只提到关键词）"
            misses.append({
                "question": question,
                "anchor": anchor,
                "truth_ords": truth_ords,
                "top1_ord": top1["ord"],
                "top1_score": top1["score"],
                "kind": kind,
            })

    n = len(QA)
    return {
        "top1": hits1 / n,
        "top3": hits3 / n,
        "mrr": rr_sum / n,
        "avg_ms": statistics.mean(latencies),
        "misses": misses,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="检索评测：不同切片参数 / 检索模式的召回对比")
    ap.add_argument("--sizes", nargs="+", type=int, default=[300, 400, 600])
    ap.add_argument("--overlap", type=int, default=80)
    ap.add_argument("--modes", nargs="+", default=["vector", "hybrid"],
                    choices=["vector", "hybrid"])
    args = ap.parse_args()

    load_dotenv(str(PROJECT / ".env"))
    config = Config.from_env()
    text = CORPUS.read_text(encoding="utf-8")

    print(f"语料 {CORPUS.name}：{len(text)} 字 ｜ overlap 固定 {args.overlap} ｜ "
          f"问题 {len(QA)} 条 ｜ 模式 {args.modes}\n")

    rows = []
    for cs in args.sizes:
        db = PROJECT / "data" / "eval" / f"readme_{cs}.sqlite3"
        conn, n_chunks, avg_len, build_s = build_db(cs, args.overlap, db, text, config)
        print(f"== chunk_size={cs} ｜ {n_chunks} 块 ｜ 平均 {avg_len:.0f} 字/块 ==")

        for mode in args.modes:
            res = evaluate(conn, config, hybrid=(mode == "hybrid"))
            print(f"   [{mode:6s}] top1 {res['top1']:.0%} ｜ top3 {res['top3']:.0%} ｜ "
                  f"MRR {res['mrr']:.3f} ｜ 平均检索 {res['avg_ms']:.0f} ms")
            for m in res["misses"]:
                print(f"      ✗ {m['kind']}：{m['question'][:18]}… "
                      f"正确块 {m['truth_ords']} → top1 块 {m['top1_ord']}")
            rows.append((cs, n_chunks, avg_len, mode, res))
        conn.close()
        print()

    # 写 markdown
    lines = [
        "# 检索评测：切片大小 × 检索模式",
        "",
        f"- 语料：`{CORPUS.name}`（{len(text)} 字）",
        f"- 问题数：{len(QA)} 条（每条有唯一锚点字符串，见 `eval_retrieval.py`）",
        f"- overlap 固定：{args.overlap} 字",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 结果",
        "",
        "| chunk_size | 检索模式 | 块数 | 平均块长 | top1 | top3 | MRR | 平均检索耗时 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for cs, n_chunks, avg_len, mode, res in rows:
        label = "hybrid（向量+关键词 RRF）" if mode == "hybrid" else "vector（纯向量）"
        lines.append(
            f"| {cs} | {label} | {n_chunks} | {avg_len:.0f} 字 | {res['top1']:.0%} | "
            f"{res['top3']:.0%} | {res['mrr']:.3f} | {res['avg_ms']:.0f} ms |"
        )

    # 同一份库上 vector → hybrid 的提升
    lines += ["", "## 混合检索带来了什么", "",
              "| chunk_size | top1 提升 | top3 提升 | MRR 提升 |", "|---|---|---|---|"]
    by_key = {(cs, mode): res for cs, _n, _l, mode, res in rows}
    for cs in args.sizes:
        v, h = by_key.get((cs, "vector")), by_key.get((cs, "hybrid"))
        if v and h:
            lines.append(
                f"| {cs} | {v['top1']:.0%} → {h['top1']:.0%}"
                f"（{(h['top1'] - v['top1']) * 100:+.0f}pt） "
                f"| {v['top3']:.0%} → {h['top3']:.0%}"
                f"（{(h['top3'] - v['top3']) * 100:+.0f}pt） "
                f"| {v['mrr']:.3f} → {h['mrr']:.3f} |"
            )

    lines += [
        "",
        "## 失败类型分析",
        "",
        "未命中不都是「检索不行」。下面是每条失败的实际形态，用来决定下一步该改什么。",
        "",
        "| chunk_size | 模式 | 问题 | 正确块 | 实际 top1 块 | 判定 |",
        "|---|---|---|---|---|---|",
    ]
    for cs, _n, _len, mode, res in rows:
        for m in res["misses"]:
            lines.append(
                f"| {cs} | {mode} | {m['question']} | {m['truth_ords']} | "
                f"{m['top1_ord']} | {m['kind']} |"
            )

    lines += [
        "",
        "三类失败的应对方向完全不同：",
        "",
        "| 失败类型 | 根因 | 该怎么改 |",
        "|---|---|---|",
        "| 相邻块 | 答案被块边界切到隔壁，语义上隔壁那块才是「最像问题」的 | 调 chunk_size、加大重叠，或检索时把命中块的邻居一起带出来 |",
        "| 跑偏（词面相似） | 纯向量对「字面重合但语义不同」的段落区分度不够 | 加 rerank（cross-encoder 重排）或混合检索 |",
        "| 只提到关键词不含答案 | 清单/索引式段落天然包含大量术语，向量相似度很高但没内容 | 混合检索里给关键词命中降权，或按「是否包含答案句式」重排 |",
        "",
    ]

    lines += [
        "",
        "## 口径说明",
        "",
        "- **top1 / top3**：命中标准是「返回的块里包含该问题的锚点字符串」。",
        "  不用「第几块」当标准答案——块编号会随切片参数变化，换个参数基准就废了。",
        "- **MRR**（平均倒数排名）：第一次命中排在第几位取倒数再平均，比 top1/top3 更细腻。",
        "- **只变 chunk_size，overlap 固定 80**：两个变量一起动无法归因。",
        "  代价是不同块大小的重叠比例不同（300 字块 27%、600 字块 13%），这是本表的已知混淆因素。",
        "",
        "## 待补充（诚实清单）",
        "",
        "- 语料只有 1 篇文档、20 条问题，样本量小，结论只能看趋势不能当绝对值。",
        "- 全是「能查到」的正例，没有「库里没有答案时会不会瞎编」的负例测试。",
        "- 没有做混合检索（BM25 + 向量）与 rerank 的对比组。",
    ]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告已写入 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
