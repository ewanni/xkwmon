"""Minimal Linux ``hidraw`` transport for the XKW panel.

The original service talks to the panel through ``TempComm.dll`` on Windows.
On Linux the same 65-byte reports can be written straight to ``/dev/hidrawN``;
no ``libhidapi`` dependency is required.

The first byte of every report is the HID report number.  The panel declares a
single unnumbered 64-byte input/output report, so hidraw expects a leading
``0x00`` that the kernel strips before it reaches the device.
"""

from __future__ import annotations

import errno
import glob
import os
import select
import time
from typing import Iterable

from . import protocol


class HidrawError(OSError):
    """Raised when the panel cannot be opened or written to."""


class HidrawDevice:
    """A single open ``/dev/hidraw*`` node."""

    def __init__(self, path: str, fd: int, vid: int | None = None, pid: int | None = None):
        self.path = path
        self.fd = fd
        self.vid = vid
        self.pid = pid

    # -- discovery ---------------------------------------------------------

    @staticmethod
    def enumerate(root: str = "/sys/class/hidraw") -> list[dict[str, object]]:
        """List hidraw nodes with their USB ids and product name."""
        result: list[dict[str, object]] = []
        for uevent in sorted(glob.glob(os.path.join(root, "hidraw*", "device", "uevent"))):
            node = os.path.basename(os.path.dirname(os.path.dirname(uevent)))
            info: dict[str, object] = {
                "path": f"/dev/{node}",
                "vid": None,
                "pid": None,
                "name": "",
                "bus": None,
            }
            try:
                with open(uevent, "r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        key, _, value = line.strip().partition("=")
                        if key == "HID_ID":
                            parts = value.split(":")
                            if len(parts) == 3:
                                info["bus"] = int(parts[0], 16)
                                info["vid"] = int(parts[1], 16)
                                info["pid"] = int(parts[2], 16)
                        elif key == "HID_NAME":
                            info["name"] = value
            except OSError:
                continue
            result.append(info)
        return result

    @staticmethod
    def find(vid: int = protocol.DEFAULT_VID, pid: int = protocol.DEFAULT_PID) -> str | None:
        """Return the ``/dev/hidrawN`` path matching ``vid``/``pid``."""
        for info in HidrawDevice.enumerate():
            if info["vid"] == vid and info["pid"] == pid:
                return str(info["path"])
        return None

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(
        cls,
        path: str | None = None,
        vid: int = protocol.DEFAULT_VID,
        pid: int = protocol.DEFAULT_PID,
    ) -> "HidrawDevice":
        """Open the panel, discovering it by USB ids when ``path`` is None."""
        resolved = path or cls.find(vid, pid)
        if not resolved:
            raise HidrawError(
                f"panel {vid:04x}:{pid:04x} not found "
                f"(is it plugged in? see `xkwmon devices`)"
            )
        try:
            fd = os.open(resolved, os.O_RDWR)
        except PermissionError as exc:
            raise HidrawError(
                f"permission denied opening {resolved}. Run as root or install "
                f"the udev rule (systemd/99-xkw-panel.rules)."
            ) from exc
        except OSError as exc:
            raise HidrawError(f"cannot open {resolved}: {exc}") from exc
        return cls(resolved, fd, vid, pid)

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            finally:
                self.fd = -1

    def __enter__(self) -> "HidrawDevice":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- I/O ---------------------------------------------------------------

    def write_report(self, report: bytes) -> int:
        """Write one report, retrying the 64-byte form if the kernel objects."""
        if self.fd < 0:
            raise HidrawError("device is closed")
        if len(report) != protocol.REPORT_SIZE:
            raise ValueError(f"report must be {protocol.REPORT_SIZE} bytes, got {len(report)}")
        try:
            return os.write(self.fd, report)
        except OSError as exc:
            if exc.errno in (errno.EINVAL, errno.EMSGSIZE):
                # Some kernels/descriptors expect the report without the
                # leading report-id byte.
                return os.write(self.fd, report[1:])
            raise

    def read_report(self, timeout: float = 1.0) -> bytes | None:
        """Read one report, or ``None`` when nothing arrives in ``timeout``."""
        if self.fd < 0:
            raise HidrawError("device is closed")
        try:
            ready, _, _ = select.select([self.fd], [], [], timeout)
        except InterruptedError:
            return None
        if not ready:
            return None
        try:
            return os.read(self.fd, 4096)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return None
            raise

    # -- handshake ---------------------------------------------------------

    def handshake(self, attempts: int = 2, timeout: float = 0.5) -> str | None:
        """Perform the mandatory handshake.

        Returns the name of the successful checksum variant, or ``None`` if the
        panel did not answer (for example because it is asleep).
        """
        variants = protocol.handshake_report_variants()
        for attempt in range(max(1, attempts)):
            for index, (report, expected, name) in enumerate(variants):
                try:
                    self.write_report(report)
                except OSError:
                    raise
                answer = self.read_report(timeout)
                if answer and protocol.verify_handshake_response(answer, expected):
                    return name
                if answer is None and attempt == 0 and index == 0:
                    # No answer at all: give the panel a moment and retry once.
                    time.sleep(0.1)
            time.sleep(0.05)
        return None

    def probe_handshake(self, timeout: float = 0.5) -> list[dict[str, object]]:
        """Send every handshake variant once and report the raw answers.

        Useful to distinguish a sleeping panel (no answer at all) from a
        checksum/formula mismatch (a non-empty but unverified answer).
        """
        results: list[dict[str, object]] = []
        for report, expected, name in protocol.handshake_report_variants():
            self.write_report(report)
            answer = self.read_report(timeout)
            results.append(
                {
                    "variant": name,
                    "expected": expected,
                    "answer": answer,
                    "verified": verified,
                }
            )
        return results


def find_panel(vid: int = protocol.DEFAULT_VID, pid: int = protocol.DEFAULT_PID) -> str | None:
    """Convenience wrapper around :meth:`HidrawDevice.find`."""
    return HidrawDevice.find(vid, pid)


def describe_devices(devices: Iterable[dict[str, object]] | None = None) -> str:
    devices = list(devices if devices is not None else HidrawDevice.enumerate())
    if not devices:
        return "no hidraw devices found"
    lines = []
    for info in devices:
        vid = info.get("vid")
        pid = info.get("pid")
        ids = f"{vid:04x}:{pid:04x}" if isinstance(vid, int) and isinstance(pid, int) else "?:?"
        lines.append(f"{info['path']}  {ids}  {info.get('name') or '?'}")
    return "\n".join(lines)
