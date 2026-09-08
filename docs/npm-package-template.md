# npm 库包模板

参考 `aily-blockly-libraries` 的“一库一个 npm 包”形式，本仓库提供可复制的
[`templates/arduino-library`](../templates/arduino-library) 模板。本地 Node.js 同步脚本
已支持按此结构从上游仓库生成 npm 包并发布到两个 registry，见 [npm 同步说明](npm-sync.md)。

## 仓库与包目录

手动维护正式库时，可以将模板复制到仓库根目录，以库名作为目录名，与参考项目一致。
自动同步则从 `repositories.txt` 读取上游仓库，在 `dist/npm/<slug>/<version>/` 生成包，
无需手动复制模板。手动创建的目录示意：

```text
aily-coder-libraries/
├── templates/
│   └── arduino-library/        # 本次提供的模板
└── example-library/            # 复制后创建的正式库目录示意
    ├── package.json           # npm 包元数据与发布文件清单
    ├── src.7z                 # Arduino 库源码
    ├── readme.md              # 使用说明、API 和示例
    └── LICENSE                # 对应内容的许可证
```

不包含 `block.json`、`generator.js`、`toolbox.json` 或积木文本的 `i18n/`。
库直接提供 Arduino C/C++ 源码，无需 JavaScript 的 `main`、`exports` 或构建入口。
需要独立的 AI 使用说明时再添加 `readme_ai.md`，并将它加入 `files`。

## package.json 字段

以模板中的实际 `package.json` 为准，不重复维护另一份配置示例。

| 字段 | 约定 |
| --- | --- |
| `name` | 使用 `@aily-project-coder/lib-<slug>`；`slug` 使用小写字母、数字和连字符，与已有 Blockly 的 `@aily-project/lib-*` 区分 |
| `version` | npm 包版本，使用三段语义版本；首次包装时可与上游库版本一致，后续修改包内容也必须使用新的 npm 版本 |
| `private` | 模板设为 `true`；复制为正式库并替换占位内容后移除 |
| `description` | 库的简短说明；正式库中替换模板描述 |
| `license` | 按包内实际代码填写；示例代码为 MIT，包装其他库时保留其原始许可证 |
| `keywords` | 用途、硬件型号、协议等检索词，不包含积木类型名 |
| `files` | 明确纳入 `src.7z`、`readme.md`、`LICENSE`；npm 同时纳入 `package.json` |

正式库可按已知来源补充 npm 标准的 `author`、`homepage`、`repository` 字段；不要将
本仓库误写成第三方库的源码来源。显示名称先使用 README 标题和 Arduino 原库名，
不预设尚未接入的 `nickname_*`、`tags`、`tested`、`tester` 等自定义字段。

## Arduino 源码

沿用参考项目的 `src.7z`，归档内保留 `src/<Arduino库名>/` 这一层。模板内是一个
最小的 `ExampleLibrary`，包含 `library.properties`、头文件、实现文件、Basic 示例和
许可证。完整展开结构见模板的 [readme.md](../templates/arduino-library/readme.md)。

- `library.properties.name` 是 Arduino 原库名，不替换成 npm 的 scoped 包名。
- `library.properties.version` 记录源码版本；仅修改 npm 包说明等内容时，无需伪造新的上游源码版本。
- 保留原有的 `author`、`maintainer`、`sentence`、`paragraph`、`url`、`category` 等元数据。
- 架构兼容性保留在 `architectures`，对外头文件保留在 `includes`；不复制 Blockly 的 `compatibility.core` 或电压字段。
- 保留源码中的实际文件和原有目录结构，包括示例、许可证及编译所需资源，不只挑选 `.h` 和 `.cpp`。

Arduino 的 `depends` 与 npm 的 `dependencies` 分属不同的包名和版本体系，不能直接
复制。保留上游 `depends`，并在 README 说明如何准备这些 Arduino 依赖；仅在对应依赖
已经有明确的 npm 包名和适用版本时添加 npm `dependencies`。无依赖的模板省略该字段。

## 从模板创建一个库

在仓库根目录用 PowerShell 复制模板：

```powershell
Copy-Item -LiteralPath ./templates/arduino-library -Destination ./example-library -Recurse
Set-Location ./example-library
```

1. 将 `example-library` 换成实际库目录名，修改 `package.json` 中的包名、版本、描述、关键词和许可证。
2. 解压 `src.7z`，替换 `src/ExampleLibrary/` 为真实 Arduino 库，保留原始元数据与许可证。
3. 将 `src/` 整个目录以 7z 极限压缩生成一个全新的 `src.7z`，替换模板归档。不要在旧归档上追加，以免残留已删除文件。
4. 更新 `readme.md` 和包根目录的许可证；为正式库移除 `private`。
5. 检查压缩包和 npm 发布清单：

```powershell
# 在上述 example-library 目录中运行；其他系统将 ../scripts/7za.exe 改为 ../scripts/7z。
../scripts/7za.exe t ./src.7z
../scripts/7za.exe l ./src.7z
npm pack --dry-run --ignore-scripts
npm pack --ignore-scripts
```

生成的 `.tgz` 应只包含 `package/package.json`、`package/src.7z`、
`package/readme.md` 和 `package/LICENSE`。解压编辑产生的 `src/` 不在 `files`
清单中，不会再随 npm 包重复分发。以上命令只在本地检查和打包，不发布到 registry。

自动采集源码、生成 `src.7z` 和发布 npm 包可使用 [本地 Node.js 同步脚本](npm-sync.md)。
同步脚本还会生成 `libraries-coder-index.json`，供安装端读取 npm 包索引。
