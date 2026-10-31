"""
kb.py 的单元测试：分词、混合检索融合、切片参数入口。

为什么不放在 test_app.py：那个文件测的是 HTTP 层，这里是检索内核，
混在一起会让"接口挂了"和"检索算错了"两个问题互相干扰。
"""

from array import array

import pytest

import kb


# ============================================================
# 1) 分词
# ============================================================
def test_tokenize_中文按二字滑窗():
    toks = kb._tokenize("切片重叠")
    assert toks == ["切片", "片重", "重叠"]


def test_tokenize_单字也能留下():
    assert kb._tokenize("好") == ["好"]


def test_tokenize_英文标识符带下划线要整体保留():
    """
    `max_steps` 拆成 max 和 steps 就丢掉了它作为一个符号的辨识度——
    而"含代码标识符的精确查询"正是纯向量最容易失手的场景，关键词这一路必须认得出它。
    """
    toks = kb._tokenize("看 max_steps 这个参数")
    assert "max_steps" in toks
    assert "max" not in toks
    assert "steps" not in toks


def test_tokenize_大小写归一():
    assert "json" in kb._tokenize("JSON 解析")


# ============================================================
# 2) 混合检索：融合必须真的改变排序
# ============================================================
def _seed(conn, texts):
    """直接写库，向量全一样——这样余弦分完全相等，排序只能由关键词那一路决定。"""
    vec = array("f", [1.0, 0.0, 0.0, 0.0])       # 已归一化，模长为 1
    cur = conn.execute(
        "INSERT INTO documents (name, content_hash, n_chunks, created_at) VALUES (?, ?, ?, ?)",
        ("t.txt", "hash-seed", len(texts), "2026-01-01 00:00:00"),
    )
    doc_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO chunks (doc_id, ord, text, vec, dim) VALUES (?, ?, ?, ?, ?)",
        [(doc_id, i, t, vec.tobytes(), 4) for i, t in enumerate(texts)],
    )
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('dim', '4')")
    conn.commit()
    return doc_id


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """假向量化：不联网，且所有块余弦相同，逼出"只靠关键词能不能分出胜负"。"""
    def fake_embed(texts, config=None, **kwargs):
        return [array("f", [1.0, 0.0, 0.0, 0.0]) for _ in texts]

    monkeypatch.setattr(kb, "embed_texts", fake_embed)

    c = kb.connect(tmp_path / "kb_test.sqlite3")
    _seed(c, [
        "这一块讲的是前端怎么按空行切分事件缓冲，和切片参数无关。",
        "切片的 overlap 参数决定相邻两块重叠多少字，默认 80。",
    ])
    yield c
    c.close()


def test_混合检索把含关键词的块顶到第一(conn):
    hits = kb.search(conn, "overlap 参数是什么", top_k=2, config=None, hybrid=True)

    assert "overlap" in hits[0]["text"], "关键词命中的块应该排第一"
    assert hits[0]["lexical"] > 0, "第一名的关键词分不该是 0"

    # ★ 两个块的 fused 会完全相等，这是 RRF 在小候选集上的对称性：
    #   一条余弦排第一、关键词排第二，另一条正好相反，两边各得 1/61+1/62。
    #   所以排序不是靠 fused 分出胜负，而是靠"平局以关键词分为准"这条规则。
    #   断言写 fused 大小关系会误判——这条用例本身就是这个坑的证据。
    assert hits[0]["fused"] == hits[1]["fused"]
    assert hits[0]["lexical"] > hits[1]["lexical"], "平局时应由关键词分裁决"


def test_纯向量模式下两块打平(conn):
    """
    对照组：向量完全相同时，纯向量分不出胜负。
    这条用例的存在是为了证明上一条的提升来自融合，而不是偶然。
    """
    hits = kb.search(conn, "overlap 参数是什么", top_k=2, config=None, hybrid=False)

    assert hits[0]["score"] == hits[1]["score"]
    assert "fused" in hits[0]
    assert hits[0]["lexical"] == 0.0, "关掉混合检索就不该算关键词分"


def test_检索结果不暴露内部排序字段(conn):
    hits = kb.search(conn, "重叠多少字", top_k=1, config=None, hybrid=True)
    assert not any(k.startswith("_") for k in hits[0]), f"内部字段漏出来了：{hits[0].keys()}"
    for key in ("score", "lexical", "fused", "text", "doc", "doc_id", "ord"):
        assert key in hits[0]


def test_空查询返回空列表(conn):
    assert kb.search(conn, "   ", top_k=3, config=None) == []


# ============================================================
# 3) 切片参数入口（评测脚本依赖它按不同块大小建库）
# ============================================================
def test_add_document_支持自定义切片大小(tmp_path, monkeypatch):
    def fake_embed(texts, config=None, **kwargs):
        return [array("f", [1.0, 0.0, 0.0, 0.0]) for _ in texts]

    monkeypatch.setattr(kb, "embed_texts", fake_embed)

    c = kb.connect(tmp_path / "kb2.sqlite3")
    text = "这是第一段。" * 40 + "\n\n" + "这是第二段。" * 40

    small = kb.add_document(c, "a.txt", text, chunk_size=100, overlap=20)
    big = kb.add_document(c, "b.txt", text + "\n\n结尾不同", chunk_size=400, overlap=20)

    assert small["n_chunks"] > big["n_chunks"], "块设小，切出来的块数应该更多"
    c.close()
