# Aily Coder Libraries

这个仓库以 `repositories.txt` 为唯一库清单，直接检查各 Git 仓库的 tag，自行生成库包和
`libraries-coder-index.json`。整个过程不读取 Arduino 官方索引，也不下载 Arduino
官方 ZIP；索引中的元数据、包大小和校验值都由本项目从对应 tag 独立生成。

ZIP 同时写入 RustFS 与 Cloudflare R2 各自独立的 package bucket；同步状态只写入
Cloudflare R2 的 package bucket。最终索引写入两端名为 `ailyblockly` 的 bucket 根目录，
文件名都为 `libraries-coder-index.json`，但其中的下载 URL 分别指向对应存储。
`ailyblockly` 中不存放状态或其他内部文件。

## 同步流程

```text
repositories.txt
       │ 规范化 URL，并按持久化 cursor 选取本轮仓库
       ▼
git ls-remote --tags
       │ 识别新增 tag，并解析到确定的 commit
       ▼
读取 tag 根目录的 library.properties
       │ 校验字段、库名和版本
       ▼
从该 commit 生成确定性 ZIP，计算 size 和 SHA-256
       ├──────────────► RustFS package bucket
       └──────────────► Cloudflare R2 package bucket
       │ 两端包内容均校验成功
       ▼
生成并写入同步 state ──────► Cloudflare R2 package bucket/.state/
       │ R2 state 校验成功
       ├──────────────► 生成 RustFS URL 索引 ──► RustFS ailyblockly bucket
       └──────────────► 生成 R2 URL 索引 ──────► R2 ailyblockly bucket
```

发布顺序固定为：双端 package → Cloudflare R2 state → 两端各自的 index。索引不会引用
尚未在两端都确认存在的 ZIP。状态保存扫描位置、待重试仓库、tag OID、已生成版本和上一次
状态摘要；state 写入或校验失败时，本轮不会继续发布索引。

## 字段来源

索引条目按以下规则生成：

| 索引字段 | 来源 |
| --- | --- |
| `name`、`version`、`author`、`maintainer`、`sentence`、`paragraph` | tag 根目录的 `library.properties` 同名字段 |
| `website` | `library.properties` 的 `url` |
| `category`、`architectures` | `library.properties` |
| `dependencies` | `library.properties` 的 `depends` |
| `providesIncludes` | `library.properties` 的 `includes` |
| `license` | `library.properties` 的可选 `license` |
| `repository` | `repositories.txt` 中的仓库 URL |
| `types` | 本项目固定约定 `['Arduino']`，输出为 JSON 数组 `["Arduino"]` |
| `archiveFileName` | 规范化后的库名与版本组成的 ZIP 文件名 |
| `url` | 对应存储的 `RUSTFS_PUBLIC_DOWNLOAD_BASE_URL` 或 `R2_PUBLIC_DOWNLOAD_BASE_URL` 后追加 `archiveFileName` |
| `size`、`checksum` | 本项目生成 ZIP 后计算；checksum 使用 SHA-256 |

只有符合库结构和元数据规则的 tag 才会进入索引。同一仓库的已发布库名会锁定，后续 tag
不能改名。

## 对象路径与不可变规则

package、state 和 index 的精确位置为：

```text
RustFS <PACKAGE_BUCKET>/
└── <archiveFileName>

Cloudflare R2 <PACKAGE_BUCKET>/
├── <archiveFileName>
└── .state/aily_coder_library_state.json

RustFS ailyblockly/libraries-coder-index.json
Cloudflare R2 ailyblockly/libraries-coder-index.json
```

package bucket 必须是新建的独立 bucket，不能使用 `ailyblockly`。RustFS 与 Cloudflare R2
的 package bucket 名称可以不同。包直接存放在各自 package bucket 的根目录；同步 state
只存放在 Cloudflare R2 package bucket 的 `.state/` 下。两端 `ailyblockly` 中都只存放
根目录的最终索引 JSON。两个索引的对象名相同，除 `url` 指向各自 package 下载入口外，
库条目内容相同。

扁平路径意味着 `archiveFileName` 是全局唯一键，因此同步器采用严格的不可覆盖规则：

- 已有对象的大小或 SHA-256 与候选包不同，视为文件名碰撞并拒绝覆盖；
- 两个库产生相同库名和版本，或产生相同文件名但内容不同，不会覆盖先前包；
- 已发布 tag 如果被移动到其他 commit，保留原包和原索引条目，不使用变更后的 tag 覆盖；
- tag 消失不会自动删除已经发布的版本和 ZIP。

## GitHub Actions 配置

在仓库 Settings → Secrets and variables → Actions 中配置：

| 类型 | 名称 | 说明 |
| --- | --- | --- |
| Variable | `RUSTFS_PUBLIC_DOWNLOAD_BASE_URL` | 必填；RustFS package bucket 根目录的公开下载基址 |
| Variable | `R2_PUBLIC_DOWNLOAD_BASE_URL` | 必填；R2 package bucket 根目录的公开下载基址 |
| Variable | `MAX_REPOSITORIES_PER_RUN` | 每轮最多扫描的仓库数，默认 `250`；`0` 表示扫描本周期全部剩余仓库 |
| Variable | `SCAN_WORKERS` | 并行扫描和上传数，默认及上限均为 `4` |
| Secret | `RUSTFS_ENDPOINT` | RustFS S3 endpoint |
| Secret | `RUSTFS_ACCESS_KEY_ID` | RustFS access key ID |
| Secret | `RUSTFS_SECRET_ACCESS_KEY` | RustFS secret key |
| Secret | `RUSTFS_PACKAGE_BUCKET` | RustFS 新建的 package bucket，不能是 `ailyblockly` |
| Secret | `R2_ACCOUNT_ID` | Cloudflare account ID |
| Secret | `R2_ACCESS_KEY_ID` | Cloudflare R2 access key ID |
| Secret | `R2_SECRET_ACCESS_KEY` | Cloudflare R2 secret key |
| Secret | `R2_PACKAGE_BUCKET` | Cloudflare R2 新建的 package bucket，不能是 `ailyblockly` |

例如两端 package bucket 的公开根地址分别如下时，可配置：

```text
RUSTFS_PUBLIC_DOWNLOAD_BASE_URL=https://rustfs-packages.example.com
R2_PUBLIC_DOWNLOAD_BASE_URL=https://r2-packages.example.com
```

程序分别在两个值后追加 `/Example-1.0.0.zip`，生成内容不同的两份索引，再以相同对象名
`libraries-coder-index.json` 上传到各自的 `ailyblockly` bucket。两个公开下载基址必须不同。
S3 API endpoint 不能作为公开下载地址；公开读取和自定义域名由 bucket/CDN 策略负责，
上传程序不设置 ACL。

运行 Action 前必须先在两端创建 package bucket。两套凭据都需要：

- RustFS package bucket 只需对根目录包对象的 Head/Get/Put 权限；
- Cloudflare R2 package bucket 需要对根目录包对象及
  `.state/aily_coder_library_state.json` 的 Head/Get/Put 权限；
- `ailyblockly` bucket 只需对根目录 `libraries-coder-index.json` 的 Head/Get/Put
  权限。

RustFS region 默认 `us-east-1`，Cloudflare R2 region 默认 `auto`。本地运行可用
`RUSTFS_REGION`、`R2_REGION` 覆盖，也可用 `R2_ENDPOINT` 代替 `R2_ACCOUNT_ID` 派生的
endpoint。程序兼容 `RUSTFS_ACCESS_KEY` 和 `RUSTFS_SECRET_KEY` 凭据别名。

## 首次同步与续传

首次同步需要检查近万个仓库及其历史 tag，因此 GitHub Action 按
`MAX_REPOSITORIES_PER_RUN` 分批运行，并通过 `aily_coder_library_state.json` 中的 cursor
跨任务续传。`SCAN_WORKERS` 只控制本轮并发量，不会让多个任务同时发布共享状态；workflow
的 concurrency 配置会串行化运行。

bootstrap 只有在完整走完 `repositories.txt` 且失败重试队列清空后才结束；在此之前只持续
上传已验证包并更新 R2 state，不发布首份公开索引。扫描失败的仓库会在后续任务中优先重试；
连续 3 个同步轮次（每轮内部会立即尝试两次）仍失败时，本轮将其视为已评估，避免单个长期
不可用仓库永久阻塞首份索引，进入稳态巡检后仍会周期性重试。完成首轮全量评估后才发布
索引；之后每轮将新发现且已经双端确认的版本增量加入索引。正式 workflow 每 6 小时运行
一次，也支持手动指定本轮仓库数和并发数。

为避免第三方仓库耗尽 Action 临时盘，同步器把单个 tag 的解压源码限制为 512 MiB，并分别
把单仓库累计 Git 对象和本轮候选 ZIP 限制为 512 MiB；`git fetch` 与 `git archive` 在运行
过程中持续检查并越限终止。逻辑批次再按最多 4 个 worker 分窗口执行，每个窗口完成包上传
并释放临时目录后才开始下一个窗口，因此临时文件不会随本轮 250 个仓库持续累积。

## 本地检查

安装项目后运行测试，并显式禁止生成 Python bytecode 缓存：

```shell
python -m pip install -e .
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
```

PowerShell 使用：

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m unittest discover -s tests -v
```

不配置对象存储凭据也可以扫描少量仓库，并在本地生成两份索引：

```powershell
$env:PYTHONPATH = "src"
python -m aily_coder_libraries.sync `
  --dry-run `
  --max-repositories 2 `
  --workers 2 `
  --rustfs-public-download-base-url https://rustfs-packages.example.invalid `
  --r2-public-download-base-url https://r2-packages.example.invalid `
  --output-directory dist/dry-run
```

输出文件为：

```text
dist/dry-run/rustfs/libraries-coder-index.json
dist/dry-run/r2/libraries-coder-index.json
```

dry-run 不访问 RustFS/R2；两个占位域名只写入各自 JSON 的 `url`。扫描时生成的 ZIP 只用于
计算大小和 SHA-256，结束后随临时目录删除。
