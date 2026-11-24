# 口述史馆检索 API

FastAPI + SQLite 实现的检索服务：编辑按稿号刊发/撤稿分词后的转写稿，读者用查询词做
BM25 检索并凭游标翻页；撤稿即时生效，续页自动跳过被撤版本；同号重刊只进入重刊后
新开的检索。**检索全程流式处理，单次请求的工作内存只取决于最大单篇稿件的大小、
查询词数与页长，与在刊稿件总数无关**，可在 256 MiB 地址空间内服务数千篇稿件。

## 目录结构

```
app/main.py                # 服务全部实现（FastAPI + sqlite3 标准库）
app/requirements.txt       # 运行依赖
tests/test_api.py          # 关键操作测试（pytest + TestClient）
tests/test_bounded_search.py # 定内存/快照/旧库升级强校验测试
Dockerfile                 # 服务镜像
compose.yaml               # Docker Compose 启动（mem_limit 256m）
demo.sh                    # 端到端演示脚本（撤稿/重刊/不阻塞计分）
demo_memory.sh             # 限内存（256 MiB RLIMIT_AS）4500 篇大语料演示
demo_memory_impl.py        # 上者的 Python 实现
tools/repro_oom.py         # 修复前 500 的最小复现脚本（256MiB + 4500 篇）
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
bash demo_memory.sh                # 256MiB 地址空间 + 4500 篇限内存检索/撤稿/重刊演示
python3 -m pytest tests/ -v        # 自动化测试（15 个用例）
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

- 首查在**单个只读事务**内读取一致快照（WAL 快照，不阻塞刊发/撤稿）：版本高水位
  `snapshot_seq`、语料统计 N/avgdl/各词 df、命中总数、逐篇计分全部基于同一份
  已提交的在刊数据，计分期间并发的刊发/撤稿不会串进本次结果。BM25 取 Okapi
  （k1=1.2，b=0.75，idf 取 `ln(1+(N-df+0.5)/(df+0.5))` 保证为正）。命中至少一个
  查询词的稿件按 **分数降序、同分按稿号升序**返回。
- 服务端为每个检索只登记**查询词、页长、语料统计（N、avgdl、各词 df）**
  （`searches` 表，O(查询词数)），**不保存整张榜单**；翻页位置
  `(上一页末条分数, 稿号)` 放在客户端持有的不透明游标里（base64 JSON）。
- **定内存计分**（修复旧版 500 的核心）：旧实现把全部在刊稿的词频读进列表再整体
  排序，4500 篇 × 512 词语料在 256 MiB 地址空间下首查/续页均 MemoryError（即 500）。
  现改为：
  - 新增语料级倒排表 `postings(term, version, tf)`（`WITHOUT ROWID`，主键
    `(term, version)`，另建 `version` 索引），刊发时与稿件同一短事务写入；
  - N、avgdl、df、命中总数全部由 **SQL 聚合**得到，不读取稿件正文；
  - 候选稿件经倒排表 EXISTS 过滤后由游标**逐行流式产出**，每篇只解析自己的 tf
    （O(单篇大小)），计分后随即释放，进程内任何时刻至多持有一篇稿件的词频；
  - 排序只维护一个容量为页长的**有界最小堆**（堆顶为当前最差命中），
    最终按 `(分数降序, 稿号升序)` 输出一页；
  - 因而请求工作内存为 **O(最大单篇 tf 大小 + 查询词数 + 页长)**，
    与在刊稿件总数无关。
- 续页时取出冻结的语料统计，对 `version <= snapshot_seq` 且**当前仍在刊**的候选
  流式重算——计分公式与词序固定，相同输入浮点结果逐位一致，分数与顺序与首查相同；
  再用键集条件（分数 > 锚点跳过；同分稿号 ≤ 锚点跳过）取游标之后的 `size` 条。
  被撤版本在候选过滤时被跳过，后续命中自然上移补足当页；重刊版本
  `version > snapshot_seq`，不会进入旧检索，只出现在重刊后新开的检索中。
  `remaining_hits` 在同一事务内逐篇计数，精确反映游标之后当前仍有效的命中数。
- 游标可重复使用（幂等），续页可临时指定不同页长。
- **升级兼容**：升级前已存在的库没有 `postings` 表，启动时自动建表并按行流式
  回填历史版本（不整体载入语料）；`searches` 表结构与游标编码未变，
  升级前签发的游标可继续沿首查的分数和次序翻页。

### 撤稿不阻塞计分

- SQLite 开 **WAL**：读（计分）与写（撤稿）互不等待；每请求独立连接，写事务
  只做一条 UPDATE/INSERT（刊发为稿件 + 倒排同事务），毫秒级释放写锁；
  应用层没有任何全局锁。
- 同步视图函数由 Starlette 线程池并发执行（池上限收紧到 8，见下），
  慢速计分请求不占着事件循环。
- `/search` 的 `debug_delay_seconds` 是**测试钩子**（默认 0），人为拉长计分耗时，
  用于演示/验证撤稿不必等计分结束（见 `demo.sh` 第 9 步与
  `tests/test_api.py::test_withdraw_does_not_wait_for_scoring`）。

### 256 MiB 地址空间适配

题目给定进程地址空间 256 MiB。除检索算法的定内存改造外，还有两处运行时适配：

- **glibc malloc arena**：默认按 CPU 数创建 arena，每个 arena 预留大块虚拟地址，
  uvicorn 启动后 VmSize 即约 234 MiB，余量不足 16 MiB，库文件一大就会在
  `sqlite3.connect` 处报 `unable to open database file`。镜像内置
  `MALLOC_ARENA_MAX=2`，启动 VmSize 降到约 175 MiB，留出约 80 MiB 余量。
- **工作线程栈**：AnyIO 默认线程池上限 40，每线程预留 8 MiB 栈。应用在
  lifespan 中把默认 limiter 收紧到 8（`MAX_WORKER_THREADS`），避免线程栈
  预留把虚拟内存吃光。
- WAL 是库级持久属性，只在初始化时 `PRAGMA journal_mode=WAL` 一次，
  每连接不再重复切换，减少受限地址空间下的分配器往返。

## 实测结果（交付前验证）

- `python3 -m pytest tests/ -q`：**15 passed**（原 4 个端到端测试 + 新增 11 个
  定内存/快照/升级测试，见 `tests/test_bounded_search.py`）：
  - 与独立参考 BM25 实现逐篇比对分数、df、N、avgdl，全榜顺序
    （分数降序、同分稿号升序）完全一致；
  - 页长 3 逐页翻完拼回的榜单与全量榜单逐元素相同；
  - 撤 3 篇后续页跳过被撤版本并补满，`remaining_hits` 与参考计数精确相等；
  - 同号重刊版本不进旧游标、新检索可见且带新版本号；
  - 并发刊发/撤稿下慢检索的统计、分数、`total_hits` 同属首查时刻快照；
  - tracemalloc 验证语料 400→2000 篇（5 倍）时检索峰值 Python 内存不随语料
    线性增长，且绝对值 < 64 MiB；页长 100 的堆占用同样有界；
  - 模拟升级前旧库（无 postings）启动自动回填；并手工构造升级前签发的
    searches 行与旧格式游标，验证升级后仍沿原分数/次序翻页、跳过期间撤稿。
- **256 MiB 限内存复现与演示**（`demo_memory.sh`，`RLIMIT_AS=256MiB`，
  4500 篇 × 512 词、每篇 ≥510 互异词 + 两个常见词）：
  - 修复前：首查直接 `MemoryError`（经 ASGI 表现为 500），已用
    `tools/repro_oom.py` 复现；
  - 数据准备阶段直调刊发端点写入 4500 篇（省去数千次 HTTP 往返，刊发逻辑与
    线上完全一致）；**检索、撤稿、同号重刊、游标续页全部走真实 HTTP API**；
  - 修复后：首查 HTTP 200、`total_hits=4500`，前 30 页（size=10）与独立
    size=100 参照榜前 300 条逐元素一致、分数非增、`remaining_hits` 精确递减；
  - 撤 3 篇（位于旧游标之后）+ 同号重刊 1 篇后，用变更前签发的旧游标续页：
    被撤/重刊版本均不出现、中间页补满 100 条，游标后精确取回
    `4500-100-3=4397` 条；新开检索可见重刊版本（version=4501），
    `total_hits=4498`；
  - 干净环境下进程 VmSize 稳定约 172 MiB、RSS 约 53 MiB，地址空间余量约
    84 MiB，全程无 OOM。
- `./demo.sh` 对本地 uvicorn 实例的小语料全流程仍然跑通（撤稿/重刊/慢计分
  不阻塞）。
- 边界：无命中查询词返回空页、空查询 422、撤不存在稿号 404、坏游标 400、
  未知游标 404。

## 剩余事项

- 本环境没有 Docker，`Dockerfile`/`compose.yaml` 未实际构建过；镜像内的
  `MALLOC_ARENA_MAX`、CMD 与本地验证过的启动方式一致，建议在有 Docker 的
  环境跑一次 `docker compose up --build`，并用 `docker stats` 复核 256m 限额。
- 续页采用"冻结统计 + 流式重算"，单次续页仍需扫描首查快照内的候选版本，
  时间复杂度 O(首查快照命中数)；内存已与语料规模脱钩，进一步提速可考虑
  按词项维护更紧凑的整型倒排（当前 postings 已可用覆盖索引避免回表）。
- `searches` 表随首查数量单调增长，需要加过期清理（如按 `created_at` 定期删除，
  过期游标返回 404 的逻辑已就位）。
- 稿号按字典序排序，建议馆方使用等宽编号（如 `MS-001`）；如需数值序可改存整数列。
- 未做认证鉴权与限流；`debug_delay_seconds` 测试钩子上生产前建议移除或加开关。
- 分数一致性依赖同一服务版本内浮点重算的确定性；滚动升级计分公式期间，旧游标
  的分数可能与新公式不一致（可通过在游标中记录公式版本号来防御）。
- 旧库回填 postings 已按每批 200 个版本分批提交（不整体载入语料）；若历史库
  极大（百万级版本），启动回填仍会占用一段时间，可进一步改为后台迁移并记录
  迁移水位，避免阻塞启动。
