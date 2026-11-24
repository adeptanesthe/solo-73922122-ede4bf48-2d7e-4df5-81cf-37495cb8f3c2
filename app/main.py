"""口述史馆检索 API（定内存分页版）

- 编辑按稿号刊发分词后的转写稿；在刊稿号拒绝重复刊发，撤稿后可同号重刊。
- 读者首查用当时全部在刊稿计算 BM25，凭游标续页。
- 游标只保存查询与首查时的语料统计（N / avgdl / 各词 df），不保存整张榜单；
  续页时用冻结的统计重算分数，跳过当前已撤稿的版本，从后续命中补足当页。
- 同号重刊得到新的版本序号，只进入重刊后新开的检索。
- SQLite 开 WAL、每请求独立连接、写事务尽量短，撤稿不必等正在计分的请求结束。

定内存保证
----------
首查与续页都不把语料整体读进进程：

1. 语料统计（N、avgdl、各词 df）全部由 SQL 聚合得到，内存 O(查询词数)；
2. 候选稿件通过倒排 postings 表 +  manuscripts 行上的流式游标逐篇产出，
   每次只解析单篇 tf（O(单篇大小)），计分后随即释放；
3. 只保留一个容量为页长的有界堆和最终页（O(页长)），命中总数由
   `COUNT(DISTINCT version)` 直接得到，不物化命中列表。

因此单次请求的工作内存只取决于最大单篇稿件的大小、查询词数与页长，
与在刊稿件总数无关。
"""

from __future__ import annotations

import base64
import heapq
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import anyio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("DB_PATH", "oral_history.db")

# Okapi BM25 参数
K1 = 1.2
B = 0.75

MAX_PAGE_SIZE = 100

# 同步视图由 Starlette 线程池执行。默认上限 40 个线程，每个线程在受限地址空间
# （如 256 MiB RLIMIT_AS）下要预留 8 MiB 栈，会把虚拟内存吃光导致后续
# sqlite 连接分配失败。压到 8：检索为 IO/CPU 混合的短请求，足够并发。
MAX_WORKER_THREADS = 8


# ------------------------------------------------------------------ 数据库

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL 是库级持久属性，_init_db 已设置；这里只设忙等，避免每连接重复切模式
    # （在 256MiB 地址空间下，多余的 PRAGMA 往返也会放大分配器压力）
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = _connect()
    try:
        # WAL 是库级持久属性，初始化时设置一次即可：读（计分）不阻塞写（撤稿）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS manuscripts (
                version      INTEGER PRIMARY KEY AUTOINCREMENT,  -- 版本序号，单调递增
                doc_id       TEXT NOT NULL,                      -- 稿号
                tokens       TEXT NOT NULL,                      -- 分词结果(JSON 数组)
                tf           TEXT NOT NULL,                      -- 词频(JSON 对象)
                length       INTEGER NOT NULL,                   -- 文档长度(词数)
                status       TEXT NOT NULL DEFAULT 'published'
                             CHECK (status IN ('published', 'withdrawn')),
                published_at TEXT NOT NULL,
                withdrawn_at TEXT
            );
            -- 同一稿号任一时刻至多一个在刊版本：重复刊发被唯一索引拒绝
            CREATE UNIQUE INDEX IF NOT EXISTS uq_manuscripts_published_doc
                ON manuscripts(doc_id) WHERE status = 'published';
            CREATE INDEX IF NOT EXISTS ix_manuscripts_status_version
                ON manuscripts(status, version);

            -- 语料级倒排表：每个 (版本, 词项) 一行词频。
            -- 检索时按词项取候选版本，逐篇流式计分，避免把全语料读进内存。
            CREATE TABLE IF NOT EXISTS postings (
                version INTEGER NOT NULL REFERENCES manuscripts(version),
                term    TEXT NOT NULL,
                tf      INTEGER NOT NULL,
                PRIMARY KEY (term, version)
            ) WITHOUT ROWID;
            -- 候选流按 version 有序产出，保证每篇只处理一次且顺序确定
            CREATE INDEX IF NOT EXISTS ix_postings_version
                ON postings(version);

            -- 每个检索游标只登记查询与首查时的语料统计，不保存整张榜单
            CREATE TABLE IF NOT EXISTS searches (
                id           TEXT PRIMARY KEY,
                terms        TEXT NOT NULL,        -- 查询词(JSON 数组)
                size         INTEGER NOT NULL,     -- 首查页长
                snapshot_seq INTEGER NOT NULL,     -- 首查时的版本高水位
                n_docs       INTEGER NOT NULL,     -- 首查时在刊文档数 N
                avgdl        REAL NOT NULL,        -- 首查时平均文档长度
                dfs          TEXT NOT NULL,        -- 各查询词的 df(JSON 对象)
                created_at   TEXT NOT NULL
            );
            """
        )
        # 兼容升级前已存在的库：为历史版本分批回填倒排表。
        # 游标逐行拉取、每批固定行数提交，不把历史语料整体读进内存。
        BATCH_VERSIONS = 200
        while True:
            rows = conn.execute(
                "SELECT m.version AS version, m.tf AS tf FROM manuscripts m"
                " WHERE NOT EXISTS (SELECT 1 FROM postings p WHERE p.version = m.version)"
                " ORDER BY m.version LIMIT ?",
                (BATCH_VERSIONS,),
            ).fetchall()
            if not rows:
                break
            batch: list[tuple[int, str, int]] = []
            for row in rows:
                tf: dict[str, int] = {}
                for term in json.loads(row["tf"]):
                    tf[term] = tf.get(term, 0) + 1
                batch.extend((row["version"], term, f) for term, f in tf.items())
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(
                    "INSERT OR IGNORE INTO postings (version, term, tf) VALUES (?,?,?)",
                    batch,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 收紧默认线程池，避免地址空间受限时线程栈预留耗尽虚拟内存
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = MAX_WORKER_THREADS
    _init_db()
    yield


app = FastAPI(title="口述史馆检索 API", version="2.0.0", lifespan=lifespan)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ BM25

def _bm25_idf(n_docs: int, df: int) -> float:
    # Robertson idf 变体，+1 保证为正：命中至少一个查询词的文档得分必为正
    return math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))


def _corpus_stats(conn: sqlite3.Connection, terms: list[str],
                  max_version: Optional[int]) -> tuple[int, float, dict[str, int]]:
    """SQL 聚合取语料统计：在刊文档数 N、平均长度 avgdl、各查询词 df。

    全程不把文档读进内存，内存占用 O(查询词数)。续页时按首查版本高水位截断。
    """
    where = "status = 'published'"
    params: list = []
    if max_version is not None:
        where += " AND version <= ?"
        params.append(max_version)

    row = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(AVG(length), 0.0) AS avgdl"
        f" FROM manuscripts WHERE {where}",
        params,
    ).fetchone()
    n_docs, avgdl = row["n"], row["avgdl"]

    placeholders = ",".join("?" for _ in terms)
    df_rows = conn.execute(
        f"SELECT p.term AS term, COUNT(*) AS df"
        f" FROM postings p JOIN manuscripts m ON m.version = p.version"
        f" WHERE p.term IN ({placeholders}) AND m.{where}"
        f" GROUP BY p.term",
        [*terms, *params],
    ).fetchall()
    dfs = {t: 0 for t in terms}
    dfs.update({r["term"]: r["df"] for r in df_rows})
    return n_docs, avgdl, dfs


def _iter_candidates(conn: sqlite3.Connection, terms: list[str],
                     max_version: Optional[int]):
    """流式产出命中任一查询词的在刊候选 (version, doc_id, tf, length)。

    倒排表按 (term, version) 组织，再按 version 归并：每个候选版本只产出一次，
    逐行拉取、逐篇解析单篇 tf，不缓存整个结果集。
    """
    placeholders = ",".join("?" for _ in terms)
    sql = (
        "SELECT m.version AS version, m.doc_id AS doc_id, m.tf AS tf, m.length AS length"
        " FROM manuscripts m"
        " WHERE m.status = 'published'"
        " AND EXISTS (SELECT 1 FROM postings p"
        "             WHERE p.version = m.version AND p.term IN (" + placeholders + "))"
    )
    params: list = [*terms]
    if max_version is not None:
        sql += " AND m.version <= ?"
        params.append(max_version)
    sql += " ORDER BY m.version"
    # execute 不在客户端缓冲全部行，迭代逐行拉取 -> 工作内存与语料规模无关
    for row in conn.execute(sql, params):
        yield row["version"], row["doc_id"], json.loads(row["tf"]), row["length"]


def _count_hits(conn: sqlite3.Connection, terms: list[str],
                max_version: Optional[int]) -> int:
    """命中（至少含一个查询词的在刊版本）总数，SQL 直接计数，不物化列表。"""
    placeholders = ",".join("?" for _ in terms)
    sql = (
        "SELECT COUNT(DISTINCT m.version) AS c"
        " FROM manuscripts m"
        " WHERE m.status = 'published'"
        " AND EXISTS (SELECT 1 FROM postings p"
        "             WHERE p.version = m.version AND p.term IN (" + placeholders + "))"
    )
    params: list = [*terms]
    if max_version is not None:
        sql += " AND m.version <= ?"
        params.append(max_version)
    return conn.execute(sql, params).fetchone()["c"]


def _score_one(tf: dict[str, int], dl: int, terms: list[str],
               idfs: dict[str, float], avgdl: float) -> float:
    """单篇 BM25 计分；固定词序累加，保证相同输入浮点结果逐位一致。"""
    score = 0.0
    if avgdl <= 0.0:
        return score
    norm = K1 * (1.0 - B + B * dl / avgdl)
    for term in terms:
        idf = idfs.get(term)
        if idf is None:
            continue
        f = tf.get(term)
        if f:
            score += idf * (f * (K1 + 1.0)) / (f + norm)
    return score


class _HeapEntry:
    """有界堆元素。

    榜单顺序为 (分数降序, 稿号升序)，堆顶须是"当前最差"的命中：
    分数更低者更差；同分时稿号字典序更大者更差。
    """

    __slots__ = ("score", "doc_id", "version")

    def __init__(self, score: float, doc_id: str, version: int):
        self.score = score
        self.doc_id = doc_id
        self.version = version

    def __lt__(self, other: "_HeapEntry") -> bool:
        # 榜单顺序为 (分数降序, 稿号升序)；最小堆堆顶须是"最差"命中：
        # 分数更低者更差；同分时稿号字典序更大者更差。
        if self.score != other.score:
            return self.score < other.score
        return self.doc_id > other.doc_id

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, _HeapEntry)
                and self.score == other.score and self.doc_id == other.doc_id)

    def __gt__(self, other: "_HeapEntry") -> bool:
        # 优于堆顶 = 分数更高，或同分时稿号更小
        if self.score != other.score:
            return self.score > other.score
        return self.doc_id < other.doc_id

    def as_hit(self) -> dict:
        return {"doc_id": self.doc_id, "version": self.version, "score": self.score}


def _top_hits(heap: list[_HeapEntry], size: int) -> list[dict]:
    """把有界堆按 (分数降序, 稿号升序) 倒成最终页。"""
    ordered = sorted(heap, key=lambda h: (-h.score, h.doc_id))
    return [h.as_hit() for h in ordered[:size]]


# ------------------------------------------------------------------ 游标

def _encode_cursor(search_id: str, score: float, doc_id: str) -> str:
    payload = json.dumps({"sid": search_id, "after": [score, doc_id]}, ensure_ascii=False)
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(token: str) -> tuple[str, float, str]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
        score, doc_id = payload["after"]
        return str(payload["sid"]), float(score), str(doc_id)
    except Exception:
        raise HTTPException(status_code=400, detail="无法识别的游标")


# ------------------------------------------------------------------ 请求模型

class PublishRequest(BaseModel):
    doc_id: str = Field(min_length=1, max_length=128, description="稿号")
    tokens: list[str] = Field(min_length=1, description="分词后的转写稿")


class SearchRequest(BaseModel):
    terms: Optional[list[str]] = Field(default=None, description="查询词（已分词）")
    query: Optional[str] = Field(default=None, description="空白分隔的查询串，与 terms 二选一")
    size: int = Field(default=10, ge=1, le=MAX_PAGE_SIZE, description="页长")
    debug_delay_seconds: float = Field(
        default=0.0, ge=0.0, le=30.0,
        description="测试钩子：人为拉长计分耗时，用于演示撤稿不阻塞计分",
    )


class ContinueRequest(BaseModel):
    cursor: str = Field(description="上一页返回的 next_cursor")
    size: Optional[int] = Field(default=None, ge=1, le=MAX_PAGE_SIZE,
                                description="缺省沿用首查页长")


def _resolve_terms(req: SearchRequest) -> list[str]:
    raw = req.terms if req.terms else (req.query.split() if req.query else [])
    terms = list(dict.fromkeys(t for t in raw if t))  # 去重保序
    if not terms:
        raise HTTPException(status_code=422, detail="请通过 terms 或 query 提供至少一个查询词")
    return terms


# ------------------------------------------------------------------ 刊发 / 撤稿

@app.post("/manuscripts", status_code=201)
def publish_manuscript(req: PublishRequest):
    """刊发分词后的转写稿。在刊稿号重复刊发返回 409；撤稿后可同号重刊。"""
    tokens = [t for t in req.tokens if t]
    if not tokens:
        raise HTTPException(status_code=422, detail="tokens 不能为空")
    tf: dict[str, int] = {}
    for t in tokens:
        tf[t] = tf.get(t, 0) + 1
    posting_rows = [(t, f) for t, f in tf.items()]  # O(单篇互异词数)

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO manuscripts (doc_id, tokens, tf, length, status, published_at)"
                " VALUES (?,?,?,?, 'published', ?)",
                (req.doc_id, json.dumps(tokens, ensure_ascii=False),
                 json.dumps(tf, ensure_ascii=False), len(tokens), _utcnow()),
            )
            version = cur.lastrowid
            conn.executemany(
                "INSERT INTO postings (version, term, tf) VALUES (?,?,?)",
                [(version, term, f) for term, f in posting_rows],
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=409,
                                detail=f"稿号 {req.doc_id} 已有在刊版本，请先撤稿再重刊")
    finally:
        conn.close()
    return {"doc_id": req.doc_id, "version": version, "status": "published",
            "length": len(tokens)}


@app.post("/manuscripts/{doc_id}/withdraw")
def withdraw_manuscript(doc_id: str):
    """撤稿：立即生效，后续检索请求（含续页）不再返回该版本。"""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "UPDATE manuscripts SET status = 'withdrawn', withdrawn_at = ?"
            " WHERE doc_id = ? AND status = 'published' RETURNING version",
            (_utcnow(), doc_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=404, detail=f"稿号 {doc_id} 没有在刊版本")
        conn.execute("COMMIT")
        version = row["version"]
    finally:
        conn.close()
    return {"doc_id": doc_id, "version": version, "status": "withdrawn"}


@app.get("/manuscripts")
def list_manuscripts():
    """列出全部版本（含已撤稿），便于观察版本演变。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT version, doc_id, tokens, length, status, published_at, withdrawn_at"
            " FROM manuscripts ORDER BY version"
        ).fetchall()
    finally:
        conn.close()
    return {"manuscripts": [
        {**dict(r), "tokens": json.loads(r["tokens"])} for r in rows
    ]}


# ------------------------------------------------------------------ 检索

@app.post("/search")
def first_search(req: SearchRequest):
    """首查：用当前全部在刊稿计算 BM25，返回第一页与续页游标。

    语料统计、计分与命中计数取自同一个只读事务的在刊快照；
    全程流式处理，工作内存 O(单篇大小 + 查询词数 + 页长)。
    """
    terms = _resolve_terms(req)

    conn = _connect()
    try:
        # 单个只读事务 = 同一份已提交快照（WAL 下不阻塞刊发/撤稿）
        conn.execute("BEGIN")
        snapshot_seq = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM manuscripts"
        ).fetchone()["v"]
        n_docs, avgdl, dfs = _corpus_stats(conn, terms, None)
        total_hits = _count_hits(conn, terms, None)

        if req.debug_delay_seconds > 0:
            time.sleep(req.debug_delay_seconds)  # 测试钩子：模拟长耗时计分

        idfs = {t: _bm25_idf(n_docs, dfs[t]) for t in terms if dfs.get(t, 0) > 0}
        heap: list[_HeapEntry] = []
        if idfs and avgdl > 0.0:
            for version, doc_id, tf, dl in _iter_candidates(conn, terms, None):
                score = _score_one(tf, dl, terms, idfs, avgdl)
                if score <= 0.0:
                    continue
                entry = _HeapEntry(score, doc_id, version)
                if len(heap) < req.size:
                    heapq.heappush(heap, entry)
                elif entry > heap[0]:
                    heapq.heapreplace(heap, entry)
                # tf 随循环变量释放，进程内任何时刻至多持有一篇稿件的词频
        conn.execute("COMMIT")
    finally:
        conn.close()

    page = _top_hits(heap, req.size)

    # 还有后续命中时登记检索（只存统计，不存榜单），签发游标
    next_cursor = None
    if total_hits > len(page):
        search_id = uuid.uuid4().hex
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO searches (id, terms, size, snapshot_seq, n_docs, avgdl, dfs, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (search_id, json.dumps(terms, ensure_ascii=False), req.size, snapshot_seq,
                 n_docs, avgdl, json.dumps(dfs, ensure_ascii=False), _utcnow()),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
        next_cursor = _encode_cursor(search_id, page[-1]["score"], page[-1]["doc_id"])

    return {
        "terms": terms,
        "size": req.size,
        "snapshot_seq": snapshot_seq,
        "total_hits": total_hits,
        "hits": page,
        "next_cursor": next_cursor,
    }


@app.post("/search/next")
def continue_search(req: ContinueRequest):
    """续页：沿用首查冻结的分数与顺序，按当次在刊快照跳过已撤稿与首查后刊发的版本。

    重算只扫描 `version <= snapshot_seq` 且当前仍在刊的候选；命中总数在同一
    只读事务内计数，remaining_hits 精确反映游标之后、当前仍有效的命中数。
    """
    search_id, after_score, after_doc_id = _decode_cursor(req.cursor)

    conn = _connect()
    try:
        conn.execute("BEGIN")
        srow = conn.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()
        if srow is None:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=404, detail="游标不存在或已过期")

        terms = json.loads(srow["terms"])
        dfs = json.loads(srow["dfs"])
        size = req.size if req.size is not None else srow["size"]
        snapshot_seq = srow["snapshot_seq"]
        n_docs, avgdl = srow["n_docs"], srow["avgdl"]
        idfs = {t: _bm25_idf(n_docs, dfs[t]) for t in terms if dfs.get(t, 0) > 0}

        heap: list[_HeapEntry] = []
        remaining_total = 0  # 游标之后仍有效的命中数（精确）
        if idfs and avgdl > 0.0:
            for version, doc_id, tf, dl in _iter_candidates(conn, terms, snapshot_seq):
                score = _score_one(tf, dl, terms, idfs, avgdl)
                if score <= 0.0:
                    continue
                # 键集分页：榜单顺序 (分数降序, 稿号升序)，只保留严格位于游标
                # (after_score, after_doc_id) 之后者：
                #   分数更高 -> 在锚点之前，跳过；
                #   同分且稿号 <= 锚点 -> 锚点本身或其之前，跳过。
                if score > after_score or (score == after_score and doc_id <= after_doc_id):
                    continue
                remaining_total += 1
                entry = _HeapEntry(score, doc_id, version)
                if len(heap) < size:
                    heapq.heappush(heap, entry)
                elif entry > heap[0]:
                    heapq.heapreplace(heap, entry)
        conn.execute("COMMIT")
    finally:
        conn.close()

    page = _top_hits(heap, size)
    next_cursor = (
        _encode_cursor(search_id, page[-1]["score"], page[-1]["doc_id"])
        if remaining_total > len(page) else None
    )
    return {
        "terms": terms,
        "size": size,
        "remaining_hits": remaining_total,
        "hits": page,
        "next_cursor": next_cursor,
    }


# ------------------------------------------------------------------ 其他

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def index():
    return {
        "service": "口述史馆检索 API",
        "endpoints": {
            "publish": "POST /manuscripts",
            "withdraw": "POST /manuscripts/{doc_id}/withdraw",
            "list": "GET /manuscripts",
            "first_search": "POST /search",
            "continue_search": "POST /search/next",
        },
    }
