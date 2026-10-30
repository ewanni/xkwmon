"""The long running service: sensors -> HID panel, plus the UDP API."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from . import protocol, sensors
from .api import UdpApi
from .config import Config
from .hidraw import HidrawDevice, HidrawError

LOGGER = logging.getLogger(__name__)


def rpm_value(snapshot: "sensors.Snapshot", mode: str = "clock") -> float:
    """Value for the panel's "RPM" field.

    ``clock`` shows the CPU frequency (the patched Windows behaviour),
    ``fan`` shows a pump/fan tachometer reading when the board exposes one.
    GPU fan entries are percentages and are therefore ignored.
    """
    if mode != "fan":
        return float(snapshot.cpu_clock)
    fans = [fan for fan in snapshot.fans if "gpu" not in str(fan.get("name", "")).lower()]
    for fan in fans:
        name = str(fan.get("name", "")).lower()
        if "cpu" in name or "pump" in name:
            return float(fan.get("speed", 0) or 0)
    return max((float(fan.get("speed", 0) or 0) for fan in fans), default=0.0)


def setup_logging(config: Config) -> None:
    """Configure logging according to the config file."""
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    handlers: list[logging.Handler] = []
    if config.is_log and config.log_file:
        try:
            directory = os.path.dirname(config.log_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            handlers.append(logging.FileHandler(config.log_file, encoding="utf-8"))
        except OSError as exc:  # pragma: no cover - log file is optional
            print(f"cannot open log file {config.log_file}: {exc}")
    handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


class PanelService:
    """Owns the sensor loop, the panel writer and the UDP server."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.reader = sensors.SensorReader()
        self._snapshot: sensors.Snapshot | None = None
        self._snapshot_lock = threading.Lock()
        self._stop = threading.Event()
        self._running = threading.Event()
        self._running.set()
        self._device: HidrawDevice | None = None
        self._variant: str | None = None
        self._udp: UdpApi | None = None
        self._threads: list[threading.Thread] = []
        self._last_connect_attempt = 0.0
        self._last_status_log = time.monotonic()
        self._static_cache: list[dict[str, Any]] | None = None
        self._handshake_warned = False
        self._frames_written = 0

    # -- static information ------------------------------------------------

    def _static(self) -> list[dict[str, Any]]:
        if self._static_cache is None:
            gpu_name = ""
            driver = ""
            gpu = getattr(self.reader, "_gpu", None)
            if gpu is not None:
                gpu_name = gpu.name
                try:
                    driver = gpu.driver_version()
                except Exception:
                    driver = ""
            self._static_cache = sensors.static_items(gpu_name, driver)
        return self._static_cache

    # -- snapshot access ---------------------------------------------------

    def current_snapshot(self) -> sensors.Snapshot | None:
        with self._snapshot_lock:
            return self._snapshot

    def _sensor_loop(self) -> None:
        delay = self.config.refresh_delay / 1000.0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                snap = self.reader.sample()
                with self._snapshot_lock:
                    self._snapshot = snap
            except Exception:
                LOGGER.exception("sensor sampling failed")
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, delay - elapsed))

    # -- panel -------------------------------------------------------------

    def _connect(self) -> None:
        now = time.monotonic()
        if now - self._last_connect_attempt < 2.0:
            return
        self._last_connect_attempt = now
        try:
            device = HidrawDevice.open(
                self.config.hid_path or None,
                self.config.hid_vid,
                self.config.hid_pid,
            )
        except HidrawError as exc:
            LOGGER.debug("panel not available: %s", exc)
            return

        variant = device.handshake(attempts=self.config.handshake_attempts)
        if variant:
            LOGGER.info("panel %s connected, handshake ok (%s)", device.path, variant)
            self._handshake_warned = False
        else:
            LOGGER.warning(
                "panel %s opened but did not answer the handshake. "
                "It is probably asleep: unplug USB for ~30 s and plug it back in.",
                device.path,
            )
            self._handshake_warned = True
            try:
                for result in device.probe_handshake():
                    answer = result["answer"]
                    LOGGER.info(
                        "  handshake %s: expected=0x%04x verified=%s answer=%s",
                        result["variant"],
                        int(result["expected"]),
                        result["verified"],
                        answer.hex(" ") if answer else "<none>",
                    )
            except OSError as exc:
                LOGGER.debug("handshake probe failed: %s", exc)
        self._device = device
        self._variant = variant

    def _disconnect(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            finally:
                LOGGER.info("panel disconnected")
                self._device = None

    def _panel_loop(self) -> None:
        delay = self.config.send_delay / 1000.0
        last_error_log = 0.0
        while not self._stop.is_set():
            started = time.monotonic()
            snapshot: sensors.Snapshot | None = None
            if self._running.is_set():
                if self._device is None:
                    self._connect()
                if self._device is not None:
                    snapshot = self.current_snapshot()
                    if snapshot is not None:
                        data = snapshot.as_frame_data()
                        data["rpm"] = rpm_value(snapshot, self.config.rpm_mode)
                    else:
                        data = {}
                    frame = protocol.build_data_frame(data)
                    try:
                        self._device.write_report(frame)
                        self._frames_written += 1
                    except OSError as exc:
                        now = time.monotonic()
                        if now - last_error_log > 5.0:
                            LOGGER.warning("panel write failed: %s (will reconnect)", exc)
                            last_error_log = now
                        self._disconnect()

                now = time.monotonic()
                interval = self.config.status_log_interval
                if interval > 0 and now - self._last_status_log >= interval:
                    self._last_status_log = now
                    self._log_status(snapshot, self._device is not None)

            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, delay - elapsed))

    def _log_status(self, snapshot: sensors.Snapshot | None, connected: bool) -> None:
        if snapshot is None:
            LOGGER.info("panel: %s (%d frames sent so far)",
                        "connected" if connected else "waiting for panel",
                        self._frames_written)
            return
        LOGGER.info(
            "panel:%s CPU %.0f°C %3.0f%% %.0fMHz %.1fW | GPU %.0f°C %3.0f%% %.0fMHz %.1fW | "
            "RAM %.0f%% | net %.1f/%.1f MB/s | disk %.0f%% | frames %d",
            "up" if connected else "down",
            snapshot.cpu_temp,
            snapshot.cpu_load,
            snapshot.cpu_clock,
            snapshot.cpu_power,
            snapshot.gpu_temp,
            snapshot.gpu_load,
            snapshot.gpu_clock,
            snapshot.gpu_power,
            snapshot.ram_percent,
            snapshot.net_up / 1e6,
            snapshot.net_down / 1e6,
            snapshot.disk_load,
            self._frames_written,
        )

    # -- UDP ---------------------------------------------------------------

    def _udp_handler(self, packet: protocol.UdpPacket) -> str | None:
        if packet.cmd == protocol.CMD_RUNDATA:
            snapshot = self.current_snapshot()
            rows = sensors.rundata_items(snapshot) if snapshot else []
            LOGGER.debug("rundata request from client")
            return protocol.items_to_json(rows)
        if packet.cmd == protocol.CMD_STATICDATA:
            LOGGER.debug("staticdata request from client")
            return protocol.items_to_json(self._static())
        if packet.cmd == protocol.CMD_OPEN:
            LOGGER.info("UDP open: resuming panel stream")
            self._running.set()
            return None
        if packet.cmd == protocol.CMD_STOP:
            LOGGER.info("UDP stop: pausing panel stream")
            self._running.clear()
            return None
        LOGGER.debug("unknown UDP command %r", packet.cmd)
        return None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        LOGGER.info(
            "starting xkwmon: refresh=%dms send=%dms udp=%s:%d panel=%04x:%04x",
            self.config.refresh_delay,
            self.config.send_delay,
            self.config.is_udp,
            self.config.local_port,
            self.config.hid_vid,
            self.config.hid_pid,
        )
        if self.config.is_udp:
            self._udp = UdpApi(self.config.local_port, self._udp_handler)
            self._udp.start()

        for name, target in (
            ("xkwmon-sensors", self._sensor_loop),
            ("xkwmon-panel", self._panel_loop),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        LOGGER.info("stopping")
        self._stop.set()
        if self._udp is not None:
            self._udp.stop()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._disconnect()

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                self._stop.wait(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def status(self) -> dict[str, Any]:
        snapshot = self.current_snapshot()
        return {
            "panel": self._device.path if self._device else None,
            "handshake": self._variant,
            "frames": self._frames_written,
            "streaming": self._running.is_set(),
            "snapshot": snapshot.to_dict() if snapshot else None,
        }
