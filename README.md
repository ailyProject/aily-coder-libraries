# Aily Coder Libraries

本仓库根据 `repositories.txt` 中的 Git 仓库地址生成 Arduino 库的 npm 包，供 Aily Coder
使用。本地同步入口使用 Node.js，读取上游 Release/tag 的 `library.properties`，下载源码、
解压并重新压缩为 `src.7z`，再组织 npm 包并发布到 CN、EU 两个 registry，生成
`libraries-coder-index.json` 并上传到 RustFS、R2。

## 本地 npm 同步

需要 Node.js 22.12+、Git 和 7-Zip。Windows 下将 `7za.exe` 放到仓库的 `scripts/`
目录，脚本会自动识别，无需将该目录加入 `PATH` 或设置 `SEVEN_ZIP_PATH`。
先安装依赖，再少量试运行：

```shell
npm ci
npm test
node scripts/sync-libraries.mjs --dry-run --max-repositories 2
```

同步模式的 `--dry-run` 完整生成本地包和索引预览，不访问 npm registry 或对象存储，也不需要发布凭据。包输出默认位于
`dist/npm/<slug>/<version>/`，包含 `package.json`、`src.7z`、`readme.md`、上游许可证
以及待发布的 `.tgz`；索引位于 `dist/npm/libraries-coder-index.json`。

发布前配置以下环境变量，然后运行 `npm run sync`：

- `CN_CODER_REGISTRY_URL`、`EU_CODER_REGISTRY_URL`：两个目标 npm registry 的 URL。
- `CN_CODER_NPM_TOKEN`、`EU_CODER_NPM_TOKEN`：分别具有对应 registry 发布权限的 token。
- `RUSTFS_ENDPOINT`、`RUSTFS_ACCESS_KEY_ID`、`RUSTFS_SECRET_ACCESS_KEY`：RustFS 索引上传配置。
- `R2_ACCOUNT_ID`（或 `R2_ENDPOINT`）、`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`：R2 索引上传配置。

使用默认仓库清单正式同步时，将本批次在两端发布成功的库组成索引，上传到两端的
`ailyblockly/libraries-coder-index.json`。`--max-repositories` 只限制处理数量；上传的索引
仅包含本批次成功的库，会替换远端索引，不合并旧条目，其他库失败不阻止上传。
dry-run 或指定其他清单时，仅生成本地索引。
索引字段按 npm 包整理，包名统一为 `@aily-project-coder/lib-*`；现有读取旧 ZIP 索引的
Coder 服务端需要同步迁移后才能使用，详见 [索引字段与兼容性](docs/npm-sync.md#索引字段与兼容性)。

也可以复制 [环境变量示例](.env.npm-sync.example) 为本地 `.env.npm-sync`，填入配置后运行：

```shell
node --env-file=.env.npm-sync scripts/sync-libraries.mjs
```

少量测试发布并上传索引时，仍需配置全部 npm 和 RustFS/R2 环境变量：

```shell
node --env-file=.env.npm-sync scripts/sync-libraries.mjs --max-repositories 10
```

这会处理默认清单的前 10 个库，用其中双端发布成功的库生成索引并替换两端原索引；其他库失败不阻止索引上传。

完整参数、版本选择与失败重试规则见 [Node.js npm 同步说明](docs/npm-sync.md)。

## 卸载 npm 库包

`--unpublish` 删除配置的 CN、EU 两个 registry 中全部 `@aily-project-coder/lib-*` 包的
所有版本。沿用上述四项 npm 配置，token 需要对应 registry 的 unpublish 权限；无需
`repositories.txt`、本地 `dist`、Git、7-Zip 或 RustFS/R2 配置。

先只读预览目标，再运行实际卸载：

```shell
node --env-file=.env.npm-sync scripts/sync-libraries.mjs --unpublish --dry-run
node --env-file=.env.npm-sync scripts/sync-libraries.mjs --unpublish
```

卸载模式的 `--dry-run` 会访问两个 registry 列出包，不执行删除。实际卸载先显示两个
registry URL 和包名，必须交互输入 `UNPUBLISH` 才会删除，没有无人值守确认参数。
`--repositories`、`--max-repositories`、`--workers` 仅用于同步，与 `--unpublish`
同时传入会报错，不能用来限制卸载范围。

卸载不清理本地包，也不改写 RustFS/R2 索引；已上线索引需要另行更新，避免指向已删除的包。
详见 [卸载远端包](docs/npm-sync.md#卸载远端包)。

## npm 库包模板

面向 npm 包组织的初版模板位于 [`templates/arduino-library`](templates/arduino-library)，
目录、字段和复制方法见 [npm 库包模板说明](docs/npm-package-template.md)。模板参考
`aily-blockly-libraries`，保留包元数据、Arduino 源码压缩包和文档，去掉 Blockly 积木相关内容。
本地 Node.js 同步脚本已支持按此结构生成和发布 npm 包；模板本身保持 `private: true`。

## 添加库

要让一个库参与同步：

1. 在 `repositories.txt` 中添加仓库的 HTTP(S) URL，每行一个；
2. 确保仓库至少有一个 Git tag，且该 tag 根目录包含有效的 `library.properties`；
3. 运行测试和少量 dry-run；
4. 提交 Pull Request，并说明新增库及其仓库地址。

空行和以 `#` 开头的注释会被忽略。URL 会经过规范化检查，请勿添加指向同一仓库的
重复地址。验证新增库时，可通过 `--repositories` 指向仅包含该 URL 的临时清单。
