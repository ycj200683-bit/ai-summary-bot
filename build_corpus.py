"""
build_corpus.py —— 把 corpus/*.txt 合成 data.csv（W9 第一个 pandas 练习）
作者：俊 ｜ 阶段：M3 / W9

【这个脚本干什么】
  把 corpus/ 下散落的 txt 文件，变成一个整齐的 CSV 表格。

  输入：corpus/001.txt … 010.txt
        每个文件格式固定：第一行标题 / 第二行空行 / 第三行起正文
  输出：data.csv，四列
        id       001 … 010
        title    标题
        content  正文
        chars    正文字数

【为什么要单独写个脚本，不直接在 main.py 里做】
  这是数据处理里很常见的"分层"：
    build_corpus.py 负责「把原始素材整理成表」——一次性、可重复跑
    main.py          负责「读表 → 干活 → 写表」——每天跑
  两件事分开，谁出问题查谁，不会搅在一起。

【怎么跑】
  python build_corpus.py

【状态：已完成】
  骨架里原来留了 4 个 TODO，俊已全部填好并跑通（2026-10-19）：
      第 3 步  df = pd.DataFrame(rows)
      第 4 步  df["chars"] = df["content"].str.len()
      第 5 步  print(df.shape) / head(3) / 字数 min-max-mean
      第 6 步  df.to_csv(path, index=False, encoding="utf-8-sig")
  产出 data.csv：10 行 × 4 列，字数 980–3797，均值 2204.4

【读回时的坑（实测确认）】
  pd.read_csv("data.csv") 默认会把 id 列推断成 int64：
      "001" → 1   ← 前导零没了
  想让 id 保持 "001" 这种字符串，必须显式指定：
      pd.read_csv("data.csv", dtype={"id": str})
  后续 main.py 如果要靠 id 反查 corpus/001.txt，这条是必须的。

【怎么跑都对：路径与 cwd 无关】
  脚本用 __file__ 推出自己的绝对路径，所以从任何目录启动都能找到 corpus/。
  在 C:\\Users\\QQ229 这种上级目录下运行，也不会再报"没有 txt 文件"。

  ★ 上面那行写成双反斜杠是必须的，别改成单个。
    Python 普通字符串里，反斜杠 + 大写 U 是 Unicode 转义的开始，
    后面跟不了合法编码就直接 SyntaxError（这个坑写这段注释时刚踩了一次）。
    要写 Windows 路径，三个办法任选：
        "C:\\\\Users\\\\xxx"   转义
        r"C:\\Users\\xxx"    前面加 r 变原始字符串（推荐）
        用 / 代替： "C:/Users/xxx"
"""

import glob      # 用来按通配符找文件：glob("corpus/*.txt")
import os        # 路径拼接、取文件名
import sys       # 出错时 sys.exit(1) 让退出码非 0

import pandas as pd      # 本次的主角
# pandas 的惯例是 import ... as pd，全世界的代码都这么写，跟着写就行


# ============================================================
# 路径基准：★ 这一段是踩过坑改的，务必读懂
#   之前的写法是 CORPUS_DIR = "corpus"（相对路径）。
#   相对路径的参照物是「你从哪个目录启动脚本」，叫 cwd（current working directory）。
#   于是出现这种现象：
#       在项目目录里运行  → 找到 corpus/  → 正常
#       在 C:\Users\QQ229 里运行 → 去找 C:\Users\QQ229\corpus\ → 报"没有 txt 文件"
#   代码一个字没改，只因为"从哪儿运行"不同就成败不同——这是新手最迷惑的 bug 之一。
#
#   解法：以「脚本自己所在的目录」为基准，与 cwd 彻底解耦。
#       __file__                  当前这个 .py 文件的路径（可能是相对的）
#       os.path.abspath(__file__) 转成绝对路径
#       os.path.dirname(...)      取它所在的文件夹
#   这样不管你从 C:\、从桌面、还是从 Git Bash 里启动，结果都一样。
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CORPUS_DIR = os.path.join(BASE_DIR, "corpus")     # 语料目录（绝对路径）
OUT_CSV = os.path.join(BASE_DIR, "data.csv")      # 输出文件（绝对路径）
MIN_CHARS = 500                # 正文最少多少字才算合格，太短的丢掉


# ============================================================
# 第 1 步：找出所有语料文件（已写好，读懂就行）
# ============================================================
def list_corpus_files() -> list[str]:
    """返回 corpus/ 下所有 txt 的路径，按文件名排序（001, 002, …）。"""
    # glob 支持通配符；用 os.path.join 拼路径，跨平台不会出错
    pattern = os.path.join(CORPUS_DIR, "*.txt")
    files = glob.glob(pattern)
    # 必须排序！glob 返回的顺序是操作系统给的，不保证 001 在 002 前面
    return sorted(files)


# ============================================================
# 第 2 步：解析单个文件（已写好，读懂就行）
# ============================================================
def parse_one(path: str) -> dict:
    """
    读一个 txt，拆成「标题 + 正文」，返回一个字典。

    文件格式约定（见 corpus/README.md）：
        第一行        标题
        第二行        空行
        第三行开始    正文
    所以用一次 split("\n\n", 1) 就能干净地切成两半：
        split 的第二个参数 1 表示"最多切一刀"，剩下的全部归后半部分——
        这样正文里即使有空行也不会被多切。
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    # maxsplit=1：只在第一处空行切一刀，正文内部的空行保留
    parts = raw.split("\n\n", 1)
    if len(parts) != 2:
        raise ValueError(f"{path} 格式不对：第一行标题后必须有一个空行")

    title = parts[0].strip()
    content = parts[1].strip()

    return {
        "id": os.path.splitext(os.path.basename(path))[0],   # 001.txt → "001"
        "title": title,
        "content": content,
    }


# ============================================================
# 第 3 步：把列表变成 DataFrame  ★ TODO 1
# ============================================================
def build_dataframe(rows: list[dict]) -> pd.DataFrame:
    """
    把 [{"id":.., "title":.., "content":..}, {...}, ...] 变成一张表。

    【TODO 1】用 pd.DataFrame(rows) 建表。

    想清楚再写：
      rows 是一个「列表套字典」。每个字典是【一行】，字典的 key 是【列名】。
      pandas 正好认这种形状——不需要你告诉它有几列，它自己从 key 里推出来。

    为什么 DataFrame 值得学：
      一列数据（比如所有 content）就是一个 Series。
      DataFrame = 共用同一个行索引的一堆 Series 拼在一起。
      后面 df["content"] 拿到的是 Series，df[["content"]] 拿到的才是 DataFrame。
      —— 这个区别是 pandas 新手最容易绕晕的地方。

    写法提示：
      df = pd.DataFrame(rows)          ← 就一行
      return df
    """
    df = pd.DataFrame(rows)
    return df


# ============================================================
# 第 4 步：算出每篇的字数  ★ TODO 2
# ============================================================
def add_char_count(df: pd.DataFrame) -> pd.DataFrame:
    """
    新增一列 chars = 正文字数。这一步是「向量化思维」的入门。

    【TODO 2】把 df["content"].str.len() 赋给新列 df["chars"]。

    为什么不写 for 循环？
      新手的第一反应是：
          for i in range(len(df)):
              df.loc[i, "chars"] = len(df.loc[i, "content"])
      能跑，但慢几十倍，而且写法啰嗦。
      pandas 的做法是「整列一起算」——.str 让 Series 里的每个字符串
      都执行一次 .len()，一行顶一个循环。这就是向量化。

    为什么是 .str.len() 而不是 len()？
      len(df["content"]) 算的是「有多少行」（Series 的长度）。
      df["content"].str.len() 算的才是「每一格的字符串有多少字」。
      这两个差一个 .str，结果完全不同——很多人的 bug 就出在这。

    写法提示：
      df["chars"] = df["content"].str.len()
      return df
    """
    df["chars"] = df["content"].str.len()
    return df


# ============================================================
# 第 5 步：打印健康检查  ★ TODO 3
# ============================================================
def report(df: pd.DataFrame) -> None:
    """
    建完表先别急着存，看一眼它长什么样。这一步是你的"验收"。

    【TODO 3】打印三样东西：
      ① 表格形状：print(df.shape)        → 应该是 (10, 4)，即 10 行 4 列
      ② 前三行：  print(df.head(3))      → 看一眼内容有没有错位
      ③ 字数范围：print(df["chars"].min(), df["chars"].max(), df["chars"].mean())

    .shape 返回的是一个元组 (行数, 列数)。
    为什么值得先看 shape：如果 id/title/content 解析错位，这里立刻就露馅
    （比如 10 行变成 20 行，说明 split 切错了）。
    """
    print(df.shape)
    print(df.head(3))
    print(df["chars"].min(), df["chars"].max(), df["chars"].mean())


# ============================================================
# 第 6 步：写成 CSV  ★ TODO 4
# ============================================================
def save_csv(df: pd.DataFrame, path: str) -> None:
    """
    把表存成 CSV，供 main.py 后面用 read_csv 读回来。

    【TODO 4】df.to_csv(path, index=False, encoding="utf-8-sig")

    三个参数各有讲究，想清楚再写：

      index=False
        不写的话，CSV 第一列会多出一列 0,1,2,3…（pandas 的行号）。
        那是内存里的索引，不是你的数据。存文件时几乎总是要关掉它。
        👉 这是 to_csv 最常见的一个坑。

      encoding="utf-8-sig"
        sig = signature，就是文件开头那 3 个不可见字节（BOM）。
        Windows 上双击用 Excel 打开 UTF-8 无 BOM 的 CSV，中文会乱码；
        带 BOM 就正常。
        👉 这和 W8 里 FileHandler 必须写 encoding="utf-8" 是同一类问题：
           编码不是"可选项"，不写就要在别人的机器上炸。

      sep 默认是逗号，这里不用管。
    """
    df.to_csv(path, index=False, encoding="utf-8-sig")


# ============================================================
# 主流程：把六步串起来
# ============================================================
def main():
    files = list_corpus_files()
    if not files:
        print(f"❌ 这个目录里没有 txt 文件：\n   {CORPUS_DIR}")
        print("   请确认 corpus/ 已建好、且里面是 001.txt 这样的文件")
        sys.exit(1)

    print(f"找到 {len(files)} 个语料文件\n")

    # 逐个解析，顺便过滤掉太短的
    rows = []
    for path in files:
        item = parse_one(path)
        n = len(item["content"])
        flag = "✅" if n >= MIN_CHARS else "⚠️ 太短，跳过"
        print(f"  {item['id']}.txt  {n:5d} 字  {flag}  {item['title'][:32]}")
        if n >= MIN_CHARS:
            rows.append(item)

    if not rows:
        print("\n❌ 没有合格的语料")
        sys.exit(1)

    print(f"\n合格 {len(rows)} 篇，开始建表\n")

    df = build_dataframe(rows)          # TODO 1
    df = add_char_count(df)             # TODO 2
    report(df)                          # TODO 3
    save_csv(df, OUT_CSV)               # TODO 4

    print(f"\n✅ 已写出 {OUT_CSV}（{len(df)} 行 × {len(df.columns)} 列）")


# ============================================================
# 进阶练习（做不做都行，但做了对 W9 后面的活很有用）
# ============================================================
#
# 【练习 A：搞懂 axis】
#   在 report() 最后加这一行，跑一下看看：
#
#       print(df.apply(lambda row: len(row["title"]), axis=1))
#
#   然后把 axis=1 改成 axis=0，会发现报错。
#   想一想为什么：
#     axis=0 → 沿"行"的方向往下走，传给 lambda 的是【一列】（Series）→ 没有 row["title"]
#     axis=1 → 沿"列"的方向横着走，传给 lambda 的是【一行】（Series）→ 有 row["title"]
#   一句话记忆：axis 是"要消掉的那个方向"，也是"移动的方向"。
#     对表格来说，axis=0 表示按列算（得到每列一个值），axis=1 表示按行算（得到每行一个值）。
#
# 【练习 B：加一个"标题长度"列】
#   模仿 add_char_count，加一列 title_chars = df["title"].str.len()。
#   你会发现代码几乎不用改——这就是向量化的好处。
#
# 【练习 C：找出最长的三篇】
#       print(df.nlargest(3, "chars")[["id", "chars", "title"]])
#   想一想 nlargest 和 sort_values(ascending=False).head(3) 有什么区别。
#
# 【练习 D：自己验证一遍读回来的东西对不对】
#       check = pd.read_csv(OUT_CSV)
#       print(check.shape)          # 应该还是 (10, 4)
#       print(check.columns.tolist())   # ['id', 'title', 'content', 'chars']
#   注意：read_csv 读回来时 id 列会变成字符串 "001" 还是整数 1？
#   动手验证一下，这关系到后面 main.py 里怎么关联数据。


if __name__ == "__main__":
    main()
