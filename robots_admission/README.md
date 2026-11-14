# robots_admission — 可解释的 robots.txt 抓取准入模块

调度器在**真正发出请求之前**调用本模块：给定完整 HTTP/HTTPS URL、采集器
product token 和带时区的判断时刻，得到 `allow` / `disallow` /
`needs_refresh` 三种结论之一，并附带完整理由（规范化 origin、使用的缓存
表示、有效期限、选中的 User-agent 组、命中规则的原文与行号；未命中时也
有明确原因）。

`needs_refresh` 表示**当前没有可用策略**，调度器必须暂缓该 URL，绝不能把
它当作许可。

- 只依赖 Python 标准库；缓存存放在单个 SQLite 文件中。
- 本模块不做任何网络访问，也不读取机器当前时间：所有时刻都由调用方传入。
- 缓存是判断的唯一权威来源：不读取运行事件账本（eventledger）或 WARC
  抓取事实（warcdb）。

## 1. Python API

```python
from datetime import datetime, timezone
from robots_admission import RobotsCache

cache = RobotsCache("cache.db")

# 用一次取件结果推进缓存（演练/生产都由调用方喂入本地字节）
result = cache.update(
    "http://example.com/robots.txt",      # 目标 origin 的任一 URL
    fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),  # 必须带时区
    status=200,                            # 200 / 304 / 404 / 410 / 429 / 5xx
    ttl_seconds=3600,                      # 非负整数
    body=b"User-agent: *\nDisallow: /x\n", # 原始 robots 字节（可含 BOM）
    etag="v1",                             # 可选校验标识
)
print(result.accepted, result.reason, result.diagnostics)

# 准入判断
d = cache.check("http://example.com/x", "MyBot",
                datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc))
print(d.outcome)        # allow / disallow / needs_refresh
print(d.origin)         # 规范化后的 origin（缓存隔离键）
print(d.cache)          # 使用的缓存表示：kind/status/fetched_at/valid_until/etag
print(d.group_agents, d.selection)   # 选中的 User-agent 组与选择方式
print(d.rule)           # 命中规则：原文、行号、规范化形式、有效长度
print(d.reason)         # 人读理由（未命中任何规则时也有明确原因）
print(d.diagnostics)    # 解析期的行级诊断（被忽略的规则等）
```

辅助接口（均可导入）：

- `parse_robots(body)` — 解析 robots 字节为 `ParsedPolicy`（含诊断）；
- `evaluate(policy, token, target)` — 纯规则层判定，便于单测；
- `normalize_url(url)` — URL → `(Origin, 匹配对象字节)`；
- `parse_origin(url)` — URL → 规范化 `Origin`；
- `render_decision(d)` — 把判断渲染成多行人读文本；
- `cache.origins()` / `cache.failures()` —  introspection（不参与判断）。

## 2. 命令行入口

```
# 喂入一次取件结果（body 来自本地文件或字面文本，绝不访问网络）
python -m robots_admission --db cache.db update --url http://example.com/robots.txt \
    --fetched-at 2026-01-01T00:00:00+00:00 --status 200 --ttl 3600 \
    --etag v1 --body robots.txt

# 单条判断（--text 同时打印人读渲染）
python -m robots_admission --db cache.db check --url http://example.com/x \
    --agent MyBot --now 2026-01-01T00:30:00+00:00 --text

# 批量检查：同一份候选 URL 清单分别以两个采集器身份检查
python -m robots_admission --db cache.db batch --urls candidates.txt \
    --agent AlphaBot --agent GammaBot --now 2026-01-01T00:30:00+00:00

python -m robots_admission --db cache.db origins     # 已有策略的 origin
python -m robots_admission --db cache.db failures    # 429/5xx 刷新失败记录
```

每个命令输出一份 JSON；参数非法时向 stderr 输出 JSON 错误并以退出码 1
结束。`batch` 的 JSON 含每个 URL × 每个 agent 的完整判断与汇总计数。

## 3. robots 规则子集（明确不支持所有私有扩展）

- robots 为 UTF-8 字节，允许开头 BOM；按 CR/LF/CRLF 分行；`#` 之后是注释；
  字段名不区分大小写；不认识的字段静默忽略。
- 相邻 `User-agent` 行属于同一组；遇到 `Allow`/`Disallow` 进入该组规则
  部分，之后的 `User-agent` 开始新组；空行不会拆开相邻 `User-agent`。
- 空 `Allow`/`Disallow` 不构成规则；非空规则必须以 `/` 开头，否则忽略并
  给出行级诊断；含非法百分号转义的规则同样忽略并给出诊断。
- 采集器参数是单个非空 ASCII product token：组内非 `*` token 以不区分
  大小写的子串方式出现在参数中即匹配；取最长 token 的组，同长度的组
  全部合并；只有没有任何具体 token 匹配时才用 `*` 组，`*` 的限制绝不
  混入具体采集器的规则。
- 匹配对象是 URL 的 path（空 path 视为 `/`）加上存在时的 `?query`；
  fragment 不参与；路径大小写敏感；`*` 匹配任意长度，末尾 `$` 锚定到
  匹配对象末尾，否则按前缀匹配。
- URL 与规则做相同的字节规范化：非 ASCII 字符 UTF-8 后逐字节转大写
  百分号编码；已编码的非保留 ASCII（字母数字 `-._~`）解码；其他百分号
  编码保留并统一十六进制大写。`%2F` 不会变成路径分隔符，`%3F` 不会
  变成查询分隔符；保留下来的百分号编码按三个 ASCII 字节参与匹配与
  长度计算。含残缺/非十六进制转义的 URL 被拒绝。
- 多条命中时按规范化后去掉 `*` 和末尾 `$` 的字节数取最长，同长度
  `Allow` 优先——与规则在文件中的先后无关。

## 4. 缓存语义

- **origin 隔离**：scheme 小写、主机名小写（Unicode 域名经 IDNA 转
  ASCII、IPv6 字面地址归一化）、http:80 与 https:443 端口省略，其余
  端口保留。http 与 https、不同非默认端口互不共享缓存。含用户名密码、
  无主机、非法端口、非 http/https 的 URL 一律拒绝。
- **有效期**：`fetched_at <= now < fetched_at + ttl_seconds`；到期的那
  一刻即返回 `needs_refresh`。
- **200**：建立或替换该 origin 的规则策略；空文件有效（无路径限制）。
  无效 UTF-8、超过 512 KiB 的内容会被拒绝，现有策略不变。
- **404/410**：建立有到期时间的无规则表示（视为不限制）。
- **304**：仅在已有 200 表示且调用方 ETag 与其一致时刷新有效期，沿用
  原 robots 字节与规则；无前件或标识不符一律拒绝，不凭空生成许可。
- **单调性**：每个 origin 的更新 `fetched_at` 必须严格晚于上一次已接受
  的取件结果，否则拒绝且缓存不变（防止迟到刷新覆盖新策略）。
- **429/5xx**：只记录此次刷新失败（`failures` 表，仅供观察），不延长
  旧策略有效期、不删除旧表示、不推进单调时钟；旧表示仍在有效区间时
  按旧规则判断，已过期或不存在时返回 `needs_refresh`；下一次更晚的
  有效结果可恢复服务。
- **其他状态码**：拒绝。
- 非法时刻（无时区）、负 ttl、不合类型的参数都会抛错，且不会改动缓存。
- 每次取件结果的时间检查、解析与缓存替换在一个 SQLite 事务中完成；
  数据库重开后判断依据不变。

## 5. 离线验收演练

```
python3 -I demo/run_robots_drill.py
```

演练不访问网络、不读机器时钟：所有 robots 取件结果由脚本以本地字节喂入。
覆盖：两个具体 agent 与 `*` 的分流、同长度组合并与 Allow 平局优先、带
查询的 `$`/`*`、`%7E` 与 `~` 等价而 `%2F` 与 `/` 不等价、中文路径与其
UTF-8 编码等价、默认端口与大小写主机共享策略但跨 scheme/端口不共享、
IPv6、200 后正确 304 刷新且规则不变、到期点/429/5xx/下一次有效更新的
逐步判断、无前件 304、ETag 不符、迟到更新、坏编码与超限内容下旧策略
保持可用、批量入口双身份检查、以及新进程重开数据库核对同一判断。每个
判断都打印真实理由与命中行号，并与独立硬编码预期比对（共 150+ 项断言）。

回归测试（需要 pytest）：

```
python3 -m pytest tests/test_robots_admission.py -q
```
