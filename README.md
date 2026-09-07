# Aily Coder Libraries

本仓库根据 `repositories.txt` 中的 Git 仓库发布记录和 tag，独立生成 Arduino 库包和
`libraries-coder-index.json`。`library.properties` 元数据取自对应 tag，包大小和校验值
由生成的 ZIP 计算；整个过程不读取 Arduino 官方索引，也不下载 Arduino 官方 ZIP。

生成的库包与索引均在 RustFS 和 Cloudflare R2 各发布一份，供 Aily Coder 使用。

## 工作方式

```text
repositories.txt
       │
       ▼
GitHub 优先定位 Latest Release；无可用 Release 时发现全部 tag
       │
       ▼
读取候选 tag 根目录的 library.properties
       │
       ▼
按发布规则选出最高版本，仅为最终候选生成确定性 ZIP、size 与 SHA-256
       │
       ▼
将 ZIP 同步到 RustFS 与 Cloudflare R2
       │
       ▼
保存同步状态，发布两端各自的 libraries-coder-index.json
```

每个成功的轮询批次都遵循 package → state → index 的顺序。版本进入状态与索引前，
其 ZIP 必须已在两端确认存在且内容匹配；状态保存或校验失败时，不发布本轮索引。
首次完整扫描尚未结束时，公开索引也会在每轮更新，包含截至当前已确认的版本。

GitHub 仓库能确认 Latest Release 时，只处理它指向的 tag，并把该 Release 作为仓库的发布
意图；仅有新 tag、但尚未创建 Release 的版本不会提前进入索引。只有明确没有正式 Release
时才比较全部 tag；其他 Git 托管平台也始终走该回退路径。若 Release 对应源码本身无效，
该 tag 会按现有规则跳过，而不会转而发布其他普通 tag。若因网络等故障无法确认 Release
状态，本轮会失败并由外层重试。
首次同步时，每个库只发布主路径中最高的有效语义版本；后续发现更高版本时增量发布，已有
版本继续保留在同步状态、对象存储和公开索引中。

全 tag 回退路径会先比较版本，未选中的旧版本只记录 tag 状态而不生成 ZIP；同一 commit 的
多个 tag 也只生成一次。版本号始终读取自 tag 内的 `library.properties`，不能依赖可能与
库版本不一致的 tag 名称。Latest Release 只用于选定 tag；源码仍通过 Git 按 OID 校验，并
由同步器生成相同的确定性 ZIP，不直接采用 Release asset 或 GitHub zipball。

这里的“已有版本”仅指本次全新 bootstrap 及其后成功发布的版本。重新开始前遗留的
ZIP 和 state 不在保留范围内，需先按部署说明清理；同步器不会自动删除对象。

为保护已经发布的版本，同步器遵循以下规则：

- 同名 ZIP 已存在但内容不同时，拒绝覆盖；
- 不同仓库产生相同库名和版本时，拒绝覆盖已有包；
- 已发布 tag 被移动后，不使用新内容覆盖原版本；
- tag 消失时，不自动删除已发布的版本和 ZIP。

索引字段、库与版本规则见[索引格式与库要求](docs/index-format.md)。

## 添加库

要让一个库参与索引生成：

1. 在 `repositories.txt` 中添加仓库的 HTTP(S) URL，每行一个；
2. 确保仓库至少有一个 Git tag，且该 tag 根目录包含有效的 `library.properties`；
3. 运行测试和少量 dry-run；
4. 提交 Pull Request，并说明新增库及其仓库地址。

空行和以 `#` 开头的注释会被忽略。URL 会经过规范化检查，请勿添加指向同一仓库的
重复地址。验证新增库时，可通过 `--repositories` 指向仅包含该 URL 的临时清单。

## 本地检查

需要 Python 3.11+ 和 Git。安装项目并运行测试：

```shell
python -m pip install -e .
python -m unittest discover -s tests -v
```

不配置 RustFS 或 R2 凭据，也可以用 dry-run 扫描少量仓库并生成本地候选索引：

```bash
aily-coder-libraries-sync \
  --dry-run \
  --max-repositories 2 \
  --workers 2 \
  --rustfs-public-download-base-url https://rustfs-packages.example.invalid \
  --r2-public-download-base-url https://r2-packages.example.invalid \
  --output-directory dist/dry-run
```

PowerShell 请使用反引号作为续行符，或将参数写在同一行。

dry-run 不访问对象存储。生成的索引位于：

```text
dist/dry-run/rustfs/libraries-coder-index.json
dist/dry-run/r2/libraries-coder-index.json
```

## 部署与维护

GitHub Actions 所需配置、对象存储布局和首次同步行为见[部署说明](DEPLOYMENT.md)。
