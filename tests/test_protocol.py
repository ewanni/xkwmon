"""Unit tests for the wire protocols (no hardware access required)."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xkwmon import protocol  # noqa: E402


class HandshakeTests(unittest.TestCase):
    def test_report_shape(self) -> None:
        report, expected = protocol.build_handshake()
        self.assertEqual(len(report), 65)
        self.assertEqual(report[0], 0x00)
        self.assertEqual(report[1], protocol.CMD_HANDSHAKE)
        self.assertEqual(report[2:5], b"\x00\x00\x00")
        token = (report[5] << 8) | report[6]
        nonce = report[7:11]
        self.assertEqual(expected, protocol.expected_response(token, nonce))
        challenge = protocol.challenge_value(token, nonce)
        self.assertEqual(report[11], (challenge >> 8) & 0xFF)
        self.assertEqual(report[12], challenge & 0xFF)
        self.assertNotEqual(expected, challenge)  # the DLL uses two values
        self.assertEqual(report[63], protocol._checksum1(report))
        self.assertEqual(report[64], protocol._checksum2(report, include_c1=True))

    def test_doc_variant_keeps_compatibility(self) -> None:
        report, expected = protocol.build_handshake(challenge="doc")
        self.assertEqual(report[11], (expected >> 8) & 0xFF)
        self.assertEqual(report[12], expected & 0xFF)

    def test_checksum_variants(self) -> None:
        report, _ = protocol.build_handshake(c2_includes_c1=False)
        self.assertEqual(report[64], protocol._checksum2(report, include_c1=False))
        self.assertNotEqual(report[64], protocol._checksum2(report, include_c1=True))

    def test_expected_response_is_16_bit(self) -> None:
        expected = protocol.expected_response(0xFFFF, b"\xff\xff\xff\xff")
        self.assertGreaterEqual(expected, 0)
        self.assertLessEqual(expected, 0xFFFF)

    def test_verify_response_accepts_both_alignments(self) -> None:
        _, expected = protocol.build_handshake()
        answer = bytearray(65)
        answer[5] = (expected >> 8) & 0xFF
        answer[6] = expected & 0xFF
        answer[63] = protocol._checksum1(answer)
        answer[64] = protocol._checksum2(answer, include_c1=True)
        answer = bytes(answer)
        self.assertTrue(protocol.verify_handshake_response(answer, expected))
        # hidraw returns the payload without the leading report id
        self.assertTrue(protocol.verify_handshake_response(answer[1:], expected))
        self.assertFalse(protocol.verify_handshake_response(b"\x00" * 64, expected))
        self.assertFalse(protocol.verify_handshake_response(b"", expected))


class DataFrameTests(unittest.TestCase):
    def test_layout(self) -> None:
        when = dt.datetime(2026, 10, 30, 21, 5, 9, 250000)
        data = {
            "cpu_temp": 58,
            "gpu_temp": 64,
            "cpu_load": 14,
            "cpu_clock": 5400,
            "gpu_load": 98,
            "gpu_clock": 1912,
            "ram_percent": 74,
            "disk_load": 12,
            "cpu_power": 69.2,
            "gpu_fan": 69,
            "gpu_power": 169.3,
            "net_up": 7592,
            "net_down": 7680,
        }
        frame = protocol.build_data_frame(data, when=when)
        self.assertEqual(len(frame), 65)
        self.assertEqual(frame[0], 0x00)
        self.assertEqual(frame[1], protocol.CMD_DATA)
        self.assertEqual(frame[2], 58)
        self.assertEqual(frame[3], 64)
        self.assertEqual(
            list(frame[4:13]),
            [21, 5, 9, 25, 20, 26, 10, 30, (when.weekday() + 1) % 7],
        )
        self.assertEqual(frame[13], 14)
        self.assertEqual(int.from_bytes(frame[14:16], "big"), 5400)
        self.assertEqual(frame[16], 98)
        self.assertEqual(int.from_bytes(frame[17:19], "big"), 1912)
        self.assertEqual(frame[20], 74)
        self.assertEqual(frame[24], 12)
        self.assertEqual(int.from_bytes(frame[25:27], "big"), 5400)  # RPM slot = CPU clock
        self.assertEqual(int.from_bytes(frame[27:29], "big"), 0)     # CPU voltage (none)
        self.assertEqual(int.from_bytes(frame[29:31], "big"), 692)   # 69.2 W * 10
        self.assertEqual(int.from_bytes(frame[31:33], "big"), 69)    # GPU fan
        self.assertEqual(int.from_bytes(frame[33:35], "big"), 0)     # GPU voltage
        self.assertEqual(int.from_bytes(frame[35:37], "big"), 1693)  # 169.3 W * 10
        self.assertEqual(list(frame[37:41]), [0, 0, 0, 0])
        self.assertEqual(int.from_bytes(frame[41:45], "big"), 7592)
        self.assertEqual(int.from_bytes(frame[45:49], "big"), 7680)

    def test_empty_and_out_of_range(self) -> None:
        when = dt.datetime(2026, 10, 30, 21, 5, 9, 250000)
        frame = protocol.build_data_frame(when=when)
        self.assertEqual(len(frame), 65)
        # everything except the date/time block is zero
        self.assertTrue(all(byte == 0 for byte in frame[2:4]))
        self.assertTrue(all(byte == 0 for byte in frame[13:50]))
        frame = protocol.build_data_frame({"cpu_temp": 999, "cpu_load": -5, "cpu_clock": 1 << 20})
        self.assertEqual(frame[2], 255)
        self.assertEqual(frame[13], 0)
        self.assertEqual(int.from_bytes(frame[14:16], "big"), 0xFFFF)


class UdpTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        packet = protocol.build_udp_packet("rundata", '[{"type":"Cpu"}]', 0x1234)
        self.assertTrue(packet.startswith(protocol.MAGIC))
        self.assertTrue(packet.endswith(protocol.MAGIC))
        parsed = protocol.parse_udp_packet(packet)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.cmd, "rundata")
        self.assertEqual(parsed.cmd_no, 0x1234)
        self.assertEqual(parsed.cmd_type, 1)
        self.assertEqual(parsed.data, '[{"type":"Cpu"}]')

    def test_empty_data_becomes_space(self) -> None:
        parsed = protocol.parse_udp_packet(protocol.build_udp_packet("open", "", 7))
        assert parsed is not None
        self.assertEqual(parsed.data, " ")

    def test_rejects_malformed(self) -> None:
        self.assertIsNone(protocol.parse_udp_packet(b""))
        self.assertIsNone(protocol.parse_udp_packet(b"\xff\xfe"))
        self.assertIsNone(protocol.parse_udp_packet(b"XXXX" + b"\x00" * 8 + b"XXXX"))
        # no separator
        bad = protocol.MAGIC + bytes([1, 0, 1]) + b"rundata" + protocol.MAGIC
        self.assertIsNone(protocol.parse_udp_packet(bad))

    def test_make_item_nests_json_string(self) -> None:
        item = protocol.make_item("Ram", [{"Memory": 15.9, "Used Memory": 13.25}])
        self.assertEqual(item["type"], "Ram")
        self.assertIsInstance(item["data"], str)
        self.assertEqual(json.loads(item["data"]), [{"Memory": 15.9, "Used Memory": 13.25}])
        array = json.loads(protocol.items_to_json([item]))
        self.assertEqual(array[0]["type"], "Ram")
        self.assertEqual(json.loads(array[0]["data"])[0]["Memory"], 15.9)


if __name__ == "__main__":
    unittest.main()
