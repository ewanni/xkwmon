"""Integration test: the service answers the original UDP protocol.

The HID panel is intentionally not required; opening it will fail without root
and the service must keep working (it just logs that the panel is unavailable).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from xkwmon import api  # noqa: E402


class ServiceUdpTests(unittest.TestCase):
    def test_rundata_and_staticdata(self) -> None:
        port = 18890
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.ini")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "[config]\n"
                    "fefresh_delay=200\n"
                    "send_delay=200\n"
                    "IsLog=False\n"
                    f"localPort={port}\n"
                    "IsUdp=True\n"
                    "LogLevel=DEBUG\n"
                )
            env = dict(os.environ)
            env["PYTHONPATH"] = ROOT
            env["PYTHONUNBUFFERED"] = "1"
            process = subprocess.Popen(
                [sys.executable, "-m", "xkwmon", "-c", config_path, "serve"],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                packet = None
                for _ in range(25):
                    time.sleep(0.2)
                    packet = api.query("127.0.0.1", "rundata", port, timeout=0.5)
                    if packet is not None:
                        break
                self.assertIsNotNone(packet, "service did not answer rundata")
                assert packet is not None
                rows = json.loads(packet.data)
                types = {row["type"] for row in rows}
                self.assertIn("Cpu", types)
                self.assertIn("Ram", types)
                cpu = json.loads(next(row["data"] for row in rows if row["type"] == "Cpu"))
                self.assertEqual(cpu[0]["name"], "packge")
                for key in ("temperature", "load", "clock", "powers", "voltage"):
                    self.assertIn(key, cpu[0])

                static = api.query("127.0.0.1", "staticdata", port, timeout=1.0)
                self.assertIsNotNone(static)
                assert static is not None
                static_rows = json.loads(static.data)
                static_types = {row["type"] for row in static_rows}
                self.assertIn("SystemOS", static_types)
                self.assertIn("Cpu", static_types)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                if process.stdout is not None:
                    process.stdout.close()


class SensorSmokeTests(unittest.TestCase):
    def test_snapshot_ranges(self) -> None:
        from xkwmon.sensors import SensorReader

        reader = SensorReader()
        time.sleep(0.3)
        snapshot = reader.sample()
        self.assertGreaterEqual(snapshot.cpu_load, 0.0)
        self.assertLessEqual(snapshot.cpu_load, 100.0)
        self.assertGreaterEqual(snapshot.ram_percent, 0.0)
        self.assertLessEqual(snapshot.ram_percent, 100.0)
        self.assertGreater(snapshot.ram_total_gb, 0.0)
        self.assertGreaterEqual(snapshot.cpu_clock, 0.0)
        self.assertTrue(snapshot.cpu_rows()[0]["name"] == "packge")
        # frame must accept the snapshot without errors
        from xkwmon.protocol import build_data_frame

        frame = build_data_frame(snapshot.as_frame_data())
        self.assertEqual(len(frame), 65)


if __name__ == "__main__":
    unittest.main()
