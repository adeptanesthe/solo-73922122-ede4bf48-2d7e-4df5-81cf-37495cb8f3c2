# 口述史馆检索 API

FastAPI + SQLite 实现的检索服务：编辑按稿号刊发/撤稿分词后的转写稿，读者用查询词做
BM25 检索并凭游标翻页；撤稿即时生效，续页自动跳过被撤版本；同号重刊只进入重刊后
新开的检索。

## 目录结构

```
app/main.py            # 服务全部实现（FastAPI + sqlite3 标准库）
app/requirements.txt   # 运行依赖
tests/test_api.py      # 关键操作测试（pytest + TestClient）
Dockerfile             # 服务镜像
compose.yaml           # Docker Compose 启动
demo.sh                # 端到端演示脚本（撤稿/重刊/不阻塞计分）
```

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务监听 http://127.0.0.1:8000 ，SQLite 数据在命名卷 search_data 中
# 重新演示前如需清空数据：docker compose down -v
```

### 本地开发

```bash
pip install -r app/requirements.txt
cd app && uvicorn main:app --host 127.0.0.1 --port 8000
```

### 运行演示与测试

```bash
./demo.sh                          # 对 127.0.0.1:8000 跑完整场景（需 jq）
python3 -m pytest tests/ -v        # 自动化测试
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/manuscripts` | 刊发分词后的转写稿；在刊稿号重复刊发返回 409 |
| POST | `/manuscripts/{doc_id}/withdraw` | 撤稿（立即生效）；无在刊版本返回 404 |
| GET  | `/manuscripts` | 列出全部版本（含已撤稿），便于观察版本演变 |
| POST | `/search` | 首查：BM25 计分，返回第一页与 `next_cursor` |
| POST | `/search/next` | 凭游标续页，可临时改页长 |
| GET  | `/healthz` | 健康检查 |

### 请求示例

```bash
# 刊发
curl -X POST localhost:8000/manuscripts -H 'Content-Type: application/json' \
  -d '{"doc_id":"MS-001","tokens":["抗战","北平","胡同","童年","粮票"]}'

# 首查：查询词 + 页长（terms 数组，或用 "query":"抗战 北平"）
curl -X POST localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"terms":["抗战","北平"],"size":2}'

# 续页
curl -X POST localhost:8000/search/next -H 'Content-Type: application/json' \
  -d '{"cursor":"<上页返回的 next_cursor>"}'

# 撤稿 / 同号重刊
curl -X POST localhost:8000/manuscripts/MS-001/withdraw
curl -X POST localhost:8000/manuscripts -H 'Content-Type: application/json' \
  -d '{"doc_id":"MS-001","tokens":["北平","抗战","重逢","口述","整理"]}'
```

`demo.sh` 串起完整场景：刊发 7 篇 → 重复刊发被拒(409) → 首查(页长 2) →
撤掉第 3 名 → 续页跳过被撤版本并补足当页 → 同号重刊 → 旧游标看不到重刊版、
新检索能看到 → 撤稿不等待慢速计分请求。

## 关键设计

### 版本与刊发语义

- `manuscripts.version` 为自增版本序号，单调递增，作为全局"时间戳"。
- 部分唯一索引 `UNIQUE(doc_id) WHERE status='published'` 在数据库层保证同一稿号
  任一时刻至多一个在刊版本：在刊时重复刊发被 409 拒绝；撤稿后同号重刊插入新行、
  获得新版本号。

### 首查与游标

- 首查在**单个读事务**内读取一致快照：当前全部在刊稿 + 版本高水位
  `snapshot_seq`，据此计算 BM25（Okapi，k1=1.2，b=0.75，idf 取
  `ln(1+(N-df+0.5)/(df+0.5))` 保证为正）。命中至少一个查询词的稿件按
  **分数降序、同分按稿号升序**返回。
- 服务端为每个检索只登记**查询词、页长、语料统计（N、avgdl、各词 df）**
  （`searches` 表，O(查询词数)），**不保存整张榜单**；翻页位置
  `(上一页末条分数, 稿号)` 放在客户端持有的不透明游标里（base64 JSON）。
- 续页时取出冻结的语料统计，对 `version <= snapshot_seq` 且**当前仍在刊**的版本
  重算分数——相同输入下浮点结果逐位一致，因此分数与顺序与首查相同；再用键集
  分页取游标之后的 `size` 条。被撤版本在过滤时被跳过，后续命中自然上移补足当页；
  重刊版本 `version > snapshot_seq`，不会进入旧检索，只出现在重刊后新开的检索中。
- 游标可重复使用（幂等），续页可临时指定不同页长。

### 撤稿不阻塞计分

- SQLite 开 **WAL**：读（计分）与写（撤稿）互不等待；每请求独立连接，写事务
  只做一条 UPDATE/INSERT，毫秒级释放写锁；应用层没有任何全局锁。
- 同步视图函数由 Starlette 线程池并发执行，慢速计分请求不占着事件循环。
- `/search` 的 `debug_delay_seconds` 是**测试钩子**（默认 0），人为拉长计分耗时，
  用于演示/验证撤稿不必等计分结束（见 `demo.sh` 第 9 步与
  `tests/test_api.py::test_withdraw_does_not_wait_for_scoring`）。

## 实测结果（交付前验证）

- `python3 -m pytest tests/ -v`：**4 passed**
  - 刊发/重复刊发 409/撤稿/重刊版本递增；
  - 完整走查：榜序与分数符合手算 BM25（MS-001 与 MS-005 同分按稿号升序），
    续页跳过被撤的 MS-003 并由 MS-006/MS-007 补足，续页分数与首查逐位一致，
    重刊版(version=8)不进旧游标、进新检索；
  - 续页改页长、游标幂等复用、坏游标 400、未知游标 404；
  - 并发：3 秒慢速计分期间撤稿 **60ms 内返回**，慢检索仍按其快照返回结果。
- `./demo.sh` 对本地 uvicorn 实例全流程跑通，关键输出：
  - 首查页 1：`MS-001, MS-005`（同分 0.9271）；
  - 撤 MS-003 后续页：`MS-006, MS-007`（被撤版本消失、后续命中补足）；
  - 重刊 MS-003 → `version=8`；旧游标第 3 页仅 `MS-002`；新检索含 `MS-003 v8`；
  - 4 秒慢计分期间撤稿耗时约 60ms。
- 边界抽查：无命中查询词返回空页、空查询 422、撤不存在稿号 404、坏游标 400。

## 剩余事项

- 本环境没有 Docker，`Dockerfile`/`compose.yaml` 未实际构建过；镜像 CMD 与本地
  验证过的启动命令一致（`uvicorn main:app`），建议在有 Docker 的环境跑一次
  `docker compose up --build` 复核。
- 续页采用"冻结统计 + 重算"，单次续页代价 O(在刊稿数)，是"不为游标存榜单"约束下
  的直接实现；稿件量大时可在内存里建语料级倒排索引（仍不按游标存榜单）来加速。
- `searches` 表随首查数量单调增长，需要加过期清理（如按 `created_at` 定期删除，
  过期游标返回 404 的逻辑已就位）。
- 稿号按字典序排序，建议馆方使用等宽编号（如 `MS-001`）；如需数值序可改存整数列。
- 未做认证鉴权与限流；`debug_delay_seconds` 测试钩子上生产前建议移除或加开关。
- 分数一致性依赖同一服务版本内浮点重算的确定性；滚动升级计分公式期间，旧游标
  的分数可能与新公式不一致（可通过在游标中记录公式版本号来防御）。
