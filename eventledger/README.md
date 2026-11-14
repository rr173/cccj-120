# eventledger — 离线爬虫运行事件账本

`eventledger` 把分批导出的爬虫运行事件（本地 UTF-8 JSONL）还原成每个请求
真实经历的尝试过程，并写入一个独立的 SQLite 账本。它**只依赖 Python 标准库**，
不访问网络，也不读取本仓库的加密档案（`archivestore`）或任何 WARC 正文。

## 事件格式

每行一个 JSON 对象，七个公共字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `run_id` | 字符串 | 一次运行；不同 run 的状态互不补齐 |
| `request_id` | 字符串 | 请求标识；同一 id 可在不同 run 独立使用 |
| `attempt` | 正整数 | 尝试序号，从 1 递增 |
| `seq` | 正整数 | 同一请求内严格递增的事件序号 |
| `event_id` | 字符串 | 全局事件身份，用于幂等重放 |
| `event_type` | 字符串 | 六种类型之一 |
| `event_time` | 字符串 | **必须带时区**的 ISO-8601 时刻，原值保留 |

事件类型与各自的附加字段：

- `queued`（无附加字段）
- `started`（无附加字段）
- `response`：`status_code`（100–599）、`response_bytes`（非负整数），
  **只存状态码和字节数，不存响应正文**
- `retry_scheduled`：`next_attempt`，必须等于当前 `attempt + 1`
- `failed`：`error`（字符串）
- `cancelled`：`reason`（字符串）

任何额外字段都会原样保留（账本保存完整原始事件 JSON）。允许跨文件、跨请求
乱序输入；同一请求的事件按 `seq` 重建。

## 状态机规则

- 每个请求的首个事件必须是 `attempt=1` 的 `queued`。
- 每个 attempt 以 `started` 开始；下一次 `started` 才算重试真正开始
  （`retry_scheduled` 只表示安排了重试）。
- `response` 后若同 attempt 跟随 `retry_scheduled`，则该响应不是终态；
  否则该响应即终态（2xx/3xx 记成功，4xx/5xx 且未再重试记失败）。
- 每个请求最多一个终态事件（终态 `response`、`failed`、`cancelled`）。
  终态之后再追加 `started`/`response`/`retry_scheduled`，或在
  `failed`/`cancelled` 之后追加任何事件，都会拒绝整批。
- 重复 `seq`、缺少前置事件、跳过 attempt、非法转换都会报错，并指出
  `run_id`、`request_id`、1 基行号和原因。
- 没有终态事件的请求显示为 `queued` / `running` / `waiting_retry`，
  **不会因为文件结束而被判失败**，也不会被编造耗时。

## 导入的原子性与幂等性

一个 JSONL 文件 = 一个批次 = 一个 SQLite 事务：

1. 文件逐行流式读取与结构校验，合规行即时写入 TEMP 暂存表（`temp_store=FILE`，
   不落进程内存）；任一行结构非法只记录问题，不中断收集。
2. 整文件读完后，用集合化 SQL 校验 event_id 身份、跨批 seq，并对每个受影响
   请求把“已提交 + 本批新增”事件按 `seq` 合并重放状态机。
3. 全部通过才用一条 `INSERT ... SELECT` 合并并 `COMMIT`；任一校验失败或提交前
   进程被强退，整批回滚，账本里不会留下该批的半条记录或半个请求状态。

幂等规则：

- 相同 `event_id` 且内容逐字段一致 → 重放，不新增记录（批内、跨批都一样）。
- 相同 `event_id` 但内容不同 → 抛 `EventConflictError`，拒绝整批。
- 修正输入或重启后可安全重新导入。

## Python API

```python
from eventledger import Ledger, ValidationError, EventConflictError

with Ledger("ledger.db") as ledger:
    result = ledger.import_file("batch-2026-11-14.jsonl")
    print(result.events_inserted, result.events_replayed)

    timeline = ledger.request_timeline("run-1", "req-7")   # 按 attempt、seq 排序
    print(timeline.outcome, timeline.duration_seconds, timeline.events)

    summary = ledger.run_summary("run-1")                  # 一次 run 的汇总
    print(summary.success, summary.failed, summary.cancelled,
          summary.waiting_retry, summary.running, summary.queued,
          summary.success_rate, summary.durations_seconds)

    ledger.export_jsonl("export.jsonl", run_id="run-1")    # 稳定、可复跑导出
```

`outcome` 取值：`success` / `failed` / `cancelled` / `waiting_retry` /
`running` / `queued`。

- **成功率分母**只包含有最终成功或失败结论的请求（`decided`）；取消和尚未
  终结的请求不进分母，没有可判定请求时 `success_rate` 为 `None`。
- **耗时**从首次 `started` 到终态 `response` 或 `failed`，按带时区的真实时刻
  计算；没有终态的请求 `duration_seconds` 为 `None`。
- 导出按 `(run_id, request_id, seq)` 稳定排序、对象键排序输出；重新导入导出
  文件不会增加事件，重复导出字节一致。

## 命令行

```bash
python -m eventledger --ledger ledger.db import a.jsonl b.jsonl
python -m eventledger --ledger ledger.db runs
python -m eventledger --ledger ledger.db timeline --run run-1 --request req-7
python -m eventledger --ledger ledger.db summary --run run-1
python -m eventledger --ledger ledger.db export --out export.jsonl [--run run-1]
python -m eventledger --ledger ledger.db stats [--run run-1]
```

成功时 stdout 打印一行 JSON；批次被拒绝时 stderr 打印带 `problems` 数组
（每项含 `run_id`/`request_id`/`event_id`/`line`/`reason`）的 JSON 并以
状态码 1 退出，账本保持不变。

## 可重复演练（无需外网）

```bash
# 单元测试（标准库 unittest）
python3 -I tests/test_eventledger.py

# 端到端演练，覆盖全部正常/异常场景并打印大文件内存数据
python3 -I demo/run_eventledger_drill.py
```

演练覆盖：拆分多文件乱序导入（等待重试→运行→成功）、批次重放统计不变、
未终结请求、HTTP 500 后重试成功、取消、时区偏移耗时、不同 run 的同名
request_id、稳定导出与重新导入、重复 seq、跳过 attempt、终态后追加、
同 event_id 不同内容、末行截断、提交前 SIGKILL 强退后重启重导。

### 大文件内存特性（演练第 11 节实测）

以 120,000 个事件（20,000 个请求 × 6 事件，约 20.3 MiB JSONL）为例：

- 导入约 4–5 秒（约 2.5–2.7 万事件/秒）；
- 导入期间 RSS 实际增长约 **5 MiB**（约为输入文件的 0.23–0.25 倍），
  Python 堆峰值仅几十 KiB 量级；
- 暂存表通过 `PRAGMA temp_store=FILE` 落盘，状态机用单条有序游标按请求分组
  流式重放，**整个文件不会被一次性读入内存**；
- 同一大文件重放为纯幂等操作，插入 0 行、计数不变。

## 数据库结构

单文件 SQLite，主表 `events` 以 `(run_id, request_id, seq)` 为主键、
`event_id` 唯一索引；`payload` 列保存完整原始事件 JSON，`event_time_us`
保存换算后的 epoch 微秒用于真实时刻计算，`event_time` 保留原始带时区字符串。
