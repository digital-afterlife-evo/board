# KX-R530 键盘桥接与上位机对接说明

版本：2026-09-24  
目标固件：`kxr530_esp32s3`，ESP32-S3，ESP-IDF 6.1

## 1. 系统职责

ESP32 固件负责接收 USB HID 键盘输入、接收上位机 UART 协议帧，并通过同一个打印队列驱动 KX-R60。上位机不需要实现 KX-R60 的 GPIO 和 ACK 时序。

```text
USB HID keyboard
        │ USB OTG Host / Boot HID
        ▼
usb_keyboard.c ── ASCII/control bytes ──┐
                                        ▼
UART0 protocol_task ──► printer_enqueue() ──► printer_task ──► kxr60_send_byte()
                                        │                         │
                                        └──── UART responses ◄────┘
                                                                ▼
                                                          KX-R530
```

`printer_task` 是唯一调用 KX-R60 发送函数的任务。键盘和上位机数据都进入 `printer_enqueue()`，因此不会并发打乱 ACK 握手。

## 2. 硬件接口

### UART 控制台

- UART0，115200 baud，8N1
- 默认 TX：GPIO43；RX：GPIO44
- 当前开发板通常对应 COM5，实际端口以 Windows 枚举结果为准
- COM5 用于烧录和运行时二进制协议
- USB Serial/JTAG 不作为应用控制台，因为 USB OTG Host 会占用 ESP32-S3 USB PHY 和 GPIO19/20

### USB 键盘

- 键盘接到连接 USB OTG D+/D- 的 Type-C 口。
- OTG 口必须有 5 V VBUS；当前开发板需要焊接背面的 USB OTG 供电脚位。
- 另一只 Type-C 口用于 UART0、烧录和监视器。
- 当前实现支持 Boot Protocol HID Keyboard；非 Boot Protocol 键盘可能被忽略。

正常启动日志：

```text
USB HID keyboard bridge ready
USB HID keyboard connected
```

## 3. USB 键盘实现

代码位置：

- `main/usb_keyboard.c`
- `main/keyboard_map.c`
- `main/keyboard_map.h`

按键只在 HID 报告中首次出现时发送一次，保持按下不会产生固件级重复发送。Caps Lock 在 ESP32 内维护状态，并通过 HID Output Report 回写键盘 LED；固件优先使用报告 ID 0，失败后尝试报告 ID 1。

### 当前映射

| USB 键盘按键 | 发送字节或行为 |
|---|---|
| A-Z | ASCII 小写或大写；Shift 与 Caps Lock 使用 XOR 逻辑 |
| 0-9、标点、Shift+标点 | 标准 US HID 键位对应 ASCII |
| Space | `0x20` |
| Enter | `0x0A 0x0D`，先换行，再回到新行行首，按 KX-R530 实机行为确定 |
| Backspace | `0x08` |
| Tab | `0x09` |
| Caps Lock | 不发送字符，切换大小写状态并更新键盘 LED |
| Ctrl+H / Ctrl+I / Ctrl+J / Ctrl+M | `0x08` / `0x09` / `0x0A` / `0x0D` |
| Ctrl+[ | `0x1B`，ESC 起始字节 |
| 数字小键盘 0-9 | 对应 ASCII 数字 |
| 数字小键盘小数点 | `.` |
| 方向键、Insert、Delete、F1-F24、Esc、Alt、Win | 不发送，记录 `unmapped HID key` |
| 打字机 Code 键 | 当前没有已确认的通用 KX-R60 字节，未绑定 USB 按键 |

上游资料中出现过的扩展序列尚未确认适用于 KX-R530，因此未绑定到普通键：

| 功能 | 字节序列 |
|---|---|
| 粗体开/关 | `0x1B 0x45` / `0x1B 0x46` |
| 下划线开/关 | `0x1B 0x2D 0x31` / `0x1B 0x2D 0x30` |

## 4. 固件任务架构

当前协议版 `app_main()` 的启动顺序：

1. 初始化 UART0 传输层。
2. `printer_start(16384)` 创建 KX-R60 打印任务和 16 KiB 队列。
3. `usb_keyboard_start(emit_keyboard_byte)` 启动 USB Host；键盘字节回调调用 `printer_enqueue()`。
4. 创建 `protocol_task`，负责 UART 收包、协议解码、设备状态和响应帧。
5. `app_main()` 返回是正常行为，后台任务继续运行。

`printer.c` 内部使用 `s_kxr_mutex` 保护 KX-R60，使用 StreamBuffer 保存待打印字节。ACK 超时后会设置 `faulted` 并清空队列；上位机必须处理错误并等待恢复。

## 5. UART 协议帧

UART 线上不是纯文本。每帧使用 COBS 编码，帧尾为 `0x00`。上位机按 `0x00` 分帧后 COBS decode。

解码后的原始帧布局，整数为 little-endian：

| 偏移 | 长度 | 字段 |
|---:|---:|---|
| 0 | 1 | 协议版本，当前为 `1` |
| 1 | 1 | 消息类型 |
| 2 | 1 | flags，当前为 `0` |
| 3 | 16 | request_id |
| 19 | 4 | sequence |
| 23 | 2 | payload_length |
| 25 | N | payload |
| 25+N | 4 | CRC32 |

CRC32 覆盖偏移 0 到 payload 末尾，不包含 CRC 字段。算法初值为 `0xFFFFFFFF`，多项式为 `0xEDB88320`，最后按位取反。实现见 `main/protocol.c` 的 `device_crc32()`。

最大 payload 为 480 字节，最大 COBS 编码帧为 560 字节。

## 6. 消息类型

| 数值 | 名称 | 方向 | Payload |
|---:|---|---|---|
| 1 | `HELLO` | ESP32 -> Host | JSON 设备信息 |
| 2 | `HELLO_ACK` | Host -> ESP32 | 空 |
| 3 | `STATE` | ESP32 -> Host | JSON 状态 |
| 4 | `INPUT_DELTA` | 双向 | ASCII 输入增量 |
| 5 | `INPUT_SUBMITTED` | 双向 | 请求提交确认 |
| 6 | `PRINT_DATA` | Host -> ESP32 | ASCII 打印数据 |
| 7 | `PRINT_END` | Host -> ESP32 | 空 |
| 8 | `PRINT_CREDIT` | ESP32 -> Host | JSON credit/queued |
| 9 | `PRINT_PROGRESS` | ESP32 -> Host | 空 |
| 10 | `RECOVER` | Host -> ESP32 | 空 |
| 11 | `CANCEL` | Host -> ESP32 | 空 |
| 12 | `ERROR` | ESP32 -> Host | JSON 错误 |
| 13 | `PING` | Host -> ESP32 | 任意 payload |
| 14 | `PONG` | ESP32 -> Host | 原样回显 payload |

## 7. 上位机交互流程

设备启动后发送：

```json
{"device":"kxr530-esp32s3","protocol":1,"charset":"ascii","printer_capacity":16384}
```

上位机收到 `HELLO` 后发送 `HELLO_ACK`，再读取 `STATE` 和 `PRINT_CREDIT`。

直接打印时：

1. 生成 16 字节随机 `request_id`。
2. 发送 `PRINT_DATA(request_id, ascii_chunk)`。
3. 根据 `PRINT_CREDIT` 控制未确认数据量，不要连续灌满 UART。
4. 处理 `PRINT_PROGRESS`、`PRINT_CREDIT` 和 `ERROR`。
5. 完成后发送 `PRINT_END(request_id)`。
6. 等待 `STATE.state == "editing"`。

`INPUT_DELTA`/`INPUT_SUBMITTED` 是设备编辑状态接口；USB 键盘输入不会作为 UART 回显帧发送，而是直接进入 ESP32 打印队列。上位机如果要显示键盘镜像，需要在自己的输入层维护。

上位机应实现：

- 串口读线程和 `0x00` 分帧
- COBS decode 和 CRC32 校验
- HELLO/HELLO_ACK 握手
- request_id 管理
- PRINT_CREDIT 限流
- 断线后重新等待 HELLO，不复用旧 sequence/request_id
- ERROR 和 fault 状态处理

不要使用 `idf_monitor` 作为协议客户端。当前 UART 承载 COBS 二进制帧，直接查看会出现乱码或 `Failed to decode multiple lines`；应使用自有二进制串口客户端。

## 8. 故障诊断

| 现象 | 判断 |
|---|---|
| `Checksum mismatch between flashed and built applications` | 芯片镜像与 monitor 使用的 ELF 不同；必须用同一个 `build-v6.1` 目录重新烧录 |
| `main_task: Returned from app_main()` | 当前协议版正常，`app_main()` 创建后台任务后返回 |
| JSON/乱码 | 当前 UART 输出 COBS 二进制协议帧，不是纯文本日志 |
| 只有 `USB HID keyboard bridge ready` | USB Host 已启动但没有检测到键盘，检查 OTG VBUS 和连接 |
| `keyboard LED report failed` | 键盘不支持标准 Boot HID LED 报告，普通输入仍可继续 |
| `printer_queue_full` | 上位机发送过快，应根据 `PRINT_CREDIT` 限流 |
| `faulted=true` | KX-R60 ACK 或电气链路失败，应停止发送并处理 `RECOVER` |
