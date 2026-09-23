from __future__ import annotations

import argparse
import sys

from .api import AgentGateway
from .bridge import DeviceService


def run() -> int:
    parser = argparse.ArgumentParser(description="KX-R530 Agent bridge")
    parser.add_argument("--port", default="COM5", help="serial port")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--api-port", type=int, default=8765)
    args = parser.parse_args()

    service = DeviceService()
    try:
        service.bridge.open(args.port, args.baud)
    except Exception as exc:
        print(f"serial connection failed: {exc}", file=sys.stderr)
    gateway = AgentGateway(service, port=args.api_port)
    gateway.start()

    try:
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import (QApplication, QLabel, QMainWindow, QPushButton,
                                     QTextEdit, QVBoxLayout, QWidget)
    except ImportError:
        print(f"headless bridge running: HTTP http://127.0.0.1:{args.api_port}, "
              f"WebSocket ws://127.0.0.1:{args.api_port + 1}")
        try:
            while True:
                input()
        except (EOFError, KeyboardInterrupt):
            gateway.stop()
            service.bridge.close()
            return 0

    qt_app = QApplication(sys.argv)
    window = QMainWindow()
    window.setWindowTitle("KX-R530 Agent Bridge")
    status = QLabel("connecting")
    candidate = QTextEdit()
    candidate.setReadOnly(True)
    queue = QLabel("queue: 0")
    recover = QPushButton("Recover")
    recover.clicked.connect(service.recover)
    layout = QVBoxLayout()
    for widget in (status, candidate, queue, recover):
        layout.addWidget(widget)
    root = QWidget()
    root.setLayout(layout)
    window.setCentralWidget(root)

    def refresh() -> None:
        snapshot = service.snapshot.as_dict()
        status.setText(f"{snapshot['state']} | {snapshot.get('port') or 'no serial'}")
        candidate.setPlainText(snapshot.get("candidate", ""))
        queue.setText(f"queue: {snapshot.get('queued_bytes', 0)} / "
                      f"{snapshot.get('capacity', 0)} | credit {snapshot.get('credit', 0)}")

    timer = QTimer()
    timer.timeout.connect(refresh)
    timer.start(250)
    window.resize(640, 420)
    window.show()
    result = qt_app.exec()
    gateway.stop()
    service.bridge.close()
    return result


if __name__ == "__main__":
    raise SystemExit(run())
