"""demo_memory.sh 的实现：256MiB 地址空间下限内存分页检索全流程。

用 RLIMIT_AS 模拟题目给定的 256 MiB 进程地址空间。
- 数据准备（4500 篇刊发）直接调用端点函数，省去数千次 HTTP 往返；
  刊发逻辑与线上完全一致（同一函数、同一事务与 409 校验）。
- 检索、撤稿、同号重刊、游标续页全部走真实 HTTP API。
"""

from __future__ import annotations

import json
import os

# 必须在任何重型原生库导入前设置：限制 glibc malloc arena 数量，
# 避免 256MiB 地址空间被多 arena 预留占满（与 Dockerfile 的 ENV 一致）。
os.environ.setdefault("MALLOC_ARENA_MAX", "2")

import resource
import sys
import tempfile
import threading
import time
import socket
import urllib.error
import urllib.request

LIMIT_MIB = int(os.environ.get("LIMIT_MIB", "256"))
N_DOCS = int(os.environ.get("N_DOCS", "4500"))
SIZE = 10
COMMON = ["抗战", "北平"]

# 1) 先于一切重型导入设置地址空间上限
_bytes = LIMIT_MIB * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (_bytes, _bytes))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app"))

import uvicorn  # noqa: E402

import main  # noqa: E402
from main import PublishRequest  # noqa: E402


def _free_port() -> int:
    """向内核申请一个当前空闲端口（避免与残留实例的 8000 冲突）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = _free_port()
BASE = f"http://127.0.0.1:{PORT}"


def _http(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _rss_mib() -> float:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return float("nan")


def _vmsize_mib() -> float:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmSize:"):
                return int(line.split()[1]) / 1024
    return float("nan")


def _hr(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def main_run() -> None:
    workdir = tempfile.mkdtemp(prefix="oral-history-demo-")
    main.DB_PATH = os.path.join(workdir, "oral_history.db")
    main._init_db()

    _hr(f"1) 数据准备：直调刊发端点写入 {N_DOCS} 篇（每篇 510 互异词 + {COMMON}）")
    t0 = time.monotonic()
    for i in range(N_DOCS):
        tokens = [f"u{i:05d}_{k}" for k in range(510)] + COMMON
        main.publish_manuscript(
            PublishRequest(doc_id=f"MS-{i:05d}", tokens=tokens))
    print(f"   刊发完成，用时 {time.monotonic() - t0:.1f}s；"
          f"VmSize {_vmsize_mib():.1f} MiB / RSS {_rss_mib():.1f} MiB"
          f"（地址空间硬上限 {LIMIT_MIB} MiB）")

    # 起真实 HTTP 服务（绑定内核分配的空闲端口），后续操作全部走 API
    # 数据准备产生大量短命分配，先归还 glibc 空闲堆再启动服务
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass
    config = uvicorn.Config(main.app, host="127.0.0.1", port=PORT,
                            log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            urllib.request.urlopen(BASE + "/healthz", timeout=2)
            break
        except Exception:
            time.sleep(0.1)
    print(f"   HTTP 服务已在 {BASE} 就绪")

    _hr("2) 首查两个常见词（旧实现此处全量载入 4500 篇词频 -> MemoryError/500）")
    t0 = time.monotonic()
    code, first = _http("POST", "/search", {"terms": COMMON, "size": SIZE})
    print(f"   HTTP {code}，用时 {time.monotonic() - t0:.2f}s，"
          f"VmSize {_vmsize_mib():.1f} / RSS {_rss_mib():.1f} MiB")
    assert code == 200, first
    print(f"   total_hits = {first['total_hits']}（应为 {N_DOCS}），"
          f"首页 {len(first['hits'])} 条，snapshot_seq = {first['snapshot_seq']}")
    assert first["total_hits"] == N_DOCS and len(first["hits"]) == SIZE
    assert first["next_cursor"]

    # 独立参照榜：另起一条 size=100 的游标链取前 300 条，
    # 与下面 size=10 的主链是两条独立游标，交叉验证不同页长下的顺序一致性。
    _, ref0 = _http("POST", "/search", {"terms": COMMON, "size": 100})
    assert ref0["total_hits"] == N_DOCS and len(ref0["hits"]) == 100
    ref_order = [h["doc_id"] for h in ref0["hits"]]
    ref_cur = ref0["next_cursor"]
    for _ in range(2):  # 再取 200 条，凑齐前 300
        _, rp = _http("POST", "/search/next", {"cursor": ref_cur})
        assert len(rp["hits"]) == 100
        ref_order.extend(h["doc_id"] for h in rp["hits"])
        ref_cur = rp["next_cursor"]
    assert len(ref_order) == 300

    _hr(f"3) 续页 {SIZE} 条/页：翻前 30 页，与独立参照榜逐元素对照，剩余数精确递减")
    cursor = first["next_cursor"]
    collected = [h["doc_id"] for h in first["hits"]]
    scores = [h["score"] for h in first["hits"]]
    # remaining_hits 语义：本次锚点之后、含本页的命中总数。
    # 首个续页锚点在首页 SIZE 条之后 -> N-SIZE；每翻一页锚点后移一页。
    expected_remaining = N_DOCS - SIZE
    for page_no in range(2, 31):
        code, page = _http("POST", "/search/next", {"cursor": cursor})
        assert code == 200, page
        assert len(page["hits"]) == SIZE
        assert page["remaining_hits"] == expected_remaining
        collected.extend(h["doc_id"] for h in page["hits"])
        scores.extend(h["score"] for h in page["hits"])
        expected_remaining -= len(page["hits"])
        cursor = page["next_cursor"]
    assert collected == ref_order[: len(collected)]
    assert all(a >= b for a, b in zip(scores, scores[1:]))
    print(f"   前 30 页共 {len(collected)} 条，与独立 size=100 参照榜前 "
          f"{len(collected)} 条完全一致，分数非增，remaining_hits 精确；"
          f"RSS {_rss_mib():.1f} MiB")

    _hr("4) 撤稿 3 篇 + 同号重刊 1 篇，再用变更前签发的旧游标（页长 100）续页")
    # 变更前签发游标（模拟读者手里升级前/变更前的游标）；首页先交付 100 条
    code, before = _http("POST", "/search", {"terms": COMMON, "size": 100})
    assert code == 200 and before["next_cursor"]
    old_cursor = before["next_cursor"]
    first_page_n = len(before["hits"])  # 100

    # 选游标“之后”的稿（参照榜 150..152），这样续页必须真实跳过它们并补满
    withdrawn = ref_order[150:153]
    for d in withdrawn:
        code, body = _http("POST", f"/manuscripts/{d}/withdraw")
        assert code == 200, body
    print(f"   已撤稿：{withdrawn}（位于旧游标之后）")
    republish_id = withdrawn[0]
    code, rep = _http("POST", "/manuscripts",
                      {"doc_id": republish_id,
                       "tokens": ["抗战", "北平", "重刊", "新内容"] * 128})
    assert code == 201, rep
    republish_version = rep["version"]
    print(f"   {republish_id} 同号重刊 -> version={republish_version}")

    # 游标之后原本有 N-100 条；撤的 3 篇都在游标之后 -> 续页有效命中 = N-100-3。
    # 重刊版 version 高于首查快照，不进入旧检索。
    after_cursor_live = N_DOCS - first_page_n - len(withdrawn)
    seen: list[str] = []
    page_no = 0
    cur = old_cursor
    # 首个续页锚点在首页之后，remaining_hits = 游标后全部有效命中（含本页）；
    # 之后每页锚点后移一页，remaining_hits 减去上一页条数。
    expected_remaining = after_cursor_live
    while cur:
        code, page = _http("POST", "/search/next", {"cursor": cur})
        assert code == 200, page
        page_no += 1
        ids = [h["doc_id"] for h in page["hits"]]
        assert not set(withdrawn).intersection(ids), "被撤/重刊版本泄漏进旧游标页"
        assert page["remaining_hits"] == expected_remaining
        seen.extend(ids)
        expected_remaining -= len(ids)
        cur = page["next_cursor"]
        if cur:
            # 还有下一页：本页必然补满 100 条
            assert len(ids) == 100
    # 末页 remaining_hits 等于末页条数（锚点之后只剩这些），最终递推归零
    assert expected_remaining == 0
    assert len(seen) == after_cursor_live and len(set(seen)) == after_cursor_live
    print(f"   旧游标翻 {page_no} 页，游标后取回 {len(seen)} 条"
          f"（= {N_DOCS}-{first_page_n}-{len(withdrawn)}），")
    print(f"   被撤 {withdrawn} 与重刊 version={republish_version} 均未出现，"
          f"中间页补满 100 条，remaining_hits 全程精确")

    _hr("5) 重刊后新开的检索：能看到同号重刊版本，统计反映当前在刊快照")
    code, fresh = _http("POST", "/search", {"terms": COMMON, "size": 100})
    assert code == 200
    hit = next(h for h in fresh["hits"] if h["doc_id"] == republish_id)
    assert hit["version"] == republish_version
    # 撤 3 篇、同号补回 1 篇：在刊命中净减 2
    assert fresh["total_hits"] == N_DOCS - 2
    print(f"   新检索命中 {republish_id} version={hit['version']}，"
          f"total_hits={fresh['total_hits']}（= {N_DOCS} - 3 撤 + 1 重刊）")

    _hr(f"全部断言通过。VmSize {_vmsize_mib():.1f} MiB / RSS {_rss_mib():.1f} MiB"
        f"，远低于地址空间上限 {LIMIT_MIB} MiB")
    server.should_exit = True


if __name__ == "__main__":
    main_run()
