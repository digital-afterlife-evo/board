# KX-R530 Agent bridge

This service owns the ESP serial connection and exposes the Agent API.

完整中文交接文档：[后端通信协议与联调说明](docs/backend-handoff.md)。
文档按当前代码核对，包含 HTTP、WebSocket、UART 帧格式、状态机及已知限制。

```powershell
python -m pip install -r host/requirements.txt
python -m host.app --port COM5
```

HTTP is served on `http://127.0.0.1:8765` and the Agent WebSocket endpoint is
`ws://127.0.0.1:8766/api/v1/agent`.

Useful calls:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/v1/health
Invoke-RestMethod http://127.0.0.1:8765/api/v1/print -Method Post `
  -ContentType 'application/json' -Body '{"text":"HELLO WORLD"}'
```

The serial protocol is binary COBS/CRC and is implemented in `host/protocol.py`.
Keyboard text is buffered on the ESP32 and uploaded on Ctrl+Enter. The host
displays the submitted text and forwards it as a WebSocket `input.submitted`
event. See the handoff document before relying on long messages, reconnects,
flow control, or `/api/v1/input/submit`, which currently has an ID argument bug.
