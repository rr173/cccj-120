# sitemapdiscover — 从站点地图发现待抓网页（离线，纯标准库）

采集组接手一个网站时拿到的往往只是几个根地图 URL，以及一份“URL → 本地
已下载文件”的映射。本模块沿地图索引深度优先走到真正的网页，产出一份带
来源的抓取清单：

- **不发网络请求**（只读本地文件，映射表之外的 URL 一律不访问）；
- **不判断 robots 许可**（准入是 `robots_admission` 的职责）；
- **不解释网页内容**（正文解析是 `tableextract` / `warcdb` 的职责）。

它只做一件事：把“根地图 + 本地来件”变成“去重后的网页清单 + 每页的全部
来源位置 + 明确完整/不完整的发现报告”。

```
sitemapdiscover/
  urls.py      统一的 URL 规则（origin 规范化、路径/query 逐字节保留）
  mapping.py   source_url → 本地文件映射（JSON，重复即为配置冲突）
  profile.py   本次运行的各项限额
  reader.py    单文件流式读取：gzip 魔数 → 有界解压 → 严格 UTF-8 → expat
  store.py     本次运行的临时 SQLite（maps/pages/sources/edges/issues）
  engine.py    迭代式深度优先遍历、原子地图、报告与流式写出
  __main__.py  命令行：discover / pages
```

---

## 1. 调用接口

```python
from sitemapdiscover import Discovery, DownloadMapping, Limits

mapping = DownloadMapping.from_file("mapping.json")
limits = Limits(max_depth=8, max_entries_per_map=50_000, max_maps=200)

with Discovery(["https://example.com/sitemap_index.xml"], mapping, limits) as d:
    d.run()                                    # 遍历整批来件
    report = d.report                          # 汇总（含 complete）
    for page in d.iter_pages():                # 逐条流式读取，不整表materialize
        print(page.page_seq, page.url, page.lastmod)
        for source in page.sources:
            print("   ", source.source_url, source.entry_index,
                  source.loc, source.lastmod)
    for edge in d.iter_edges():                # 全部引用边（含重复边）
        print(edge.parent_url, edge.entry_index, edge.child_url,
              edge.resolution, edge.lastmod)
    for node in d.iter_maps():                 # 已访问/失败的地图节点
        print(node.seq, node.status, node.url)
    for issue in d.iter_issues():              # 环、深度/数量限额
        print(issue.kind, issue.child_url, issue.chain)
    d.write_report("report.jsonl", format="jsonl")   # 流式写出
# 离开 with 即删除本次创建的临时 SQLite 与目录
```

一次性入口（CLI 用的也是它）：

```python
from sitemapdiscover import discover_to_report
summary = discover_to_report(roots=[...], mapping_path="mapping.json",
                             report_path="report.jsonl")
```

`Discovery.run()` 只能调用一次；未 run 就读结果会报
`SitemapDiscoverError`。`report.to_dict()` 的结构：

```json
{
  "complete": false,
  "roots": ["https://example.com/sitemap_index.xml"],
  "origins": ["https://example.com"],
  "limits": {"max_file_bytes": ..., "max_decompressed_bytes": ...,
             "max_entries_per_map": ..., "max_maps": ...,
             "max_depth": ..., "max_loc_length": ...},
  "counts": {
    "map_nodes": 8,        // 尝试过的不同地图身份数（成功 + 失败）
    "maps_read": 5,        // 真正读完并通过全部校验的不同地图文件数
    "map_failures": 3,     // 失败的地图节点数（含缺来件）
    "issue_failures": 1,   // 无自身文件的失败：环、深度/数量限额
    "edges": 9,            // 全部引用边条数（重复边计入）
    "page_entries": 7,     // 原始网页条目数（去重前）
    "sitemap_entries": 14, // 原始下级地图条目数
    "pages": 6             // 去重后的网页数
  },
  "map_status_counts": {"complete": 5, "error": 1, "missing_mapping": 1,
                        "revisit": 0, "cycle": 1}
}
```

**`complete` 显式给出**，只要有一个失败地图、环或限额停点就是 `false`；
CLI 同时以非零退出码表示“发现不完整”。部分结果永远不称为“整站已发现”。

### 内存由什么决定

| 局部数据 | 规模 |
|---|---|
| 活动遍历栈 | O(引用深度)，每层一个 ~56 字节的 frame |
| 解析器缓冲 | 一个 64 KiB 读块 + 一个 256 KiB 解压块 + 当前元素文本（受 `max_loc_length` 限制） |
| 暂存条目 | SQLite 里一整张地图的条目（单条校验通过才落入，事务机制保证原子） |
| 结果读取 | `iter_pages()` 每次只取一页及其来源；`iter_edges()/iter_maps()` 逐行 |
| 映射表 | 配置里的条目数（仅 URL 与路径字符串） |

地图、网页、来源、引用边**全部**在临时 SQLite 文件里（`temp_store=FILE`，
页缓存封顶 2 MiB），Python 不持有整份 XML、整棵解析树或全部网页列表。
演练场景 5 在 `RLIMIT_AS=96 MiB` 的独立进程里处理 12 万条短 URL，峰值
RSS ≈19 MiB，且相对 1 000 条的基线增长不足 4 MiB。

---

## 2. URL 规则（根、地图内容、映射表共用一套）

**身份键**（查映射、合并重复网页都用它）：

```
<scheme 小写>://<IDNA/小写主机>[:<非默认端口>]<路径原样>[?<query 原样>]
```

- scheme 只接受 `http`/`https`（其余一律拒绝）；
- 主机名小写、Unicode 域名 IDNA 成 ASCII、IPv6 字面量压缩为规范形式；
- 默认端口（http 80 / https 443）省略，其他端口保留；
- **路径与 query 原样保留**：`%2F` 与 `/` 是两个不同网页，`%7E` 与 `~`
  不同，`.`/`..` 不折叠、重复斜杠不合并、query 内的顺序与大小写不变；
- 拒绝：用户名/密码、缺主机、非法端口、任何 fragment（包括空的 `#`）、
  ASCII 空白/控制字符、路径或 query 里残缺的百分号转义。

所以映射表里 `https://e.test/a%2Fb` 与 `https://e.test/a/b` 是两条互不
相干的来件；`https://E.TEST:443/x` 与 `https://e.test/x` 是同一条（重复
即为配置冲突）。

### 跨 origin

每个地图只能引用**与自己同 origin** 的下级地图和网页。origin = 规范化
scheme + 主机 + 端口。`http://e.test` 与 `https://e.test`、`https://e.test`
与 `https://e.test:8443` 都算不同 origin。地图里出现跨 origin 的 `loc`
时，整张地图被拒绝，报告指出**具体条目号**（以及行号/列号）。

不同 origin 可以各自作为根；它们的地图、网页、计数互不串用——不会拿一个
根的来件去填另一个根的缺口（各自仍受自己的映射表约束）。

---

## 3. 地图内容

- 内容为 UTF-8 XML；根元素是 `urlset` 或 `sitemapindex`，且位于
  `http://www.sitemaps.org/schemas/sitemap/0.9` 命名空间。
- 也支持**单个 gzip member** 包装的 XML：按文件魔数 `1f 8b` 判断，不看
  扩展名（`.xml` 里装 gzip 也照样解压）。
- 扩展命名空间（`<image:image>`、`<ext:loc>` 等）的**整个子树忽略**，扩展
  里的 `loc` 绝不会被当成正式网址；缺正式 `loc` 时按 `missing_loc` 拒绝。
- `urlset` 的每个 `url`、`sitemapindex` 的每个 `sitemap`：
  - **恰有一个直接 `loc`**，去掉首尾空白（空格/tab/CR/LF）后不能为空；
  - **至多一个直接 `lastmod`**，必须是有效的 `YYYY-MM-DD` 日历日
    （`2026-02-30`、`2026-01-01T00:00:00Z` 都被拒绝，不猜时间戳和时区）；
  - 其余正式命名空间的未知字段（`priority`、`changefreq` 等）跳过。
- 重复 `loc`、重复 `lastmod`、空 `loc`、`loc` 内有子元素、条目之间有
  游离文本，都明确拒绝并带条目号。
- **DTD/实体声明一律拒绝**（`<!DOCTYPE ...>`、`<!ENTITY ...>`），不做实体
  扩展、不读外部实体。
- 坏 XML、非 UTF-8（含 XML 声明非 UTF-8）、gzip 截断 / CRC 或长度不符 /
  拼接第二个 member / member 后有多余尾字节，都有独立拒绝类型。

拒绝类型（`MapRejection.kind`）一览：

| kind | 含义 |
|---|---|
| `missing_file` / `unreadable` | 映射指向的本地文件不存在/打不开 |
| `bad_root` | 根元素不是正式命名空间的 urlset/sitemapindex |
| `invalid_xml` | 坏 XML、尾部截断、根外内容等 |
| `unsupported_encoding` | XML 声明了非 UTF-8 编码 |
| `invalid_utf8` | 内容不是合法 UTF-8 |
| `doctype_forbidden` / `entity_forbidden` | DTD / 实体声明 |
| `missing_loc` / `duplicate_loc` / `empty_loc` | loc 缺失/重复/为空 |
| `invalid_loc` | loc 不是可接受的绝对 http(s) URL（含内嵌空白、fragment 等） |
| `cross_origin` | loc 与本地图不同 origin |
| `duplicate_lastmod` / `invalid_lastmod` | lastmod 重复/非法 |
| `loc_has_children` / `unexpected_text` | 结构越界 |
| `gzip_invalid` / `gzip_truncated` / `gzip_crc` / `gzip_concatenated` / `gzip_trailing` | gzip 层问题 |
| `limit_file_bytes` / `limit_decompressed` / `limit_entries` / `limit_loc_length` | 限额 |

---

## 4. 遍历语义

- **顺序**：根按给定顺序；每个根内部按地图条目顺序深度优先（一个根的
  整个子树处理完才轮到下一个根）。索引可以引用另一个索引。
- **地图身份** = `(origin, 规范化 URL)`。每个地图文件在一次运行中**最多
  解析一次**；再次到达只记一条 `revisit` 边并增加 `arrival_count`。
- **环**：只有沿**活动遍历栈**回到祖先才算环，报告打印完整链
  （`A -> B -> A`）。`A`、`B` 都指向 `C` 是共享子图，不是环；`C` 只解析
  一次，两条边都保留。
- **根**：完全相同的根第二次出现记为 `duplicate_root`，不再解析；互相引用
  的多个根会在第二棵树里形成环（明确 `complete=false`）。
- **所有引用边都返回**（重复边也返回），每条边带父地图、条目序号、
  子 URL、来源根、深度、解析结果（`read`/`revisit`/`cycle`/`limit_depth`/
  `limit_maps`/`missing_mapping`）和该条目的 `lastmod`（只描述下级地图）。
- **不悄悄跳过**：找不到本地来件、超出深度或地图数量限额的分支都落到
  报告中（`missing_mapping` 节点或 `issue`），并让 `complete=false`。

### 原子性

某份地图要等**解压、XML 尾部、全部条目**都验证完，才在同一个 SQLite
事务里把网页、来源和下级引用一起提交。文件末尾损坏、最后一条越界、
gzip 校验失败，都不会让同一地图前面读出的网页或下级引用生效。单份地图
失败不影响其他已排定的独立分支；失败索引的条目一条都不派生。

### 网页与来源

- 网页按**第一次有效出现**的顺序编号（从 1）。
- 同一规范化 URL 只占一个网页记录，但保存它在每张地图里的**全部来源
  位置**：`source_url`、从 1 开始的 `entry_index`、原始 `loc`、`lastmod`。
- 重复来源日期可以不同，不算冲突；网页汇总 `lastmod` = 所有有效来源的
  **最大日期**，没有日期的来源不会覆盖已有日期。
- 索引条目里的 `lastmod` 只挂在引用下级地图的边上，**不会**变成该地图
  所有网页的修改日。
- 报告同时给出原始条目数（`page_entries`、`sitemap_entries`）和去重网页
  数（`pages`）。

---

## 5. 限额（在定位点失败，不静默截断）

| 参数 | 默认 | 含义 |
|---|---|---|
| `max_file_bytes` | 256 MiB | 单份地图读取的压缩字节数（读取途中检查） |
| `max_decompressed_bytes` | 512 MiB | 单份地图解压出的字节数（压缩炸弹在此停住） |
| `max_entries_per_map` | 100 000 | 单份地图的 url/sitemap 条目数 |
| `max_maps` | 1 000 | 本次运行实际读取的**不同**地图文件数（共享子图只算一次） |
| `max_depth` | 10 | 引用深度，根为 0 |
| `max_loc_length` | 8 192 | 单个去空白后 `loc` 的字符数 |

限额值非法（0、负数、非整数、未知名称）在开始遍历前报 `LimitsError`。
超限的地图整体拒绝，报告给出尝试的条目号 / 字节数 / 深度。

---

## 6. 命令行

```
# 1) 准备映射（path 相对映射文件所在目录）
cat mapping.json
{
  "files": [
    {"source_url": "https://shop.example.com/maps/root-a.xml", "path": "story-root-a.xml"},
    {"source_url": "https://shop.example.com/maps/shared.xml.gz", "path": "story-shared.xml.gz"}
  ]
}

# 2) 准备根（一行一个，空行与 # 注释忽略；--roots 可重复）
cat roots.txt
https://shop.example.com/maps/root-a.xml
https://shop.example.com/maps/root-b.xml

# 3) 发现并写报告
python3 -m sitemapdiscover discover \
    --roots roots.txt \
    --mapping mapping.json \
    --report report.jsonl \
    --format jsonl \
    --max-depth 8 --max-entries-per-map 50000

# stdout: {"complete": true, "report": "report.jsonl", "counts": {...}, "failures": 0}

# 4) 逐条回放报告里的网页（流式，不整读文件）
python3 -m sitemapdiscover pages --report report.jsonl
```

| 退出码 | 含义 |
|---|---|
| 0 | 发现完整（`complete=true`），报告已写出 |
| 3 | 发现不完整：有失败地图 / 环 / 限额停点；报告已写出且首行 `complete=false` |
| 2 | 配置问题：根或映射文件不可用、重复 `source_url`、非法限额、报告不可写；**不产生报告** |
| 1 | 其他模块错误（向 stderr 输出 JSON） |

JSONL 报告每行一个对象，`type` 依次为 `report`、`root`、`map`、`edge`、
`issue`、`page`；`page` 行含 `page_seq`、`url`、`origin`、汇总 `lastmod`、
`source_count` 与全部 `sources`。`--no-sources` 可省略来源明细（仍保留
`source_count`）。`--format json` 写单个 JSON 文档，其数组同样由游标
逐条产生，不先聚成 Python 列表。

`--cache-dir DIR` 可指定临时 SQLite 的目录；默认每次运行新建临时目录，
结束后删除。模块**从不修改输入文件**（来件、映射、根清单都只读）。

---

## 7. 演练与测试

```
python3 -I demo/run_sitemap_drill.py     # 78 项检查，夹具在 demo/sitemap_fixtures/
python3 -m unittest tests.test_sitemapdiscover -v
```

演练覆盖：双根共享 gzip 地图 + 子索引的发现顺序/来源/汇总日期逐项核对
（预期值在脚本里手工列出）；真环的链；缺来件分支；末尾坏 XML 与 gzip
CRC 错之前读出的网页没有混入而兄弟分支仍在；共享子图只读一次；命名空间
诱饵；跨 origin；重复字段；非法日期；空图；gzip 拼接；各类限额与压缩
炸弹；CLI 成功/不完整/非法参数三种退出码；以及 12 万条短 URL 在
`RLIMIT_AS=96 MiB` 独立进程中的发现与逐条消费（记录输入规模、网页数、
峰值 RSS，并与 1 000 条基线对比）。
