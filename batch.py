"""
batch.py —— 批量摘要模式（M3 / W9）
作者：俊

【它解决什么问题】
  单条模式一次摘要一段文字；批量模式一次摘要一整张 CSV。
  入口还是 main.py：
      python main.py --input data.csv --text-col content --output out.csv
      python main.py --input data.csv --text-col content --output out.csv --limit 5

【为什么要单独一个文件，不塞进 main.py】
  main.py 现在 300 多行，管的是「单条摘要」。
  批量是另一件事：读表、逐行循环、逐行容错、写表。
  职责不同就分文件——出问题时你知道该翻哪个。

【本文件的分工】
  load_input()       读 CSV，顺便做列校验            （已写好）
  summarize_rows()   逐行调用，收集结果              ★ 你写
  save_output()      加列存成新文件                  ★ 你写
"""

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


def summarize_rows(bot, df: pd.DataFrame, text_col: str) -> pd.DataFrame:
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
    """
    total = len(df)

    summaries: list[str] = []
    statuses: list[str] = []
    errors: list[str] = []
    latencies: list[int] = []

    for i, (_idx, row) in enumerate(df.iterrows(), start=1):
        # iterrows() 每次给一对 (行号, 这一行)。行号这里用不上，用 _idx 表示"故意不用"。
        text = str(row[text_col]).strip()

        # ==================================================
        # TODO 1：空正文跳过，别浪费 API 调用
        # ==================================================
        if not text:                      # text 为空字符串就跳过
            summaries.append("")          # 摘要留空
            statuses.append(STATUS_SKIP)  # 状态记 SKIP
            errors.append("")             # 没有错误
            latencies.append(0)           # 没发请求，耗时为 0
            continue                      # 直接进入下一行，不调 API

        t0 = time.time()          # 开始计时（time.time() 返回秒，小数部分也是秒）

        # ==================================================
        # TODO 2：调一次 API，并且只让「这一行」失败
        #   注意：这里没有 return。summary/status/error 是 summarize_rows 的
        #   局部变量，直接赋值，下面的 append 就能用；加了 return 会结束整个
        #   函数（只处理第一行就退出）。
        # ==================================================
        try:
            summary = bot.summarize(text)
            status = STATUS_OK
            error = ""
        except Exception as e:
            summary = ""
            status = STATUS_FAIL
            error = f"{type(e).__name__}: {e}"

        cost_ms = int((time.time() - t0) * 1000)      # 耗时（毫秒，取整）

        summaries.append(summary)
        statuses.append(status)
        errors.append(error)
        latencies.append(cost_ms)

        # 进度提示：跑 100 行时，这一行是你的"它还活着吗"
        mark = "✅" if status == STATUS_OK else ("⏭" if status == STATUS_SKIP else "❌")
        print(f"  [{i}/{total}] {mark} {row['id']}  {cost_ms} ms")

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
