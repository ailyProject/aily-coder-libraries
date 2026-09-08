# 库列表整理记录（2026-09-07）

原列表 9,941 行，其中 9,939 个仓库地址、2 个空行。已移除 **3,567 个地址**及 2 个空行，保留 **6,372 个地址**。原顺序和保留行格式不变。

## 筛选口径

- 默认“几年未更新”为超过 **3 年**，界线为 **2023-09-07 00:00 UTC**。GitHub 取默认分支最新提交、最近推送及最新正式 Release 时间的最大值；不以 issues、星标或普通仓库信息修改日期判定更新。其他平台同时查看最新提交、仓库活动和发布记录。近期任何一种活动存在，就不按长期未更新移除。
- 已归档、明确废弃和长期未更新分别标注。归档或长期未更新不代表不能编译，只表示不符合本次维护活跃度筛选条件。
- GitHub 无法解析的地址另用 REST API 复核；其他平台 404 另查一次。404 只能证明当前公开地址不可用，不能区分删除、私有化及迁移。临时错误不作为失效依据。
- 功能重复只处理同一仓库的多个地址、维护者明确提供的替代关系及已核实的继承实现。不同硬件、接口、内存需求或独立修复的相似库继续保留；不能按名称或推送时间直接认定可互换。
- 没有新添列表之外的库，也没有修改库源码。替代库可能要求调整业务代码；本次未进行逐库编译、硬件运行或依赖闭包验证。

## 统计（互斥分类）

| 移除原因 | 地址数 |
| --- | ---: |
| 超过 3 年未更新 | 2936 |
| 平台标记已归档 | 419 |
| 复核后仍不可访问 | 172 |
| 维护者声明废弃、停止维护或明确被替代 | 34 |
| 多个地址指向同一仓库 | 6 |

部分条目同时符合多个条件，表中每个地址仅计一次，优先记录人工确认的废弃/替代关系。

## 核实范围与证据

- 9,855 个 GitHub 地址通过 GraphQL 批量获取元数据；另有 84 个 GitLab、Bitbucket 和 Antares 地址通过各平台 API 查询。
- 对未被归档及长期未更新规则排除的 6,370 个 GitHub 条目检查常见 README 路径，其中 6,097 个获取到说明，其余 273 个未获取到常见路径 README，不能保证不存在未发现的废弃声明。关键词命中经过人工区分，旧版本或单个 API 废弃不会导致整个库被移除。
- [完整逐项记录](repository-audit-2026-09-07.jsonl) 每行对应原列表一行，包含原行号、原文、决定、原因、API 来源及取得的提交/推送/发布日期；人工决定另附 evidence 和 replacements。`decision=keep` 表示未命中本次规则，不是可用性认证；`decision=review` 表示保留待核实。
- `replacements_retained` 只列本次结果中实际保留的替代库；`replacements` 是维护者建议或已核实的替代关系，两者应区分。
- 原文件 SHA-256：`5f82989abbac383bd5b67851877b795887d981b87012adbb7e238eee1a714f6f`；整理后 SHA-256：`1ac11cd3d179d54d17db345b4e450cf664dd80312343a02f51db35117350d49c`。

## 明确废弃或被替代的条目

| 移除库及证据 | 判断 | 本列表保留的替代库 |
| --- | --- | --- |
| [AlexandreHiroyuki/MovingAveragePlus](https://github.com/AlexandreHiroyuki/MovingAveragePlus#readme) | 官方声明废弃，DataTome 覆盖原有全部功能 | [AlexandreHiroyuki/DataTome](https://github.com/AlexandreHiroyuki/DataTome) |
| [arduino-libraries/Arduino_MachineControl](https://github.com/arduino-libraries/Arduino_PortentaMachineControl#readme) | 官方新版明确替代旧库，需要迁移 API | [arduino-libraries/Arduino_PortentaMachineControl](https://github.com/arduino-libraries/Arduino_PortentaMachineControl) |
| [arduino-libraries/Arduino_NineAxesMotion](https://github.com/arduino-libraries/Arduino_NineAxesMotion#readme) | 官方 README 声明已废弃且不再维护 | 无已确认且保留的直接替代项 |
| [ayushsharma82/ESPConnect](https://github.com/ayushsharma82/ESPConnect#readme) | 官方已废弃并推荐 NetWizard | [ayushsharma82/NetWizard](https://github.com/ayushsharma82/NetWizard) |
| [dejwk/roo_io_arduino](https://github.com/dejwk/roo_io_arduino#readme) | 官方声明已废弃，功能已由 roo_io 自包含实现 | [dejwk/roo_io](https://github.com/dejwk/roo_io) |
| [gicking/LIN_master_Arduino](https://github.com/gicking/LIN_master_portable_Arduino#readme) | 作者明确由更可移植的新版取代 | [gicking/LIN_master_portable_Arduino](https://github.com/gicking/LIN_master_portable_Arduino) |
| [gmag11/NtpClient](https://github.com/gmag11/NtpClient#readme) | 作者宣布停止主要开发并建议使用 ESP 核心自带时间接口 | 无已确认且保留的直接替代项 |
| [knolleary/pubsubclient](https://github.com/knolleary/pubsubclient#readme) | 作者在 2026 年声明不再维护，并建议使用仍维护的 MQTT 库 | 无已确认且保留的直接替代项 |
| [m5stack/M5Atom](https://github.com/m5stack/M5Atom#readme) | 官方明确废弃旧库并推荐 M5GFX 与 M5Unified | [m5stack/M5GFX](https://github.com/m5stack/M5GFX), [m5stack/M5Unified](https://github.com/m5stack/M5Unified) |
| [m5stack/M5AtomS3](https://github.com/m5stack/M5AtomS3#readme) | 官方明确废弃旧库并推荐 M5GFX 与 M5Unified | [m5stack/M5GFX](https://github.com/m5stack/M5GFX), [m5stack/M5Unified](https://github.com/m5stack/M5Unified) |
| [m5stack/M5Core-Ink](https://github.com/m5stack/M5Core-Ink#readme) | 官方明确废弃旧库并推荐 M5GFX 与 M5Unified | [m5stack/M5GFX](https://github.com/m5stack/M5GFX), [m5stack/M5Unified](https://github.com/m5stack/M5Unified) |
| [m5stack/M5Core2](https://github.com/m5stack/M5Core2#readme) | 官方明确废弃旧库并推荐 M5GFX 与 M5Unified | [m5stack/M5GFX](https://github.com/m5stack/M5GFX), [m5stack/M5Unified](https://github.com/m5stack/M5Unified) |
| [m5stack/M5CoreS3](https://github.com/m5stack/M5CoreS3#readme) | 官方明确废弃旧库并推荐 M5GFX 与 M5Unified | [m5stack/M5GFX](https://github.com/m5stack/M5GFX), [m5stack/M5Unified](https://github.com/m5stack/M5Unified) |
| [m5stack/M5Stack](https://github.com/m5stack/M5Stack#readme) | 官方明确废弃旧库并推荐 M5GFX 与 M5Unified | [m5stack/M5GFX](https://github.com/m5stack/M5GFX), [m5stack/M5Unified](https://github.com/m5stack/M5Unified) |
| [m5stack/M5StickC](https://github.com/m5stack/M5StickC#readme) | 官方明确废弃旧库并推荐 M5GFX 与 M5Unified | [m5stack/M5GFX](https://github.com/m5stack/M5GFX), [m5stack/M5Unified](https://github.com/m5stack/M5Unified) |
| [m5stack/M5StickC-Plus](https://github.com/m5stack/M5StickC-Plus#readme) | 官方明确废弃旧库并推荐 M5GFX 与 M5Unified | [m5stack/M5GFX](https://github.com/m5stack/M5GFX), [m5stack/M5Unified](https://github.com/m5stack/M5Unified) |
| [m5stack/M5StickCPlus2](https://github.com/m5stack/M5StickCPlus2#readme) | 官方明确废弃旧库并推荐 M5GFX 与 M5Unified | [m5stack/M5GFX](https://github.com/m5stack/M5GFX), [m5stack/M5Unified](https://github.com/m5stack/M5Unified) |
| [m5stack/M5Unit-UHF-RFID](https://github.com/m5stack/M5Unit-UHF-RFID#readme) | 官方声明废弃并推荐 M5Unit-RFID | [m5stack/M5Unit-RFID](https://github.com/m5stack/M5Unit-RFID) |
| [johnrickman/LiquidCrystal_I2C](https://github.com/johnrickman/LiquidCrystal_I2C/compare/master...markub3327:master) | README 声明旧库已归档迁移；列表中 markub3327 分支保留原有实现并继续更新至 2.0.0，包含原分支全部提交 | [markub3327/LiquidCrystal_I2C](https://github.com/markub3327/LiquidCrystal_I2C) |
| [matthijskooijman/arduino-lmic](https://github.com/matthijskooijman/arduino-lmic#readme) | 官方已停止维护并推荐 MCCI 版本 | [mcci-catena/arduino-lmic](https://github.com/mcci-catena/arduino-lmic) |
| [mobizt/ESP-Line-Notify](https://github.com/mobizt/ESP-Line-Notify#readme) | 官方已标记 DEPRECATED | 无已确认且保留的直接替代项 |
| [mobizt/ESP-Mail-Client](https://github.com/mobizt/ESP-Mail-Client#readme) | 官方已停止维护，推荐 ReadyMail | [mobizt/ReadyMail](https://github.com/mobizt/ReadyMail) |
| [mobizt/Firebase-Arduino-WiFi101](https://github.com/mobizt/Firebase-Arduino-WiFi101#readme) | 官方已废弃，推荐继续维护的 FirebaseClient | [mobizt/FirebaseClient](https://github.com/mobizt/FirebaseClient) |
| [mobizt/Firebase-Arduino-WiFiNINA](https://github.com/mobizt/Firebase-Arduino-WiFiNINA#readme) | 官方已废弃，推荐继续维护的 FirebaseClient | [mobizt/FirebaseClient](https://github.com/mobizt/FirebaseClient) |
| [mobizt/Firebase-ESP-Client](https://github.com/mobizt/Firebase-ESP-Client#readme) | 官方已废弃，推荐继续维护的 FirebaseClient | [mobizt/FirebaseClient](https://github.com/mobizt/FirebaseClient) |
| [mobizt/Firebase-ESP32](https://github.com/mobizt/Firebase-ESP32#readme) | 官方已废弃，推荐继续维护的 FirebaseClient | [mobizt/FirebaseClient](https://github.com/mobizt/FirebaseClient) |
| [mobizt/Firebase-ESP8266](https://github.com/mobizt/Firebase-ESP8266#readme) | 官方已废弃，推荐继续维护的 FirebaseClient | [mobizt/FirebaseClient](https://github.com/mobizt/FirebaseClient) |
| [mobizt/SerialTCPClient](https://github.com/mobizt/SerialTCPClient#readme) | 官方已停止维护并由 SerialNetworkBridge 替代 | [mobizt/SerialNetworkBridge](https://github.com/mobizt/SerialNetworkBridge) |
| [neptune2/simpleDSTadjust](https://github.com/neptune2/simpleDSTadjust#readme) | 官方说明该功能已由 ESP8266 核心修复，旧补丁库已过时 | 无已确认且保留的直接替代项 |
| [nkolban/ESP32_BLE_Arduino](https://github.com/nkolban/ESP32_BLE_Arduino#readme) | 官方声明仓库废弃；BLE 已包含在 ESP32 Arduino 核心中 | 无已确认且保留的直接替代项 |
| [NorthernWidget/Logger](https://github.com/NorthernWidget/Logger#readme) | 官方在弃用通知中引导使用新一代数据记录器，旧 ALog 已退出当前支持方案 | 无已确认且保留的直接替代项 |
| [Qudor-Engineer/DMD32](https://github.com/Qudor-Engineer/DMD32#readme) | 作者明确宣布不再维护 | 无已确认且保留的直接替代项 |
| [Seeed-Studio/TFT_Touch_Shield_V2](https://github.com/Seeed-Studio/TFT_Touch_Shield_V2#readme) | 官方声明旧库废弃，仅继续支持 Seeed_Arduino_LCD | 无已确认且保留的直接替代项 |
| [xoseperez/eeprom32_rotate](https://github.com/xoseperez/eeprom32_rotate#readme) | 作者说明 ESP32 SDK 已破坏兼容性，库不再维护 | 无已确认且保留的直接替代项 |

Seeed_Arduino_LCD 是 TFT_Touch_Shield_V2 官方推荐的替代库，但不在原列表中；本次仅移除原列表条目，没有新增或核定该替代库的维护状态。

## 相同仓库地址去重

| 移除地址 | 保留地址 |
| --- | --- |
| https://github.com/adafruit/Adafruit_AHT10 | https://github.com/adafruit/Adafruit_AHTX0 |
| https://github.com/BlaT2512/sevenSegment | https://github.com/BlaT2512/Segment |
| https://github.com/koendv/Arduino-RTTStream | https://github.com/koendv/RTTStream |
| https://github.com/m5stack/M5Unit-4RELAY | https://github.com/m5stack/M5Unit-RELAY |
| https://github.com/arielzw/DPS-Power-Supply-library-for-Arduino | https://github.com/arielzw/DPS-Power-Supply |
| https://github.com/juano2310/SuperCAN | https://github.com/juano2310/SuperCANBus |

## 保留待核实

| 地址 | 未确认事项 |
| --- | --- |
| https://gitlab.com/alexpr0/ssd1306wire.git | 未能核实发布记录，保留待复核 |
| https://gitlab.com/Enrico204/sam32wifiesp | 未能核实发布记录，保留待复核 |
| https://bitbucket.org/mgf_ryan/smartsystem | 未能核实发布记录，保留待复核 |
| https://gitlab.com/8bitforce/kdram2560/ | 未能核实发布记录，保留待复核 |
| https://github.com/OpenSynaptic/OSynaptic-FX | README 同时称自身为当前源库及已弃用仓库，说明互相矛盾，保留待核实 |

## 功能相近但没有直接合并的示例

- SparkFun u-blox GNSS v2 与 v3：新版 README 要求旧 M8 模块继续使用 v2，不能只保留 v3。证据：[v3 官方说明](https://github.com/sparkfun/SparkFun_u-blox_GNSS_v3#readme)。
- HomeSpan 与 HomeSpan-zh：API 比较发现中文分支还包含 `src/Network.cpp` 修改，不能将其当作纯文档副本。证据：[分支比较](https://github.com/HomeSpan/HomeSpan/compare/master...CuiYao631:master)。
- MPU6050：轻量函数接口与较完整的传感器实现并不直接互换；AdvancedSerial：标签/十六进制输出与链式日志及级别控制的侧重点不同。
- LogicAnalyzer：SUMP 协议采集固件与通用分析辅助库不同；Alarm：定时回调与阈值报警不同，虽同名仍保留。

其余未确认等价的同名库如下，保留并不表示已经完成所有 API 对比：

| 库名 | 保留地址 |
| --- | --- |
| advancedserial | https://github.com/ZeeDesigns7/AdvancedSerial, https://github.com/klenov/advancedSerial |
| alarm | https://github.com/Muhammed-jbareen/Alarm, https://github.com/zimbora/esp32-alarm |
| base64 | https://github.com/agdl/Base64, https://github.com/Densaugeo/base64_arduino |
| ezbutton | https://github.com/ArduinoGetStarted/button, https://github.com/IPdotSetAF/EZButton |
| homespan | https://github.com/HomeSpan/HomeSpan, https://github.com/CuiYao631/HomeSpan-zh |
| irremote | https://github.com/Seeed-Studio/IRSendRev, https://github.com/z3t0/Arduino-IRremote |
| led | https://github.com/dirkohme/LED, https://github.com/yesbotics/arduino-lib-led |
| logicanalyzer | https://github.com/gillham/logic_analyzer, https://github.com/RobTillaart/logicAnalyzer |
| mhz19 | https://github.com/malokhvii-eduard/arduino-mhz19, https://github.com/fikrielektro21/MHZ19 |
| modbusrtu | https://github.com/zimbora/esp32-ModbusRTU, https://github.com/peto-3210/ModbusRTU |
| mpu6050 | https://github.com/Ewan-Dev/mpu6050, https://github.com/ElectronicCats/mpu6050 |
| rtc | https://github.com/cvmanjoo/RTC, https://github.com/Pandiyarajk/rtc |
| serialterminal | https://github.com/SMFSW/SerialTerminal, https://github.com/siroshy/SerialTerminalIO |
| simplewifimanager | https://github.com/Esslangamer20/SimpleWiFiManager, https://github.com/mvoss96/SimpleWifiManager |
| ssd1306 | https://github.com/lexus2k/ssd1306, https://github.com/vkumpan/SSD1306 |
| trioe | https://github.com/MJBeltran13/trioe, https://github.com/MJBeltran13/Bucopi_library |
| ultrasonic | https://github.com/ErickSimoes/Ultrasonic, https://github.com/Pranjal-Prabhat/ultrasonic-arduino |

## 校验

已逐行验证保留列表是原列表的有序子集、每个移除项都有原因、保留项不存在同仓库地址重复，且人工标记保留的替代库确实在输出中。原行内容保存在逐项记录，便于追溯及恢复。
