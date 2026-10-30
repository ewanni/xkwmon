"""Command line interface for :mod:`xkwmon`."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from typing import Sequence

from . import __version__, api, config as config_module, protocol, sensors
from .hidraw import HidrawDevice, HidrawError
from .service import PanelService, setup_logging


def _config(args: argparse.Namespace) -> config_module.Config:
    return config_module.load(getattr(args, "config", None))


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = _config(args)
    setup_logging(cfg)
    service = PanelService(cfg)

    def _stop(signum, frame):  # noqa: ANN001, ARG001
        service.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    service.run_forever()
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    reader = sensors.SensorReader()
    time.sleep(0.35)  # allow delta based sensors to produce a value
    snapshot = reader.sample()
    if args.json:
        _print_json(snapshot.to_dict())
    else:
        print(f"CPU   {snapshot.cpu_temp:6.1f} °C  load {snapshot.cpu_load:5.1f} %  "
              f"clock {snapshot.cpu_clock:7.0f} MHz  power {snapshot.cpu_power:5.1f} W")
        print(f"GPU   {snapshot.gpu_name or '-':<28} {snapshot.gpu_temp:5.1f} °C  "
              f"load {snapshot.gpu_load:5.1f} %  clock {snapshot.gpu_clock:5.0f} MHz  "
              f"fan {snapshot.gpu_fan:3.0f} %  power {snapshot.gpu_power:5.1f} W")
        print(f"RAM   {snapshot.ram_used_gb:5.1f} / {snapshot.ram_total_gb:5.1f} GiB  "
              f"({snapshot.ram_percent:4.1f} %)")
        print(f"NET   up {snapshot.net_up / 1e6:8.2f} MB/s  down {snapshot.net_down / 1e6:8.2f} MB/s")
        print(f"DISK  load {snapshot.disk_load:5.1f} %")
        if snapshot.fans:
            print("FANS  " + ", ".join(f"{fan['name']}={fan['speed']:.0f}" for fan in snapshot.fans))
        if snapshot.cpu_cores:
            print("CORES " + ", ".join(
                f"{core['name']}:{core.get('temperature', 0):.0f}°C" for core in snapshot.cpu_cores[:8]
            ) + (" ..." if len(snapshot.cpu_cores) > 8 else ""))
    return 0


def cmd_static(args: argparse.Namespace) -> int:
    reader = sensors.SensorReader()
    gpu = getattr(reader, "_gpu", None)
    items = sensors.static_items(gpu.name if gpu else "", gpu.driver_version() if gpu else "")
    if args.json:
        _print_json(items)
    else:
        for item in items:
            rows = json.loads(item["data"])
            print(f"[{item['type']}]")
            for row in rows:
                print("  " + ", ".join(f"{key}={value}" for key, value in row.items()))
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    cfg = _config(args)
    devices = HidrawDevice.enumerate()
    for info in devices:
        marker = " <- panel" if info["vid"] == cfg.hid_vid and info["pid"] == cfg.hid_pid else ""
        vid, pid = info["vid"], info["pid"]
        ids = f"{vid:04x}:{pid:04x}" if isinstance(vid, int) and isinstance(pid, int) else "?:?"
        print(f"{info['path']}  {ids}  {info.get('name') or '?'}{marker}")
    if not devices:
        print("no hidraw devices found")
        return 1
    return 0


def cmd_panel(args: argparse.Namespace) -> int:
    cfg = _config(args)
    setup_logging(cfg)
    try:
        device = HidrawDevice.open(args.path or cfg.hid_path or None, cfg.hid_vid, cfg.hid_pid)
    except HidrawError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"opened {device.path}")
    try:
        variant = device.handshake(attempts=max(1, args.attempts))
        if variant:
            print(f"handshake: OK ({variant})")
        else:
            print("handshake: no valid answer, diagnosing:")
            for result in device.probe_handshake():
                answer = result["answer"]
                shown = answer.hex(" ") if answer else "<no answer>"
                print(
                    f"  {result['variant']:<14} expected=0x{int(result['expected']):04x} "
                    f"verified={result['verified']} answer={shown}"
                )
            if not args.force:
                return 2

        reader = sensors.SensorReader()
        time.sleep(0.35)
        frames = 0
        for index in range(max(0, args.frames)):
            snapshot = reader.sample()
            frame = protocol.build_data_frame(snapshot.as_frame_data())
            device.write_report(frame)
            frames += 1
            if args.hex:
                print(frame.hex(" "))
            if args.frames > 1:
                time.sleep(max(0.0, args.interval))
        print(f"sent {frames} data frame(s)")
    finally:
        device.close()
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    packet = api.query(args.host, args.cmd, args.port, timeout=args.timeout)
    if packet is None:
        print("no answer", file=sys.stderr)
        return 1
    print(packet.data)
    return 0


def cmd_raw(args: argparse.Namespace) -> int:
    """Print a data frame without touching the panel (useful for debugging)."""
    reader = sensors.SensorReader()
    time.sleep(0.35)
    snapshot = reader.sample()
    frame = protocol.build_data_frame(snapshot.as_frame_data())
    if args.hex:
        print(frame.hex(" "))
    else:
        for index, value in enumerate(frame):
            print(f"{index:2d}  0x{value:02x}  {value:3d}")
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xkwmon",
        description="XKW USB display driver (HID panel + UDP API) for Linux",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c", "--config", metavar="PATH", help="config.ini path (default: /etc/xkwmon/config.ini)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the service (systemd uses this)")
    serve.set_defaults(func=cmd_serve)

    probe = subparsers.add_parser("probe", help="print one sensor sample")
    probe.add_argument("--json", action="store_true", help="machine readable output")
    probe.set_defaults(func=cmd_probe)

    static = subparsers.add_parser("static", help="print static system information")
    static.add_argument("--json", action="store_true", help="machine readable output")
    static.set_defaults(func=cmd_static)

    devices = subparsers.add_parser("devices", help="list hidraw devices")
    devices.set_defaults(func=cmd_devices)

    panel = subparsers.add_parser("panel", help="handshake and send test frames")
    panel.add_argument("--path", help="explicit /dev/hidrawN")
    panel.add_argument("--frames", type=int, default=5, help="number of frames to send")
    panel.add_argument("--interval", type=float, default=0.5, help="delay between frames, s")
    panel.add_argument("--attempts", type=int, default=3, help="handshake attempts")
    panel.add_argument("--hex", action="store_true", help="dump each frame")
    panel.add_argument("--force", action="store_true", help="send frames even without handshake")
    panel.set_defaults(func=cmd_panel)

    query = subparsers.add_parser("query", help="query a running service over UDP")
    query.add_argument("--host", default="127.0.0.1")
    query.add_argument("--port", type=int, default=8890)
    query.add_argument("--cmd", default=protocol.CMD_RUNDATA,
                       choices=[protocol.CMD_RUNDATA, protocol.CMD_STATICDATA])
    query.add_argument("--timeout", type=float, default=2.0)
    query.set_defaults(func=cmd_query)

    raw = subparsers.add_parser("raw", help="print the HID data frame as hex/table")
    raw.add_argument("--hex", action="store_true")
    raw.set_defaults(func=cmd_raw)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
