"""定内存分页检索的强校验测试。

覆盖：
- 与独立参考 BM25 实现逐篇对比分数与全榜顺序（分数降序、同分稿号升序）；
- 游标逐页拼回的榜单与一次性全量榜单完全一致；
- 撤稿后续页跳过被撤版本、补足当页、remaining_hits 精确；
- 同号重刊后旧游标看不到新版本、新检索可见；
- 首查统计/计分/命中数取自同一在刊快照（计分期间并发刊发不串快照）；
- 峰值工作内存只取决于单篇大小/查询词数/页长，与语料规模无关（tracemalloc）；
- 升级前库（无 postings 表）启动时自动回填，旧游标语义不变。
"""

import base64
import gc
import json
import math
import os
import sys
import tempfile
import tracemalloc

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
from main import app  # noqa: E402

K1, B = main.K1, main.B


# ----------------------------------------------------------- 独立参考实现

def ref_idf(n, df):
    return math.log(1.0 + (n - df + 0.5) / (df + 0.5))


def ref_bm25(tf, dl, terms, dfs, n_docs, avgdl):
    score = 0.0
    for t in terms:
        f = tf.get(t)
        if not f:
            continue
        idf = ref_idf(n_docs, dfs[t])
        score += idf * (f * (K1 + 1.0)) / (f + K1 * (1.0 - B + B * dl / avgdl))
    return score


def ref_ranking(corpus, terms):
    """corpus: {doc_id: tokens}。返回 [(doc_id, score)] 全榜。"""
    n_docs = len(corpus)
    avgdl = sum(len(toks) for toks in corpus.values()) / n_docs
    tfs = {d: {} for d in corpus}
    for d, toks in corpus.items():
        for t in toks:
            tfs[d][t] = tfs[d].get(t, 0) + 1
    dfs = {t: sum(1 for d in corpus if t in tfs[d]) for t in terms}
    hits = []
    for d, toks in corpus.items():
        s = ref_bm25(tfs[d], len(toks), terms, dfs, n_docs, avgdl)
        if s > 0:
            hits.append((d, s))
    hits.sort(key=lambda x: (-x[1], x[0]))
    return hits, dfs, n_docs, avgdl


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "test.sqlite3"))
    main._init_db()
    with TestClient(app) as c:
        yield c


def _publish(c, doc_id, tokens):
    r = c.post("/manuscripts", json={"doc_id": doc_id, "tokens": tokens})
    assert r.status_code == 201, r.text
    return r.json()["version"]


def _build_corpus(seed=0):
    """25 篇长度不一、含重复词的语料，制造同分与不同 tf。"""
    corpus = {}
    for i in range(25):
        toks = [f"共{j % 4}" for j in range(3 + i % 5)]  # 常见词，tf 随 i 变化
        toks += [f"独{i:02d}_{k}" for k in range(4)]     # 每篇 4 个独有词
        if i % 3 == 0:
            toks += ["特批"]
        corpus[f"MS-{i:03d}"] = toks
    return corpus


def _publish_corpus(c, corpus):
    for d, toks in corpus.items():
        _publish(c, d, toks)


# ----------------------------------------------------------- 分数与榜序

def test_scores_match_reference_and_ordering(client):
    corpus = _build_corpus()
    _publish_corpus(client, corpus)
    terms = ["共0", "共2", "特批"]

    expected, dfs, n_docs, avgdl = ref_ranking(corpus, terms)

    r = client.post("/search", json={"terms": terms, "size": 100})
    assert r.status_code == 200
    data = r.json()
    assert data["total_hits"] == len(expected)
    assert [(h["doc_id"], h["score"]) for h in data["hits"]] == expected

    # 排序契约：分数降序，同分稿号升序
    scores = [h["score"] for h in data["hits"]]
    ids = [h["doc_id"] for h in data["hits"]]
    for a, b in zip(scores, scores[1:]):
        assert a >= b
    for h1, h2 in zip(data["hits"], data["hits"][1:]):
        if h1["score"] == h2["score"]:
            assert h1["doc_id"] < h2["doc_id"]

    # df 与 N 冻结进 searches 行，与参考实现一致
    sid = main._decode_cursor(data["next_cursor"])[0] if data["next_cursor"] else None
    if sid:
        conn = main._connect()
        row = conn.execute("SELECT * FROM searches WHERE id=?", (sid,)).fetchone()
        conn.close()
        assert row["n_docs"] == n_docs
        assert math.isclose(row["avgdl"], avgdl, rel_tol=1e-12)
        assert json.loads(row["dfs"]) == dfs


def test_pagination_reconstructs_full_ranking(client):
    corpus = _build_corpus()
    _publish_corpus(client, corpus)
    terms = ["共1", "共3"]
    expected, *_ = ref_ranking(corpus, terms)

    # 页长 3 逐页翻完，拼回全榜
    first = client.post("/search", json={"terms": terms, "size": 3}).json()
    assert first["total_hits"] == len(expected)
    seen = [(h["doc_id"], h["score"]) for h in first["hits"]]
    cursor = first["next_cursor"]
    pages = 1
    while cursor:
        resp = client.post("/search/next", json={"cursor": cursor}).json()
        seen.extend((h["doc_id"], h["score"]) for h in resp["hits"])
        cursor = resp["next_cursor"]
        pages += 1
        assert pages <= len(expected) + 1
    assert seen == expected

    # 每页最多 10 条（默认页长）
    default_first = client.post("/search", json={"terms": terms}).json()
    assert len(default_first["hits"]) <= 10


def test_remaining_hits_exact_after_withdrawals(client):
    corpus = _build_corpus()
    _publish_corpus(client, corpus)
    terms = ["共0", "共1"]
    expected, *_ = ref_ranking(corpus, terms)
    total = len(expected)

    first = client.post("/search", json={"terms": terms, "size": 5}).json()
    assert first["total_hits"] == total

    # 撤掉榜内 3 篇（含首页 1 篇、后续 2 篇）
    withdraw_ids = {expected[2][0], expected[6][0], expected[7][0]}
    for d in withdraw_ids:
        assert client.post(f"/manuscripts/{d}/withdraw").status_code == 200

    page2 = client.post("/search/next",
                        json={"cursor": first["next_cursor"]}).json()
    # 当页补满 5 条，且不含任何被撤稿号
    assert len(page2["hits"]) == 5
    assert not withdraw_ids.intersection(h["doc_id"] for h in page2["hits"])
    # remaining_hits = 游标之后当前仍有效的命中数（精确）
    anchor_score, anchor_id = (first["hits"][-1]["score"],
                               first["hits"][-1]["doc_id"])
    live_after = [
        (d, s) for d, s in expected
        if d not in withdraw_ids
        and (s < anchor_score or (s == anchor_score and d > anchor_id))
    ]
    assert page2["remaining_hits"] == len(live_after)

    # 继续翻页直到耗尽，拼回的榜单等于参考榜单剔除被撤稿
    seen = [(h["doc_id"], h["score"]) for h in first["hits"]
            if h["doc_id"] not in withdraw_ids]
    seen.extend((h["doc_id"], h["score"]) for h in page2["hits"])
    cursor = page2["next_cursor"]
    while cursor:
        resp = client.post("/search/next", json={"cursor": cursor}).json()
        seen.extend((h["doc_id"], h["score"]) for h in resp["hits"])
        cursor = resp["next_cursor"]
    assert seen == [(d, s) for d, s in expected if d not in withdraw_ids]


def test_republish_hidden_from_old_cursor_visible_to_new_search(client):
    corpus = _build_corpus()
    _publish_corpus(client, corpus)
    terms = ["共2", "特批"]
    expected, *_ = ref_ranking(corpus, terms)

    first = client.post("/search", json={"terms": terms, "size": 4}).json()
    target = expected[3][0]
    old_version = next(h["version"] for h in first["hits"] if h["doc_id"] == target) \
        if target in [h["doc_id"] for h in first["hits"]] else None

    # 撤稿并同号重刊（新内容、新版本号）
    r = client.post(f"/manuscripts/{target}/withdraw")
    assert r.status_code == 200
    v_new = _publish(client, target, ["共2", "共2", "全新内容", "特批"])
    assert v_new > r.json()["version"]

    # 旧游标翻完：看不到重刊版本，remaining_hits 不含它
    cursor = first["next_cursor"]
    old_seen = [h["doc_id"] for h in first["hits"]]
    while cursor:
        resp = client.post("/search/next", json={"cursor": cursor}).json()
        old_seen.extend(h["doc_id"] for h in resp["hits"])
        for h in resp["hits"]:
            if h["doc_id"] == target:
                pytest.fail("重刊版本泄漏进旧检索")
        cursor = resp["next_cursor"]
    assert target not in old_seen or old_version is not None

    # 新检索：重刊版本可见，且携带新版本号
    fresh = client.post("/search", json={"terms": terms, "size": 100}).json()
    hit = next(h for h in fresh["hits"] if h["doc_id"] == target)
    assert hit["version"] == v_new


def test_first_search_uses_one_committed_snapshot(client):
    """计分期间并发刊发：慢请求的统计、分数、total_hits 同属首查时刻快照。"""
    _publish(client, "S-1", ["抗战", "北平"])
    _publish(client, "S-2", ["抗战", "上海"])

    import threading
    started = threading.Event()
    outcome = {}

    def slow():
        started.set()
        outcome["r"] = client.post("/search", json={
            "terms": ["抗战"], "size": 1, "debug_delay_seconds": 2.0})

    t = threading.Thread(target=slow)
    t.start()
    assert started.wait(5)
    # 慢请求已取快照、正在 sleep，此时刊发 + 撤稿各一
    _publish(client, "S-3", ["抗战", "广州"])
    client.post("/manuscripts/S-1/withdraw")
    t.join(timeout=10)

    data = outcome["r"].json()
    # 快照在刊发/撤稿之前：N=2，total_hits=2；首页 1 条仍是撤稿前的 S-1
    assert data["total_hits"] == 2
    assert [h["doc_id"] for h in data["hits"]] == ["S-1"]
    sid = main._decode_cursor(data["next_cursor"])[0]
    conn = main._connect()
    row = conn.execute("SELECT n_docs, snapshot_seq FROM searches WHERE id=?",
                       (sid,)).fetchone()
    conn.close()
    assert row["n_docs"] == 2
    assert row["snapshot_seq"] == 2  # S-3 的 version=3 不在快照内


def test_no_hit_query(client):
    _publish(client, "X-1", ["a", "b"])
    r = client.post("/search", json={"terms": ["不存在的词"]})
    assert r.status_code == 200
    assert r.json()["total_hits"] == 0 and r.json()["hits"] == []
    assert r.json()["next_cursor"] is None


def test_empty_corpus_search(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "empty.sqlite3"))
    main._init_db()
    with TestClient(app) as c:
        r = c.post("/search", json={"terms": ["x"]})
        assert r.status_code == 200
        body = r.json()
        assert body["total_hits"] == 0 and body["hits"] == []
        assert body["next_cursor"] is None


# ----------------------------------------------------------- 定内存

def _publish_bulk(c, n_docs, vocab_per_doc=510, common=("抗战", "北平"),
                  id_offset=0):
    for i in range(n_docs):
        idx = id_offset + i
        tokens = [f"u{idx:05d}_{k}" for k in range(vocab_per_doc)] + list(common)
        r = c.post("/manuscripts",
                   json={"doc_id": f"MS-{idx:05d}", "tokens": tokens})
        assert r.status_code == 201, r.text


def test_working_memory_independent_of_corpus_size(client):
    """语料翻倍时，首查/续页的 Python 峰值增量不应随语料规模增长。"""
    gc.collect()
    tracemalloc.start()
    try:
        _publish_bulk(client, 400)
        gc.collect()
        base = tracemalloc.take_snapshot()

        def peak_of(fn):
            gc.collect()
            tracemalloc.reset_peak()
            fn()
            _, peak = tracemalloc.get_traced_memory()
            return peak

        def first_and_next():
            r = client.post("/search", json={"terms": ["抗战", "北平"], "size": 10})
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["total_hits"] == 400
            nxt = client.post("/search/next",
                              json={"cursor": data["next_cursor"]})
            assert nxt.status_code == 200, nxt.text
            assert nxt.json()["remaining_hits"] == 390

        peak_small = peak_of(first_and_next)

        _publish_bulk(client, 1600, id_offset=400)  # 总语料 400 -> 2000，5 倍
        gc.collect()

        def first_and_next_big():
            r = client.post("/search", json={"terms": ["抗战", "北平"], "size": 10})
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["total_hits"] == 2000
            nxt = client.post("/search/next",
                              json={"cursor": data["next_cursor"]})
            assert nxt.status_code == 200, nxt.text
            assert nxt.json()["remaining_hits"] == 1990

        peak_big = peak_of(first_and_next_big)

        # 工作内存增量有界：允许抖动，但不得随语料线性膨胀
        # （旧实现 2000 篇时仅此路径就要吃掉上百 MiB）
        assert peak_big < peak_small * 2 + 8 * 1024 * 1024, (
            f"peak_small={peak_small}, peak_big={peak_big}")
        # 绝对值同样应远小于 256MiB 进程预算
        assert peak_big < 64 * 1024 * 1024
    finally:
        tracemalloc.stop()


def test_page_size_bounds_heap_memory(client):
    _publish_bulk(client, 300)
    gc.collect()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        r10 = client.post("/search", json={"terms": ["抗战"], "size": 10}).json()
        _, p10 = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        r100 = client.post("/search", json={"terms": ["抗战"], "size": 100}).json()
        _, p100 = tracemalloc.get_traced_memory()
        assert len(r10["hits"]) == 10 and len(r100["hits"]) == 100
        # 大页长允许用更多内存，但仍是有界的小量，而非全语料量级
        assert p100 < 32 * 1024 * 1024
    finally:
        tracemalloc.stop()


# ----------------------------------------------------------- 旧库升级

def test_legacy_db_backfills_postings(tmp_path, monkeypatch):
    """模拟升级前的库（只有 manuscripts、无 postings）：启动回填后检索/游标正常。"""
    db = str(tmp_path / "legacy.sqlite3")
    monkeypatch.setattr(main, "DB_PATH", db)
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE manuscripts (version INTEGER PRIMARY KEY AUTOINCREMENT,"
        " doc_id TEXT, tokens TEXT, tf TEXT, length INTEGER, status TEXT DEFAULT 'published',"
        " published_at TEXT, withdrawn_at TEXT)")
    legacy = {
        "L-1": ["抗战", "北平", "胡同"],
        "L-2": ["抗战", "上海"],
        "L-3": ["知青", "北大荒"],
    }
    for d, toks in legacy.items():
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        conn.execute("INSERT INTO manuscripts (doc_id,tokens,tf,length,status,published_at)"
                     " VALUES (?,?,?,?,'published','t')",
                     (d, json.dumps(toks), json.dumps(tf), len(toks)))
    conn.commit()
    conn.close()

    main._init_db()  # 触发回填
    conn = main._connect()
    n_postings = conn.execute("SELECT COUNT(*) c FROM postings").fetchone()["c"]
    conn.close()
    assert n_postings == sum(len(set(toks)) for toks in legacy.values())

    with TestClient(app) as c:
        r = c.post("/search", json={"terms": ["抗战", "北平"], "size": 1})
        assert r.status_code == 200
        data = r.json()
        assert data["total_hits"] == 2
        assert [h["doc_id"] for h in data["hits"]] == ["L-1"]
        p2 = c.post("/search/next", json={"cursor": data["next_cursor"]}).json()
        assert [h["doc_id"] for h in p2["hits"]] == ["L-2"]
        assert p2["remaining_hits"] == 1 and p2["next_cursor"] is None


def test_cursor_issued_before_upgrade_still_pages(client):
    """升级前签发的游标（旧 searches 行 + base64 游标）升级后仍按原分数/次序翻页。"""
    corpus = _build_corpus()
    _publish_corpus(client, corpus)
    terms = ["共0", "共1"]

    # 先用现行实现取得首查会冻结的统计与全榜（模拟升级前那次首查的结果）
    probe = client.post("/search", json={"terms": terms, "size": 100}).json()
    full = probe["hits"]
    anchor = full[0]  # 旧游标锚在首条之后

    # 手工写入一条“升级前”登记的 searches 行（结构与旧版完全一致）
    conn = main._connect()
    sid = "legacy-search-0001"
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO searches (id, terms, size, snapshot_seq, n_docs, avgdl, dfs, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (sid, json.dumps(terms), 100, probe["snapshot_seq"],
         # 从现行库反查统计，保证与升级前首查一致
         conn.execute("SELECT COUNT(*) n FROM manuscripts WHERE status='published'"
                      ).fetchone()["n"],
         conn.execute("SELECT AVG(length) a FROM manuscripts WHERE status='published'"
                      ).fetchone()["a"],
         json.dumps({t: sum(1 for d in corpus if t in corpus[d]) for t in terms}),
         "2000-01-01T00:00:00+00:00"))
    conn.execute("COMMIT")
    conn.close()

    # 用与旧版相同的编码方式签发游标
    old_cursor = main._encode_cursor(sid, anchor["score"], anchor["doc_id"])

    # 升级后期间撤掉榜内一篇（位于锚点之后），旧游标续页须跳过它
    victim = full[5]["doc_id"]
    assert client.post(f"/manuscripts/{victim}/withdraw").status_code == 200

    seen = []
    cur = old_cursor
    while cur:
        resp = client.post("/search/next", json={"cursor": cur}).json()
        seen.extend((h["doc_id"], h["score"]) for h in resp["hits"])
        cur = resp["next_cursor"]

    expected = [(h["doc_id"], h["score"]) for h in full[1:]
                if h["doc_id"] != victim]
    # 顺序与分数与升级前首查完全一致，仅剔除升级后被撤的版本
    assert seen == expected
    assert all(a[1] >= b[1] for a, b in zip(seen, seen[1:]))
