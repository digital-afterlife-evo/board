# KX-R530 / ESP32-S3 KX-R60 接口

原生 ESP-IDF 固件，通过 SN74LS07N 接口电路驱动松下 KX-R530。
当前固件使用串口上的 COBS/CRC 全双工协议和后台打印队列；上电不自动打印。
PC 端桥接程序位于仓库根目录的 `host/`，提供 PyQt6 界面、HTTP API 和 Agent WebSocket。
协议参考和 GPL-3.0 许可见 [NOTICE.md](NOTICE.md) 和 [LICENSE](LICENSE)。

## 当前硬件约定

按用户 2026-09-22 更新：使用已确认的真实 **5 V** 电源，ACK 外部上拉改为 **10 kΩ**。
SN74LS07 是非反相开集电极缓冲器，固件不额外反相。

| 信号 | ESP32-S3 | LS07 输入 → 输出 | 打字机杜邦线 | Mini-DIN |
|---|---|---|---|---|
| ONLINE | GPIO47 → | 1 → 2 | 紫（原红） | 8 |
| TXD | GPIO2 → | 3 → 4 | 蓝 | 6 |
| STB | GPIO42 → | 5 → 6 | 白（原青） | 7 |
| ACK | → GPIO1 | 9 → 8 | 灰 → LS07 pin 9 | 4 |
| GND | GND | pin 7 | 黑 | 3 |

- LS07 pin 14 → 已确认的 +5 V，pin 7 → GND；14 和 7 旁路 **100 nF**。
- **LS07 pin 8 / GPIO1 → 10 kΩ → ESP32 的 3.3 V**，不是 5 V。
- 推荐 LS07 输入 pin 1、3、5 各用一只 10 kΩ 上拉至 3.3 V，保证启动/复位时释放输出。
  若只有一只 10 kΩ，优先用于 ACK；启动上拉需要另外三只电阻。
- LS07 未用输入 pin 11、13 接地，输出 pin 10、12 悬空。
- ESP32、LS07、打字机黑线、外部 5 V 电源共地。
- 打字机侧 pin 2、4、6 不增加到 3.3 V 的上拉，它们由打字机侧上拉至约 5 V。
- 橙线 / Mini-DIN pin 5 是 **12 V**，单独绝缘，不接面包板。
- 独立 5 V 仅给 LS07 供电时，不需要再接开发板的 5 V 引脚；避免与 USB 电源互相回灌。
- GPIO 定义集中在 `components/kxr60/include/hw_config.h`。

## 本机开发环境与构建

本机使用 EIM 管理的 ESP-IDF `v6.1.0`，源码位于
`C:/esp/v6.1/esp-idf`，工具和 Python 环境位于 `C:/Espressif/tools`。
上游检出到 `upstream/`，仅供参考，不参与构建。项目级激活脚本
`tools/export-idf.ps1` 已指向这套本机环境。

在项目根目录的 PowerShell 中执行：

```powershell
. .\tools\export-idf.ps1
$env:PYTHONUTF8 = '1'
idf.py -B build-v6.1 set-target esp32s3
idf.py -B build-v6.1 build
```

后续代码修改只需 `idf.py -B build-v6.1 build`。`set-target` 会重新生成配置，不需要每次执行。
固件在 `build-v6.1/kxr530_esp32s3.bin`；烧录时应使用 IDF 同时写入引导程序和分区表。

当前双 Type-C 固件使用 **UART0/115200** 作为控制台，通常对应板载 CP210x/CH340
串口（例如 COM5），默认 UART0 使用 ESP32-S3 GPIO43/44；USB Serial/JTAG 不再作为
应用控制台，以便 USB OTG Host 能独占 GPIO19/20。
自定义 UART 引脚不得与 KX-R60 四根 GPIO 重叠。具体 COM 号和 Type-C 接口映射需按开发板
丝印及设备管理器确认。

## USB HID 键盘桥接

固件通过 ESP32-S3 的 **USB OTG Host 口**接收 Boot Protocol USB HID 键盘，另一只 Type-C
口作为 UART0 控制台和烧录口。键盘必须接在连接到 USB OTG D+/D- 的那只 Type-C 口；仅支持
USB Serial/JTAG 或 USB-UART 的 Type-C 口不能作为键盘 Host。OTG 口还必须为键盘提供稳定的
VBUS 5 V，具体哪个物理接口是 OTG 口取决于开发板型号。

键盘连接后，监视器中应出现 `USB HID keyboard connected`。按键只在按下沿发送一次，键盘保持
按下不会重复发送；发送仍经过 KX-R60 的 ACK 握手，并与控制台命令串行化。

除普通字母外，当前映射如下：

| USB 键盘按键 | 发送字节或行为 | 说明 |
|---|---|---|
| 数字、标点、Shift+数字/标点 | 对应 ASCII | 使用标准 US HID 键位定义 |
| Shift + 字母 | `A`..`Z` | 发送大写 ASCII |
| Caps Lock | 不发送字节 | 在固件中切换大小写状态 |
| Space | `0x20` | 空格 |
| Enter | `0x0A` + `0x0D` | 先换行，再回到新行行首；KX-R530 专用顺序 |
| Backspace | `0x08` | 退格 |
| Tab | `0x09` | KX-R60 是否执行制表由打字机决定 |
| Ctrl+H / Ctrl+I / Ctrl+J / Ctrl+M | `0x08` / `0x09` / `0x0A` / `0x0D` | 常用控制字节 |
| Ctrl+[ | `0x1B` | ESC 起始字节 |
| 打字机 Code 键 | 未映射 | USB 键盘没有对应的 KX-R60 通用字节；当前不会假装发送 Code |
| 方向键、Insert、Delete、F1..F24、Esc、Alt、Win、Num Lock | 不发送 | 日志记录为 `unmapped HID key` |

KX-R60 的基本打印接口没有为方向键、光标移动和大多数打字机面板功能定义通用字节。
上游资料中出现的 ESC 扩展序列（例如粗体、下划线）尚未确认适用于 KX-R530，因此当前
不会把 F 键或其他普通键擅自映射成这些序列。

### KX-R60 特殊字节参考表

下面是协议资料和上游测试代码中出现过的特殊序列。它们是发送给接口的字节序列，
不等同于物理按下打字机的 Code 键；只有普通 ASCII 和基础控制键已经接入 USB 键盘桥接。

| 功能 | 字节序列 | 当前 USB 键盘状态 |
|---|---|---|
| 回车并换行 | `0x0A 0x0D` | Enter 已映射，KX-R530 专用顺序 |
| 退格 | `0x08` | Backspace、Ctrl+H 已映射 |
| Tab | `0x09` | Tab、Ctrl+I 已映射，打字机是否执行由机器决定 |
| 换行 | `0x0A` | Ctrl+J 已映射 |
| ESC 起始 | `0x1B` | Ctrl+[ 已映射 |
| 粗体开 | `0x1B 0x45` | 未绑定按键 |
| 粗体关 | `0x1B 0x46` | 未绑定按键 |
| 下划线开 | `0x1B 0x2D 0x31` | 未绑定按键 |
| 下划线关 | `0x1B 0x2D 0x30` | 未绑定按键 |
| Code 键本身 | 无已确认通用字节 | 未映射 |

也可以单独构建 UART 版本，不影响默认 USB 配置：

```powershell
idf.py -B tests/build/uart -D SDKCONFIG=tests/build/sdkconfig.uart -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.defaults.uart' build
# UART 版本烧录/监视，COMxx 同样需要替换为真实端口：
idf.py -B tests/build/uart -D SDKCONFIG=tests/build/sdkconfig.uart -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.defaults.uart' -p COMxx flash monitor
```

确认端口后：

```powershell
python -m serial.tools.list_ports -v
# 将 COMxx 替换为已确认的 ESP32-S3 端口
idf.py -p COMxx flash monitor
```

不根据蓝牙 COM 端口猜测烧录目标。串口监视器退出键为 Ctrl+]。

## 首次接线验证与打印（旧版行控制台参考）

下面的 `status/raw/print` 命令对应旧版人机控制台，仅用于理解线路和 KXR60
低层行为。当前固件启动的是 Agent 二进制协议，请使用仓库根目录的 `host/` 桥接程序。

1. 先不连接打字机，确认 LS07 pin 14 约 5 V、共地正确。要检查 ACK 接收通路时，
   将 LS07 pin 9 明确接 GND，pin 8 应为低；再将 pin 9 明确置为合法高电平，
   pin 8 应约 3.3 V。不要让 TTL 输入悬空来判断可靠性。
2. 固件启动后，ONLINE、TXD、STB 的 ESP32 侧应为高电平。启动不发送任何字节。
3. 接上打字机，检查 ONLINE/TXD/STB 的打字机侧约 5 V，ACK 空闲预期低，
   GPIO1 始终只在 0～3.3 V 范围内。电压预期来自交接文档，仍须在本机实测。
4. 按 **KX-R530 本机已确认的操作方法**进入 On-Line 模式；不能套用上游 KX-R435 的快捷键。
5. 依次执行，每次等待 `kxr60>` 提示符：

```text
status
raw 41
repeat 41 10
print HELLO WORLD
demo
```

`raw 41` 应先在纸上打印一个 A。确认成功后再发文字。

| 命令 | 行为 |
|---|---|
| `status` | GPIO、初始化/故障状态、ACK、最近驱动错误、成功字节数、超时数、失败位与期望 ACK |
| `raw XX` | 恰好两位十六进制，发送任意一个字节，包括 00；例如 `raw 0D`、`raw 0A` |
| `repeat XX N` | 将一个字节发送 N 次，N 为 1..100，每次成功握手后等待 200 ms；首次错误立即停止 |
| `print TEXT` | 发送 TEXT 字面内容，保留空格和引号；**不追加 CR/LF** |
| `demo` | 发送 `HELLO WORLD` 加 CR/LF，共 13 字节 |
| `recover` | 重新初始化并清除故障锁定，保留计数，不发送字节 |
| `help` | 显示命令说明 |

控制台基于 ESP-IDF 驱动与 stdio，支持 CR、LF、CRLF 回车，退格和 Ctrl-U 清除输入。
每行最多 255 字节（含命令），超长、非 ASCII、其他不支持的控制字符均整行拒绝。
中文/UTF-8 不会被截成字节发送；`print` 不解释 `\r` / `\n` 转义。
旧版控制台采用同步打印；**每次只提交一条命令，等待提示符，不批量粘贴/连续串流**。
Ctrl-C 只在编辑输入时清行，不能中断正在执行的同步打印。需要停机时可退出打字机
On-Line 模式；ACK 超时后固件释放输出。后台队列和取消由当前协议固件的
`printer_task` 与 `CANCEL` 消息负责。

## 协议与恢复

每字节发送前先等 ACK 空闲 LOW，再将 ONLINE 拉 LOW。按 D0～D7 发送，
TXD 设置后用 `esp_rom_delay_us(50)` 保持至少约 50 μs，STB 拉 LOW，等 ACK HIGH，
STB 拉 HIGH，等 ACK LOW。第八位完成后 ONLINE 拉 HIGH，再将 TXD 恢复 HIGH。
最后这一 TXD 释放是相对上游的明确改动，不改变 STB 采样和字节结束顺序。

ACK 默认每阶段超时 500 ms，可在 `menuconfig → KX-R60 interface` 中修改。
轮询使用单调微秒计时；长等待每约 1 ms 让出一个 FreeRTOS tick，成功字节之间也让出一个 tick。
不关闭中断，不在 50 μs 建立时间内用 `vTaskDelay`。调度可能延长阶段时长，
实际握手波形、LS07 传播延迟和上拉 RC 仍需逻辑分析仪验证。

超时/驱动错误先释放 ONLINE/STB/TXD，再输出日志并锁定发送，避免自动重发半个字节。
完成 ACK 的字节才计为成功；“成功字节数”不是纸面打印反馈。
排查线路、电源和 On-Line 状态，必要时退出/重新进入 On-Line 模式或重启打字机以清除半字节状态，
然后执行 `recover`、`status`、`raw 41`。`recover` 本身不能保证打字机内部状态已经恢复。

INFO 记录逐字节开始/结束和错误；DEBUG 额外记录位与握手耗时。
正常联调用 INFO，位级日志会改变时序。

底层 API 与串口独立，当前由专用 `printer_task` 单独拥有；网络/协议回调只能把数据放入队列。
任务结束前调用 `kxr60_deinit()`，正常软件重启注册了释放钩子。
异常掉电/硬复位仍依靠外部上拉保证状态。

## Agent 协议与上位机

新固件不再把串口当作 `print TEXT` 行命令解释。设备发送 `HELLO`、状态、候选区
事件和打印 credit；主机发送 `INPUT_DELTA`、`INPUT_SUBMITTED`、`PRINT_DATA` 和
`PRINT_END`。帧使用 COBS 编码，以 `0x00` 分隔，并带 CRC32。单帧负载上限为 480
字节，设备打印 FIFO 为 16 KiB。

在 PC 上安装依赖并启动桥接程序：

```powershell
python -m pip install -r host/requirements.txt
python -m host.app --port COM5
```

HTTP 服务默认监听 `127.0.0.1:8765`，Agent WebSocket 默认监听
`ws://127.0.0.1:8766/api/v1/agent`。可通过 `GET /api/v1/health`、`GET /api/v1/state`、
`POST /api/v1/device/recover`、`POST /api/v1/input/submit` 和 `POST /api/v1/print`
做基本联调。`/api/v1/print` 按 handoff 流程直接发送 `PRINT_DATA`，再发送
`PRINT_END`，由设备的 `PRINT_CREDIT` 控制发送速度。

设备端仍保留 ASCII 严格校验。当前提交接口会先把输入放入设备打印队列，再进入
thinking 状态；Agent 回复按 ASCII 分片发送，设备按 KXR60 ACK 速度逐字节输出。

键盘 HID 文件暂时保留在 `main/`，本版启动流程不主动接管键盘；后续接入应把按键事件
送入同一个输入状态机和打印队列，不能直接调用 KXR60 驱动。

## 验证

主机模拟测试直接编译生产驱动，模拟打字机 ACK：

```powershell
python tests/run_host_tests.py
# 也可显式指定本地 C 编译器
python tests/run_host_tests.py --cc D:/Dev-Cpp/MinGW64/bin/gcc.exe
```

测试涵盖 256 个字节的位序和建立时间、16 种逐位 ACK 故障、初始 ACK 卡高、部分写入计数、
故障锁定与恢复、初始化/GPIO 失败、空指针和停用、慢 ACK 的任务让出。
现有脚本中的旧版控制台测试仍保留作历史回归参考；新的主机协议测试位于 `host/test_*.py`，
覆盖 COBS/CRC、坏帧重同步、回复分片、credit 不足时延迟 `PRINT_END`。
模拟测试和编译不等同于真实电气/机械验证。

2026-09-22 本机验证结果：ESP32-S3 目标设置成功；USB Serial/JTAG 和 UART0
两种配置均编译成功；上述主机模拟测试全部通过。默认 USB 应用固件为 210,000 字节。

实机验收待完成：安全电压、启动波形、纸上打印 A/HELLO WORLD、ACK 故障后释放与恢复。
