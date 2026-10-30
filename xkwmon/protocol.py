"""Wire formats used by the XKW USB panel and by the original UDP API.

Two independent protocols are implemented here, both reverse engineered from
the Windows build:

* HID panel (``VID 0145 / PID 1001``): 65-byte reports where byte 0 is the HID
  report id (always 0, stripped by the kernel) and bytes 1..64 are the payload.
  Report 1 is the handshake, report 2 is the periodic data frame.
* UDP API (default port 8890): ``FF FE FF FD | type | no_hi | no_lo |
  "cmd|data" | FF FE FF FD`` with UTF-8 payload.

The module is dependency free so it can be reused by any Python program.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import struct
from typing import Any, Iterable, Mapping

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

MAGIC = b"\xff\xfe\xff\xfd"

REPORT_ID = 0x00
REPORT_PAYLOAD = 64          # bytes declared by the HID report descriptor
REPORT_SIZE = REPORT_PAYLOAD + 1

CMD_HANDSHAKE = 0x01
CMD_DATA = 0x02

#: HID ids of the supported panel (XinKeWei / HWCX "USB Display").
DEFAULT_VID = 0x0145
DEFAULT_PID = 0x1001

#: UDP command names understood by the original service.
CMD_RUNDATA = "rundata"
CMD_STATICDATA = "staticdata"
CMD_OPEN = "open"
CMD_STOP = "stop"


# --------------------------------------------------------------------------- #
# HID handshake
# --------------------------------------------------------------------------- #

def _checksum1(buf: bytes | bytearray) -> int:
    """``c1 = XOR(i * buf[1 + i]) for i in 0..59`` (mod 256)."""
    value = 0
    for i in range(60):
        value ^= (i * buf[1 + i]) & 0xFF
    return value & 0xFF


def _checksum2(buf: bytes | bytearray, include_c1: bool = True) -> int:
    """``c2 = SUM(i * buf[1 + i])`` (mod 256).

    The published description says ``i in 0..62`` which spans bytes 1..63,
    i.e. it covers the already written ``c1`` byte.  Some builds compute it
    before ``c1`` is stored, so both variants are supported and probed.
    """
    upto = 63 if include_c1 else 62
    value = 0
    for i in range(upto):
        value += i * buf[1 + i]
    return value & 0xFF


def expected_response(token: int, nonce: bytes) -> int:
    """Compute the 16-bit token the panel must echo back (bytes 5..6).

    Reverse engineered from ``TempComm.dll`` at ``0x1800010f0``::

        E = (nonce[3] + nonce[2]*256) ^
            (token_lo + nonce[1] + (token_hi + nonce[0])*256)
    """
    if len(nonce) != 4:
        raise ValueError("nonce must be 4 bytes")
    token_hi = (token >> 8) & 0xFF
    token_lo = token & 0xFF
    left = (nonce[3] + nonce[2] * 256) & 0xFFFF
    right = (token_lo + nonce[1] + (token_hi + nonce[0]) * 256) & 0xFFFF
    return (left ^ right) & 0xFFFF


def challenge_value(token: int, nonce: bytes) -> int:
    """Value the device validates in bytes 11..12 of the handshake.

    This is *not* the same as :func:`expected_response`.  The native code
    computes it as::

        C = token ^ ((nonce[1] + nonce[3]) + (nonce[0] + nonce[2])*256)

    and stores it big-endian (high byte at 11, low byte at 12).
    """
    if len(nonce) != 4:
        raise ValueError("nonce must be 4 bytes")
    mixed = (nonce[1] + nonce[3] + (nonce[0] + nonce[2]) * 256) & 0xFFFF
    return (token ^ mixed) & 0xFFFF


def _write_challenge(buf: bytearray, value: int) -> None:
    buf[11] = (value >> 8) & 0xFF
    buf[12] = value & 0xFF


def build_handshake(
    c2_includes_c1: bool = True,
    rng=os.urandom,
    challenge: str = "native",
) -> tuple[bytes, int]:
    """Build a 65-byte handshake report.

    Returns ``(report, expected)`` where ``expected`` is the value that must
    appear in bytes 5..6 of the device answer.

    ``challenge`` selects what to store in bytes 11..12: ``"native"`` uses the
    value recovered from the DLL, ``"doc"`` uses the formula from the older
    reverse-engineering notes (kept for probing).
    """
    buf = bytearray(REPORT_SIZE)
    buf[0] = REPORT_ID
    buf[1:] = rng(REPORT_PAYLOAD)

    buf[1] = CMD_HANDSHAKE
    buf[2] = buf[3] = buf[4] = 0

    token = struct.unpack(">H", rng(2))[0]
    buf[5] = (token >> 8) & 0xFF
    buf[6] = token & 0xFF

    nonce = rng(4)
    buf[7:11] = nonce

    expected = expected_response(token, nonce)
    if challenge == "doc":
        _write_challenge(buf, expected)
    else:
        _write_challenge(buf, challenge_value(token, nonce))

    buf[63] = _checksum1(buf)
    buf[64] = _checksum2(buf, include_c1=c2_includes_c1)
    return bytes(buf), expected


def _as_logical(report: bytes) -> bytes:
    """Normalise a read report to the 65-byte "report id + payload" layout."""
    if len(report) == REPORT_SIZE:
        return report
    if len(report) == REPORT_PAYLOAD:
        return b"\x00" + report
    return report


def verify_handshake_response(report: bytes, expected: int) -> bool:
    """Check a device answer against the handshake expectations.

    The panel echoes the token in bytes 5..6 and repeats both checksums.  The
    read buffer alignment differs between Windows (65 bytes) and hidraw
    (64 bytes), so both layouts are accepted.
    """
    if not report:
        return False

    for logical in (report, _as_logical(report)):
        if len(logical) < 7:
            continue
        if ((logical[5] << 8) | logical[6]) == expected:
            return True
        # Tolerate a one byte shift as well (defensive, some USB stacks strip
        # the report id and do not pad it back).
        if len(logical) >= 8 and ((logical[6] << 8) | logical[7]) == expected:
            return True
    return False


def handshake_report_variants(rng=os.urandom) -> list[tuple[bytes, int, str]]:
    """Return the handshake candidates to try, best guess first."""
    variants = []
    for challenge in ("native", "doc"):
        report, expected = build_handshake(challenge=challenge, rng=rng)
        variants.append((report, expected, f"challenge-{challenge}"))
    return variants


# --------------------------------------------------------------------------- #
# HID data frame
# --------------------------------------------------------------------------- #

def _u8(value: float | int | None) -> int:
    if value is None:
        return 0
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(255, number))


def _u16(value: float | int | None) -> int:
    if value is None:
        return 0
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(0xFFFF, number))


def _u32(value: float | int | None) -> int:
    if value is None:
        return 0
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(0xFFFFFFFF, number))


def _voltage_word(value: float | int | None) -> int:
    """Encode volts as ``whole * 256 + hundredths`` (SetCpuVoltage format)."""
    if not value:
        return 0
    try:
        volts = float(value)
    except (TypeError, ValueError):
        return 0
    whole = int(volts)
    hundredths = int(round((volts - whole) * 100)) % 100
    return max(0, min(0xFFFF, whole * 256 + hundredths))


def build_data_frame(snapshot: Mapping[str, Any] | None = None, when: _dt.datetime | None = None) -> bytes:
    """Build the 65-byte periodic data report (command 0x02).

    ``snapshot`` uses the keys produced by :class:`xkwmon.sensors.Snapshot`.
    Missing or invalid values are sent as zero, which also keeps the panel
    awake while sensors are unavailable.
    """
    snap = snapshot or {}
    now = when or _dt.datetime.now()

    buf = bytearray(REPORT_SIZE)
    buf[0] = REPORT_ID
    buf[1] = CMD_DATA

    # Temperatures and loads
    buf[2] = _u8(snap.get("cpu_temp"))
    buf[3] = _u8(snap.get("gpu_temp"))

    # Date / time: hour, minute, second, ms/10, year/100, year%100,
    # month, day, day of week (0 = Sunday, like .NET DayOfWeek).
    buf[4] = _u8(now.hour)
    buf[5] = _u8(now.minute)
    buf[6] = _u8(now.second)
    buf[7] = _u8(now.microsecond // 10000)
    buf[8] = _u8(now.year // 100)
    buf[9] = _u8(now.year % 100)
    buf[10] = _u8(now.month)
    buf[11] = _u8(now.day)
    buf[12] = _u8((now.weekday() + 1) % 7)

    buf[13] = _u8(snap.get("cpu_load"))
    struct.pack_into(">H", buf, 14, _u16(snap.get("cpu_clock")))

    buf[16] = _u8(snap.get("gpu_load"))
    struct.pack_into(">H", buf, 17, _u16(snap.get("gpu_clock")))

    # Memory: 19 = unused value 1, 20 = used percent ("RAM LOAD").
    buf[19] = 0
    buf[20] = _u8(snap.get("ram_percent"))
    struct.pack_into(">H", buf, 21, 0)

    # Disk: 23 = unused value, 24 = load.
    buf[23] = 0
    buf[24] = _u8(snap.get("disk_load"))

    # The layout below was taken from SetDeviceTime in TempComm.dll.  Note the
    # "RPM" slot is 25..26 (SetFanRPM): the patched Windows build fed it the
    # CPU frequency, and we keep that behaviour.
    struct.pack_into(">H", buf, 25, _u16(snap.get("rpm", snap.get("cpu_clock"))))
    struct.pack_into(">H", buf, 27, _u16(_voltage_word(snap.get("cpu_voltage"))))
    struct.pack_into(">H", buf, 29, _u16(snap.get("cpu_power", 0) * 10))

    struct.pack_into(">H", buf, 31, _u16(snap.get("gpu_fan")))
    struct.pack_into(">H", buf, 33, _u16(_voltage_word(snap.get("gpu_voltage"))))
    struct.pack_into(">H", buf, 35, _u16(snap.get("gpu_power", 0) * 10))

    # 37..40: reserved / unused disk duplicates, kept zero.

    struct.pack_into(">I", buf, 41, _u32(snap.get("net_up")))
    struct.pack_into(">I", buf, 45, _u32(snap.get("net_down")))

    buf[49] = _u8(snap.get("display_mode", 0))
    return bytes(buf)


# --------------------------------------------------------------------------- #
# UDP API
# --------------------------------------------------------------------------- #

def build_udp_packet(cmd: str, data: str, cmd_no: int, cmd_type: int = 1) -> bytes:
    """Encode an UDP packet exactly like ``UdpCommand.Combination``."""
    if not data:
        data = " "
    payload = f"{cmd}|{data}".encode("utf-8")
    return (
        MAGIC
        + bytes([cmd_type & 0xFF, (cmd_no >> 8) & 0xFF, cmd_no & 0xFF])
        + payload
        + MAGIC
    )


class UdpPacket:
    __slots__ = ("cmd", "data", "cmd_no", "cmd_type")

    def __init__(self, cmd: str, data: str, cmd_no: int, cmd_type: int):
        self.cmd = cmd
        self.data = data
        self.cmd_no = cmd_no
        self.cmd_type = cmd_type

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"UdpPacket(cmd={self.cmd!r}, cmd_no={self.cmd_no}, cmd_type={self.cmd_type}, data={self.data!r})"


def parse_udp_packet(buffer: bytes) -> UdpPacket | None:
    """Decode an UDP packet, returning ``None`` for anything malformed."""
    minimum = len(MAGIC) * 2 + 3
    if len(buffer) < minimum:
        return None
    if not buffer.startswith(MAGIC):
        return None
    # The original implementation only validates the leading magic (its end
    # check indexes the buffer from the start), so be equally lenient.
    if not buffer.endswith(MAGIC):
        return None

    offset = len(MAGIC)
    cmd_type = buffer[offset]
    cmd_no = (buffer[offset + 1] << 8) | buffer[offset + 2]
    body = buffer[offset + 3: len(buffer) - len(MAGIC)]
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None

    separator = text.find("|")
    if separator < 2:
        return None
    return UdpPacket(text[:separator], text[separator + 1:], cmd_no, cmd_type)


def items_to_json(items: Iterable[Mapping[str, Any]]) -> str:
    """Serialise ``[{"type": ..., "data": ...}]`` the way Newtonsoft does."""
    return json.dumps(list(items), ensure_ascii=False, separators=(",", ":"))


def make_item(type_name: str, rows: list[Mapping[str, Any]]) -> dict[str, str]:
    """Build one ``{"type", "data"}`` element with the nested JSON string."""
    return {
        "type": type_name,
        "data": json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
    }
