# 部署与维护

本文面向仓库维护者和自部署者。一般使用和贡献流程见 [README](README.md)。

## 对象存储布局

部署前分别创建 RustFS 和 Cloudflare R2 的 package bucket，并确认两端固定的索引 bucket
`ailyblockly` 已存在。package bucket 名称可以不同，但不能使用 `ailyblockly`。

```text
RustFS <PACKAGE_BUCKET>/
└── libraries/
    └── <archiveFileName>

Cloudflare R2 <PACKAGE_BUCKET>/
├── libraries/
│   └── <archiveFileName>
└── .state/aily_coder_library_state.json

RustFS ailyblockly/libraries-coder-index.json
Cloudflare R2 ailyblockly/libraries-coder-index.json
```

库包的对象前缀固定为 `libraries/`，当前不支持自定义 package bucket 内的库文件路径。
最终索引仍存放在 `ailyblockly` bucket 根目录；同步状态仍只写入 R2 package bucket 的
`.state/` 下。两个索引除包下载 `url` 外内容相同。

### 从旧包路径升级

旧版本将 ZIP 存放在 package bucket 根目录，当前版本改用 `libraries/` 前缀。已有部署必须
先在 RustFS 和 R2 中把每个根目录 ZIP 复制到 `libraries/<archiveFileName>`，保持内容与
SHA-256 元数据不变，并确认两端对象完整后再运行新版同步。同步器不会为已处理 tag 自动
回填新对象路径。迁移期间保留原对象，避免旧索引失效。

### R2 state 必须保持私有

R2 package bucket 同时包含公开的 `libraries/*.zip` 和内部 `.state/*`。
`R2_PUBLIC_DOWNLOAD_BASE_URL` 必须指向 bucket 根入口；程序会自动在下载 URL 中追加
`/libraries`。在该根入口后请求 `/.state/aily_coder_library_state.json` 时，必须返回
`403` 或 `404`。

若通过自定义域名实施访问控制，应关闭 `r2.dev` 公共开发地址，否则 bucket 仍可从该地址
直接访问。使用 R2 Custom Domain 配合 WAF，或使用 Worker，仅允许需要公开的
`libraries/*.zip`。部署后应通过所有公开域名实际请求上述 state 路径，确认无法读取。
上传程序不会设置 ACL，也不会检查对象是否可从公网访问。

相关 Cloudflare 文档：

- [R2 public buckets](https://developers.cloudflare.com/r2/buckets/public-buckets/)
- [使用 WAF 控制 R2 访问](https://developers.cloudflare.com/cache/interaction-cloudflare-products/waf-snippets/)
- [在 Worker 中访问 R2](https://developers.cloudflare.com/r2/api/workers/workers-api-usage/)

## GitHub Actions 配置

workflow 当前使用自托管的 macOS ARM64 runner。运行前需注册满足 `runs-on` 全部标签的
runner，或按自己的环境调整该配置；runner 需安装 Git。具体标签以 workflow 为准，避免在
部署文档中重复环境专用名称。

在仓库 Settings → Secrets and variables → Actions 中配置：

| 类型 | 名称 | 说明 |
| --- | --- | --- |
| Variable | `RUSTFS_PUBLIC_DOWNLOAD_BASE_URL` | 必填；RustFS package bucket 的公开根入口 |
| Variable | `R2_PUBLIC_DOWNLOAD_BASE_URL` | 必填；受保护的 R2 package bucket 根入口 |
| Variable | `MAX_REPOSITORIES_PER_RUN` | 可选；每轮最多扫描的仓库数，默认 `250` |
| Variable | `SCAN_WORKERS` | 可选；并行扫描和上传数，默认及上限为 `4` |
| Secret | `RUSTFS_ENDPOINT` | RustFS S3 endpoint |
| Secret | `RUSTFS_ACCESS_KEY_ID` | RustFS access key ID |
| Secret | `RUSTFS_SECRET_ACCESS_KEY` | RustFS secret key |
| Secret | `RUSTFS_PACKAGE_BUCKET` | RustFS package bucket |
| Secret | `R2_ACCOUNT_ID` | Cloudflare account ID |
| Secret | `R2_ACCESS_KEY_ID` | Cloudflare R2 access key ID |
| Secret | `R2_SECRET_ACCESS_KEY` | Cloudflare R2 secret key |
| Secret | `R2_PACKAGE_BUCKET` | Cloudflare R2 package bucket |

例如：

```text
RUSTFS_PUBLIC_DOWNLOAD_BASE_URL=https://rustfs-packages.example.com
R2_PUBLIC_DOWNLOAD_BASE_URL=https://r2-packages.example.com
```

两个公开下载基址必须不同，且都不要手动追加 `/libraries`；程序会自动追加该固定前缀。
这里应填写能够匿名读取 `libraries/*.zip` 的 bucket/CDN 根入口，不要默认使用上传所需的
S3 API endpoint。公开读取、自定义域名和访问控制由 bucket/CDN 策略负责。

## 最小权限

同步器只执行以下对象操作：

- RustFS package bucket：`libraries/*.zip` 的 Head/Get/Put；
- R2 package bucket：`libraries/*.zip` 及 `.state/aily_coder_library_state.json` 的
  Head/Get/Put；
- 两端 `ailyblockly` bucket：根目录 `libraries-coder-index.json` 的 Head/Get/Put。

代码不会请求删除或列出 bucket。实际凭据应按存储平台支持的最小粒度限制到上述 package
和 index bucket；Cloudflare R2 可使用仅作用于这两个 bucket 的
[`Object Read & Write`](https://developers.cloudflare.com/r2/api/tokens/) token。RustFS
region 默认为 `us-east-1`，R2 region 默认为 `auto`。本地部署可用
`RUSTFS_REGION`、`R2_REGION` 覆盖，也可以用 `R2_ENDPOINT` 代替由 `R2_ACCOUNT_ID` 派生的
endpoint。RustFS 凭据兼容 `RUSTFS_ACCESS_KEY` 和 `RUSTFS_SECRET_KEY` 别名。

## 首次同步与续传

同步器按批次扫描 `repositories.txt`，并通过 R2 中的持久状态跨任务续传。首次完整评估库
清单且待重试队列清空前，只上传已验证包并更新状态，不发布首份公开索引。完成首次评估后，
首次评估每个库时只发布当时最高的有效语义版本；后续发现更高版本时增量加入索引，
已有版本和 ZIP 保持不变。

扫描器先读取新或变化 tag 的 `library.properties`，同步器完成版本及冲突判断后，仅为
最终可发布的最高版本生成 ZIP。未选中的 tag 会写入 state 以避免后续重复处理，但不会
执行源码归档和 ZIP 压缩。

若要从头执行这套首次同步规则，应先删除两端 package bucket 的 `libraries/*`，并删除
R2 package bucket 的 `.state/aily_coder_library_state.json`，再运行 `full_bootstrap`。
不要清空整个 bucket；同步器本身不会执行这些删除操作。首次 bootstrap 完成后，后续
增量发布的旧版本、ZIP 和索引条目均继续保留。

首次部署时，可在 Actions 页面手动运行 `Sync library index`，并勾选 `full_bootstrap`。
该选项会在同一个自托管 runner job 中按 `max_repositories`（默认 `250`）自动循环；每批由
独立进程处理并持久化 state，然后从最新 cursor 继续。仅该次手动任务的超时会放宽到
48 小时。任务被取消或 runner 中断时，后续运行会从最后一个成功批次继续。待重试仓库也
会在循环中按现有规则处理；完整清单评估完成且重试队列清空后，才发布首份索引。后续定时
任务没有该手动输入，每次仍只处理一个批次。

每个对象存储请求已配置 SDK 的 standard 重试模式（`max_attempts=5`）；若同步进程仍因
瞬时故障失败，workflow 会等待 15 秒、30 秒，最多执行 3 次完整同步。首次 bootstrap 的
批次续跑状态码 `75` 不计为故障，也不会触发这层重试。3 次均失败时任务才以最后一次
状态码退出。

workflow 支持定时和手动运行，并通过 concurrency 配置避免多个任务同时发布共享状态。
具体调度、批次和资源限制以
[sync-library-index.yml](.github/workflows/sync-library-index.yml) 与源码为准。
