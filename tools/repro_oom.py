"""复现：256MiB 地址空间下，4500 篇 512 词（>=510 互异）语料首查/续页 500。

用法：python3 tools/repro_oom.py   （可 LIMIT_MIB / 经环境变量覆盖上限）
"""
import json
import os
import resource
import sys
import tempfile

LIMIT_MIB = int(os.environ.get("LIMIT_MIB", "256"))
resource.setrlimit(resource.RLIMIT_AS, (LIMIT_MIB * 1024 * 1024,
                                        LIMIT_MIB * 1024 * 1024))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

fd, db = tempfile.mkstemp(suffix=".sqlite3")
os.close(fd); os.unlink(db)
main.DB_PATH = db
N_DOCS = 4500

with TestClient(main.app) as c:
    print(f"刊发 {N_DOCS} 篇，每篇 512 词（510 互异 + 2 个常见词）...", flush=True)
    common = ["抗战", "北平"]
    for i in range(N_DOCS):
        # 每篇 510 个互异词 + 2 个常见词；常见词在几乎所有篇中出现
        uniq = [f"u{i:04d}_{k}" for k in range(510)]
        tokens = uniq + common
        r = c.post("/manuscripts", json={"doc_id": f"MS-{i:04d}", "tokens": tokens})
        assert r.status_code == 201, r.text
        if i % 500 == 0:
            print(f"  刊发 {i}", flush=True)

    print("首查两个常见词 size=10 ...", flush=True)
    try:
        r = c.post("/search", json={"terms": common, "size": 10})
        print("首查 HTTP", r.status_code)
        if r.status_code != 200:
            print(r.text[:500])
            sys.exit(1)
        data = r.json()
        print("total_hits =", data["total_hits"], "首页条数 =", len(data["hits"]))
        cur = data["next_cursor"]
    except MemoryError:
        print("首查抛 MemoryError"); sys.exit(2)

    print("续页 ...", flush=True)
    try:
        r = c.post("/search/next", json={"cursor": cur})
        print("续页 HTTP", r.status_code)
        if r.status_code != 200:
            print(r.text[:500]); sys.exit(1)
        print("remaining_hits =", r.json()["remaining_hits"])
    except MemoryError:
        print("续页抛 MemoryError"); sys.exit(2)
print("OK")
