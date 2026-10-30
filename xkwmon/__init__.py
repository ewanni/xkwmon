"""Python driver for the XKW / HWCX USB display panel (0145:1001).

This package is a Linux port of the reverse engineered Windows service
``SystemTemperatureMonitoring``.  It provides:

* :mod:`xkwmon.protocol` - HID handshake/data frame and the UDP wire format
* :mod:`xkwmon.hidraw`   - dependency free ``/dev/hidraw`` transport
* :mod:`xkwmon.sensors`  - CPU/GPU/RAM/network/disk sensors from ``/proc``
* :mod:`xkwmon.api`      - UDP server compatible with the original service
* :mod:`xkwmon.service`  - the long running service
* :mod:`xkwmon.cli`      - the ``xkwmon`` command line tool
"""

from __future__ import annotations

__version__ = "1.0.0"

from .protocol import (  # noqa: E402,F401  (re-exported convenience API)
    DEFAULT_PID,
    DEFAULT_VID,
    build_data_frame,
    build_handshake,
    build_udp_packet,
    parse_udp_packet,
)

__all__ = [
    "__version__",
    "DEFAULT_VID",
    "DEFAULT_PID",
    "build_handshake",
    "build_data_frame",
    "build_udp_packet",
    "parse_udp_packet",
]
