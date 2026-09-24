# 数字余生 · 实体打字机链路

本仓库负责黑客松项目「数字余生」的实体文字呈现：ESP32-S3 接收 USB 键盘输入、驱动松下 KX-R530 打字机，Windows 上位机连接串口，并与相邻的 `evo-backend` 对接。网页对话由 `../web` 和 `../evo-backend` 完成；实体设备断开时，网页仍可独立对话。

## 组成与数据流

```text
USB 键盘 → espidf/ 固件 → KX-R60 接口 → KX-R530 纸面
                    ↕ UART0（COBS/CRC）
                host/ 上位机
                    ↕ HTTP 8765 + WebSocket 8766
              ../evo-backend → 聊天模型
```

- `espidf/` 是独立 Git 仓库，内含 ESP-IDF 固件、硬件接线和构建说明；更改固件时先检查它自己的 Git 状态。
- `host/` 是 Python 上位机：`protocol.py` 编解码串口帧，`bridge.py` 管理设备状态和打印流，`api.py` 提供本机 HTTP 与 Agent WebSocket，`app.py` 提供图形或无界面入口。
- 实体键盘输入在 ESP32 暂存，Ctrl+Enter 提交后转交后端；模型回复经上位机流式送往打印机。实体打字机仅支持适配的 ASCII/英文输出，网页保留完整中文对话。

## 启动上位机

需要 Python 3.10+，以及已经烧录并接线的设备。先在仓库根目录安装依赖，再确认设备管理器中的真实串口号：

```powershell
python -m pip install -r "host/requirements.txt"
python -m host.app --port COM4 --headless
```

`start.cmd` 使用本仓库 `.venv`，默认 COM4、无界面运行，并设置机械打印节奏；本机端口若不同可传入 `--port COMx`。图形界面可省略 `--headless`，启动后手动连接串口。上位机默认只监听 `127.0.0.1`：HTTP 为 `http://127.0.0.1:8765`，Agent WebSocket 为 `ws://127.0.0.1:8766/api/v1/agent`。

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/v1/health"
Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/v1/state"
```

健康检查仅证明 HTTP 进程可达；还需查看设备连接、WebSocket 和实际串口状态。直接调用 `/api/v1/print` 会产生纸面输出，请在设备准备好后使用。完整协议、硬件接线与固件构建分别见 [上位机交接说明](host/docs/backend-handoff.md) 和 [固件说明](espidf/README.md)；交接说明记录了特定时点的联调状态，遇到接口差异以当前代码和实测为准。

## 联调与验证

在相邻后端 `.env` 中设置 `BACKEND_BOARD_HTTP_URL=http://127.0.0.1:8765`；WebSocket 默认可由该地址推导，也可显式设置 `BACKEND_BOARD_WS_URL`。启动顺序为上位机、后端、网页。网页输入与实体键盘输入分别进入后端会话；打印队列的“交付”状态不能代替纸面完整性检查。

```powershell
python -m unittest discover -s "host" -p "test_*.py"
```

固件在 `espidf/` 中使用 ESP-IDF 构建；烧录、端口与供电操作前请核对该目录的接线表。上位机与固件的协议测试只能覆盖软件行为，不能证明实际 ACK 电平、走纸或落字正确。

## 来源与许可

`espidf/` 的 KX-R60 传输时序基于上游接口项目改造，来源及 GPL-3.0 许可见 [固件归属说明](espidf/NOTICE.md) 与 [固件许可证](espidf/LICENSE)。本仓库的目标是数字余生实体演示链路，而非通用打字机驱动发行版。
