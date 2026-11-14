# archivestore — 本机档案加密存储模块

把采集到的大型原始字节档案加密后放到不可信的共享磁盘上。模块直接保管采集
原件的字节，不解释文件内的业务字段，不提供云服务或账号界面。只使用
`cryptography` 的 AES-256-GCM，不自行设计加密算法。

四个操作：**seal**（收件加密）、**read-range**（局部读取）、**verify**
（全档校验）、**rotate**（保管密钥轮换），同时提供可导入的 Python API
和等价的命令行界面。

```
archivestore/      可导入模块
warcdb/            离线采集事实库（WARC 解析 + SQLite 事实库，见 WARCDB.md）
eventledger/       离线爬虫运行事件账本（JSONL → SQLite，见 eventledger/README.md）
robots_admission/  可解释的 robots.txt 抓取准入模块（见 robots_admission/README.md）
tableextract/      离线网页数据表提取器（本地 HTML → 矩形数据，见第 9 节）
sitemapdiscover/   从站点地图发现待抓网页（根地图 + 本地映射 → 去重清单，见第 10 节）
demo/              可复做的端到端演练（run_demo.py）+ 独立参考解密器（refdec.py）
                   + 事实库演练（run_warc_demo.py）+ 账本演练（run_eventledger_drill.py）
                   + 抓取准入演练（run_robots_drill.py）
                   + 数据表提取演练（run_table_drill.py，夹具在 demo/table_fixtures/）
                   + 站点地图发现演练（run_sitemap_drill.py，夹具在 demo/sitemap_fixtures/）
tests/             pytest 回归测试
```

依赖：Python ≥ 3.10、`cryptography`、标准库 `sqlite3`。

---

## 1. 存储布局

```
<storage>/
    archive.db          SQLite 目录库（WAL 模式）
    blobs/<archive_id>.bin   每份档案一个密文文件
    tmp/                seal 进行中的临时区
<keys>/                 ← 必须在存储目录之外，由使用者保管
    <key_id>.key        每个文件恰好 32 字节（一把 256 位 KEK）
```

SQLite 只登记：描述（descriptor）、描述认证标签、封装后的 DEK、密文定位
（blob 相对路径）、当前 key_id。**绝不存明文 DEK 或 KEK**。把 `storage/`
整个复制走但没有密钥文件，读不出任何原件（演练 step 4 实测）。

### 1.1 档案描述 descriptor（固定 78 字节，全大端，无歧义）

| 偏移 | 长度 | 字段 |
|---|---|---|
| 0  | 4  | magic `ARC1` |
| 4  | 2  | 格式版本 = 1 |
| 6  | 16 | archive_id（每次 seal 随机） |
| 22 | 8  | 原件长度 orig_len |
| 30 | 4  | 块长 B（明文字节/块） |
| 34 | 8  | 正文块数 N = ceil(orig_len / B) |
| 42 | 4  | nonce 前缀（每份档案随机） |
| 46 | 32 | 原件 SHA-256 |

描述本身不保密，但被认证（见下）。解码时校验 magic、版本、B 范围、
`N == ceil(orig_len/B)` 的一致性。

### 1.2 正文块与 nonce

每份档案随机取 **4 字节 nonce 前缀**，拼接 **8 字节大端块序号** 构成 12 字节
GCM nonce：

- 序号 **0** 保留给档案描述认证记录：`AES-GCM(DEK, nonce=前缀‖0, 明文="",
  AAD=descriptor)`，产出 16 字节 `desc_tag` 存入目录库；
- 正文块从 **1** 编号，不得重复；第 i 块（1-based）存于 blob 偏移
  `(i-1)*(B+16)`，内容为 `密文(≤B 字节) ‖ 16 字节标签`；
- 第 i 块的 AAD = `b"ARCB1" ‖ SHA256(descriptor) ‖ i(8字节) ‖ plen_i(4字节)`，
  即绑定**描述、块序号、本块真实明文长度**——剪切、调序、跨档案替换、
  把尾部短块换成长块（或反之）都会认证失败。

blob 文件的应有总尺寸由描述唯一确定（`N=0` 时为 0；否则
`(N-1)*(B+16) + 末块明文长 + 16`）。截短或附加多余字节都会被尺寸检查拒绝。

### 1.3 DEK 封装信封 envelope（固定 60 字节）

```
12 字节随机 nonce ‖ AES-256-GCM(KEK, DEK) 密文 32 字节 ‖ 16 字节标签
AAD = b"ARCW1" ‖ archive_id ‖ SHA256(descriptor) ‖ len(key_id)(2字节) ‖ key_id
```

信封绑定 archive_id、**描述摘要**和 **key_id**：换了任何一样都解不开。
每次封装用独立随机 nonce（seal 一次、rotate 每份档案各一次）。

---

## 2. 操作语义

### seal(source, key_id, block_size) → archive_id

1. 每次 seal 全新随机生成 archive_id、256 位 DEK、nonce 前缀；**中断过的
   seal 不会续用，重新 seal 就是新档案、新 DEK、新 nonce**。
2. 流式两遍读源文件（先算 SHA-256，再逐块加密），**内存占用 O(B)**，
   完整原件不驻留内存；两遍之间源文件尺寸变化会报错。
3. 密文先写 `tmp/seal-*.part`，fsync 后原子 rename 进 `blobs/`，再 fsync
   目录，最后在单个 SQLite 事务里登记目录。**只有登记成功才算已收件**：
   写盘后、登记前退出留下的孤儿 blob 没有任何目录行，API 不会把它当成
   档案（`list_archives`/`open`/`verify` 都只认目录库）；`tmp/` 里的
   `.part` 残骸可由 `cleanup_stale_tmp()` 清理，它只删符合 seal 临时命名
   且足够老的文件，**绝不碰 `blobs/` 或其他档案**。

### read-range(archive_id, start, end, out) → ReadStats

1. 先解封 DEK（认证信封）并认证描述，再检查 blob 应有总尺寸，然后只定位
   并读取覆盖 `[start,end)` 的块，逐块验标签后截取写出。**不会为读几十
   字节解密整份原件**；返回实际读取的密文块数与密文字节数。
2. 跨块、尾部短块、空档案（只允许 `[0,0)`）、零长范围都明确支持；
   `start<0`、`end<start`、`end>orig_len`、非整数一律拒绝。
3. 未触及的坏块不影响本次局部读取；但 `verify` 会读全档案、验所有块并
   核对原件 SHA-256。
4. 输出写到**新目标**：先写同目录临时文件、fsync、再 `link(2)` 原子就位；
   目标已存在则拒绝（不破坏已有目标）；任何失败只留下随即删除的
   `.read-*.part` 残骸，不会留下可当成完整结果的文件；**从不向标准输出
   流式吐出未完成结果**。

### verify(archive_id) → VerifyStats

认证描述 → 检查 blob 尺寸 → 逐块验标签解密 → 累加 SHA-256 与描述中的原件
摘要比对。正文截短、附加多余正文、改描述、移换块、错密钥全部会被拒绝。

### rotate(old_key_id, new_key_id) → 轮换份数

1. 在 `BEGIN IMMEDIATE` 事务内：逐份用旧 KEK 解封（认证信封）并用 DEK
   认证描述；**有一份解不开就拒绝整轮**，目录库回滚保持不变。
2. 全部认证通过后，用新 KEK 重新封装每份 DEK（新随机 nonce），同一事务
   提交。**密文正文与描述的字节原封不动**（轮换不宣称检查过全部正文）。
3. 整个库的封套换代只有一次可见切换：进程在提交前或提交后被强退，重开
   所见只能是全旧或全新（SQLite 事务保证，演练 step 6 用 SIGKILL 实测）。
4. 已拿到并解封旧封套的读者（`ArchiveHandle` 持有内存中的 DEK）可以读完；
   新打开的读者总是取到完整的某一代封套，不会拼到半套。
5. seal 与 rotate 都经 SQLite 写锁串行化；进程退出（含被杀）即释放写占用。

---

## 3. 密钥交接步骤（key handover）

1. **生成**：`python -c "from archivestore import generate_key_file;
   generate_key_file('<keys>/k1.key')"`，或 `head -c 32 /dev/urandom >
   k1.key && chmod 600 k1.key`。密钥文件必须放在存储目录**之外**。
2. **交接给进程**：使用者把 `key_id → 密钥文件路径` 的映射交给当前进程
   （API 的 `ArchiveStore(storage, {"k1": "/path/k1.key"})`，或 CLI 的
   `--key k1=/path/k1.key`）。key_id 只是外部名字，模块不验证其来源。
3. **值班轮换**：新值生成 `k2.key` 并安全分发给需要继续读取的进程后，
   任一持有新旧两把密钥的进程执行
   `rotate(old_key_id="k1", new_key_id="k2")`。成功后：只持新 KEK 的进程
   能打开全部档案；只持旧 KEK 的进程打不开当前目录记录。确认新密钥可用
   后，由使用者自行按保密期限销毁旧密钥文件（模块不替你做销毁）。
4. **错误与日志**：异常信息和日志只含 key_id、archive_id、路径、尺寸，
   绝不回显密钥字节。测试与演练只用运行时临时生成的合成密钥，仓库里
   没有任何固定密钥。

---

## 4. 安全边界（明确不承诺的事项）

- **不承诺清除原始来件**：seal 只读源文件，不擦除它；源文件的处置归
  采集流程管。
- **不承诺清除进程内存或操作系统缓存**：DEK/明文块在内存中的残留、
  页缓存、交换分区等都不在防护范围内（`ArchiveHandle.close()` 只是
  尽力丢弃引用）。
- **不承诺阻止"持有旧库副本 + 旧 KEK"的人读旧副本**：rotate 只改当前
  目录库里的封套；攻击者若同时握有轮换前的整个库副本和旧 KEK，仍能
  读那个旧副本。轮换的意义是：当前目录记录对只持旧 KEK 者关闭。
- **不处理攻击者回放整个历史库的情况**：若攻击者把目录库整体回滚到
  旧代，模块无法察觉（这需要库外的代际计数/签名机制，超出本模块范围）。
- **描述不保密**：archive_id、长度、块长、原件 SHA-256 对应答可见；
  其中 SHA-256 可用于"已知文件识别"。长度与块布局也不作填充隐藏。
- 机密性/完整性依赖：AES-256-GCM（cryptography 实现）、每档案独立随机
  DEK、每信封独立随机 nonce、nonce 序号不重复、KEK 文件本身的保密。
- 本模块不管业务字段、不做云服务、不做账号/权限界面；访问控制由操作
  系统对密钥文件与存储目录的权限决定。

---

## 5. API 速览

```python
from archivestore import ArchiveStore, generate_key_file

generate_key_file("/keys/k1.key")                 # 一次性，存储目录之外
store = ArchiveStore("/storage", {"k1": "/keys/k1.key", "k2": "/keys/k2.key"})

aid = store.seal("capture.bin", key_id="k1", block_size=1 << 20)
stats = store.read_range(aid, 1000, 2000, "out.bin")   # ReadStats(blocks_read, ...)
info = store.verify(aid)                                # VerifyStats(...)
n = store.rotate(old_key_id="k1", new_key_id="k2")

h = store.open(aid)        # 持有 DEK 的句柄，可跨过轮换继续读
h.read_range(0, 100, "head.bin"); h.close()
store.close()
```

CLI（每个命令打印一行 JSON 结果；错误打印到 stderr 并退出码 1）：

```
python -m archivestore --storage S --key k1=/keys/k1.key seal --source F --key-id k1 --block-size 1048576
python -m archivestore --storage S --key k1=/keys/k1.key read-range --archive HEX --start 0 --end 100 --out O
python -m archivestore --storage S --key k1=/keys/k1.key verify --archive HEX
python -m archivestore --storage S --key k1=... --key k2=... rotate --old k1 --new k2
python -m archivestore --storage S list
```

## 6. 复做演练与测试

```
python3 -m pytest tests/ -q      # 88 个回归测试
python3 demo/run_demo.py         # 端到端演练（约 1 分钟，工作区保留在 /tmp 下）
```

演练覆盖：两份含零字节原件 + 一个空原件；新进程读首段/跨块段/尾段并与原
字节切片比较；独立参考解密器 `demo/refdec.py`（不调用正式读函数）核对格式；
同档案换块、跨档案挪块、改长度、截短、错密钥分别失败；未访问块改坏时局部
读取仍可用而 verify 报错；轮换前后正文与描述摘要一致（未重写大数据）、仅
新密钥可读回；在轮换提交两侧分别 SIGKILL 后重开只见全旧/全新；已解封读者
跨过轮换继续读；64 MiB 大原件的峰值内存与窄范围真实 I/O 受块长 B 支配
（B=1MiB 时 seal 峰值 RSS ≈33 MiB，B=8MiB 时 ≈68 MiB，与总档案长度无关）。

---

## 7. 离线采集事实库 warcdb

采集组把多轮网页抓取的 WARC 原件 seal 进本档案库之后，`warcdb/` 负责把
已登记的 archive_id 解析成可按"网址 + 抓取时刻"查询的事实库：严格按
Content-Length 界定 WARC 记录边界，处理 response 与 revisit（含跨档案、
多层、迟到的引用），把每次抓取的身份、网址、时间、HTTP 状态与头、引用
边、解析状态、正文来源 archive_id 与精确字节区间存进独立 SQLite 文件
（不复制正文、不存密钥），并提供引用链查询、按时刻选择最新抓取、以及
沿来源区间核对摘要后的正文导出。格式约定、选择规则与诊断分类见
**[WARCDB.md](WARCDB.md)**；端到端演练：

```
python3 demo/run_warc_demo.py
```

---

## 8. 抓取准入模块 robots_admission

调度器在真正发出请求前，用 `robots_admission/` 判断某个采集器身份能否
抓取某个 URL：按 origin 隔离的 SQLite 缓存（scheme/IDNA 主机/端口归一），
User-agent 组选择（最长 token、同长度合并、`*` 不混入具体规则）、
`*`/`$` 路径匹配与统一的百分号/UTF-8 字节规范化（`%2F` 不会变成路径
分隔符），以及 200/304/404/410/429/5xx 的完整缓存语义（有效期区间、
单调 fetched_at、ETag 校验、失败只记录不延寿）。每个判断都给出规范化
origin、缓存表示、有效期限、选中组与命中规则的原文和行号；
`needs_refresh` 表示没有可用策略，调度器必须暂缓而不是放行。缓存是
判断的唯一权威来源，不读取 eventledger 账本或 warcdb 事实。API、批量
入口与缓存语义详见 **[robots_admission/README.md](robots_admission/README.md)**；
离线验收演练（不访问网络，150+ 项断言）：

```
python3 -I demo/run_robots_drill.py
```

---

## 9. 离线网页数据表提取器 tableextract

采集人员下载的公开统计网页里，表格常把地区名跨多行、年份标题横跨
数列；直接按每个 `tr` 的 `td` 顺序取数会把指标挪到错误的列。
`tableextract/` 用 Python 标准库读取本地 UTF-8 HTML，把选定的一张
`table` 还原成矩形数据，并保留每个值来自哪个原始单元格。它是纯
"本地字节进、结构化数据出"的组件：**不发网络请求，也不接入
archivestore 档案加密、warcdb 抓取事实库、eventledger 事件账本或
robots_admission 缓存**；不引入浏览器、外部 HTML 服务或网页解析依赖。

### 9.1 支持的 HTML 子集（显式闭合，不做浏览器式修复）

- 标签名与属性名不区分大小写；属性值按原值比较（区分大小写）。
- 文档必须显式闭合：每个非空元素（void 元素如 `br`/`img`/`meta`
  除外）都要有匹配的结束标签；`<tag/>` 视为立即闭合的完整元素。
- 选中表格内：`<table>` 下只允许 `thead`/`tbody`/`tfoot`/`tr`，
  行组下只允许 `tr`，`tr` 下只允许 `td`/`th`；连续的直接 `tr`
  构成一个隐式行组；`caption`/`colgroup` 等不在子集内。
- 单元格内容是行内标记，可再嵌套 `<table>`；内层表格是一张独立的
  表（可按自己的 id 单独提取），其内容不进入外层单元格，也不增加
  外层的行数或列数。
- 选中表格出现缺失闭合、非法嵌套、文本/标签错位时，按行号定位并
  拒绝，**不悄悄补一个看似正常的数据集**；子集之外的任意坏标签不
  保证被接受。

### 9.2 文本规则

行内标签只贡献文字；字符实体解码；每个 `br` 视为一个空格；连续
ASCII 空白（空格/制表/换行/回车/换页）折成一个空格并去掉首尾空白
（`&nbsp;` 是 U+00A0，不参与折叠，会被保留）。注释、`script`、
`style` 的内容不算数据。空单元格是真实存在的源单元格（文本为
`""`），与补齐出来的 `null` 缺口严格区分。

### 9.3 矩形化规则

- `rowspan`/`colspan` 缺省为 1；属性值去掉首尾 ASCII 空白后必须是
  十进制非负整数，`colspan` 必须大于 0，`rowspan` 可以为 0；属性
  重复出现即拒绝。
- 每行的源单元格按文档顺序从该行最左边尚未占用的列落位，再按声明
  跨度占据矩形；不会为绕开中间占位而把 `colspan` 拆成几段。与先前
  跨行单元格发生覆盖时拒绝，报告两个源单元格与冲突坐标，不采用
  后者覆盖前者。
- `thead`、每个 `tbody`、`tfoot` 各自构成行组（相邻两个 `tbody`
  不是同一组）；无行组标签的连续直接 `tr` 构成隐式行组。正数
  `rowspan` 超过本组最后一行即拒绝；`rowspan=0` 延伸到本组末行，
  绝不流进下一组。
- 所有行最后补齐到本表最大列数，缺位为 `null`（不引用虚构的空
  单元格）。坐标一律从 0 开始，指展开后的表格位置；被跨度覆盖的
  位置引用原源单元格，不额外造出多个源单元格。

### 9.4 标题与数据

`header_rows` 指定文档顺序中前多少行为列标题（0 表示没有标题；
超过实际行数报错）。跨行单元格不得同时覆盖标题行和数据行。每列
的标题路径从上往下收集相应标题位置的源单元格文字：空文字略去；
同一源单元格因 `rowspan` 在该列重复出现时只收一次；两个不同源
单元格文字碰巧相同仍分别保留；横跨多列的标题进入每个相关列的
路径。不猜测哪一行才是"真标题"，也不把 `tbody` 里的 `th` 自动
删成表头。

### 9.5 上限（超限在分配巨大网格之前报错）

输入不超过 4 MiB；结果不超过 10000 行、256 列、2 000 000 个网格
位置。非法 UTF-8、重复/负数/小数跨度、布局冲突等失败都带目标表格
id、来源行号和具体原因。

### 9.6 API

```python
from tableextract import extract_table, TableExtractError

try:
    res = extract_table(html_bytes, "regional-stats", header_rows=2)
except TableExtractError as exc:
    print(exc)          # [table 'regional-stats'; line 30, col 8] 具体原因
else:
    res.n_rows, res.n_cols          # 表格尺寸（展开后）
    res.row_groups                  # [RowGroupInfo(index, kind, rows, first_row)]
    res.source_cells[12].text       # 源单元格：按出现顺序编号
    res.source_cells[12].kind       #   "td" | "th"
    res.source_cells[12].row_group  #   所在行组编号（kind 另存）
    (res.source_cells[12].row, res.source_cells[12].col)      # 左上坐标
    (res.source_cells[12].span_rows, res.source_cells[12].span_cols)  # 实际跨度
    (res.source_cells[12].line, res.source_cells[12].col_offset)      # 源位置
    res.grid[r][c]                  # 单元格引用矩阵：源单元格编号或 None
    res.header_paths[c]             # 每列标题路径 [HeaderEntry(cell, text)]
    res.data_rows[i].values         # 数据行展开文本值（缺口为 None）
    res.data_rows[i].cells          # 对应源单元格编号（缺口为 None）
    res.to_dict()                   # 以上全部的可 JSON 化字典
```

相同输入重复提取的编号与结果顺序稳定一致。参数校验：`html_bytes`
必须是字节，`table_id` 必须是 `str`，`header_rows` 必须是非负
`int`（负数/小数/布尔报 `ValueError`）。

### 9.7 命令行

```
python3 -m tableextract 页面.html --table-id regional-stats --header-rows 2
```

成功时把完整结果 JSON 写到标准输出；失败时把 `{"error": ...}` 写到
标准错误并以非零码退出，**不在标准输出留下可当成成功数据的半份
结果**。

### 9.8 错误类型（均为 `TableExtractError` 子类）

`InputTooLargeError`（输入超 4 MiB）、`InvalidUtf8Error`、
`TableNotFoundError`、`DuplicateTableIdError`、
`HtmlStructureError`（缺失闭合/非法嵌套/结构错位）、
`SpanAttributeError`（重复/非法跨度）、`LayoutConflictError`
（跨度占位冲突，附冲突坐标与双方源行号）、
`RowspanOutOfGroupError`（正数 rowspan 越出本行组）、
`HeaderRowsError`（header_rows 超行数）、`HeaderBoundaryError`
（跨行单元格压标题/数据边界）、`ResultLimitError`（行/列/格位置
超限）。

### 9.9 演练与回归测试

```
python3 -I demo/run_table_drill.py   # 93 项检查，夹具在 demo/table_fixtures/
python3 -m pytest tests/ -q          # 含 tests/test_tableextract.py
```

演练覆盖：地区列跨行 + 两层"年份/指标"跨列表头的逐格坐标核对；
`rowspan=0` 在两个 `tbody` 之间停止；内外表格各自提取；实体/`br`/
注释/`script`/`style` 的文本规则；真实空单元格与 `null` 缺口的
区分；无表头与 `tbody` 内 `th` 的保留；横跨单元格撞上前一行占位
的拒绝（证明不会生成错列结果）；跨度越组、跨标题/数据边界、坏
结构、重复 id、错误跨度、输入与网格上限的拒绝；以及 CLI 成功/
失败两条路径。所有预期坐标、标题路径和值都在演练脚本里人工列出，
不靠再次调用布局函数生成。


## 10. 站点地图发现 sitemapdiscover

采集组接手一个网站时拿到的往往只是几个根地图 URL，以及一份“URL → 本地
已下载文件”的映射。`sitemapdiscover/` 沿地图索引深度优先找到真正的网页，
产出**带来源**的抓取清单：同一网页只占一条记录，但保存在每张地图中的
全部来源位置（source_url、条目序号、原始 loc、lastmod）；网页汇总日期取
所有有效来源的最大日期。整个过程**不下载文件、不判断 robots 许可、不解释
网页内容**。

要点：

- 根地址与地图内引用都必须是绝对 http(s) URL；scheme/主机/默认端口按统一
  规则规范化，路径与 query 逐字节保留（`%2F` 与 `/` 是两个网页）。查映射、
  合并重复网页都用同一身份规则；重复 `source_url` 属于配置冲突。
- 地图只能是 UTF-8 XML 的 `urlset`/`sitemapindex`（正式命名空间）或单个
  gzip member；gzip 按魔数判断。扩展命名空间的子树整体忽略，扩展里的
  `loc` 不能冒充正式网址。重复/非法 `loc`、`lastmod`、DTD/实体、坏 XML、
  非 UTF-8、gzip 截断/CRC 错/拼接/尾字节都有明确拒绝原因。
- 每个地图只能引用同 origin 的下级地图和网页；跨 origin 会指出具体条目
  并拒绝该地图。不同 origin 可各自作根，计数互不串用。
- 深度以根为 0；根按给定顺序、地图内按条目顺序深度优先，索引可引用索引。
  地图身份 `(origin, URL)` 每次运行最多解析一次；只有活动栈上的回边才是环
  并打印完整链，共享子图只读一次。所有引用边（含重复边）都返回；缺来件、
  超深度、超数量的分支都落到报告，`complete=false` 且 CLI 退出码非零。
- 每份地图在解压、XML 尾部、全部条目都验证完后才在单个事务里提交；末尾
  损坏或 gzip 校验失败不会让前面读出的网页生效。单图失败不影响其他独立
  分支。报告列出根、已访问地图、引用边、失败节点与原因，并区分“实际读取
  的不同地图数”与“引用条数”。
- 流式解压/解析，大批中间状态放在**本次运行自建的临时 SQLite**（运行结束
  删除，不是持久业务目录）。可调限额：单文件压缩字节、解压字节、每地图
  条目数、不同地图数、引用深度、单个 loc 长度。`iter_pages()`/报告写出
  逐条进行，不把全部网页读成列表。

```
python3 -m sitemapdiscover discover --roots roots.txt --mapping mapping.json \
        --report report.jsonl
python3 -m sitemapdiscover pages --report report.jsonl      # 逐条回放
python3 -I demo/run_sitemap_drill.py                       # 78 项离线演练
python3 -m unittest tests.test_sitemapdiscover -v
```

完整命令、退出码与失败含义、内存构成见
**[sitemapdiscover/README.md](sitemapdiscover/README.md)**。
