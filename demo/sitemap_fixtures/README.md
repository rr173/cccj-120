# sitemapdiscover 演练夹具

`run_sitemap_drill.py` 会在每次运行时把静态来件复制到一个全新的临时工作区
（映射文件与报告都写在那里），并另外生成大批短 URL 地图。仓库里的这些文件
只是 **输入样本**，从不被修改；所有预期值（发现顺序、来源、汇总日期、
失败分类）都手工列在演练脚本里，不由正式遍历器反向生成。

| 文件 | 用途 |
|---|---|
| `story-root-a.xml` | 根索引 A：共享 gzip 地图 → 子索引 |
| `story-root-b.xml` | 根索引 B：共享 gzip 地图 |
| `story-shared.xml.gz` | 两张根共享的 gzip urlset，含跨地图重复网页 |
| `story-sub.xml` | 第二层索引/子地图：重复网页 + 新网页 + 不同 lastmod |
| `demo-roots.txt` | 演示用根顺序 |
| `demo-mapping.json` | source_url → 本地文件映射（path 相对映射文件目录）；gzip 地图按魔数识别 |
| `full-roots.txt`, `full-mapping.json` | 失败/边界演练的根与映射 |
| `full-*.xml` / `full-*.xml.gz` | 失败、环、命名空间诱饵、跨 origin、限额来件 |

本目录只放纯文本/XML/gzip 输入；不含可执行脚本。批量地图由演练脚本生成到
临时目录，不进入仓库。
