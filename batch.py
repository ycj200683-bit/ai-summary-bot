"""
batch.py —— 批量摘要模式（M3 / W9 完成，W10 扩充断点续跑）
作者：俊

【它解决什么问题】
  单条模式一次摘要一段文字；批量模式一次摘要一整张 CSV。
  入口还是 main.py：
      python main.py --input data.csv --text-col content --output out.csv
      python main.py --input data.csv --text-col content --output out.csv --limit 5
      python main.py --input data.csv --resume        ← W10：接着上次的进度跑

【为什么要单独一个文件，不塞进 main.py】
  main.py 现在 300 多行，管的是「单条摘要」。
  批量是另一件事：读表、逐行循环、逐行容错、写表。
  职责不同就分文件——出问题时你知道该翻哪个。

【本文件的分工】
  load_input()      读 CSV，顺便做列校验            （已写好）
  load_done()       读上次的结果，挑出已完成的行     （已写好，W10 新增）
  summarize_rows()  逐行调用，收集结果              ★ 你写 TODO 4 / 5
  _flush()          跑的过程中分段落盘               （已写好，W10 新增）
  save_output()     最终存成新文件                  （已写好）
  print_summary()   跑完给汇总                      （已写好）
"""

import os
import time

import pandas as pd


# 结果列名提成常量：拼错列名是低级 bug 的高发区，写成常量至少能被 IDE 补全
COL_SUMMARY = "summary"
COL_STATUS = "status"
COL_ERROR = "error"
COL_LATENCY = "latency_ms"

STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

# ★W10：每处理多少行落盘一次。
#   1 是最稳的（断在第几行就保住第几行），但每行都要把整张表重写一遍。
#   行数上万时要把它调大（比如 50），否则写盘本身会变成瓶颈。
#   这就是工程里典型的「可靠性 vs 性能」取舍——先定可靠，量大了再调。
FLUSH_EVERY = 1


def TODO_未实现(说明: str):
    """占位函数：跑到这里就说明对应的 TODO 还没填。（填完把这行调用删掉）"""
    raise NotImplementedError(
        "\n\n还没填的 TODO：\n  " + 说明 + "\n\n"
        "把整段 TODO_未实现(...) 换成提示的代码，然后再跑一次。\n"
    )


def load_input(path: str, text_col: str, limit: int | None = None) -> pd.DataFrame:
    """
    读入 CSV，检查正文列存在，可选只取前 N 行。

    三个细节都有原因：
      ① dtype={"id": str}
         不加这个，pandas 会把 "001" 推断成整数 1（前导零丢失），
         以后想靠 id 反查 corpus/001.txt 就找不到了。这是实测确认过的坑。

      ② 列校验提前做
         如果 --text-col 拼错了（比如写成 contents），与其跑到第 1 行才发现
         取不到数据，不如读进来第一秒就报错，并把现有列名打出来。

      ③ .head(limit).copy()
         head() 返回的是原表的一个【视图】，不是新表。在视图上加列会触发
         pandas 的 SettingWithCopyWarning（本质就是"你以为在改副本，其实在改原件"）。
         .copy() 明确复制一份，警告就没了。
         👉 这正好用上你昨天学的深浅拷贝——pandas 的视图就是"共享同一块数据"。
    """
    df = pd.read_csv(path, dtype={"id": str})

    if text_col not in df.columns:
        raise SystemExit(
            f"❌ CSV 里没有列 {text_col!r}\n"
            f"   现有列：{list(df.columns)}"
        )

    if limit:
        df = df.head(limit).copy()
        print(f"（--limit 生效：只处理前 {limit} 行）")

    return df


def load_done(path: str) -> dict[str, tuple[str, str, str, int]]:
    """
    ★W10 新增：读上一次的输出文件，挑出「已经成功或跳过」的行。

    返回 {id: (summary, status, error, latency_ms)}，给 summarize_rows 用来跳过。
    文件不存在 / 读不动 / 列不全，一律返回 {} —— 等价于「从零开始」，
    绝不能因为读旧文件失败就把整批任务搞崩。

    【为什么只认 ok 和 skip，不认 fail】
      fail 说明上次真的没成功，这次应该重试。
      ok / skip 才是「已经拿到结果，不用再花钱」。
      断点续跑的语义是"跳过已完成的"，不是"跳过所有处理过的"——差一个字，后果相反。

    （这段我写好了，你读懂就行；注意里面的 pd.isna，W10 数据清洗会正式讲。）
    """
    if not os.path.exists(path):
        return {}

    try:
        prev = pd.read_csv(path, dtype={"id": str})
    except Exception:
        return {}

    need = {COL_SUMMARY, COL_STATUS, COL_ERROR, COL_LATENCY}
    if not need.issubset(prev.columns):
        return {}

    finished = prev[prev[COL_STATUS].isin([STATUS_OK, STATUS_SKIP])]

    def _text(v) -> str:
        # NaN 直接 str() 会变成字符串 "nan"（真值），必须先用 isna 拦掉
        return "" if pd.isna(v) else str(v)

    return {
        str(r["id"]): (
            _text(r[COL_SUMMARY]),
            str(r[COL_STATUS]),
            _text(r[COL_ERROR]),
            int(r[COL_LATENCY]) if not pd.isna(r[COL_LATENCY]) else 0,
        )
        for _i, r in finished.iterrows()
    }


def summarize_rows(
    bot,
    df: pd.DataFrame,
    text_col: str,
    out_path: str | None = None,
    done: dict[str, tuple[str, str, str, int]] | None = None,
) -> pd.DataFrame:
    """
    逐行调用大模型，把结果收进四列：summary / status / error / latency_ms。

    【为什么这里是 for 循环，而不是 pandas 的向量化】
      向量化适合「纯计算」——算 10 万个数，向量化快上百倍。
      但这里每一行都要发一次 HTTP 请求，要等好几秒，属于「慢 I/O」。
      这种场景向量化帮不上忙，反而让你：
        · 看不见进度（跑 100 行不知道卡在第几行）
        · 无法单行容错（一行报错整批废掉）
        · 出错不知道是哪一行
      👉 判断标准：这段代码在等外部的东西（网络/磁盘/数据库）吗？是 → 用循环。

    【为什么可以用 except Exception 兜住整行】
      通常不推荐 catch 一切，但批处理的语义就是「一行失败不影响其他行」。
      而且我们不是把错误吞掉——type(e).__name__ 和 e 都写进 error 列，
      跑完你能在 CSV 里一行行查。吞掉才叫错，记录清楚就不叫。
      注意：这里和 @retry 是两层保险——
        @retry 处理「抖动」（429/超时，等一下可能就好）
        这里处理「彻底失败」（重试 3 次还是挂，这一行只能放弃）

    【★W10 新增两个参数，默认值让 v1.1 的行为一点没变】
      out_path  给了就在跑的过程中分段落盘 —— 断点续跑的"燃料"
      done      上次已经成功/跳过的行，命中就直接复用，不再花钱

      为什么用默认参数而不是新加必填参数：
        加参数最容易搞崩老调用方。给一个"不传就退回旧行为"的默认值，
        已有代码一行不用改也不会坏 —— 这叫【向后兼容】。
    """
    total = len(df)
    done = done or {}

    summaries: list[str] = []
    statuses: list[str] = []
    errors: list[str] = []
    latencies: list[int] = []

    for i, (_idx, row) in enumerate(df.iterrows(), start=1):
        # iterrows() 每次给一对 (行号, 这一行)。行号这里用不上，用 _idx 表示"故意不用"。
        text = str(row[text_col]).strip()
        row_id = str(row["id"])       # 断点续跑靠它认人，必须和 id 列的类型一致

        # ==================================================
        # ★ 三个分支只负责【算出四个值】，不负责收尾
        #
        #   v1.1 里「空正文」是 append 完就 continue。一加落盘立刻出 bug：
        #   continue 会把循环体后面的落盘代码一起跳过，空行永远写不进磁盘。
        #   改成 if / elif / else 之后，append、打印、落盘都只有一份，谁也漏不掉。
        #   👉 这个改法叫【单一出口】：一个循环尽量只有一条"往下走"的路。
        # ==================================================
        if row_id in done:
            # TODO 4 已完成：解包旧结果，一个 API 都不调
            #   ① 四元组按顺序分给四个变量（第 4 个是耗时，名字要叫 cost_ms，
            #      因为下面 append 用的是 cost_ms —— 写成 latency_ms 不会报错，
            #      反而会把上一行残留的耗时带下来，是最难查的那种 bug）
            #   ② mark 用 ♻️，让人一眼看出"这行是省下来的，不是这次跑的"
            summary, status, error, cost_ms = done[row_id]
            mark = "♻️"
        elif not text:
            # 空正文：不发请求，直接记 skip
            summary, status, error, cost_ms = "", STATUS_SKIP, "", 0
            mark = "⏭"

        else:
            # 正常一行：调 API，失败就地兜住
            t0 = time.time()      # 开始计时（time.time() 返回秒，小数部分也是秒）
            try:
                summary = bot.summarize(text)
                status = STATUS_OK
                error = ""
            except Exception as e:
                summary = ""
                status = STATUS_FAIL
                error = f"{type(e).__name__}: {e}"
            cost_ms = int((time.time() - t0) * 1000)   # 毫秒，取整
            mark = "✅" if status == STATUS_OK else "❌"

        summaries.append(summary)
        statuses.append(status)
        errors.append(error)
        latencies.append(cost_ms)

        # 进度提示：跑 100 行时，这一行是你的"它还活着吗"
        print(f"  [{i}/{total}] {mark} {row_id}  {cost_ms} ms")

        # 分段落盘：够 FLUSH_EVERY 行就写一次；最后一行无论如何要补一笔。
        # 少了 `i == total` 这个条件，最后不满一批的几行就永远进不了磁盘。
        if out_path and (i % FLUSH_EVERY == 0 or i == total):
            _flush(df, i, summaries, statuses, errors, latencies, out_path)

    # 一次性把四列接上表尾。
    # 为什么不在循环里一行行 df.loc[idx, "summary"] = ... ？
    #   df.loc 逐格写很慢，而且会不断触发"碎片化表"警告。
    #   收集到列表、最后一次性赋值，是 pandas 的标准做法。
    df[COL_SUMMARY] = summaries
    df[COL_STATUS] = statuses
    df[COL_ERROR] = errors
    df[COL_LATENCY] = latencies

    return df


def save_output(df: pd.DataFrame, path: str) -> None:
    """存成新文件，原表 data.csv 永不覆盖（重跑时原始数据还在）。"""
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _flush(df, n_done, summaries, statuses, errors, latencies, path: str) -> None:
    """
    ★W10 新增：把「已经算完的前 n_done 行 + 结果列」立刻写到磁盘。

    【为什么只写前 n_done 行，不是整张表】
      还没处理的行如果也写出去，它们看起来就像"处理过但没结果"。
      重跑时语义会变脏——只写已经算完的那段前缀，才讲得清"哪几行是真的完成了"。
      注意 df.head(n).copy() 里的 .copy()：head() 是视图，这个坑你在 load_input 里踩过。

    【写盘动作本身复用 save_output】
      同一个"把表存成 CSV"的动作只写一份实现。哪天要改编码或改格式，
      只改 save_output 一处，这儿自动跟着变 —— 这叫【不要重复自己（DRY）】。
    """
    part = df.head(n_done).copy()
    part[COL_SUMMARY] = summaries
    part[COL_STATUS] = statuses
    part[COL_ERROR] = errors
    part[COL_LATENCY] = latencies
    save_output(part, path)


def print_summary(df: pd.DataFrame) -> None:
    """跑完给一份汇总。批量任务不看汇总等于白跑。"""
    n = len(df)
    ok = (df[COL_STATUS] == STATUS_OK).sum()
    fail = (df[COL_STATUS] == STATUS_FAIL).sum()
    skip = (df[COL_STATUS] == STATUS_SKIP).sum()

    print("\n" + "=" * 46)
    print(f"总计 {n} 行   成功 {ok}   失败 {fail}   跳过 {skip}")
    if ok:
        print(f"成功率 {ok / n:.0%}   平均耗时 {df[COL_LATENCY].mean():.0f} ms")
    if fail:
        print("\n失败的行：")
        bad = df[df[COL_STATUS] == STATUS_FAIL][["id", COL_ERROR]]
        for _i, r in bad.iterrows():
            print(f"  {r['id']}: {r[COL_ERROR]}")
    print("=" * 46)
