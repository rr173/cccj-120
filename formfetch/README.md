# formfetch — 表单取数模块（导出页表单 → 正确 HTTP 请求 → 数据文件）

有些公开数据站没有可直接复制的下载链接：研究人员要先打开导出页，选地区
和观测种类，必要时上传一份查询编号清单，再点某个“导出”按钮；隐藏字段
和会话 Cookie 必须随这次提交一起带回去。本模块把页面上的控件状态变成正
确的 HTTP 请求，并把站点返回的数据文件取回来。

本模块只做**可复用的表单会话与请求构造**：不做网址调度、重试队列、页面
版本库（那些在仓库别的模块或不在范围内）。只用 Python 标准库；不执行
JavaScript，不启动浏览器，不模拟登录或验证码。

```
htmldoc.py   严格 HTML 子集解析（边界确定，不猜）
forms.py     表单/控件模型：归属、禁用、成功控件、修改、目标解析
encode.py    urlencoded / multipart 字节构造（流式、长度精确）
session.py   Cookie 会话（http.cookiejar），GET 页面与提交，不追跳转
api.py       fetch_page / prepare_submission / submit 三步编排
__main__.py  JSON 配置的命令行
testsite.py  本机演示导出站（仅 127.0.0.1，演练用）
```

## 1. 一条能跑到底的使用方式

```python
from formfetch import FormSession, fetch_page, prepare_submission, submit

session = FormSession()                      # 内存 Cookie 会话
page = fetch_page(session, "http://127.0.0.1:8000/export")   # GET 页面
prepared = prepare_submission(               # 纯构造，不发任何请求
    page, "export-form",                     # form 的 id
    modifications={"region": "north",        # 要改的控件 id -> 新状态
                   "kinds": ["opt-temp"],
                   "upload": ["/data/ids.txt"]},
    submitter_id="btn-download")             # 要点击的按钮 id（可省略）
print(prepared.describe())                   # 检查：有序成功控件、目标、
                                             # 方法、编码（密码已掩码）
result = submit(session, prepared, "/data/export.bin")       # 才发送
print(result.sha256, result.bytes_written)
```

- `fetch_page` 是唯一在检查前触网的步骤（GET 页面，Cookie 留在会话里）。
- `prepare_submission` **纯构造**：应用修改、解析提交按钮与目标、算出有序
  成功控件清单。它不做任何网络 I/O——调用方可以停在这里，检查
  `prepared.entries`（有序）、`prepared.url`、`prepared.method`、
  `prepared.enctype`，再决定是否发送；这一步绝不偷发提交请求。
- `submit` 只发**一次**请求，成功（2xx）才把原始字节交付到结果文件。

命令行用一份 JSON 表达同样的选择：

```json
{
  "page_url": "http://127.0.0.1:8000/export",
  "form_id": "export-form",
  "submitter_id": "btn-download",
  "modifications": {"region": "north", "kinds": ["opt-temp"],
                    "upload": ["/data/ids.txt"]},
  "output": "/data/export.bin"
}
```

```
python -m formfetch config.json            # 取页 + 提交
python -m formfetch --preview config.json  # 停在提交前，只打印预览
```

预览（有序成功控件清单、目标地址、方法、编码）在任何提交之前打印；密码
控件的值一律以 `••••••` 掩码，GET 目标的 URL 里同样掩码。

## 2. 支持的 HTML 子集（边界确定，不猜）

- 标签必须正确嵌套；需要闭合的元素必须显式闭合（`<option>` 要写
  `</option>`）。空元素（`input`、`base`、`br` 等）不得有结束标签，
  `<input ...>` 与 `<input ... />` 都接受。
- `<form>` 不得嵌套；`<textarea>` 内只能是文本（无元素、无注释）；
  `<select>` 内只能是 `option`/`optgroup`，`optgroup` 内只能是 `option`。
- 文本和属性中的字符实体正常解码（`&amp;`、`&#33;` 等）。
- 越出子集 → `HtmlSubsetError`（带行号），不猜一个可提交结果。

## 3. 控件归属（不只靠 DOM 包含）

- 支持 `input`、`textarea`、`select`、`button` 四种控件。
- 控件带显式 `form` 属性时优先：指向存在的 form 元素即归属该 form；指向
  不存在的 id、指向非 form 元素、或 `form=""`，则**不属于任何表单**。
- 无 `form` 属性时归最近的 `form` 祖先；放在 form 外且不带 `form` 属性的
  控件不属于任何表单。
- 参数按**整份文档的控件出现顺序**生成；关联控件即使出现在 form 前面也
  保留这个次序。
- 目标 form 的 id 必须唯一；修改或点击用到的控件 id 也必须唯一且属于该
  form——不会误改另一个表单里的同名控件（`FormStructureError`）。

## 4. 成功控件规则

- 无 `name` 或空 `name` 不发送；普通文本、hidden、textarea 包含空值
  （空字符串照发）；`readonly` 照发。
- `disabled` 控件不发送；`disabled fieldset` 的后代不发送，但它的**第一
  个直接 `legend`** 里的后代不受这个 fieldset 禁用；嵌套 fieldset 各自
  判定。
- checkbox/radio 只有选中才发送，缺省 `value` 为 `on`（`value=""` 则发空
  串）。radio 按（所属表单， name）分组；页面里多个 `checked` 时取文档中
  最后一个。
- select：单选无显式 `selected` 时取首个可用 option；多选无选中项时不发
  送；单选出现多个 `selected` 取最后一个可用项；option 无 `value` 时用其
  文本（去首尾空白）。option 自身或其 `optgroup` 带 `disabled` 即为禁用
  项。重复参数名、相同值的不同 option 都按次序逐项保留，绝不用字典合并。
- 只有本次实际点击的 submit 按钮计入参数（有非空 name 时）；其他
  submit/reset/button 一律不发送；不点按钮直接提交也可以（不带任何按钮
  值）。`input type=image` 不支持（需要点击坐标），点击它会得到
  `UnsupportedFeatureError`。

## 5. 修改配置（按控件 id，不是按参数名）

- 文本类（text/hidden/password/search/tel/url/email/number/textarea 及未
  知 input 类型，未知类型按 HTML 规则退化为 text）给**字符串**。
- checkbox/radio 给**布尔值**：radio 置 true 会取消同组其余 radio，置
  false 只取消自己；同一份配置里同组两个 true 在提交前报错。
- select 给**要选中的 option 的 id 列表**（替换语义）：不存在的 option、
  选中禁用 option、单选给多项，都在提交前报错；给空列表 = 清空选择
  （单选随后回落到“首个可用项”，多选则不发送）。
- file 给**本地文件路径列表**：非 `multiple` 不能给多文件；文件必须存在
  且可读（提交前检查）。未选文件（空列表）与选了零字节文件是两种状态：
  前者发空 filename 的空部件，后者发带文件名的空正文。
- 不能借修改绕开原控件的禁用、所属表单或类型：禁用控件、别的表单的控
  件、类型不符的修改一律在提交前报 `ModificationError`；按钮只能“点
  击”（`submitter_id`），不能被修改。
- 密码控件按文本处理，但预览和错误信息不回显它的值。
- 未支持的客户端表单校验（`required`、`pattern`、`min/max` 等）不仿造：
  模块不替浏览器做校验，文档在此明确说明。
- 修改先整体校验再应用；任何一项失败，整次 prepare 失败且页面模型不留
  半截状态。

## 6. 目标、方法、编码

- 相对 action 用**整页第一个** `<base href>` 解析（相对 base 先对页面
  URL 解析）；action 缺省或为空时回到本次页面 URL。
- 选中的 submitter 可用 `formaction`/`formmethod`/`formenctype` 覆盖
  form 的设置。方法限 GET/POST（`dialog` 等报错）；编码限
  `application/x-www-form-urlencoded` 与 `multipart/form-data`
  （`text/plain` 报错）。
- GET 用有序成功控件**重建**目标查询串：替换 action 原 query，不改路径；
  POST 保留 action 的 query。
- urlencoded：UTF-8、空格编码为 `+`、重复键按次序逐项保留。
- 所有字符串值的换行在编码时统一成 CRLF（textarea 及其他字符串一样），
  不凭空删掉空字符串。
- 文件上传只支持 POST multipart；GET 或 urlencoded 遇到选中文件时明确拒
  绝（`UnsupportedFeatureError`）。

## 7. multipart 与文件名约定

- multipart 真的包含各文本项和文件项（不是只发文件路径）；文件项在控件
  位置展开，多文件顺序与配置一致。
- `filename` 为文件 basename；正文保持任意二进制字节（含 CRLF、零字节、
  空文件）。
- **文件名编码约定**：`name` 与 `filename` 以原始 UTF-8 字节放入引号
  串，仅对 `"` 和 `\` 做反斜杠转义，不使用 RFC 5987 `filename*`；接收端
  按 UTF-8 解码还原。参数名或文件名含 CR/LF 一律拒绝。
- 文本部件不带 Content-Type；文件部件固定
  `Content-Type: application/octet-stream`。
- 标准边界与 CRLF，末尾有结束边界；Content-Length 由与实际发送完全相同
  的段列表求和，长度与线上字节一致。文件在发送时以 64 KiB 块流式读取，
  整个上传文件不会驻留内存（演练 step 11 实测：192 MiB 文件，发送进程峰
  值 RSS 约 29 MiB）。boundary 默认随机，也可传入固定值以便复现。

## 8. 会话、失败语义、上限

- 会话 Cookie 由 `http.cookiejar.CookieJar` 按域、路径、Secure 规则投
  递，不手工把所有 Cookie 拼给任意地址（演练 step 6 验证路径隔离与
  Secure 不在纯 HTTP 下回传）。
- 页面、配置、上传文件或目标方法有任何问题，都在**发出提交请求之前**失
  败（演练 step 2/8 用服务器请求计数证明零网络副作用）。
- 3xx 与非 2xx 都清楚报失败（`ResponseStatusError`，带状态与片段），不
  自动追跳转、不重新提交；已收到的错误页只作诊断片段，绝不作为导出数
  据文件交付。
- 上限（`Limits`，均可配）：页面 `page_bytes`（默认 16 MiB）、上传合计
  `upload_bytes`（默认 1 GiB）、结果 `result_bytes`（默认 256 MiB）。
- 结果边读边写到结果目录旁的临时文件，成功才 `os.link` 到位；失败不留
  完整结果、不覆盖已有文件（已存在的结果路径在发请求前就拒绝），临时
  文件必清理。

## 9. 错误类型

`FormFetchError` 为基类：`ConfigError`（配置）、`NetworkError`（传
输）、`ResponseStatusError`（状态）、`SizeLimitError`（上限）、
`PageDecodeError`（页面解码）、`HtmlSubsetError`（HTML 子集）、
`FormStructureError`（id/归属/控件种类）、`ModificationError`（修
改）、`UnsupportedFeatureError`（明确不支持）、`EncodeError`（编
码）、`OutputFileError`（结果路径）。CLI 对已知错误退出码 2。

## 10. 复做整次取数（接手演练）

```
python3 -I demo/run_formfetch_drill.py
```

脚本启动仅监听 127.0.0.1 的演示导出站（会话 Cookie + 隐藏票据；保存真
实请求行、头和原始正文；用**独立**的流式 multipart 解析器解析后才返回
预先生成的数据文件），并跑完整故事：

1. 取页 + 纯构造：有序成功控件清单、目标、方法、编码对硬编码期望逐项断
   言；构造期间服务器没收到任何提交。
2. 19 种无效修改 + 7 种无效提交按钮：全部提交前报错，服务器请求数不变。
3. multipart 导出：位于 form 前后的关联控件、另一个表单的同名字段、重
   复 name、readonly、空字符串、fieldset 的 legend 例外、嵌套禁用、多
   选、radio 切换、两个上传（含零字节与 CRLF 的查询清单、一个空文件）
   加一个未选文件控件。服务器独立解析的有序参数与期望一致；接收端逐字
   节比对上传文件；交付数据文件核对 SHA-256。
4. 第二个 submitter 覆盖为 GET：查询串整体替换、路径不变。
5. 第三个 submitter 覆盖目标与编码：POST urlencoded，action 的 query 保
   留。
6. Cookie 路径隔离与 Secure 约束。
7. 过期票据（403）、跳转（302）、超限结果：都不产生结果文件。
8. 已存在的结果路径在发请求前拒绝。
9. 畸形页面（嵌套 form、标签不匹配、重复 form id）。
10. CLI 三种配置（仅预览 / 完整取数 / 错误配置退出码 2）。
11. 192 MiB 上传在子进程中发送：记录文件大小与发送进程峰值 RSS，证明没
    有整文件驻留。

产物在 `demo/formfetch_out/`：`expected_params.json`（三条流程的有序期
望参数）、`uploads_manifest.json`（上传文件尺寸与摘要）、`server/`（站
点保存的原始请求 `req-*-raw.bin`、独立解析回执 `receipt-*.json`、收到
的文件部件）、`results/`（交付的数据文件）、`assertions.log`（全部断
言输出）。

单元测试（纯规则，无网络或仅本机回环）：`tests/test_formfetch.py`。

## 11. 明确不做的事

- 不执行 JavaScript、不启动浏览器、不模拟登录或验证码。
- 不仿造客户端表单校验（required/pattern/min/max 等），见第 5 节。
- 不追 3xx、不重试、不做网址调度与页面版本库。
- 不支持 `input type=image`（点击坐标）、`text/plain`、`dialog` 方法、
  GET/urlencoded 下的文件上传。
- textarea 首换行不做浏览器式剥除（子集要求页面自己写准）；文件部件的
  Content-Type 固定为 application/octet-stream。
