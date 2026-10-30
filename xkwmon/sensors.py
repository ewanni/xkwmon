"""Linux sensor backends that replace the Windows CPUID SDK.

Everything is read from ``/proc`` and ``/sys`` so the package stays dependency
free.  NVIDIA GPUs are queried through ``libnvidia-ml`` (ctypes) with an
``nvidia-smi`` fallback; Intel/AMD GPUs fall back to the DRM hwmon nodes.

Values mirror the data the original service collected with CPUID SDK:
CPU package temperature/load/clock/power, per-core rows, GPU rows, RAM usage,
fans, per-interface network throughput and disk activity.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import glob
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    text = _read(path)
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _read_float(path: str) -> float | None:
    text = _read(path)
    if text is None:
        return None
    try:
        return float(text.strip())
    except ValueError:
        return None


def _first_match(values: list[float]) -> float:
    return values[0] if values else 0.0


def _hwmon_dirs() -> list[str]:
    return sorted(glob.glob("/sys/class/hwmon/hwmon*"))


def find_hwmon(names: tuple[str, ...]) -> str | None:
    for directory in _hwmon_dirs():
        name = _read(os.path.join(directory, "name"))
        if name and name.strip() in names:
            return directory
    return None


def _hwmon_temps(directory: str) -> dict[str, float]:
    """Map label -> temperature (deg C) for a hwmon directory."""
    result: dict[str, float] = {}
    for path in sorted(glob.glob(os.path.join(directory, "temp*_input"))):
        base = path[: -len("_input")]
        raw = _read_int(path)
        if raw is None:
            continue
        label = _read(base + "_label")
        key = label.strip() if label else os.path.basename(base)
        result[key] = raw / 1000.0
    return result


def _hwmon_fans() -> list[dict[str, Any]]:
    fans: list[dict[str, Any]] = []
    for directory in _hwmon_dirs():
        chip = (_read(os.path.join(directory, "name")) or "hwmon").strip()
        for path in sorted(glob.glob(os.path.join(directory, "fan*_input"))):
            value = _read_int(path)
            if value is None:
                continue
            base = path[: -len("_input")]
            label = _read(base + "_label")
            name = label.strip() if label else f"{chip} {os.path.basename(base)}"
            fans.append({"name": name, "speed": float(value)})
    return fans


# --------------------------------------------------------------------------- #
# CPU
# --------------------------------------------------------------------------- #


def _cpu_stat() -> tuple[float, float, list[tuple[float, float]]]:
    """Return (total, idle, per-cpu list of (total, idle))."""
    text = _read("/proc/stat") or ""
    total = idle = 0.0
    per_cpu: list[tuple[float, float]] = []
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        values = [float(value) for value in parts[1:]]
        cpu_total = sum(values)
        cpu_idle = values[3] + (values[4] if len(values) > 4 else 0.0)
        if parts[0] == "cpu":
            total, idle = cpu_total, cpu_idle
        else:
            per_cpu.append((cpu_total, cpu_idle))
    return total, idle, per_cpu


def _cpuinfo_entries() -> list[dict[str, str]]:
    text = _read("/proc/cpuinfo") or ""
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
            current = {}
            continue
        key, _, value = line.partition(":")
        current[key.strip()] = value.strip()
    if current:
        entries.append(current)
    return entries


def _cpuinfo_frequencies() -> list[float]:
    return [
        float(entry["cpu MHz"])
        for entry in _cpuinfo_entries()
        if "cpu MHz" in entry
    ]


def _cpu_model_name() -> str:
    for entry in _cpuinfo_entries():
        if "model name" in entry:
            return entry["model name"]
    return "Unknown CPU"


class RaplPower:
    """Package power from RAPL ``energy_uj`` deltas."""

    def __init__(self) -> None:
        self._paths = []
        for path in sorted(glob.glob("/sys/class/powercap/intel-rapl:*/energy_uj")):
            # Only the package domain (intel-rapl:0), not the subdomains.
            if re.search(r"intel-rapl:\d+/energy_uj$", path):
                self._paths.append(path)
        self._last: dict[str, tuple[float, int]] = {}

    def sample(self) -> float:
        watts = 0.0
        now = time.monotonic()
        for path in self._paths:
            value = _read_int(path)
            if value is None:
                continue
            previous = self._last.get(path)
            self._last[path] = (now, value)
            if not previous:
                continue
            last_time, last_value = previous
            delta_time = now - last_time
            if delta_time <= 0:
                continue
            delta = value - last_value
            if delta < 0:  # counter wrapped (~32 bit microseconds)
                delta += 2**32
            watts += delta / 1_000_000.0 / delta_time
        return watts


# --------------------------------------------------------------------------- #
# GPU
# --------------------------------------------------------------------------- #

_NVML_TEMPERATURE_GPU = 0
_NVML_CLOCK_GRAPHICS = 0
_NVML_CLOCK_SM = 1


class _NvmlUtilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class NvmlGpu:
    """Thin ctypes wrapper around ``libnvidia-ml.so.1``."""

    def __init__(self) -> None:
        libname = ctypes.util.find_library("nvidia-ml") or "libnvidia-ml.so.1"
        self.lib = ctypes.CDLL(libname)
        self._init()
        self.name = self._name()

    def _init(self) -> None:
        lib = self.lib
        lib.nvmlInit_v2.restype = ctypes.c_int
        if lib.nvmlInit_v2() != 0:
            raise RuntimeError("nvmlInit_v2 failed")
        lib.nvmlDeviceGetHandleByIndex_v2.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
        lib.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
        handle = ctypes.c_void_p()
        if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)) != 0:
            raise RuntimeError("nvmlDeviceGetHandleByIndex_v2 failed")
        self.handle = handle

        lib.nvmlDeviceGetName.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint]
        lib.nvmlSystemGetDriverVersion.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        lib.nvmlDeviceGetTemperature.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
        lib.nvmlDeviceGetUtilizationRates.argtypes = [ctypes.c_void_p, ctypes.POINTER(_NvmlUtilization)]
        lib.nvmlDeviceGetClockInfo.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
        lib.nvmlDeviceGetFanSpeed.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        lib.nvmlDeviceGetPowerUsage.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]

    def _name(self) -> str:
        buffer = ctypes.create_string_buffer(96)
        try:
            if self.lib.nvmlDeviceGetName(self.handle, buffer, 96) == 0:
                return buffer.value.decode("utf-8", "replace")
        except Exception:
            pass
        return "NVIDIA GPU"

    def temperature(self) -> float | None:
        value = ctypes.c_uint()
        if self.lib.nvmlDeviceGetTemperature(self.handle, _NVML_TEMPERATURE_GPU, ctypes.byref(value)) == 0:
            return float(value.value)
        return None

    def utilization(self) -> float | None:
        value = _NvmlUtilization()
        if self.lib.nvmlDeviceGetUtilizationRates(self.handle, ctypes.byref(value)) == 0:
            return float(value.gpu)
        return None

    def clock(self) -> float | None:
        value = ctypes.c_uint()
        if self.lib.nvmlDeviceGetClockInfo(self.handle, _NVML_CLOCK_GRAPHICS, ctypes.byref(value)) == 0:
            return float(value.value)
        return None

    def fan(self) -> float | None:
        value = ctypes.c_uint()
        if self.lib.nvmlDeviceGetFanSpeed(self.handle, ctypes.byref(value)) == 0:
            return float(value.value)
        return None

    def power(self) -> float | None:
        value = ctypes.c_uint()
        if self.lib.nvmlDeviceGetPowerUsage(self.handle, ctypes.byref(value)) == 0:
            return value.value / 1000.0
        return None

    def driver_version(self) -> str:
        buffer = ctypes.create_string_buffer(80)
        try:
            if self.lib.nvmlSystemGetDriverVersion(buffer, 80) == 0:
                return buffer.value.decode("utf-8", "replace")
        except Exception:
            pass
        return ""


def nvidia_smi() -> dict[str, float] | None:
    """Fallback GPU query using ``nvidia-smi``."""
    try:
        output = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu,utilization.gpu,"
                "clocks.current.graphics,fan.speed,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if output.returncode != 0 or not output.stdout.strip():
        return None
    line = output.stdout.strip().splitlines()[0]
    parts = [part.strip() for part in line.split(",")]

    def number(index: int) -> float | None:
        try:
            return float(parts[index])
        except (IndexError, ValueError):
            return None

    return {
        "name": parts[0] if parts else "NVIDIA GPU",
        "temperature": number(1) or 0.0,
        "load": number(2) or 0.0,
        "clock": number(3) or 0.0,
        "fan": number(4) or 0.0,
        "power": number(5) or 0.0,
    }


def drm_gpu() -> dict[str, Any] | None:
    """Best-effort GPU data straight from the DRM hwmon nodes."""
    for hwmon in sorted(glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*")):
        temperature = _read_int(os.path.join(hwmon, "temp1_input"))
        clock_hz = _read_int(os.path.join(hwmon, "freq1_input"))
        if temperature is None and clock_hz is None:
            continue
        card = hwmon.split("/sys/class/drm/")[1].split("/")[0]
        return {
            "name": f"DRM {card}",
            "temperature": (temperature or 0) / 1000.0,
            "load": 0.0,
            "clock": (clock_hz or 0) / 1_000_000.0,
            "fan": 0.0,
            "power": 0.0,
        }
    return None


# --------------------------------------------------------------------------- #
# RAM / network / disk
# --------------------------------------------------------------------------- #


def read_meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    text = _read("/proc/meminfo") or ""
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if not rest:
            continue
        parts = rest.split()
        if parts:
            try:
                result[key] = int(parts[0]) * 1024
            except ValueError:
                pass
    return result


_REAL_DISK = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|hd[a-z]+|mmcblk\d+)$")


def read_diskstats() -> dict[str, int]:
    """Map whole-disk name -> milliseconds spent doing I/O."""
    result: dict[str, int] = {}
    text = _read("/proc/diskstats") or ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        if not _REAL_DISK.match(name):
            continue
        try:
            result[name] = int(parts[12])
        except ValueError:
            continue
    return result


def read_netdev() -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    text = _read("/proc/net/dev") or ""
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        if not rest:
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        try:
            result[name.strip()] = (int(fields[0]), int(fields[8]))
        except ValueError:
            continue
    return result


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #


@dataclass
class Snapshot:
    """One sensor sample, ready for the HID frame and the UDP API."""

    timestamp: float = 0.0
    cpu_temp: float = 0.0
    cpu_load: float = 0.0
    cpu_clock: float = 0.0
    cpu_power: float = 0.0
    cpu_voltage: float = 0.0
    cpu_cores: list[dict[str, Any]] = field(default_factory=list)
    gpu_name: str = ""
    gpu_temp: float = 0.0
    gpu_load: float = 0.0
    gpu_clock: float = 0.0
    gpu_fan: float = 0.0
    gpu_power: float = 0.0
    gpu_voltage: float = 0.0
    ram_total_gb: float = 0.0
    ram_used_gb: float = 0.0
    ram_avail_gb: float = 0.0
    ram_percent: float = 0.0
    net_up: float = 0.0
    net_down: float = 0.0
    net_items: list[dict[str, Any]] = field(default_factory=list)
    disk_load: float = 0.0
    fans: list[dict[str, Any]] = field(default_factory=list)

    # -- views used by the producers --------------------------------------

    def as_frame_data(self) -> dict[str, float]:
        return {
            "cpu_temp": self.cpu_temp,
            "cpu_load": self.cpu_load,
            "cpu_clock": self.cpu_clock,
            "cpu_power": self.cpu_power,
            "cpu_voltage": self.cpu_voltage,
            "gpu_temp": self.gpu_temp,
            "gpu_load": self.gpu_load,
            "gpu_clock": self.gpu_clock,
            "gpu_fan": self.gpu_fan,
            "gpu_power": self.gpu_power,
            "gpu_voltage": self.gpu_voltage,
            "ram_percent": self.ram_percent,
            "net_up": self.net_up,
            "net_down": self.net_down,
            "disk_load": self.disk_load,
            # Panel "RPM" field: the patched Windows build shows the CPU
            # frequency there, so keep that behaviour.
            "rpm": self.cpu_clock,
        }

    def cpu_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = [
            {
                "name": "packge",
                "temperature": round(self.cpu_temp, 2),
                "load": round(self.cpu_load, 2),
                "powers": round(self.cpu_power, 2),
                "voltage": round(self.cpu_voltage, 2),
                "clock": f"{self.cpu_clock:.2f}",
            }
        ]
        rows.extend(self.cpu_cores)
        return rows

    def gpu_rows(self) -> list[dict[str, Any]]:
        if not self.gpu_name and not self.gpu_temp:
            return []
        row: dict[str, Any] = {
            "name": self.gpu_name or "GPU",
            "temperature": round(self.gpu_temp, 2),
            "load": round(self.gpu_load, 2),
            "clock": round(self.gpu_clock, 2),
        }
        if self.gpu_fan:
            row["fan"] = round(self.gpu_fan, 2)
        return [row]

    def ram_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "Used Memory": round(self.ram_used_gb, 2),
                "Available Memory": round(self.ram_avail_gb, 2),
                "Memory": round(self.ram_total_gb, 2),
            }
        ]

    def fan_rows(self) -> list[dict[str, Any]]:
        return list(self.fans)

    def net_rows(self) -> list[dict[str, Any]]:
        return list(self.net_items)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["cpu_rows"] = self.cpu_rows()
        data["gpu_rows"] = self.gpu_rows()
        data["ram_rows"] = self.ram_rows()
        data["fan_rows"] = self.fan_rows()
        data["net_rows"] = self.net_rows()
        return data


class SensorReader:
    """Collects one :class:`Snapshot` per call, keeping delta state."""

    def __init__(self) -> None:
        self._cpu_prev = _cpu_stat()
        self._disk_prev = (time.monotonic(), read_diskstats())
        self._net_prev = (time.monotonic(), read_netdev())
        self._rapl = RaplPower()
        self._rapl.sample()  # seed
        self._coretemp = find_hwmon(("coretemp", "k10temp", "zenpower"))
        self._gpu = None
        self._gpu_error = None
        self._smi_available = True
        try:
            self._gpu = NvmlGpu()
        except Exception as exc:  # NVML unavailable -> fallback below
            self._gpu_error = str(exc)

    # -- individual sensors ------------------------------------------------

    def _cpu(self) -> tuple[float, list[float]]:
        previous = self._cpu_prev
        current = _cpu_stat()
        self._cpu_prev = current

        total, idle, per_cpu = current
        prev_total, prev_idle, prev_per_cpu = previous
        delta_total = total - prev_total
        delta_idle = idle - prev_idle
        load = 0.0 if delta_total <= 0 else max(0.0, min(100.0, 100.0 * (delta_total - delta_idle) / delta_total))

        per_cpu_load: list[float] = []
        for index, (cpu_total, cpu_idle) in enumerate(per_cpu):
            if index < len(prev_per_cpu):
                prev_cpu_total, prev_cpu_idle = prev_per_cpu[index]
                diff = cpu_total - prev_cpu_total
                if diff > 0:
                    per_cpu_load.append(max(0.0, min(100.0, 100.0 * (diff - (cpu_idle - prev_cpu_idle)) / diff)))
                else:
                    per_cpu_load.append(0.0)
            else:
                per_cpu_load.append(0.0)
        return load, per_cpu_load  # type: ignore[return-value]

    def _cpu_temps(self) -> tuple[float, dict[int, float]]:
        if not self._coretemp:
            self._coretemp = find_hwmon(("coretemp", "k10temp", "zenpower"))
        if not self._coretemp:
            return 0.0, {}
        temps = _hwmon_temps(self._coretemp)
        package = temps.get("Package id 0")
        if package is None:
            package = temps.get("Tctl", temps.get("Tdie"))
        if package is None and temps:
            package = max(temps.values())
        cores: dict[int, float] = {}
        for label, value in temps.items():
            match = re.match(r"Core (\d+)$", label)
            if match:
                cores[int(match.group(1))] = value
        return float(package or 0.0), cores

    def _ram(self) -> tuple[float, float, float, float]:
        info = read_meminfo()
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", info.get("MemFree", 0))
        used = max(0, total - available)
        gb = 1024.0 ** 3
        total_gb = total / gb
        used_gb = used / gb
        avail_gb = available / gb
        percent = (used / total * 100.0) if total else 0.0
        return total_gb, used_gb, avail_gb, percent

    def _network(self) -> tuple[float, float, list[dict[str, Any]]]:
        now = time.monotonic()
        current = read_netdev()
        prev_time, prev = self._net_prev
        self._net_prev = (now, current)
        elapsed = max(1e-6, now - prev_time)

        items: list[dict[str, Any]] = []
        up = down = 0.0
        for name, (rx, tx) in sorted(current.items()):
            if name == "lo":
                continue
            prev_rx, prev_tx = prev.get(name, (rx, tx))
            rx_rate = max(0.0, (rx - prev_rx) / elapsed)
            tx_rate = max(0.0, (tx - prev_tx) / elapsed)
            # The original reported the busiest interface per direction.
            up = max(up, tx_rate)
            down = max(down, rx_rate)
            if rx_rate or tx_rate:
                items.append(
                    {
                        "name": name,
                        "ReceivedPerSec": int(rx_rate),
                        "SentPerSec": int(tx_rate),
                    }
                )
        return up, down, items

    def _disk(self) -> float:
        now = time.monotonic()
        current = read_diskstats()
        prev_time, prev = self._disk_prev
        self._disk_prev = (now, current)
        elapsed_ms = max(1e-3, (now - prev_time) * 1000.0)

        busiest = 0.0
        for name, io_ms in current.items():
            prev_io = prev.get(name, io_ms)
            ratio = max(0.0, (io_ms - prev_io) / elapsed_ms) * 100.0
            busiest = max(busiest, min(100.0, ratio))
        return busiest

    def _gpu_sample(self) -> dict[str, Any]:
        if self._gpu is not None:
            try:
                return {
                    "name": self._gpu.name,
                    "temperature": self._gpu.temperature() or 0.0,
                    "load": self._gpu.utilization() or 0.0,
                    "clock": self._gpu.clock() or 0.0,
                    "fan": self._gpu.fan() or 0.0,
                    "power": self._gpu.power() or 0.0,
                }
            except Exception:
                self._gpu = None
        if self._smi_available:
            smi = nvidia_smi()
            if smi:
                return smi
            # Do not spawn a failing nvidia-smi on every sample.
            self._smi_available = False
        drm = drm_gpu()
        if drm:
            return drm
        return {}

    # -- public API --------------------------------------------------------

    def sample(self) -> Snapshot:
        snap = Snapshot(timestamp=time.time())

        try:
            cpu_load, per_cpu_load = self._cpu()
        except Exception:
            cpu_load, per_cpu_load = 0.0, []

        try:
            cpu_temp, core_temps = self._cpu_temps()
        except Exception:
            cpu_temp, core_temps = 0.0, {}

        frequencies = _cpuinfo_frequencies()
        cpu_clock = max(frequencies) if frequencies else 0.0

        try:
            cpu_power = self._rapl.sample()
        except Exception:
            cpu_power = 0.0

        # Per-core rows (only when the hardware exposes them).
        for position in sorted(core_temps):
            row = {"name": f"CPU #{position}"}
            if position < len(frequencies):
                row["clock"] = round(frequencies[position], 2)
            row["temperature"] = round(core_temps[position], 2)
            if position < len(per_cpu_load):
                row["load"] = round(per_cpu_load[position], 2)
            if len(row) == 4:  # name + clock + temperature + load
                snap.cpu_cores.append(row)

        snap.cpu_temp = cpu_temp
        snap.cpu_load = cpu_load
        snap.cpu_clock = cpu_clock
        snap.cpu_power = cpu_power

        gpu = self._gpu_sample()
        snap.gpu_name = str(gpu.get("name", ""))
        snap.gpu_temp = float(gpu.get("temperature", 0.0) or 0.0)
        snap.gpu_load = float(gpu.get("load", 0.0) or 0.0)
        snap.gpu_clock = float(gpu.get("clock", 0.0) or 0.0)
        snap.gpu_fan = float(gpu.get("fan", 0.0) or 0.0)
        snap.gpu_power = float(gpu.get("power", 0.0) or 0.0)

        try:
            (
                snap.ram_total_gb,
                snap.ram_used_gb,
                snap.ram_avail_gb,
                snap.ram_percent,
            ) = self._ram()
        except Exception:
            pass

        try:
            snap.net_up, snap.net_down, snap.net_items = self._network()
        except Exception:
            pass

        try:
            snap.disk_load = self._disk()
        except Exception:
            pass

        snap.fans = _hwmon_fans()
        if snap.gpu_fan:
            snap.fans.insert(0, {"name": "GPU FAN", "speed": round(snap.gpu_fan, 2)})

        return snap


# --------------------------------------------------------------------------- #
# Static information (the "staticdata" UDP command)
# --------------------------------------------------------------------------- #


def _dmi(name: str) -> str:
    value = _read(f"/sys/class/dmi/id/{name}")
    return value.strip() if value else ""


def _edid_vendor(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            data = handle.read(128)
    except OSError:
        return ""
    if len(data) < 10 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return ""
    code = (data[8] << 8) | data[9]
    return "".join(
        chr(((code >> shift) & 0x1F) + ord("A") - 1) for shift in (10, 5, 0)
    )


def static_items(gpu_name: str = "", driver_version: str = "") -> list[dict[str, Any]]:
    """Build the ``staticdata`` payload in the original item format."""
    from .protocol import make_item  # local import to avoid a cycle

    items: list[dict[str, Any]] = []

    os_name = "Linux"
    for line in (_read("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            os_name = line.split("=", 1)[1].strip().strip('"')
            break
    items.append(make_item("SystemOS", [{"Caption": os_name}]))

    board = {
        "SerialNumber": _dmi("board_serial"),
        "Manufacturer": _dmi("board_vendor"),
        "Product": _dmi("board_name"),
        "Version": _dmi("board_version"),
    }
    board = {key: value for key, value in board.items() if value}
    if len(board) > 1:
        items.append(make_item("BaseBoard", [board]))

    entries = _cpuinfo_entries()
    first = entries[0] if entries else {}
    cpu = {
        "Name": first.get("model name", _cpu_model_name()),
        "NumberOfLogicalProcessors": str(len(entries)) if entries else "0",
        "NumberOfEnabledCore": first.get("cpu cores", ""),
        "ProcessorId": f"{first.get('vendor_id', '')}-{first.get('family', '')}-{first.get('model', '')}",
    }
    cpu = {key: value for key, value in cpu.items() if value}
    if len(cpu) > 1:
        items.append(make_item("Cpu", [cpu]))

    gpu = {}
    if gpu_name:
        gpu["Caption"] = gpu_name
        gpu["CurrentNumberOfColors"] = "4294967296"
        version = driver_version
        if not version:
            text = _read("/proc/driver/nvidia/version") or ""
            match = re.search(r"Kernel Module\s+(\S+)", text)
            if match:
                version = match.group(1)
        if version:
            gpu["DriverVersion"] = version
        items.append(make_item("Gpu", [gpu]))

    mem_total = read_meminfo().get("MemTotal", 0)
    if mem_total:
        items.append(
            make_item(
                "Ram",
                [
                    {
                        "Manufacturer": _dmi("board_vendor") or "Unknown",
                        "Capacity": str(mem_total),
                    }
                ],
            )
        )

    disks = []
    for block in sorted(glob.glob("/sys/block/*")):
        name = os.path.basename(block)
        if not _REAL_DISK.match(name):
            continue
        size = _read_int(os.path.join(block, "size")) or 0
        model = (_read(os.path.join(block, "device", "model")) or "").strip()
        firmware = (_read(os.path.join(block, "device", "rev")) or "").strip()
        caption = model or name
        row = {"Caption": caption, "Size": str(size * 512)}
        if firmware:
            row["FirmwareRevision"] = firmware
        disks.append(row)
    if disks:
        items.append(make_item("HHD", disks))

    displays = []
    for status_path in sorted(glob.glob("/sys/class/drm/card*-*/status")):
        status = (_read(status_path) or "").strip()
        if status != "connected":
            continue
        connector = status_path.split("/")[-2]
        vendor = _edid_vendor(status_path.replace("status", "edid"))
        displays.append({"InstanceName": vendor or connector})
    if displays:
        items.append(make_item("Display", displays))

    return items


def rundata_items(snapshot: Snapshot) -> list[dict[str, Any]]:
    """Build the ``rundata`` payload in the original item format."""
    from .protocol import make_item  # local import to avoid a cycle

    items: list[dict[str, Any]] = [make_item("Cpu", snapshot.cpu_rows())]
    gpu = snapshot.gpu_rows()
    if gpu:
        items.append(make_item("Gpu", gpu))
    items.append(make_item("Ram", snapshot.ram_rows()))
    fans = snapshot.fan_rows()
    if fans:
        items.append(make_item("Fan", fans))
    net = snapshot.net_rows()
    if net:
        items.append(make_item("Network", net))
    return items
