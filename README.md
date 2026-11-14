# archivestore — 本机档案加密存储模块

把采集到的大型原始字节档案加密后放到不可信的共享磁盘上。模块直接保管采集
原件的字节，不解释文件内的业务字段，不提供云服务或账号界面。只使用
`cryptography` 的 AES-256-GCM，不自行设计加密算法。

四个操作：**seal**（收件加密）、**read-range**（局部读取）、**verify**
（全档校验）、**rotate**（保管密钥轮换），同时提供可导入的 Python API
和等价的命令行界面。

```
archivestore/      可导入模块
demo/              可复做的端到端演练（run_demo.py）+ 独立参考解密器（refdec.py）
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
python3 -m pytest tests/ -q      # 16 个回归测试
python3 demo/run_demo.py         # 端到端演练（约 1 分钟，工作区保留在 /tmp 下）
```

演练覆盖：两份含零字节原件 + 一个空原件；新进程读首段/跨块段/尾段并与原
字节切片比较；独立参考解密器 `demo/refdec.py`（不调用正式读函数）核对格式；
同档案换块、跨档案挪块、改长度、截短、错密钥分别失败；未访问块改坏时局部
读取仍可用而 verify 报错；轮换前后正文与描述摘要一致（未重写大数据）、仅
新密钥可读回；在轮换提交两侧分别 SIGKILL 后重开只见全旧/全新；已解封读者
跨过轮换继续读；64 MiB 大原件的峰值内存与窄范围真实 I/O 受块长 B 支配
（B=1MiB 时 seal 峰值 RSS ≈33 MiB，B=8MiB 时 ≈68 MiB，与总档案长度无关）。
