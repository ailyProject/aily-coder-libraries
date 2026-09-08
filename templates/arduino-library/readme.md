# ExampleLibrary

Aily Coder Arduino 库包模板。示例库提供 `exampleMessage()`，返回固定问候文本，
用于展示源码目录、头文件引用和 Arduino 示例的组织方式。

## 包内容

`package.json` 管理 npm 包身份和版本，`src.7z` 保存 Arduino 库。压缩包展开为：

```text
src/
└── ExampleLibrary/
    ├── library.properties
    ├── LICENSE
    ├── src/
    │   ├── ExampleLibrary.h
    │   └── ExampleLibrary.cpp
    └── examples/
        └── Basic/
            └── Basic.ino
```

## 使用示例

将压缩包中的 `src/ExampleLibrary` 整个目录放入 Arduino sketchbook 的 `libraries/`
目录，即 `<sketchbook>/libraries/ExampleLibrary/`，再引用头文件。该库不依赖额外硬件
或第三方库。

```cpp
#include <ExampleLibrary.h>

void setup() {
  Serial.begin(115200);
  Serial.println(exampleMessage());
}

void loop() {
}
```

## 复制模板

替换 npm 包名、版本、描述和关键词，替换 `src.7z` 中的示例库，并将本说明改为真实库的
用途、API、依赖和示例。模板的 `private: true` 表示它本身不作为正式库发布；实际库完成
替换后移除此字段。本地 Node.js 同步脚本已接入此 npm 包格式，可以从 `repositories.txt`
中的上游仓库自动生成和发布正式库包；本模板本身不发布。

## 许可证

本模板的示例代码使用 MIT 许可证，见 `LICENSE`。替换为上游库时，保留上游的许可证和
版权声明，并按实际内容更新 `package.json` 的 `license` 字段。
