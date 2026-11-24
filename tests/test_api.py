"""关键操作测试：刊发/撤稿/重刊、首查与续页、快照语义、撤稿不阻塞计分。"""

import base64
import json
import os
import sys
import tempfile
import threading
import time

import pytest

_fd, _DB = tempfile.mkstemp(suffix=".sqlite3")
os.close(_fd)
os.unlink(_DB)  # 交给应用重建
os.environ["DB_PATH"] = _DB
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """每个测试一个独立的 SQLite 库。"""
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "test.sqlite3"))
    main._init_db()
    yield


# 语料：7 篇分词转写稿。查询词 ["抗战", "北平"] 时的预期榜序见 test_walkthrough。
DOCS = {
    "MS-001": ["抗战", "北平", "胡同", "童年", "粮票"],
    "MS-002": ["抗战", "上海", "弄堂", "工厂", "夜校"],
    "MS-003": ["北平", "茶馆", "曲艺", "抗战", "胜利", "广播"],
    "MS-004": ["知青", "下乡", "北大荒", "麦田"],
    "MS-005": ["抗战", "西南联大", "昆明", "北平", "师生"],
    "MS-006": ["北平", "城墙", "骆驼", "运输队"],
    "MS-007": ["抗战", "伤兵", "医院", "护士"],
}
TERMS = ["抗战", "北平"]


def _publish(client, doc_id, tokens):
    return client.post("/manuscripts", json={"doc_id": doc_id, "tokens": tokens})


def test_publish_withdraw_republish(client, fresh_db):
    """在刊稿号拒绝重复刊发；撤稿后可同号重刊，版本号递增。"""
    r = _publish(client, "T-100", ["抗战", "重庆", "防空洞"])
    assert r.status_code == 201, r.text
    v1 = r.json()["version"]

    r = _publish(client, "T-100", ["抗战", "重庆"])
    assert r.status_code == 409  # 在刊稿号重复刊发被拒

    r = client.post("/manuscripts/T-100/withdraw")
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"

    r = client.post("/manuscripts/T-100/withdraw")
    assert r.status_code == 404  # 已撤稿，无在刊版本

    r = _publish(client, "T-100", ["抗战", "重庆", "重逢"])
    assert r.status_code == 201  # 撤稿后可同号重刊
    assert r.json()["version"] > v1  # 新版本序号

    r = client.post("/manuscripts/NO-SUCH/withdraw")
    assert r.status_code == 404


def test_walkthrough(client, fresh_db):
    """首查→撤稿→续页跳过并补足→重刊→旧游标不可见、新检索可见。"""
    for doc_id, tokens in DOCS.items():
        r = _publish(client, doc_id, tokens)
        assert r.status_code == 201, r.text

    # 重复刊发在刊稿号被拒
    assert _publish(client, "MS-001", ["抗战"]).status_code == 409

    # 全量探针：记录首查时刻的完整榜序与分数
    probe = client.post("/search", json={"terms": TERMS, "size": 100}).json()
    assert probe["next_cursor"] is None  # 5 篇命中 <= 页长，无续页
    order = [h["doc_id"] for h in probe["hits"]]
    assert order == ["MS-001", "MS-005", "MS-003", "MS-006", "MS-007", "MS-002"]
    # MS-001 与 MS-005 同分，按稿号升序
    assert probe["hits"][0]["score"] == probe["hits"][1]["score"]
    # 命中至少一个查询词：MS-004 不含查询词，不在榜上
    assert "MS-004" not in order
    scores = {h["doc_id"]: h["score"] for h in probe["hits"]}

    # query 字符串形式与 terms 等价
    probe2 = client.post("/search", json={"query": "抗战 北平", "size": 100}).json()
    assert [h["doc_id"] for h in probe2["hits"]] == order

    # 首查：页长 2
    first = client.post("/search", json={"terms": TERMS, "size": 2}).json()
    assert [h["doc_id"] for h in first["hits"]] == ["MS-001", "MS-005"]
    assert first["hits"][0]["score"] == scores["MS-001"]
    assert first["next_cursor"]

    # 首查与续页之间：馆方撤掉 MS-003（原本第 3 名）
    r = client.post("/manuscripts/MS-003/withdraw")
    assert r.status_code == 200

    # 续页：跳过已撤的 MS-003，从后续命中补足当页
    page2 = client.post("/search/next", json={"cursor": first["next_cursor"]}).json()
    assert [h["doc_id"] for h in page2["hits"]] == ["MS-006", "MS-007"]
    # 沿用首查的分数（与探针逐位一致）
    assert page2["hits"][0]["score"] == scores["MS-006"]
    assert page2["hits"][1]["score"] == scores["MS-007"]
    assert page2["next_cursor"]

    # 同号重刊 MS-003：新版本序号
    r = _publish(client, "MS-003", ["北平", "抗战", "重逢", "口述", "整理"])
    assert r.status_code == 201
    new_version = r.json()["version"]
    assert new_version > 7

    # 旧游标继续翻页：重刊版本不进入首查前已开的检索
    page3 = client.post("/search/next", json={"cursor": page2["next_cursor"]}).json()
    assert [h["doc_id"] for h in page3["hits"]] == ["MS-002"]
    assert "MS-003" not in [h["doc_id"] for h in page3["hits"]]
    assert page3["next_cursor"] is None

    # 重刊后新开的检索：能看到新版本
    fresh = client.post("/search", json={"terms": TERMS, "size": 100}).json()
    by_id = {h["doc_id"]: h for h in fresh["hits"]}
    assert by_id["MS-003"]["version"] == new_version


def test_continue_size_override_and_cursor_reuse(client, fresh_db):
    """续页可临时改页长；游标可重复使用（幂等）。"""
    for i in range(1, 5):
        _publish(client, f"S-{i}", ["抗战", f"词{i}"])
    first = client.post("/search", json={"terms": ["抗战"], "size": 2}).json()
    assert len(first["hits"]) == 2
    # 同一游标用两次，结果一致
    p2a = client.post("/search/next", json={"cursor": first["next_cursor"], "size": 1}).json()
    p2b = client.post("/search/next", json={"cursor": first["next_cursor"], "size": 1}).json()
    assert p2a["hits"] == p2b["hits"] and len(p2a["hits"]) == 1
    # 坏游标 / 未知游标
    assert client.post("/search/next", json={"cursor": "not-a-cursor"}).status_code == 400
    ghost = base64.urlsafe_b64encode(
        json.dumps({"sid": "no-such-search", "after": [1.0, "A"]}).encode()
    ).decode()
    assert client.post("/search/next", json={"cursor": ghost}).status_code == 404


def test_withdraw_does_not_wait_for_scoring(client, fresh_db):
    """撤稿提交不必等正在计分的请求结束；计分请求仍按首查时刻快照返回。"""
    _publish(client, "NB-200", ["抗战", "南京", "渡口"])
    _publish(client, "NB-201", ["抗战", "武汉", "轮渡"])

    started = threading.Event()
    outcome = {}

    def slow_search():
        started.set()
        r = client.post("/search", json={
            "terms": ["抗战"], "size": 100, "debug_delay_seconds": 3.0,
        })
        outcome["search"] = r

    t = threading.Thread(target=slow_search)
    t.start()
    assert started.wait(timeout=5)
    time.sleep(0.3)  # 确认对方进入计分阶段

    t0 = time.monotonic()
    r = client.post("/manuscripts/NB-200/withdraw")
    elapsed = time.monotonic() - t0
    t.join(timeout=10)

    assert r.status_code == 200
    assert elapsed < 2.0, f"撤稿耗时 {elapsed:.2f}s，被计分请求阻塞"
    assert outcome["search"].status_code == 200
    # 慢检索取的是撤稿前的快照，NB-200 仍在其结果中
    assert "NB-200" in [h["doc_id"] for h in outcome["search"].json()["hits"]]
