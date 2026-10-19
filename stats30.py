# -*- coding: utf-8 -*-
"""统计 out30.csv 的批量摘要结果，输出可直接写进简历的数字。"""
import csv
import re
import statistics as st

SENTENCE_ENDS = "。！？!?"
LIST_MARKER = re.compile(r"^\s*(?:[-*\u2022\u00b7]|\d{1,2}[.)\u3001])\s*")
CJK_PUNCT = "。！？；：，、"


def count_sentences(text):
    return sum(1 for ch in text if ch in SENTENCE_ENDS)


rows = []
with open("out30.csv", "r", encoding="utf-8-sig", newline="") as f:
    for row in csv.DictReader(f):
        rows.append(row)

n = len(rows)
ok = [r for r in rows if r.get("status") == "ok"]
fail = [r for r in rows if r.get("status") != "ok"]
skipped = [r for r in rows if r.get("status") == "skip"]

in_len = [len(r.get("content") or "") for r in ok]
out_len = [len(r.get("summary") or "") for r in ok]
lat = [int(r["latency_ms"]) for r in ok if r.get("latency_ms")]
sent = [count_sentences(r.get("summary") or "") for r in ok]
ratios = [o / i for o, i in zip(out_len, in_len) if i]

print("=" * 46)
print(f"总条目      {n}")
print(f"成功        {len(ok)}")
print(f"失败        {len(fail)}")
print(f"跳过        {len(skipped)}")
print(f"成功率      {len(ok) / n * 100:.1f}%")
print("=" * 46)

print(f"原文长度    最短 {min(in_len)} / 最长 {max(in_len)} / 均值 {st.mean(in_len):.0f} 字")
print(f"摘要长度    最短 {min(out_len)} / 最长 {max(out_len)} / 均值 {st.mean(out_len):.1f} 字")
print(f"压缩比      均值 {st.mean(ratios) * 100:.1f}%  （区间 {min(ratios) * 100:.1f}% ~ {max(ratios) * 100:.1f}%）")
print("-" * 46)
print(f"耗时        最短 {min(lat)} ms / 最长 {max(lat)} ms / 均值 {st.mean(lat):.0f} ms")
print(f"耗时中位数  {st.median(lat):.0f} ms")
p90 = sorted(lat)[int(len(lat) * 0.9) - 1]
print(f"P90 耗时    {p90} ms")
print("-" * 46)

dist = {}
for s in sent:
    dist[s] = dist.get(s, 0) + 1
print(f"句数分布    {dict(sorted(dist.items()))}")
print(f"句数合规    {sum(1 for s in sent if s == 3)}/{len(ok)} 条正好 3 句")

blank = sum(1 for r in ok if "\n\n" in (r.get("summary") or ""))
newline = sum(1 for r in ok if "\n" in (r.get("summary") or ""))
marker = sum(1 for r in ok if LIST_MARKER.match(r.get("summary") or ""))
sp_punct = sum(1 for r in ok if any(" " + c in (r.get("summary") or "") or c + " " in (r.get("summary") or "") for c in CJK_PUNCT))
double_sp = sum(1 for r in ok if "  " in (r.get("summary") or ""))

print("-" * 46)
print(f"残留空行    {blank}")
print(f"残留换行    {newline}")
print(f"残留列表符  {marker}")
print(f"标点带空格  {sp_punct}")
print(f"连续空格    {double_sp}")
print("=" * 46)

# 最短 / 最长各挑一条展示
si = out_len.index(min(out_len))
li = out_len.index(max(out_len))
li_lat = lat.index(max(lat))
print(f"\n最短摘要  {rows[ok.index(ok[si])]['id']}  {out_len[si]} 字")
print("  " + (ok[si].get("summary") or "").replace("\n", " "))
print(f"\n最长摘要  {ok[li]['id']}  {out_len[li]} 字")
print("  " + (ok[li].get("summary") or "").replace("\n", " "))
print(f"\n最慢一条  {ok[li_lat]['id']}  {lat[li_lat]} ms")
print("  " + (ok[li_lat].get("title") or "")[:40])
