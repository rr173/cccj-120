# batchfetch — 有预算的批量网页抓取执行器（纯标准库，本地优先）

`sitemapdiscover` 给出候选网址、`robots_admission` 决定准入之后，本模块
负责把一份候选清单变成**真正发出的 HTTP 请求**。它解决的是抓取进程的调度
问题：请求什么时候能开始、跨站跳转之后由谁占预算、取消之后哪些工作还能继续。

明确**不做**：分页采集、断点下载、持久队列、失败自动重试；也不改站点地图
发现与 robots 判定的任何既有规则。全程只用 Python 标准库（`http.client`、
`socket`、`threading`），测试服务只监听 `127.0.0.1`，不依赖真实网站或账号。

```
batchfetch/
  errors.py        异常层次：配置/清单在出网前拒绝；抓取失败不是异常
  clock.py         可注入时钟（MonotonicClock / FakeClock）与等待原语
  config.py        BatchConfig / OriginBudget、清单与配置校验
  exchange.py      单次 HTTP 交换：流式正文、SHA256、字节上限、超时
  models.py        JobResult / HopRecord / TimelineEvent 数据模型
  scheduler.py     BatchRunner：名额、间隔、轮转、跳转、取消、报告
  testsite.py      可控双端口本地 HTTP 站点（闸门/跳转/环/断流/超长正文）
  testsite_cli.py  把测试站点作为独立进程启动
  __main__.py      命令行：python -m batchfetch run ...
```

---

## 1. 输入与地址规则

### 1.1 清单（UTF-8 JSONL）

每行一个 JSON 对象，且**只能**有两个字段：

```json
{"job_id": "job-001", "url": "http://example.test/path?q=1"}
```

* `job_id` 必须是非空字符串、不含控制字符，且全清单唯一；
* `url` 必须是绝对 http(s) URL，沿用仓库统一 URL 规则
  （`sitemapdiscover.urls.parse_url`）：scheme/主机/默认端口规范化，
  主机 IDNA、IPv6 压缩；**路径与 query 逐字节保留，不会为排队被改写**；
* 清单顺序就是接收顺序，报告中的终结果严格按该顺序写出；
* 空行、非 JSON、字段缺失/多余、重复 `job_id`、非法 URL 都在**打开任何
  套接字之前**拒绝（`ManifestError` / CLI 退出码 2）。

### 1.2 origin 与预算隔离

origin 为规范化后的 `scheme://host[:port]`。**http 与 https 各占预算，
不同端口也各占预算**（`http://h:80`、`https://h:443`、`http://h:8080`
是三个 origin）。

---

## 2. 配置

| 键 / 参数 | 含义 | 约束 |
|---|---|---|
| `global_inflight` / `--global-inflight` | 全局同时在途交换上限 | 正整数 |
| `origin_inflight` / `--origin-inflight` | 默认每 origin 在途上限 | 正整数 |
| `origin_min_interval` / `--origin-min-interval` | 同一 origin 相邻两次**实际开始**时刻的最小间隔（秒） | ≥ 0 的数 |
| `request_timeout_seconds` / `--request-timeout` | 单次交换总超时（秒） | > 0 |
| `max_body_bytes` / `--max-body-bytes` | 单个响应体字节上限 | ≥ 1 |
| `max_redirects` / `--max-redirects` | 每个作业最多跳转次数 | ≥ 0 整数 |
| `origin_overrides` / `--origin-budget` | 针对具体 origin 的预算覆盖 | origin 必须是合法 origin |

`--origin-budget` 形如 `scheme://host[:port]:cap[:interval]`，可重复，例如
`--origin-budget http://127.0.0.1:8081:1:0.5`。也可以用 `--config config.json`
给出等价 JSON，命令行标志覆盖文件值。任何非正并发、非数超时、负数限额等都
在出网前抛 `BatchConfigError`（CLI 退出码 2）。

Python 形式：

```python
from batchfetch import BatchConfig, OriginBudget

config = BatchConfig(
    global_inflight=4,
    default_origin=OriginBudget(inflight_cap=2, min_interval_seconds=0.25),
    origin_overrides={"http://slow.test": OriginBudget(1, 1.0)},
    request_timeout_seconds=10.0,
    max_body_bytes=4 * 1024 * 1024,
    max_redirects=5,
).validated()
```

---

## 3. 调度语义（请求何时能开始）

* 一个**交换**从请求头发出前开始同时占用**全局名额**与**目标 origin 名额**，
  直到响应体读完、字节上限触发、流被截断或本次交换失败才释放。
* 间隔从**真正开始请求的单调时钟时刻**计算：不是清单入队时刻，也不会因为
  前一个请求提前结束而缩短。
* 调度器在当前有资格开始的各 origin 之间**轮转（round-robin）**派发；每个
  origin 内部是严格 FIFO，取最早可执行的作业。队头 origin 在等间隔或等连接时，
  调度器继续检查其他 origin，不会卡在队头上。
* 工作线程**不会先占全局名额再睡到站点允许的时间**：只有全局上限、origin
  上限与 origin 间隔同时满足时，作业才被交给工作线程，所有等待都发生在未
  持有任何名额的调度器内。

### 3.1 跳转：同一作业的后续请求，不是库的隐式行为

* 只跟随 301、302、303、307、308；相对 `Location` 用 `urljoin` 解析并再次
  走统一 URL 校验。缺少/非法 `Location`、非 HTTP 目标、超过跳数、回到本作业
  已请求过的地址，都给该作业一个明确的 `failed`（`redirect_error` /
  `redirect_limit` / `redirect_loop`）。
* 每一跳结束后**先释放原 origin 名额**，再把下一跳按真实目标 origin 作为普通
  工作项重新入队：它同样受全局上限、目标站点间隔与同站先到作业顺序约束。
  不能握着旧站点名额等新站点，也不能让跳转插到目标队列里先到作业之前。
* 终结果保留原始 URL、最终 URL、完整跳转链（每跳的 URL/origin/状态码/
  Location/字节数/摘要）与最终状态码。

### 3.2 响应体与失败分类

* 正文按 64KiB 块边读边计算 SHA256 与字节数，**整份正文不驻留内存**，本模块
  也不保存正文；空体有确定摘要（空字符串的 SHA256）。
* 超过 `max_body_bytes`、声明了 `Content-Length` 却提前断流都不是完整响应
  （`body_too_large` / `truncated_body`）。连接在 `finally` 中关闭，名额随即
  让出。
* 4xx/5xx 是**已取得的 HTTP 响应**，作业 `complete` 且保留状态码，不自动重试；
  连接失败、读超时是抓取失败（`connection_error` / `read_timeout` /
  `protocol_error`），其他独立作业继续。

---

## 4. 时间基准与可注入时钟

所有调度判断只经过 `batchfetch.clock.Clock` 抽象：

* 生产环境用 `MonotonicClock`：`now()` 即 `time.monotonic()`，条件变量是真实
  `threading.Condition`，等待真实睡眠到点或被唤醒；
* 单元测试用 `FakeClock`：时间只在测试调用 `advance()` 时前进，因此间隔/节奏
  测试不需要几十秒真实 sleep（调度单测整体在 1 秒内完成）；
* 时间线事件同时带 `mono`（调度时钟秒，互相可比、用于核对间隔）与 `wall`
  （epoch 秒，便于人读）。真实 HTTP 演练仍走真实套接字与真实单调时钟，留下
  真实请求与实际交错。

时间线不是“预计时间”：每个 `exchange_start` / `exchange_end` 都在工作线程
真正发出请求/读完响应的时刻记录，并附带当时的全局与 origin 占用、等待理由
（`global_inflight` / `origin_inflight` / `origin_interval`）与排队耗时，
可与服务端接收记录逐条核对。

---

## 5. 取消边界

* API：`runner.cancel()`；命令行：`SIGINT`（退出码 130）。
* 取消之后**不再启动排队请求或新的跳转**；已经开始的交换允许在既定
  `request_timeout_seconds` 内结束并关闭连接，保留其真实结果。
* 从未启动的作业明确标为 `cancelled`，**不会伪装成超时或 404**（无状态码，
  `failure_kind="cancelled"`）；取消时已完成作业的真实结果原样保留。
* 批次结束时所有工作线程被 join，不存在继续派发的线程；在同一进程新建批次
  不会带上一轮泄漏的名额或后台请求（有专门测试与演练第 5 步覆盖）。

---

## 6. 报告与退出码

报告为 UTF-8 JSONL（重写、不追加），顺序固定：

1. 一行 `summary`：作业总数、`complete` / `failed` / `cancelled` 计数、
   实际交换数、涉及的 origin、取消标志与配置回显；
2. 每个 `job_id` 恰有一行 `result`，**按清单输入顺序**；
3. 全部 `timeline` 事件（`batch_start` / `exchange_start` / `wait` /
   `exchange_end` / `cancelled` / `batch_end`）。

即使批次包含失败或取消，也会写出**所有**作业的结果，不会只输出成功项。

| 退出码 | 含义 |
|---|---|
| 0 | 全部作业 `complete`（含 4xx/5xx 响应） |
| 2 | 清单或配置非法，出网前拒绝，不写报告 |
| 3 | 批次跑完但含抓取失败或取消 |
| 130 | 被 SIGINT 终止（报告仍写出取消后的真实状态） |

---

## 7. Python 与命令行用法

```python
from batchfetch import BatchRunner, BatchConfig, OriginBudget, read_manifest

jobs = read_manifest("jobs.jsonl")
config = BatchConfig(
    global_inflight=4,
    default_origin=OriginBudget(2, 0.25),
    request_timeout_seconds=10.0,
    max_body_bytes=4 * 1024 * 1024,
    max_redirects=5,
).validated()

runner = BatchRunner(jobs, config)
runner.run()
for result in runner.iter_results():      # 输入顺序
    print(result.job_id, result.outcome, result.status,
          result.redirect_chain, result.failure_kind)
for event in runner.timeline:             # 实际交换时间线
    print(event.kind, event.mono, event.origin, event.detail)
runner.write_report("report.jsonl")
runner.close()

# 一次性入口（CLI 内部也用它）
from batchfetch import run_batch
summary = run_batch("jobs.jsonl", config, "report.jsonl")
```

```bash
python3 -m batchfetch run \
    --manifest jobs.jsonl --report report.jsonl \
    --global-inflight 4 --origin-inflight 2 --origin-min-interval 0.25 \
    --request-timeout 10 --max-body-bytes 4194304 --max-redirects 5 \
    [--origin-budget http://slow.test:1:1.0] [--config config.json]
```

可控双端口站点可作为独立进程启动：

```bash
python3 -m batchfetch.testsite_cli --port-a 8081 --port-b 8082 \
    --port-file /tmp/batchfetch-ports.json
```

---

## 8. 复做演练与测试

```bash
python3 -I demo/run_batchfetch_drill.py      # 32 项真实 HTTP 接手演练
PYTHONPATH=. python3 -m unittest \
    tests.test_batchfetch_validation \
    tests.test_batchfetch_scheduler \
    tests.test_batchfetch_redirects \
    tests.test_batchfetch_http \
    tests.test_batchfetch_cli -v             # 49 项回归（纯标准库）
```

演练由服务端同步闸门控制慢响应，独立硬编码的预期顺序/状态清单与服务端
接收收据分开保存，执行器自己的时间线不充当服务端证据。
