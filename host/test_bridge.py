import unittest
import uuid
import json
from unittest.mock import patch

from host.bridge import DeviceService, DeviceSnapshot, SerialBridge
from host.protocol import PRINT_DATA, PRINT_END, INPUT_SUBMITTED, STATE, CANCEL, RECOVER, Frame


class FakeBridge:
    def __init__(self):
        self.snapshot = DeviceSnapshot(connected=True, state="responding", credit=0)
        self.sent = []

    def send(self, msg_type, payload=b"", request_id=None):
        self.sent.append((msg_type, payload, request_id))


class BridgeTests(unittest.TestCase):
    def service(self):
        service = DeviceService()
        service.bridge = FakeBridge()
        service.snapshot = service.bridge.snapshot
        service.snapshot.active_request = str(uuid.uuid4())
        return service

    def test_response_is_chunked_and_end_waits_for_credit(self):
        service = DeviceService()
        fake = FakeBridge()
        service.bridge = fake
        service.snapshot = fake.snapshot
        request_id = str(uuid.uuid4())
        service.snapshot.active_request = request_id
        service.response_delta(request_id, 0, "A" * 300)
        service.response_end(request_id)
        self.assertEqual(fake.sent, [])
        service.snapshot.credit = 1000
        service._flush_pending()
        self.assertEqual([len(item[1]) for item in fake.sent[:3]], [128, 128, 44])
        self.assertEqual(fake.sent[-1][0], PRINT_END)
        self.assertTrue(all(item[0] == PRINT_DATA for item in fake.sent[:-1]))

    def test_new_short_chunks_never_overtake_pending_data_and_duplicates_do_not_print(self):
        service = self.service()
        rid = service.snapshot.active_request
        service.response_delta(rid, 0, "A" * 128)
        service.snapshot.credit = 10
        service.response_delta(rid, 1, "B")
        service.response_delta(rid, 1, "B")
        self.assertEqual(service.bridge.sent, [])
        with self.assertRaises(ValueError):
            service.response_delta(rid, 1, "C")
        service.snapshot.credit = 1000
        service.response_end(rid)
        self.assertEqual(b"".join(x[1] for x in service.bridge.sent if x[0] == PRINT_DATA), b"A" * 128 + b"B")
        service.response_end(rid)
        self.assertEqual(sum(x[0] == PRINT_END for x in service.bridge.sent), 1)

    def test_software_receipt_requires_draining_then_editing_after_end(self):
        service = self.service()
        rid = service.snapshot.active_request
        service.snapshot.credit = 1000
        service.response_delta(rid, 0, "HELLO")
        service.response_end(rid)
        def state(value):
            service._on_frame(Frame(STATE, 0, bytes(16), 0, json.dumps({"state": value, "queued_bytes": 0}).encode()))
        state("editing")
        self.assertIsNone(service.snapshot.last_delivery)
        state("draining")
        state("editing")
        self.assertEqual(service.snapshot.last_delivery["request_id"], rid)
        self.assertEqual(service.snapshot.last_delivery["status"], "drained")
        self.assertIsNone(service.snapshot.active_request)

    def test_cancel_checks_id_and_does_not_flush_old_pending_output(self):
        service = self.service()
        rid = service.snapshot.active_request
        service.response_delta(rid, 0, "OLD")
        service.response_end(rid)
        with self.assertRaises(RuntimeError):
            service.cancel(str(uuid.uuid4()))
        service.cancel(rid)
        service.snapshot.credit = 1000
        service._flush_pending()
        self.assertEqual([x[0] for x in service.bridge.sent], [CANCEL])

    def test_escape_log_stops_pending_output_once_and_blocks_late_replies(self):
        service = self.service()
        rid = service.snapshot.active_request
        messages = []
        service.set_agent_broadcast(messages.append)
        service.response_delta(rid, 0, "DO NOT PRINT")
        reader = SerialBridge(lambda _: None, lambda: None, on_escape=service.stop)
        reader._diagnostics(b"I (120) USB_KBD: unmapped HID key 0x")
        self.assertEqual(messages, [])
        reader._diagnostics(b"29\r\n")
        reader._diagnostics(b"unrelated serial data")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["type"], "input.cancelled")
        service.response_delta(rid, 1, "LATE REPLY")
        service.response_end(rid)
        service.snapshot.credit = 1000
        service.tick_output(1000)
        self.assertEqual([x[0] for x in service.bridge.sent], [CANCEL])
        service._on_frame(Frame(STATE, 0, bytes(16), 0, b'{"state":"editing","queued_bytes":0}'))
        self.assertIsNone(service.snapshot.active_request)
        with self.assertRaises(RuntimeError):
            service.print_text("STILL BLOCKED")
        service.acknowledge_stop(messages[0]["stop_id"])
        service.print_text("NEW")
        self.assertEqual(b"".join(x[1] for x in service.bridge.sent if x[0] == PRINT_DATA), b"NEW")

    def test_lost_draining_frame_can_use_ack_counter_but_idle_alone_is_insufficient(self):
        service = self.service()
        rid = service.snapshot.active_request
        service.snapshot.credit = 1000
        service.response_delta(rid, 0, "HELLO")
        service.response_end(rid)
        for count in (0, 4, 6):
            service._on_frame(Frame(STATE, 0, bytes(16), 0, json.dumps({"state": "editing", "queued_bytes": 0, "printed_bytes": count}).encode()))
            self.assertIsNone(service.snapshot.last_delivery)
        service._on_frame(Frame(STATE, 0, bytes(16), 0, b'{"state":"editing","queued_bytes":0,"printed_bytes":7}'))
        self.assertEqual(service.snapshot.last_delivery["status"], "drained")

    def test_http_submit_places_id_in_header_not_payload(self):
        service = self.service()
        service.snapshot.active_request = None
        service.snapshot.state = "editing"
        rid = service.submit()
        self.assertEqual(service.bridge.sent[-1], (INPUT_SUBMITTED, b"", uuid.UUID(rid).bytes))

    def test_direct_print_is_idempotent_and_rejects_nonprinting_controls(self):
        service = self.service()
        service.snapshot.active_request = None
        service.snapshot.state = "editing"
        service.snapshot.credit = 1000
        with self.assertRaises(ValueError):
            service.print_text("BAD\x1b")
        rid = str(uuid.uuid4())
        service.print_text("HELLO", rid)
        service.print_text("HELLO", rid)
        self.assertEqual(sum(x[0] == PRINT_DATA for x in service.bridge.sent), 1)

    def test_thinking_adds_one_dot_per_1500ms_and_stops_before_reply(self):
        service = self.service()
        service.snapshot.active_request = None
        service.snapshot.state = "editing"
        service.snapshot.credit = 1000
        with patch("host.bridge.time.monotonic", return_value=100):
            rid = service.begin_thinking(str(uuid.uuid4()))
            service.tick_output(100)
            service.tick_output(101.4)
            self.assertEqual(b"".join(x[1] for x in service.bridge.sent), b"thinking")
            service.tick_output(101.5)
            service.tick_output(103)
            self.assertEqual(b"".join(x[1] for x in service.bridge.sent), b"thinking..")
            service.response_delta(rid, 0, "ANSWER")
            service.tick_output(105)
            self.assertEqual(b"".join(x[1] for x in service.bridge.sent), b"thinking..\n\rANSWER")

    def test_physical_pacing_preserves_fifo_and_waits_after_carriage_return(self):
        service = self.service()
        service._char_interval = .08
        service._return_delay = .5
        service.snapshot.credit = 1000
        clock = [100.0]
        with patch("host.bridge.time.monotonic", side_effect=lambda: clock[0]):
            service.response_delta(service.snapshot.active_request, 0, "A\rB")
            self.assertEqual(service.bridge.sent[-1][1], b"A")
            clock[0] = 100.04; service.tick_output()
            self.assertEqual(len(service.bridge.sent), 1)
            clock[0] = 100.081; service.tick_output()
            self.assertEqual(service.bridge.sent[-1][1], b"\r")
            clock[0] = 100.5; service.tick_output()
            self.assertEqual(len(service.bridge.sent), 2)
            clock[0] = 100.582; service.tick_output()
            self.assertEqual(service.bridge.sent[-1][1], b"B")

    def test_recovery_discards_stale_pending_bytes_and_does_not_reset_a_healthy_printer(self):
        service = self.service()
        rid = service.snapshot.active_request
        service.response_delta(rid, 0, "DO NOT RESUME")
        service.response_end(rid)
        with self.assertRaises(RuntimeError):
            service.recover()
        service.snapshot.state = "fault"
        service.snapshot.last_error = "ACK timeout"
        service.recover()
        self.assertIsNone(service.snapshot.active_request)
        service.snapshot.credit = 1000
        service._on_frame(Frame(STATE, 0, bytes(16), 0, b'{"state":"editing","queued_bytes":0}'))
        service.tick_output()
        self.assertEqual([x[0] for x in service.bridge.sent], [RECOVER])
        self.assertIsNone(service.snapshot.last_error)


if __name__ == "__main__":
    unittest.main()
