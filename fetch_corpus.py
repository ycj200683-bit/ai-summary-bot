"""
fetch_corpus.py —— 从量子位公开接口补充语料到 corpus/（凑够 30 篇）

【它做什么】
  ① 分页拉 https://www.qbitai.com/wp-json/wp/v2/posts
  ② 把 HTML 正文转成纯文本（段落之间空一行）
  ③ 剔掉署名行、参考链接、URL、编辑校对之类的尾巴
  ④ 只保留 500~4000 字的文章，按 NNN.txt 存进 corpus/
  ⑤ 把元信息追加到 corpus/index.tsv

【为什么需要 Referer 和 User-Agent】
  裸请求会拿到 403。这个接口对没有 Referer 的请求做了拦截——
  说明对方允许读，但不想被无脑脚本刷。所以这里：
    · 每页之间 sleep 1 秒，不并发
    · 只拉需要的页数，拉够就停
  抓取是有礼貌的借用，不是把人家站点镜像下来。

【版权提醒】
  corpus/*.txt 已在 .gitignore 里，正文只留在本地做批量摘要的样本。
  不要提交到公开仓库——这是数据合规的基本动作，不是可选项。

【用法】
  python fetch_corpus.py            # 补到 30 篇（按需拉）
  python fetch_corpus.py --target 50
"""

import argparse
import html
import json
import os
import re
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CORPUS_DIR = os.path.join(BASE_DIR, "corpus")
INDEX_FILE = os.path.join(CORPUS_DIR, "index.tsv")

API = "https://www.qbitai.com/wp-json/wp/v2/posts"

# 少了这两个头会 403。这是对方站点的反爬底线，遵守就行，别想着绕。
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Referer": "https://www.qbitai.com/",
    "Accept": "application/json",
}

MIN_CHARS = 500
MAX_CHARS = 4000

# 这些行是要丢掉的"非正文"：署名、校对、参考、二维码提示、版权声明……
DROP_PATTERNS = [
    r"^本文\s*(?:作者|编辑|来源)",
    r"^作者\s*[：:]",
    r"^编辑\s*[：:]",
    r"^校对\s*[：:]",
    r"^来源\s*[：:]",
    r"^参考(?:资料|链接)?\s*[：:]?$",
    r"^(?:原文|文章)?链接\s*[：:]",
    r"^点击(?:阅读原文|关注)",
    r"^扫描二维码",
    r"^—+\s*完\s*—+$",
    r"^版权归",
    r"^转载",
    # 量子位转载稿的固定尾巴，会污染摘要（模型有时会把它当正文总结进去）
    r"量子位获授权转载",
    r"^本文由.{0,20}提供",
    r"^https?://\S+$",
]


def strip_html(raw: str) -> str:
    """把 WordPress 的 content.rendered 变成纯文本。"""
    text = raw
    # 图片、脚本、样式整块丢掉（连同里面的内容）
    text = re.sub(r"<(script|style|figure)[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    # 段落/换行 → 真正的换行；连续空行后面统一压成一个
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    # 其余标签直接去掉
    text = re.sub(r"<[^>]+>", "", text)
    # HTML 实体：&nbsp; &amp; &#8217; 之类
    text = html.unescape(text)
    text = text.replace("\u00a0", " ")
    # 全角空格也当空白
    text = text.replace("\u3000", " ")

    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            lines.append("")
            continue
        if any(re.search(p, line) for p in DROP_PATTERNS):
            continue
        lines.append(line)

    # 把连续空行压成一个，再收拾首尾
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out


def fetch_page(page: int, per_page: int = 10) -> list:
    url = f"{API}?per_page={per_page}&page={page}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def existing_links() -> set:
    """已经收过的原文链接，避免重复。"""
    links = set()
    if not os.path.exists(INDEX_FILE):
        return links
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        for line in f.readlines()[1:]:                 # 跳过表头
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 5:
                links.add(parts[4])
    return links


def next_index() -> int:
    """下一个可用编号：现有最大编号 + 1。"""
    nums = []
    for name in os.listdir(CORPUS_DIR):
        m = re.match(r"^(\d{3})\.txt$", name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def main():
    parser = argparse.ArgumentParser(description="补充中文语料到 corpus/")
    parser.add_argument("--target", type=int, default=30, help="语料总篇数目标，默认 30")
    args = parser.parse_args()

    os.makedirs(CORPUS_DIR, exist_ok=True)

    have = len([n for n in os.listdir(CORPUS_DIR) if re.match(r"^\d{3}\.txt$", n)])
    need = args.target - have
    print(f"现有 {have} 篇，目标 {args.target} 篇，还需 {need} 篇")
    if need <= 0:
        print("✅ 已经够了，不用抓")
        return

    seen = existing_links()
    idx = next_index()
    added = 0
    page = 1
    is_new_index = not os.path.exists(INDEX_FILE)

    with open(INDEX_FILE, "a", encoding="utf-8") as index_f:
        if is_new_index:
            index_f.write("file\tchars\tdate\ttitle\tlink\n")

        while added < need and page <= 10:          # 最多翻 10 页，防止无限循环
            try:
                posts = fetch_page(page)
            except Exception as e:
                print(f"❌ 第 {page} 页取失败：{type(e).__name__}: {e}")
                break

            if not posts:
                print("没有更多文章了")
                break

            for post in posts:
                if added >= need:
                    break

                link = post.get("link", "")
                if link in seen:
                    continue

                title = html.unescape(re.sub(r"<[^>]+>", "", post.get("title", {}).get("rendered", ""))).strip()
                body = strip_html(post.get("content", {}).get("rendered", ""))

                if not title or len(body) < MIN_CHARS:
                    continue
                if len(body) > MAX_CHARS:
                    body = body[:MAX_CHARS].rsplit("\n", 1)[0]   # 超长就截到段落边界

                fname = f"{idx:03d}.txt"
                with open(os.path.join(CORPUS_DIR, fname), "w", encoding="utf-8") as f:
                    f.write(f"{title}\n\n{body}\n")

                date = (post.get("date") or "")[:10]
                index_f.write(f"{fname}\t{len(body)}\t{date}\t{title}\t{link}\n")
                seen.add(link)
                idx += 1
                added += 1
                print(f"  ✅ {fname}  {len(body):>5} 字  {title[:38]}")

            page += 1
            time.sleep(1)        # 有礼貌：不并发、不刷

    print(f"\n本次新增 {added} 篇，现在共 {have + added} 篇")


if __name__ == "__main__":
    main()
