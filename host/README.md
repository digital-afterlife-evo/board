# KX-R530 Agent bridge

This service owns the ESP serial connection and exposes the Agent API.

```powershell
python -m pip install -r host/requirements.txt
python -m host.app --port COM5
```

HTTP is served on `http://127.0.0.1:8765` and the Agent WebSocket endpoint is
`ws://127.0.0.1:8766/api/v1/agent`.

Useful calls:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/v1/health
Invoke-RestMethod http://127.0.0.1:8765/api/v1/input/submit -Method Post `
  -ContentType 'application/json' -Body '{"text":"hello"}'
Invoke-RestMethod http://127.0.0.1:8765/api/v1/print -Method Post `
  -ContentType 'application/json' -Body '{"text":"HELLO WORLD"}'
```

The serial protocol is binary COBS/CRC and is implemented in `host/protocol.py`.
The service intentionally keeps the keyboard integration out of this first
bridge implementation.
