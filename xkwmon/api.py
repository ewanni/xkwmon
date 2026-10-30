"""UDP API server and client, byte compatible with the original service.

The server listens on ``localPort`` (default 8890), answers ``rundata`` and
``staticdata`` with a JSON array, and forwards ``open``/``stop`` to callbacks
provided by the service.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable

from . import protocol

LOGGER = logging.getLogger(__name__)


class UdpApi(threading.Thread):
    """Background UDP server."""

    def __init__(
        self,
        port: int,
        handler: Callable[[protocol.UdpPacket], str | None],
        host: str = "0.0.0.0",
    ) -> None:
        super().__init__(name="xkwmon-udp", daemon=True)
        self.port = port
        self.host = host
        self.handler = handler
        self._stop = threading.Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.settimeout(0.5)

    def bind(self) -> None:
        self._socket.bind((self.host, self.port))

    def run(self) -> None:
        try:
            self.bind()
        except OSError as exc:
            LOGGER.error("cannot bind UDP port %s: %s", self.port, exc)
            return

        LOGGER.info("UDP API listening on %s:%s", self.host, self.port)
        while not self._stop.is_set():
            try:
                data, remote = self._socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    break
                LOGGER.debug("udp recv error: %s", exc)
                continue

            packet = protocol.parse_udp_packet(data)
            if packet is None:
                LOGGER.debug("ignoring malformed packet from %s", remote)
                continue

            try:
                response = self.handler(packet)
            except Exception:  # pragma: no cover - defensive
                LOGGER.exception("handler failed for %r", packet)
                continue

            if response is None:
                continue
            try:
                reply = protocol.build_udp_packet(packet.cmd, response, packet.cmd_no, cmd_type=1)
                self._socket.sendto(reply, remote)
            except OSError as exc:
                LOGGER.debug("udp send error to %s: %s", remote, exc)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass


def query(
    host: str,
    cmd: str = protocol.CMD_RUNDATA,
    port: int = 8890,
    data: str = "",
    timeout: float = 2.0,
) -> protocol.UdpPacket | None:
    """Send one command and return the decoded answer (``None`` on timeout)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(protocol.build_udp_packet(cmd, data, 1, cmd_type=0), (host, port))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                answer, _ = sock.recvfrom(65535)
            except socket.timeout:
                return None
            packet = protocol.parse_udp_packet(answer)
            if packet is not None:
                return packet
        return None
    finally:
        sock.close()
