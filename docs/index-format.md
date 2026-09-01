# 索引格式与库要求

每个存储端都会生成一份 `libraries-coder-index.json`，并发布到索引 bucket 根目录：

```json
{
  "libraries": []
}
```

两份索引的库条目相同，只有 `url` 使用各自存储端的公开下载基址。公开下载
基址指向 package bucket 根入口，程序会自动追加固定的 `/libraries` 路径。

## 字段来源

| 索引字段 | 来源或规则 |
| --- | --- |
| `name`、`version`、`author`、`maintainer`、`sentence` | tag 根目录 `library.properties` 中的同名必填字段 |
| `paragraph` | `library.properties` 的可选同名字段；为空时不输出 |
| `website` | `library.properties` 的可选 `url`；为空时不输出 |
| `category` | 最新版本 `library.properties` 中的同名字段；缺失或无效时为 `Uncategorized` |
| `architectures` | `library.properties` 的 `architectures`；缺失时为 `["*"]` |
| `dependencies` | `library.properties` 的可选 `depends`；为空时不输出 |
| `providesIncludes` | `library.properties` 的可选 `includes`；为空时不输出 |
| `license` | `library.properties` 的可选 `license`；为空时不输出 |
| `repository` | `repositories.txt` 中的仓库 URL |
| `types` | 固定为 `["Arduino"]` |
| `archiveFileName` | 规范化后的库名和版本组成的 ZIP 文件名 |
| `url` | 对应存储端的公开下载根入口加 `/libraries/` 和 URL 编码后的 `archiveFileName` |
| `size` | ZIP 的字节数 |
| `checksum` | ZIP 的 SHA-256，格式为 `SHA-256:<64 位小写十六进制>` |

## 库与版本要求

- `library.properties` 必须位于 tag 根目录并使用 UTF-8 编码；
- 版本号接受一至三个数字段，并规范化为三个数字段，例如 `1` 变为 `1.0.0`；
- 合法的 prerelease 和 build 标识会被保留；
- 同一仓库的库名一旦锁定，后续 tag 不能更改库名；
- 库名在不同仓库之间按大小写不敏感地保持唯一。

每个库只在索引中保留结构和元数据均有效的最高语义版本。历史版本仍保留在同步状态与
对象存储中，但不会写入公开索引。

## 不可变规则

ZIP 使用 `libraries/<archiveFileName>` 作为对象键，因此 `archiveFileName` 在该固定
前缀下全局唯一：

- 已有对象的大小或 SHA-256 与候选包不同，视为冲突并拒绝覆盖；
- 两个库产生相同库名和版本，或产生相同文件名但内容不同，不覆盖先前包；
- 已发布 tag 被移动到其他 commit 时，保留原包和状态记录，公开索引仍只展示最高语义版本；
- tag 消失时，不自动删除对应的状态记录和 ZIP。
