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
    gateway = AgentGateway(service, port=args.api_port)
    gateway.start()

    try:
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel,
                                     QMainWindow, QMessageBox, QPushButton,
                                     QPlainTextEdit, QSplitter, QVBoxLayout, QWidget)
    except ImportError:
        print(f"PyQt6 未安装；无界面桥接已启动: http://127.0.0.1:{args.api_port}",
              file=sys.stderr)
        try:
            service.bridge.open(args.port, args.baud)
            while True:
                input()
        except (EOFError, KeyboardInterrupt):
            gateway.stop()
            service.bridge.close()
            return 0

    qt_app = QApplication(sys.argv)
    window = QMainWindow()
    window.setWindowTitle("KX-R530 打字机 Agent 上位机")
    window.resize(980, 760)

    port_box = QComboBox()
    port_box.setEditable(True)
    port_box.setCurrentText(args.port)
    refresh_button = QPushButton("刷新串口")
    connect_button = QPushButton("连接")
    status = QLabel("未连接")
    queue = QLabel("队列：0 / 0；credit：0")
    recover_button = QPushButton("恢复设备")

    candidate = QPlainTextEdit()
    candidate.setReadOnly(True)
    candidate.setPlaceholderText("USB 键盘实时输入会显示在这里")
    computer_input = QPlainTextEdit()
    computer_input.setPlaceholderText("在电脑输入内容；点击“实时追加”会立即送入打字机")
    agent_output = QPlainTextEdit()
    agent_output.setReadOnly(True)
    agent_output.setPlaceholderText("Agent 流式回复会显示在这里")
    activity = QPlainTextEdit()
    activity.setReadOnly(True)

    append_button = QPushButton("实时追加到设备")
    submit_button = QPushButton("确认发送 / Ctrl+Enter")
    direct_button = QPushButton("直接打印 Agent 输出")

    top = QHBoxLayout()
    for widget in (port_box, refresh_button, connect_button, status, queue, recover_button):
        top.addWidget(widget)

    input_buttons = QHBoxLayout()
    input_buttons.addWidget(append_button)
    input_buttons.addWidget(submit_button)
    input_buttons.addWidget(direct_button)

    left = QWidget()
    left_layout = QVBoxLayout(left)
    left_layout.addWidget(QLabel("键盘候选区 / 已实时打印内容"))
    left_layout.addWidget(candidate, 2)
    left_layout.addWidget(QLabel("电脑输入区"))
    left_layout.addWidget(computer_input, 2)
    left_layout.addLayout(input_buttons)

    right = QWidget()
    right_layout = QVBoxLayout(right)
    right_layout.addWidget(QLabel("Agent 回复 / 打印输出"))
    right_layout.addWidget(agent_output, 2)
    right_layout.addWidget(QLabel("事件日志"))
    right_layout.addWidget(activity, 2)

    splitter = QSplitter()
    splitter.addWidget(left)
    splitter.addWidget(right)

    root = QWidget()
    layout = QVBoxLayout(root)
    layout.addLayout(top)
    layout.addWidget(splitter, 1)
    window.setCentralWidget(root)

    def refresh_ports() -> None:
        current = port_box.currentText()
        port_box.clear()
        ports = service.bridge.ports()
        port_box.addItems([item["device"] for item in ports])
        port_box.setCurrentText(current or args.port)

    def show_error(exc: Exception) -> None:
        QMessageBox.warning(window, "操作失败", str(exc))

    def connect_device() -> None:
        try:
            if service.snapshot.connected:
                service.bridge.close()
            else:
                service.bridge.open(port_box.currentText(), args.baud)
        except Exception as exc:
            show_error(exc)

    def append_input() -> None:
        text = computer_input.toPlainText()
        if not text:
            return
        try:
            service.append_input(text)
            computer_input.clear()
        except Exception as exc:
            show_error(exc)

    def submit_input() -> None:
        try:
            request_id = service.submit()
            activity.appendPlainText(f"确认提交：{request_id}")
        except Exception as exc:
            show_error(exc)

    def direct_print() -> None:
        text = agent_output.toPlainText() or computer_input.toPlainText()
        if not text:
            return
        try:
            request_id = service.print_text(text)
            activity.appendPlainText(f"直接打印：{request_id}")
        except Exception as exc:
            show_error(exc)

    refresh_button.clicked.connect(refresh_ports)
    connect_button.clicked.connect(connect_device)
    recover_button.clicked.connect(service.recover)
    append_button.clicked.connect(append_input)
    submit_button.clicked.connect(submit_input)
    direct_button.clicked.connect(direct_print)

    def refresh_view() -> None:
        snapshot = service.snapshot.as_dict()
        status.setText(f"状态：{snapshot['state']} | 串口：{snapshot.get('port') or '无'}")
        queue.setText(f"队列：{snapshot.get('queued_bytes', 0)} / "
                      f"{snapshot.get('capacity', 0)}；credit：{snapshot.get('credit', 0)}")
        candidate.setPlainText(snapshot.get("candidate", ""))
        agent_output.setPlainText(snapshot.get("agent_output", ""))
        activity.setPlainText("\n".join(snapshot.get("events", [])))
        connect_button.setText("断开" if snapshot.get("connected") else "连接")

    refresh_ports()
    timer = QTimer()
    timer.timeout.connect(refresh_view)
    timer.start(200)
    window.show()
    result = qt_app.exec()
    gateway.stop()
    service.bridge.close()
    return result


if __name__ == "__main__":
    raise SystemExit(run())
