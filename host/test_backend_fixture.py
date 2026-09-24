"""Test-only host with a simulated UART; never opens a physical serial port."""
import json
import sys
import threading
import time
import uuid

from host.api import AgentGateway
from host.bridge import DeviceService, DeviceSnapshot
from host.protocol import Frame, INPUT_SUBMITTED, PRINT_DATA, PRINT_END, RECOVER, STATE


def main():
    service = DeviceService()

    class SimulatedBridge:
        def __init__(self):
            self.snapshot = DeviceSnapshot(connected=True, state="editing", device="kxr530-esp32s3", credit=16384, capacity=16384)
            self.snapshot.test_output = ""

        def send(self, kind, payload=b"", request_id=None):
            if kind == RECOVER:
                service._on_frame(Frame(STATE, 0, bytes(16), 0, b'{"state":"editing","queued_bytes":0}'))
            if kind == PRINT_DATA:
                self.snapshot.test_output += payload.decode("ascii")
            if kind == PRINT_END:
                def drain():
                    for state in ("draining", "editing"):
                        service._on_frame(Frame(STATE, 0, bytes(16), 0, json.dumps({"state": state, "queued_bytes": 0}).encode()))
                threading.Timer(.03, drain).start()

    service.bridge = SimulatedBridge()
    service.snapshot = service.bridge.snapshot
    gateway = AgentGateway(service, port=int(sys.argv[1]))
    gateway.start()
    stopped = threading.Event()
    def heartbeat():
        while not stopped.wait(.1):
            service.snapshot.last_device_seen_at = int(time.time() * 1000)
    threading.Thread(target=heartbeat, daemon=True).start()
    print("READY", flush=True)
    try:
        for line in sys.stdin:
            message = json.loads(line)
            rid = message.get("request_id", str(uuid.uuid4()))
            if message.get("type") == "fault":
                service.snapshot.state = "fault"
                service.snapshot.last_error = "ACK timeout: simulated"
                service._changed()
            elif message.get("type") == "disconnect":
                service.snapshot.connected = False
                service._changed()
            elif message.get("type") == "replay":
                service._emit_agent({"v": 1, "type": "input.submitted", "request_id": rid, "text": message["text"], "locally_printed": True})
            else:
                service.snapshot.test_output += message["text"]
                service._on_frame(Frame(INPUT_SUBMITTED, 0, uuid.UUID(rid).bytes, 0, message["text"].encode("ascii")))
    finally:
        stopped.set(); gateway.stop()


if __name__ == "__main__":
    main()
