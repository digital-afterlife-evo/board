import os
import unittest

from host.protocol import Decoder, PRINT_DATA, decode_frame, encode_frame


class ProtocolTests(unittest.TestCase):
    def test_round_trip_binary_payload(self):
        request_id = os.urandom(16)
        encoded = encode_frame(PRINT_DATA, request_id, b"a\x00b", 17)
        frame = decode_frame(encoded[:-1])
        self.assertEqual(frame.type, PRINT_DATA)
        self.assertEqual(frame.request_id, request_id)
        self.assertEqual(frame.sequence, 17)
        self.assertEqual(frame.payload, b"a\x00b")

    def test_decoder_resynchronizes_after_bad_frame(self):
        good = encode_frame(PRINT_DATA, payload=b"ok")
        bad = bytearray(encode_frame(PRINT_DATA, payload=b"bad"))
        bad[-2] ^= 0xFF
        decoder = Decoder()
        frames = decoder.feed(bytes(bad) + good)
        self.assertEqual([frame.payload for frame in frames], [b"ok"])


if __name__ == "__main__":
    unittest.main()
