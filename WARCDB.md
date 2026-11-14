# warcdb — 离线采集事实库

采集组把多轮网页抓取的 WARC 原件收进 `archivestore` 加密档案库之后，分析
人员需要**按网址与抓取时刻**拿到那一次的真实正文，并且知道正文究竟来自
哪份原件的哪一段字节。warcdb 在现有加密档案库之上提供这个能力：

- 输入是**已登记的 archive_id**；加密收件（seal）、范围解密
  （read-range）和换钥（rotate）全部继续使用 archivestore 现有 API，
  本模块不另起加密逻辑；
- 解析一份档案里的全部 WARC 记录（response 与 revisit），把每次抓取的
  事实写进一个**独立的 SQLite 文件**（事实库）：身份、网址、时间、
  HTTP 状态和头、引用边、解析状态、正文来源 archive_id 与正文在原
  WARC 内的准确字节区间；
- **正文不复制进事实库**，事实库也**不保存 KEK 或 DEK**；换 KEK 之后
  只持新密钥的新进程照常查询与导出（演练 step 9 实测）。

```
warcdb/warc.py       流式 WARC/1.1 + HTTP 解析器（边界只认长度，不回扫）
warcdb/stream.py     档案明文流（逐块解密认证，不落永久明文文件）
warcdb/db.py         SQLite 事实库（单事务导入、解析状态机、查询）
warcdb/core.py       FactStore：import_archive / export_body
warcdb/fixtures.py   小型夹具生成器（离线造 WARC，不依赖互联网）
warcdb/__main__.py   CLI
demo/run_warc_demo.py  端到端演练（worker 子进程 = 真实新进程）
tests/test_warcdb.py   回归测试
```

依赖：Python ≥ 3.10、`cryptography`、标准库 `sqlite3`（与 archivestore 相同）。

---

## 1. WARC 输入约定（严格）

输入为**未压缩的 WARC/1.1 字节流**，同一档案可连续放多条记录：

```
record = "WARC/1.1" CRLF
         *(field-name ":" value CRLF)
         CRLF
         content-block          ← 恰好 Content-Length 字节
         CRLF CRLF              ← 记录分隔符
```

- 记录边界**只**由 Content-Length 与分隔符决定。解析器**绝不**靠搜索
  下一个 `WARC/1.1` 字符串跳过坏内容：版本行不符、分隔符缺失、流尾
  截短都直接报错并给出偏移。正文中出现零字节、CRLF、甚至看似另一条
  记录头的字节，都不会改变边界（测试与演练正文里都埋了假记录头）。
- 头字段名**不区分大小写**；字段原值（去首尾空白后）、顺序与重复项
  都保留。折叠（续行）头不支持，拒绝。
- 头文本必须是合法 UTF-8，否则拒绝（事实库以 TEXT 存原值）。
- 下列**决定身份或边界的字段**重复出现即拒绝：WARC-Record-ID、
  WARC-Type、WARC-Date、Content-Length、WARC-Target-URI、WARC-Refers-To、
  WARC-Profile、WARC-Block-Digest、WARC-Payload-Digest。
- **WARC-Date 必须带时区**（`Z` 或 `±hh:mm`）；统一换算成 UTC 比较
  实际时刻，库存 UTC 规范串与纪元微秒整数。
- **WARC-Target-URI 以原字符串作为业务键**，不做任何归一化/合并。
- 支持的记录类型只有 `response` 与 `revisit`（profile 限
  identical-payload-digest，1.0/1.1 两种 URI 拼写都认）；warcinfo、
  request、metadata 等其他类型一律报"不支持的记录类型"。

### 1.1 上限与诊断

解析器有三个可调上限（`Limits`，CLI 有对应参数）：

| 参数 | 默认 | 含义 |
|---|---|---|
| `read_chunk` | 65536 | 解析读取块（流式读源的粒度） |
| `max_header_bytes` | 65536 | 单条 WARC 头部、HTTP 头部各自的上限 |
| `max_block_bytes` | 1 GiB | 单条内容块上限 |

诊断分类（异常类型即诊断）：**截短** `WarcTruncatedError`、**非法长
度/结构** `WarcFormatError`、**超限** `WarcLimitError`、**不支持的记
录类型/profile** `UnsupportedRecordTypeError`、**非 sha256 摘要**
`UnsupportedDigestError`、**HTTP 层问题** `HttpFormatError`、**声明摘
要与实算不符** `DigestMismatchError`。错误信息都带记录偏移与
Record-ID。`read_chunk` 只是读取粒度：把它缩到头结束符与长度字段都
跨段，解析结果与大块读取**逐字节一致**（演练 step 7 实测）。

## 2. response 记录：一条完整 HTTP 响应

内容块 = 一条完整 HTTP 响应（状态行 + 头 + CRLF CRLF + 正文），限定：

- 仅 HTTP/1.0 或 HTTP/1.1；状态行不符即拒绝；
- 必须有且仅有一个 HTTP Content-Length，且**实际正文长度**（内容块
  减去头部长度）必须等于它；
- 出现 `Transfer-Encoding`（分块）或 `Content-Encoding`（压缩）即拒绝；
- HTTP 头与二进制正文严格分开；正文是内容块内头部之后的全部字节。

## 3. 摘要：Block 与 Payload 都必须提供且匹配

- `WARC-Block-Digest`：对整个**内容块**（HTTP 头 + 正文，revisit 则
  只有 HTTP 头）计算；
- `WARC-Payload-Digest`：只对**正文**计算（revisit 记录的是被复用正
  文的摘要，解析到终点时核对）；
- 算法限定 **sha256**，`sha256:<64 位小写/大写十六进制>`；其他算法
  （如 sha1）拒绝；
- 两项都**必须提供**；response 的两项在导入时即与实算核对，revisit
  的 Block-Digest 导入时核对、Payload-Digest 在解析到终点时核对。

## 4. revisit 记录与解析（resolution）

revisit 使用 identical-payload-digest profile：

- `WARC-Refers-To` **精确**指向另一条记录的 Record-ID（就是字符串比
  对，不是 URL 比对）；
- 自己的内容块**只有本次 HTTP 响应头、没有正文**；它的 HTTP
  Content-Length 描述的是**被复用正文**的长度，解析器绝不据此越过
  WARC 内容块去吞下一条记录（revisit 块在头部结束符之外多一个字节
  都拒绝）；
- 允许**引用先到、目标后到**，允许**跨档案引用**，允许**多层
  revisit**（revisit 指向 revisit）；
- **禁止补位**：解析只沿 WARC-Refers-To 走，绝不拿相同网址或碰巧同
  摘要的其他记录顶替。

每条抓取有三种解析状态，存在事实库里、可追踪：

| 状态 | 含义 |
|---|---|
| `resolved` | 引用链到达一条 response，且本抓取的 Payload-Digest 与 HTTP 声明长度都和终点正文一致 |
| `pending` | 引用目标（某层 WARC-Refers-To）还不在事实库 |
| `error` | 引用环，或与终点的摘要/长度不一致（`status_detail` 记录具体原因与引用链） |

解析规则：

- 每条抓取独立解析到自己的终点；中间层 revisit 各自的错误不传染给
  上下游（每一层都只对自己的声明负责），但环上的所有记录都是 error；
- **迟到的 response 或中间 revisit 导入后，受影响的记录重新解析到终
  点**；已 resolved 的记录不受新导入影响（记录不可变、引用边指向精
  确 Record-ID，已完成的链不可能改变），其余记录结果不变；
- 解析出的正文来源 = 终点 response 的 `archive_id` 与其正文在原 WARC
  内的字节区间 `[source_offset, source_offset+source_length)`。

## 5. 事实库（独立 SQLite 文件）

```
fetches        每次抓取一行：record_id（主键）、archive_id、记录在原 WARC
               的字节区间与整记录 sha256、类型、target_uri、date_utc/
               date_us、HTTP 版本/状态/原因、声明正文长、两个摘要、
               refers_to（引用边）、自身正文区间（response）、解析状态、
               终点 record_id、正文来源 archive_id 与字节区间
fetch_headers  每次抓取自己的 HTTP 响应头（保序、保重复项、原名原值）
imports        每个已导入 archive_id 一行（records_total、new_fetches）
```

- **每次抓取保留自己的响应头与元数据**：revisit 的 HTTP 状态/日期/头
  是本次抓取的，绝不把终点 response 的当成它的（演练 step 5 验证：
  新抓取的 lookup 结果带新抓取的头，正文却指到旧原件）。
- **Record-ID 在事实库内唯一**：相同 ID 且整条 WARC 记录字节摘要相同
  = 重复（哪怕来自不同 archive_id），不增加抓取次数；相同 ID 内容不
  同 = 本次导入整体冲突拒绝（`ImportConflictError`），不悄悄更新旧
  记录。
- **一个 archive_id 的所有记录校验成功才一起入库**：解析与摘要核对
  全部通过后，才在单个 `BEGIN IMMEDIATE` 事务里写入并重新解析受影响
  记录。最后一条坏掉、或进程在提交前退出（含 SIGKILL），先前记录都
  不会成为该档案已导入的事实；恢复后可重新导入同一原件，已完成的
  导入也可重复确认（`already_imported`，幂等）。
- 事实库**不保存 KEK 或 DEK**（演练 step 9 把两把 KEK 的字节在
  facts.db 里搜了一遍：没有）。

## 6. 查询

### 6.1 引用链 `chain(record_id)`

返回该 Record-ID 的完整引用链：逐跳（record_id、类型、所在档案、时
间、refers_to、各自 HTTP 头）直到终点，终点类型为
`response` / `missing` / `cycle`，并给出当前解析状态与（若 resolved）
正文来源区间。

### 6.2 按网址与时刻 `lookup(target_uri, at)`

**选择规则（固定，写入本文档）**：在 `target_uri` 完全相等（原始字
符串）且抓取时刻 **不晚于** `at` 的抓取中，取时刻最新的一条；**同一
时刻并列时取 Record-ID 字典序（原始字符串按字节比较）最大的一条**。
时刻一律按 UTC 实际时刻比较（带时区的写法不同、时刻相同也算同时
刻，演练 step 5 用 `10:00+02:00` 与 `08:00Z` 验证）。

最新一次若还是 `pending` 或已是 `error`，**如实返回该次状态与原因，
不偷偷换成更早的一次**；想要更早的，调用方自己把截止时刻往前移。

## 7. 导出正文 `export_body(record_id, out)`

仅对 `resolved` 抓取可用：

1. 沿已确认的来源区间调用 archivestore 的 `read_range`（逐块认证解密）
   写到目标同目录的临时文件；
2. 流式核对读出字节的**长度**与**正文 sha256**（与库中声明的
   Payload-Digest 比对）；
3. 核对通过后 `link(2)` 原子就位到 `out`。

目标已存在则拒绝（不覆盖）；引用未解析、源档案认证失败、长度/摘要
不符时，临时文件随即删除，**不会留下可当成完整结果的文件**。临时
解密文件只活在目标同目录的 `.export-*.part`，正常结束或拒绝后都清
理（与 archivestore 的 `.read-*.part` 同一模式，同目录是硬链接原子
性的要求）。

## 8. 与加密库的关系（含换钥）

- 导入：`store.open(archive_id)` 解封 DEK、认证描述后，用
  `ArchiveHandle.iter_blocks()` **逐块解密**成明文流喂给解析器，摘要
  边读边算；**不把整份原件先解密成永久明文文件，也不把大正文装进内
  存**（峰值内存由块长 B 与解析上限决定，与档案总长无关；长正文可跨
  多个加密块）。演练 step 10：256 MiB 正文，导入/导出峰值 RSS 约
  36 MiB。
- 导出：只按来源区间做范围读取（`read_range`），未触及的块不读。
- 换钥：事实库只存 archive_id 与字节区间，轮换 KEK 后无需任何迁移；
  只持新 KEK 的新进程照常查询与导出，只持旧 KEK 的进程在
  `open`/`read_range` 处被 archivestore 拒绝。

## 9. API 与 CLI

```python
from archivestore import ArchiveStore
from warcdb import FactDB, FactStore, Limits

store = ArchiveStore("storage", {"k2": "keys/k2.key"})
db = FactDB("facts.db")
facts = FactStore(store, db)

r = facts.import_archive(aid, limits=Limits(read_chunk=1 << 20))
info = db.chain("<urn:uuid:...>")          # 完整引用链
hit = db.lookup("http://example/", us)     # 不晚于某时刻的最新抓取
res = facts.export_body(record_id, "out")  # 核对后原子就位
```

CLI（每条命令打印一行 JSON；错误打印到 stderr 并退出码 1）：

```
python -m warcdb --facts F.db --storage S --key k2=keys/k2.key import --archive HEX [--read-chunk N]
python -m warcdb --facts F.db chain --record-id RID
python -m warcdb --facts F.db lookup --uri URI --at 2026-11-14T00:00:00Z
python -m warcdb --facts F.db --storage S --key k2=... export --record-id RID --out O
python -m warcdb --facts F.db stats | fetches [--uri U] | get --record-id RID | dump
```

夹具生成器 `warcdb.fixtures`（离线，不依赖互联网）：
`response_record` / `revisit_record` / `assemble` 造整条档案并记录每条
记录、内容块、正文的精确偏移；`response_record_stream` 流式造大正文
档案；`payload_digest_hex` 算预期摘要。演练与测试都靠它。

## 10. 复做演练与测试

```
python3 -m pytest tests/ -q          # 88 个回归测试（含 archivestore 16 个）
python3 demo/run_warc_demo.py        # 端到端演练（约 1 分钟，工作区留在 /tmp）
```

演练覆盖：三份加密档案乱序到达（两层引用 → 中间引用 → 根正文），同一
抓取逐步从 pending 变 resolved；核对最终来源偏移与独立预期字节；同网
址新抓取（同一时刻两条、时区写法不同）证明取新抓取的头与旧正文、并
演示同时刻并列选择规则；环、缺失目标、摘要/长度不符、同 ID 冲突、末
条截短的多记录原件（逐条核对状态与库中实际行数）；解析读取块缩到 7
字节与 64 KiB 结果逐字节一致；提交前 SIGKILL 后无半批、重导幂等；换
KEK 后仅新密钥进程导出正文与原字节一致；256 MiB 大正文的输入规模与
实测峰值内存。

## 11. 明确不承诺的事项

- 不重新抓取、不联网；输入只认已登记的 archive_id。
- 不合并网址：Target-URI 原样为键，相似/同义网址各自独立。
- 不做内容去重：除精确 Record-ID 引用外，相同正文存几份就是几份。
- 不保证"最新抓取内容正确"：pending/error 如实报告，由采集流程补救。
- 访问控制与密钥生命周期归 archivestore 与操作系统管；事实库文件本身
  不含密钥，但它记录的网址/时间等元数据不保密。
