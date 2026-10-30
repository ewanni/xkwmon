"""Configuration compatible with the original Windows ``config.ini``.

The keys ``fefresh_delay``, ``send_delay``, ``IsLog``, ``localPort``,
``LogPath`` and ``IsUdp`` keep their original meaning so an existing config
file continues to work.  A few Linux-specific keys are added:

===================  =====================================================
``hidVid``           USB vendor id of the panel (hex or decimal)
``hidPid``           USB product id of the panel
``hidPath``          force a specific ``/dev/hidrawN`` (overrides discovery)
``LogLevel``         ``DEBUG``/``INFO``/``WARNING``/``ERROR``
``handshakeAttempts`` how many handshake rounds to try on connect
===================  =====================================================
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass

APP_NAME = "xkwmon"
DEFAULT_CONFIG = "/etc/xkwmon/config.ini"

_SECTION = "config"

DEFAULTS = {
    "fefresh_delay": 500,
    "send_delay": 500,
    "IsLog": "False",
    "localPort": 8890,
    "LogPath": "",
    "IsUdp": "True",
    "hidVid": "0x0145",
    "hidPid": "0x1001",
    "hidPath": "",
    "LogLevel": "INFO",
    "handshakeAttempts": 2,
    "StatusLogInterval": 60,
}


def _as_int(value: object, fallback: int) -> int:
    try:
        return int(str(value).strip(), 0)
    except (TypeError, ValueError):
        return fallback


def _as_bool(value: object, fallback: bool) -> bool:
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return fallback


@dataclass
class Config:
    refresh_delay: int = 500       # sensors poll interval, ms
    send_delay: int = 500          # HID frame interval, ms
    is_log: bool = False
    local_port: int = 8890
    log_path: str = ""
    is_udp: bool = True
    hid_vid: int = 0x0145
    hid_pid: int = 0x1001
    hid_path: str = ""
    log_level: str = "INFO"
    handshake_attempts: int = 2
    status_log_interval: int = 60  # seconds, 0 disables the periodic status line
    path: str = ""

    @property
    def log_file(self) -> str:
        if not self.log_path:
            return ""
        if os.path.isdir(self.log_path):
            return os.path.join(self.log_path, "log.txt")
        return self.log_path


def default_config_path() -> str:
    """Resolve the config path from ``XKWMON_CONFIG`` or sane defaults."""
    env = os.environ.get("XKWMON_CONFIG")
    if env:
        return env
    if os.path.exists(DEFAULT_CONFIG):
        return DEFAULT_CONFIG
    # Development fallback: ./config.ini next to the current directory.
    return os.path.join(os.getcwd(), "config.ini")


def load(path: str | None = None, create: bool = True) -> Config:
    """Read ``path`` (or the default), creating it with defaults if missing."""
    resolved = path or default_config_path()
    parser = configparser.ConfigParser(interpolation=None)
    # Keep the key spelling from the file (the original uses camelCase).
    parser.optionxform = str
    if os.path.exists(resolved):
        parser.read(resolved, encoding="utf-8")
    elif create:
        parser[_SECTION] = {key: str(value) for key, value in DEFAULTS.items()}
        try:
            directory = os.path.dirname(resolved)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(resolved, "w", encoding="utf-8") as handle:
                parser.write(handle)
        except OSError:
            pass

    raw: dict[str, str] = {}
    if parser.has_section(_SECTION):
        raw = {key.lower(): value for key, value in parser.items(_SECTION)}

    def value(key: str):
        return raw.get(key.lower(), DEFAULTS[key])

    config = Config(
        refresh_delay=max(50, _as_int(value("fefresh_delay"), 500)),
        send_delay=max(50, _as_int(value("send_delay"), 500)),
        is_log=_as_bool(value("IsLog"), False),
        local_port=_as_int(value("localPort"), 8890),
        log_path=str(value("LogPath") or "").strip(),
        is_udp=_as_bool(value("IsUdp"), True),
        hid_vid=_as_int(value("hidVid"), 0x0145),
        hid_pid=_as_int(value("hidPid"), 0x1001),
        hid_path=str(value("hidPath") or "").strip(),
        log_level=str(value("LogLevel") or "INFO").strip().upper(),
        handshake_attempts=max(1, _as_int(value("handshakeAttempts"), 2)),
        status_log_interval=max(0, _as_int(value("StatusLogInterval"), 60)),
        path=resolved,
    )
    return config


def write_example(path: str) -> None:
    """Write a commented example config (used by the installer)."""
    content = """[config]
; sensor poll interval and HID frame interval, milliseconds
fefresh_delay=500
send_delay=500
; write a log file when true (path below, empty = /var/log/xkwmon.log)
IsLog=False
LogPath=
LogLevel=INFO
; UDP API, same protocol/port as the original Windows service
IsUdp=True
localPort=8890
; panel USB ids (0145:1001 = XKW / HWCX USB Display)
hidVid=0x0145
hidPid=0x1001
; force a specific hidraw node, empty = auto-detect by ids
hidPath=
handshakeAttempts=2
; how often to print a status line to the journal, seconds (0 = never)
StatusLogInterval=60
"""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
