"""口述史馆检索 API

- 编辑按稿号刊发分词后的转写稿；在刊稿号拒绝重复刊发，撤稿后可同号重刊。
- 读者首查用当时全部在刊稿计算 BM25，凭游标续页。
- 游标只保存查询与首查时的语料统计（N / avgdl / 各词 df），不保存整张榜单；
  续页时用冻结的统计重算分数，跳过当前已撤稿的版本，从后续命中补足当页。
- 同号重刊得到新的版本序号，只进入重刊后新开的检索。
- SQLite 开 WAL、每请求独立连接、写事务尽量短，撤稿不必等正在计分的请求结束。
"""

from __future__ import annotations

import base64
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("DB_PATH", "oral_history.db")

# Okapi BM25 参数
K1 = 1.2
B = 0.75

MAX_PAGE_SIZE = 100


# ------------------------------------------------------------------ 数据库

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL：读（计分）不阻塞写（撤稿），写也不必等读结束
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = _connect()
    try:
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
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    _init_db()
    yield


app = FastAPI(title="口述史馆检索 API", version="1.0.0", lifespan=lifespan)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ BM25

def _bm25_idf(n_docs: int, df: int) -> float:
    # Robertson idf 变体，+1 保证为正：命中至少一个查询词的文档得分必为正
    return math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))


def _load_published_docs(conn: sqlite3.Connection, max_version: Optional[int] = None):
    """读当前在刊版本；续页时按首查的版本高水位截断，排除重刊/新刊版本。"""
    sql = ("SELECT version, doc_id, tf, length FROM manuscripts "
           "WHERE status = 'published'")
    params: tuple = ()
    if max_version is not None:
        sql += " AND version <= ?"
        params = (max_version,)
    docs = []
    for row in conn.execute(sql, params):
        docs.append((row["doc_id"], row["version"], json.loads(row["tf"]), row["length"]))
    return docs


def _score_hits(docs, terms, dfs, n_docs, avgdl):
    """用（首查冻结的）语料统计对文档计分，返回按 (分数降序, 稿号升序) 排列的命中。

    相同输入下浮点结果逐位一致，因此续页重算的分数与首查相同。
    """
    idfs = {t: _bm25_idf(n_docs, dfs[t]) for t in terms if dfs.get(t, 0) > 0}
    hits = []
    for doc_id, version, tf, dl in docs:
        score = 0.0
        for t in terms:  # 固定词序累加，保证浮点结果确定
            idf = idfs.get(t)
            if idf is None:
                continue
            f = tf.get(t)
            if f:
                score += idf * (f * (K1 + 1.0)) / (f + K1 * (1.0 - B + B * dl / avgdl))
        if score > 0.0:
            hits.append({"doc_id": doc_id, "version": version, "score": score})
    hits.sort(key=lambda h: (-h["score"], h["doc_id"]))
    return hits


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
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=409,
                                detail=f"稿号 {req.doc_id} 已有在刊版本，请先撤稿再重刊")
        version = cur.lastrowid
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
    """首查：用当前全部在刊稿计算 BM25，返回第一页与续页游标。"""
    terms = _resolve_terms(req)

    # 1) 单个读事务内取一致快照：在刊文档 + 版本高水位
    conn = _connect()
    try:
        conn.execute("BEGIN")
        docs = _load_published_docs(conn)
        snapshot_seq = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM manuscripts"
        ).fetchone()["v"]
        conn.execute("COMMIT")
    finally:
        conn.close()

    # 2) 语料统计（冻结进游标，供续页重算）
    n_docs = len(docs)
    avgdl = sum(d[3] for d in docs) / n_docs if n_docs else 0.0
    dfs = {t: sum(1 for d in docs if t in d[2]) for t in terms}

    if req.debug_delay_seconds > 0:
        time.sleep(req.debug_delay_seconds)  # 测试钩子：模拟长耗时计分

    # 3) 计分、排序、取首页
    hits = _score_hits(docs, terms, dfs, n_docs, avgdl)
    page = hits[: req.size]

    # 4) 还有后续命中时登记检索（只存统计，不存榜单），签发游标
    next_cursor = None
    if len(hits) > req.size:
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
        "total_hits": len(hits),
        "hits": page,
        "next_cursor": next_cursor,
    }


@app.post("/search/next")
def continue_search(req: ContinueRequest):
    """续页：沿用首查的分数与顺序，跳过已撤稿版本，从后续命中补足当页。"""
    search_id, after_score, after_doc_id = _decode_cursor(req.cursor)

    conn = _connect()
    try:
        conn.execute("BEGIN")
        srow = conn.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()
        if srow is None:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=404, detail="游标不存在或已过期")
        # 只取首查快照内的版本；本次请求前已撤稿的版本在此被跳过；
        # 重刊版本 version > snapshot_seq，不会进入本次检索
        docs = _load_published_docs(conn, max_version=srow["snapshot_seq"])
        conn.execute("COMMIT")
    finally:
        conn.close()

    terms = json.loads(srow["terms"])
    dfs = json.loads(srow["dfs"])
    size = req.size if req.size is not None else srow["size"]

    # 用首查冻结的统计重算：分数与顺序与首查一致
    hits = _score_hits(docs, terms, dfs, srow["n_docs"], srow["avgdl"])
    remaining = [
        h for h in hits
        if h["score"] < after_score
        or (h["score"] == after_score and h["doc_id"] > after_doc_id)
    ]
    page = remaining[:size]
    next_cursor = (
        _encode_cursor(search_id, page[-1]["score"], page[-1]["doc_id"])
        if len(remaining) > size else None
    )
    return {
        "terms": terms,
        "size": size,
        "remaining_hits": len(remaining),
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
