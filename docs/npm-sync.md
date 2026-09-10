# Node.js npm 同步

本地同步入口是 `scripts/sync-libraries.mjs`，通过 `npm run sync` 运行。它读取
`repositories.txt`，按下文“源码与版本”规则为每个库选取最新版本，将源码重新打包为
`src.7z`，组织 npm 包并把同一个 `.tgz` 分别发布到 CN、EU 两个 npm registry，最后
生成并上传库索引。

## 安装与试运行

需要 Node.js 22.12+、Git 和 7-Zip。Git 必须在 `PATH` 中。Windows 下将 `7za.exe`
放到仓库的 `scripts/` 目录，脚本会自动识别，无需将该目录加入 `PATH` 或设置
`SEVEN_ZIP_PATH`。

7-Zip 按以下顺序查找：`SEVEN_ZIP_PATH` 指定的可执行文件 → 仓库的 `scripts/7za.exe`
（其他系统为 `scripts/7z`）→ Windows 标准位置 `C:\Program Files\7-Zip\7z.exe`
→ `PATH` 中的 `7z`。仓库内的路径根据脚本自身位置确定，不依赖运行时的工作目录。

在仓库根目录运行：

```shell
npm ci
npm test
node scripts/sync-libraries.mjs --dry-run --max-repositories 2
```

同步模式的 dry-run 会访问源代码仓库，完成下载、解压、7z 压缩、npm 打包和本地索引生成；不访问
npm registry 或对象存储，无需配置发布凭据。它生成的包可以直接在本地检查：

```powershell
./scripts/7za.exe t "dist/npm/<slug>/<version>/src.7z"
./scripts/7za.exe l "dist/npm/<slug>/<version>/src.7z"
```

以上命令从仓库根目录运行，将 `<slug>` 和 `<version>` 替换为实际生成的目录名。
其他系统将 `./scripts/7za.exe` 改为 `./scripts/7z`。

源码下载支持 `HTTP_PROXY`、`HTTPS_PROXY` 和 `NO_PROXY`。未配置这些标准代理变量及
`GIT_CONFIG_COUNT` 时，脚本会沿用 Git 全局的 `http.proxy`；这项回退仅作用于源码 Git
和 GitHub 请求，不作用于 npm registry 请求。

## 配置与发布

发布模式必须提供 npm registry 配置；使用默认仓库清单时还必须提供索引存储配置，
包括通过 `--max-repositories` 限制数量的运行：

| 变量 | 用途 |
| --- | --- |
| `CN_CODER_REGISTRY_URL` | CN npm registry 的 HTTP(S) URL |
| `EU_CODER_REGISTRY_URL` | EU npm registry 的 HTTP(S) URL |
| `CN_CODER_NPM_TOKEN` | CN registry 的 token，发布时需发布权限，卸载时需 unpublish 权限 |
| `EU_CODER_NPM_TOKEN` | EU registry 的 token，发布时需发布权限，卸载时需 unpublish 权限 |
| `RUSTFS_ENDPOINT` | RustFS 的 S3 HTTP(S) endpoint |
| `RUSTFS_ACCESS_KEY_ID`、`RUSTFS_SECRET_ACCESS_KEY` | RustFS 索引写入凭据 |
| `R2_ACCOUNT_ID` 或 `R2_ENDPOINT` | R2 账号 ID，或显式 S3 endpoint（优先） |
| `R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY` | R2 索引写入凭据 |

两个 registry URL 必须不同，URL 中不要包含用户名、密码、查询参数或片段。
存储 endpoint 同样不接受凭据、查询参数或片段。`RUSTFS_REGION` 默认 `us-east-1`，
`R2_REGION` 默认 `auto`；RustFS 凭据兼容旧变量 `RUSTFS_ACCESS_KEY`、`RUSTFS_SECRET_KEY`。
索引固定写入两个存储端的 `ailyblockly` bucket 根目录，不需要原先的 package bucket
或 ZIP 公开下载基址。上传使用 Node.js S3 SDK，无需额外安装 AWS CLI。

变量已配置到当前 shell 时，运行：

```shell
npm run sync
```

也可以使用仓库提供的示例文件：

```powershell
Copy-Item -LiteralPath .env.npm-sync.example -Destination .env.npm-sync
```

在本机编辑 `.env.npm-sync`，替换示例 URL 并填入对应 token，再使用 Node.js 原生的
`--env-file` 参数加载：

```shell
node --env-file=.env.npm-sync scripts/sync-libraries.mjs
```

若要先用 10 个库测试发布和索引上传，配置上述全部 npm 和 RustFS/R2 环境变量后运行：

```shell
node --env-file=.env.npm-sync scripts/sync-libraries.mjs --max-repositories 10
```

该命令处理默认 `repositories.txt` 的前 10 个库；将其中在 CN、EU 都发布成功或确认内容
一致的库加入索引，并上传到 RustFS 和 R2，替换原索引，不合并旧条目。其他库失败不阻止上传。

脚本不会自动读取 `.env.npm-sync`；该文件已加入 `.gitignore`。不要把实际 token 写入
`package.json`、`repositories.txt` 或提交到仓库。

## 卸载远端包

`--unpublish` 删除配置的 CN、EU 两个 npm registry 中全部 `@aily-project-coder/lib-*`
包的所有版本。它直接从两个 registry 获取目标包，不依赖 `repositories.txt` 或本地
`dist`，也不需要 Git、7-Zip 或 RustFS/R2 配置。仅需安装 Node.js 和项目依赖，并提供
`CN_CODER_REGISTRY_URL`、`EU_CODER_REGISTRY_URL`、`CN_CODER_NPM_TOKEN`、
`EU_CODER_NPM_TOKEN`；两个 token 均须具有对应 registry 的 unpublish 权限。

当前列表读取依赖 [Verdaccio 6 的 `/-/all?local=1` 接口](https://github.com/verdaccio/verdaccio/blob/6.x/src/api/endpoint/api/search.ts)，
按 registry 返回的本地包清单处理；任一端接口不可用或返回错误时中止，不删除任何包。

先预览两个 registry 的目标包：

```shell
node --env-file=.env.npm-sync scripts/sync-libraries.mjs --unpublish --dry-run
```

这里的 `--dry-run` 会只读访问 npm registry 并列出目标，不执行任何删除。普通同步
模式的 `--dry-run` 仍只构建本地包和索引，不访问 npm registry 或对象存储。

实际卸载：

```shell
node --env-file=.env.npm-sync scripts/sync-libraries.mjs --unpublish
```

脚本先显示两个 registry URL 及各自的目标包名，再要求交互输入 `UNPUBLISH`。
只有输入该确认词才开始删除；没有可跳过交互确认的无人值守参数。
卸载前先停止正在运行的同步或发布任务；卸载只处理本次列出的包。
`--repositories`、`--max-repositories` 和 `--workers` 仅用于同步，卸载模式显式传入
这些参数会报错，不能用它们筛选包或限制卸载数量。

单个包卸载失败后继续处理其他包，最终返回退出码 `1` 并列出失败的 registry 和包名。
重跑会重新读取远端清单，但后端可能先移除清单条目、再删除包文件；后续文件删除失败时，
该包可能已不在清单中，需要核查失败包的后端状态，不能只以重跑结果判断残留是否清理完成。

卸载只作用于上述远端 npm 包，不清理本地包，也不改写 RustFS/R2 索引。已上线的
`libraries-coder-index.json` 须另行更新，避免继续指向已删除的包。

## 参数

`npm run sync` 后的脚本参数需要放在 `--` 后；直接调用 `node` 时不需要这层分隔符。
PowerShell 中请使用 `npm.cmd run sync -- ...` 或直接调用 `node`，避免 `npm.ps1`
转发时丢失脚本选项。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--repositories <path>` | 仓库根目录的 `repositories.txt` | 仅同步：指定仓库列表；使用其他文件时不上传索引 |
| `--output-directory <path>` | `dist/npm` | 同步模式的本地 npm 包输出目录；卸载模式用于临时 npm 配置和缓存，不从中选取目标包 |
| `--max-repositories <n>` | `0` | 仅同步：最多处理的仓库数，`0` 表示全部；只限制数量，不阻止索引上传 |
| `--workers <n>` | `4` | 仅同步：并行处理仓库数，最多 `4` |
| `--dry-run` | 关闭 | 同步模式只生成本地包和索引，不访问 npm registry 或对象存储；卸载模式只读访问两个 registry 并预览目标包 |
| `--unpublish` | 关闭 | 交互确认后，删除两个 registry 中全部 `@aily-project-coder/lib-*` 包的所有版本 |

例如，检查一份临时清单并单线程处理：

```shell
npm run sync -- --repositories repositories-small.txt --output-directory dist/npm-check --workers 1 --dry-run
```

清单中每行一个 Git 仓库的 HTTP(S) URL，空行和以 `#` 开头的注释会被忽略。

## 源码与版本

1. GitHub 仓库优先采用 Latest Release 指向的 tag。没有正式 Release 或无法确认 Release
   时，回退到比较全部 tag；其他 Git 托管平台直接比较 tag。
2. 从候选 tag 根目录的 `library.properties` 读取库名和版本；版本号不从 tag 名称猜测。
   普通 tag 的回退路径选择最高的有效库版本。
3. 通过 Git 下载并校验最终候选源码，生成源码 ZIP；解压后保持上游库的目录和文件，
   再生成新的 `src.7z`，不直接采用 Release 的附件。
4. 生成 npm 元数据和文档，以 `npm pack --ignore-scripts` 打包；不运行上游库的 npm
   生命周期脚本。
5. 将同一个 `.tgz` 发布到两个配置的 registry。正式版本使用 `latest`，预发布版本使用
   `next`。
6. 生成 `libraries-coder-index.json`。使用默认清单正式同步时，将本批次双端发布成功的库
   组成索引，上传到 RustFS 和 R2；其他库失败不阻止上传。

无法确认 Release 状态时会输出提示，继续按普通 tag 规则选择最新有效版本。
已确认的 Release 指向的源码无效时，仍报告该库失败。

Latest Release 查询每次最多等待 30 秒；网络异常或 HTTP 500、502、503、504 时最多
尝试 3 次，重试前分别等待 1 秒和 2 秒。重试耗尽后回退到普通 tag；其他查询错误也会回退。

Git `ls-remote` 和 `fetch` 遇到临时网络错误或 HTTP 408、429、500、502、503、504 时，
最多尝试 3 次，重试前分别等待 1 秒和 2 秒；每次命令仍最多执行 10 分钟。
认证失败、仓库或 tag 不存在、证书校验失败、本地权限或磁盘错误及未识别错误不重试。
重试日志带仓库地址；失败信息包含命令、下载的 tag（仅 `fetch`）、安全的错误原因、
退出码或终止信号、已尝试次数。为避免泄露代理凭据，不直接输出 Git 原始错误或命令行。

## 包结构

默认输出示意：

```text
dist/npm/<slug>/<version>/
├── package.json
├── src.7z
├── readme.md
├── LICENSE                    # 上游存在时保留；文件名按上游实际情况
└── aily-project-coder-lib-<slug>-<version>.tgz
```

包名为 `@aily-project-coder/lib-<slug>`，`slug` 根据 `library.properties.name` 生成。
自动生成的 `src.7z` 内部结构为 `src/<slug>/`，其中保留 `library.properties`、源码、
示例和许可证等原有内容。`library.properties.name` 仍保留 Arduino 原库名。具体字段
约定见 [npm 库包模板说明](npm-package-template.md)。

npm 版本会补齐三段数字并移除前缀 `v`；npm 不以 `+build` 元数据区分版本，因此 npm
包版本省略该部分，原始 `library.properties` 文件不改写。

包根目录保留上游 README 和许可证；上游缺少许可证时不默认认定为 MIT。上游 Arduino
`depends` 保留在 `library.properties`，不会自动转换为 npm `dependencies`。生成的包
不包含 Blockly 块定义、生成器、工具箱或积木翻译文件。

## 重跑与冲突

单个仓库失败后会继续处理其他仓库，结束时汇总成功和失败数量；有失败时进程返回退出码 `1`。
失败的库不进入索引，也不阻止成功库的索引上传。跳过索引上传时，`Index:` 日志会列出
实际原因，例如 `upload skipped: dry-run`。

两个 registry 分别检查同名同版本的已发布内容。内容与本次 `.tgz` 一致时跳过该端；
内容不一致时报冲突，不覆盖已经发布的版本。不同仓库映射到同一个 npm 包名也需要先
解决名称冲突。

发布不是跨 registry 的原子操作。一端成功、另一端失败时，重新运行同一命令即可检查
并跳过已成功的一端，补齐另一端，前提是重跑仍选中同一上游版本。上游出现新的 Latest
Release 后，脚本处理新选中的版本，不会自动回补旧版本失败的一端。该过程不自动撤回
已发布版本，也不删除 registry 中的旧版本。

本地已有同名同版本的 `.tgz` 时，内容不同也会拒绝替换。可以用另一个
`--output-directory` 检查变化后的包，但不能用它覆盖 registry 中已经发布的版本。

## 索引字段与兼容性

本地和上传索引均采用紧凑 JSON，无缩进、多余空白或末尾换行，字段值保持原样。

索引默认输出到 `dist/npm/libraries-coder-index.json`，保留 `{ "libraries": [...] }`
外层结构；每个库只列出本次选中的一个版本，并按 npm 包名排序。字段参考
`aily-blockly-libraries/.scripts/genjson.js` 从包元数据提取的方式，按 Coder 源码库重新整理：

| 字段 | 来源与用途 |
| --- | --- |
| `name` | npm 包名 `@aily-project-coder/lib-<slug>`，用于安装 |
| `nickname` | `library.properties.name`，保留 Arduino 原库显示名 |
| `version`、`description`、`author`、`keywords` | 生成的 npm `package.json` 同名字段 |
| `repository` | npm 标准 Git 仓库对象，指向上游源码 |
| `homepage`、`license` | npm 元数据中存在时输出 |
| `category` | Arduino `category`，缺失时为 `Uncategorized` |
| `architectures` | Arduino `architectures` 拆分为数组，缺失或为空时为 `["*"]` |
| `providesIncludes` | Arduino `includes` 拆分为头文件数组，存在时输出，用于搜索和推荐 |

不生成 Blockly 的硬件、积木、测试标记、多语言占位字段，也不生成旧 ZIP 下载专用的
`url`、`archiveFileName`、`size`、`checksum`。Arduino `depends` 仍保留在源码和 README，
不伪装成 npm `dependencies`。

同步模式的 dry-run 索引包含成功构建的库；发布模式只纳入两个 registry 都已发布成功或确认内容
一致的库。dry-run 或使用其他清单时，只生成本地预览，保留远端索引。
使用默认 `repositories.txt` 正式同步时上传索引，`--max-repositories` 只限制处理数量。
上传的索引仅包含本批次成功的库，会替换原索引，不合并旧条目；例如
`--max-repositories 10` 中 6 个成功、4 个失败时，远端索引列出这 6 个成功的库。
如果全部失败，上传的索引为 `{ "libraries": [] }`，进程仍返回退出码 `1`。

两个存储端写入的对象均为 `ailyblockly/libraries-coder-index.json`，Content-Type 为
`application/json`，Cache-Control 为 `no-store, no-cache, must-revalidate, max-age=0`。
一端上传失败仍尝试另一端，最终返回失败；重跑会重新检查包并重试索引上传。双端索引
更新也不具备跨服务原子性。

当前 `aily-coder-editor/server/coderLibraryRegistry.js` 和 `componentLibraryService.js`
仍按 ZIP 索引校验、下载和安装。上线这份 npm 索引前，需要将消费端迁移到 npm 包解析
及 `src.7z` 安装；本次脚本修改没有修改消费端。旧格式不能与这份新格式直接互换。
