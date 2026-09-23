import unittest
import uuid

from host.bridge import DeviceService, DeviceSnapshot
from host.protocol import PRINT_DATA, PRINT_END


class FakeBridge:
    def __init__(self):
        self.snapshot = DeviceSnapshot(connected=True, state="responding", credit=0)
        self.sent = []

    def send(self, msg_type, payload=b"", request_id=None):
        self.sent.append((msg_type, payload, request_id))


class BridgeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
