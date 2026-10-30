# xkwmon — 1STPLAYER TS4 240 / XKW USB display driver for Linux (Python)

Linux driver for the **1STPLAYER TS4 240** USB temperature panel (the same
hardware is sold as "XKW" / "HWCX USB Display", USB id `0145:1001`).

It reads CPU/GPU/RAM/network/disk sensors from `/proc` and `/sys`, draws them
on the panel over HID, and exposes the original UDP API on port `8890`.

No third-party Python packages, no `libhidapi` — reports are written straight
to `/dev/hidrawN`.

## Supported devices

| product | USB id | notes |
|---|---|---|
| **1STPLAYER TS4 240** | `0145:1001` | tested, panel shows CPU/GPU temp, load, RAM, "RPM" |
| XKW / HWCX "USB Display" | `0145:1001` | same MCU and protocol |
| SLAYER CPU Live Temperature, RM-Hardware, LK Digital Display | other ids | protocol is shared; ids are configurable (`hidVid`/`hidPid`) |

Search terms: 1STPLAYER TS4 240, TS4-240, 1st player TS4, XKW HWCX USB Display,
0145:1001, temperature display Linux driver.

## How the original driver works

The Windows service (`XKWLib.dll` + `TempComm.dll` + `HardwareHelp64.dll`):

1. every `fefresh_delay` ms it refreshes hardware values (CPUID SDK driver);
2. every `send_delay` ms it packs all cached values into a **65-byte HID
   report** (`cmd = 0x02`) and writes it to the panel;
3. on every (re)connect it first performs a **handshake** (`cmd = 0x01`) with
   random token/nonce and two checksum bytes — without it the panel ignores
   data frames;
4. it runs a **UDP server** on `localPort` (8890) speaking
   `FF FE FF FD | type | no_hi | no_lo | "cmd|data" | FF FE FF FD`, answering
   `rundata` / `staticdata` with JSON where each item's `data` is itself a JSON
   string (double encoded, kept for client compatibility);
5. the "RPM" field of this panel was patched to show the **CPU frequency in
   MHz** instead of a fan speed; this port keeps that as the default and lets
   you switch back with `RpmMode=fan`.

On Linux the data sources become: `/sys/class/hwmon` (CPU/GPU temperatures),
`/proc/stat` (load), `/proc/cpuinfo` (frequency), RAPL `energy_uj` (CPU power),
NVML/`nvidia-smi` (NVIDIA GPU), `/proc/meminfo`, `/proc/net/dev`,
`/proc/diskstats`.

### HID data frame (65 bytes, report id 0 + 64 payload)

| offset | field |
|---|---|
| 1 | cmd `0x02` |
| 2 | CPU temperature °C |
| 3 | GPU temperature °C |
| 4..12 | hour, minute, second, ms/10, year/100, year%100, month, day, weekday |
| 13 | CPU load % |
| 14..15 | CPU clock MHz, big-endian |
| 16 | GPU load % |
| 17..18 | GPU clock MHz, big-endian |
| 20 | RAM used % ("RAM LOAD") |
| 23 | disk value (unused) |
| 24 | disk load % |
| 25..26 | "RPM" word, big-endian (see `RpmMode`: CPU clock MHz or fan/pump RPM) |
| 27..28 | CPU voltage, big-endian (whole*256 + hundredths) |
| 29..30 | CPU power, big-endian (1/10 W) |
| 31..32 | GPU fan, big-endian |
| 33..34 | GPU voltage, big-endian |
| 35..36 | GPU power, big-endian (1/10 W) |
| 37..40 | reserved (zero) |
| 41..44 | network upload B/s, big-endian dword |
| 45..48 | network download B/s, big-endian dword |

### Handshake (65 bytes, report id 0 + 64 payload)

| offset | field |
|---|---|
| 1 | cmd `0x01` |
| 5..6 | random 16-bit token, big-endian |
| 7..10 | random 4-byte nonce |
| 11..12 | 16-bit challenge, big-endian |
| 13..62 | random padding |
| 63 | `c1 = XOR(i * b[1+i]) for i = 0..59` (mod 256) |
| 64 | `c2 = SUM(i * b[1+i]) for i = 0..62` (mod 256, includes `c1`) |

The panel answers with 65 bytes whose bytes 5..6 must contain

```
E = (nonce[3] + nonce[2]*256) ^ (token_lo + nonce[1] + (token_hi + nonce[0])*256)
```

and whose bytes 63/64 must repeat `c1`/`c2` computed over the answer.
Note: the value written into bytes 11..12 is **not** `E` — the native DLL uses a
separate challenge value
`C = token ^ ((nonce[1] + nonce[3]) + (nonce[0] + nonce[2])*256)`.
This was recovered from `TempComm.dll` at `0x1800010f0` and is what
`xkwmon.protocol.challenge_value` implements (both variants can be probed with
`xkwmon panel`).

## Quick start (no install)

```sh
python3 -m xkwmon probe      # read all sensors, no root needed
python3 -m xkwmon raw --hex  # show the HID frame that would be sent
python3 -m xkwmon devices    # list hidraw nodes and find the panel
sudo python3 -m xkwmon panel # handshake + send 5 frames (needs root)
```

## Install as a service

```sh
sudo ./install.sh          # service runs as root (default)
sudo ./install.sh --user   # service runs as unprivileged user "xkwmon"
```

This installs the package to `/opt/xkwmon`, the config to
`/etc/xkwmon/config.ini`, a launcher to `/usr/local/bin/xkwmon`, the udev rule,
and enables `xkwmon.service`.

With `--user` the installer creates the `xkwmon` system user/group and the udev
rule `99-xkw-panel.rules` gives that group access to the panel's `/dev/hidrawN`.
Nothing else needs root: `/sys` hwmon files and RAPL `energy_uj` are world
readable, and NVIDIA device nodes are `0666`.

```sh
systemctl status xkwmon
journalctl -u xkwmon -f
xkwmon query --cmd rundata   # UDP smoke test
sudo ./uninstall.sh          # keep config
sudo ./uninstall.sh --purge  # remove config + xkwmon user/group
```

The service logs a status line every `StatusLogInterval` seconds (default 60,
`0` disables it), so the journal stays quiet.

## Configuration (`/etc/xkwmon/config.ini`)

Same keys and meaning as the original:

```ini
[config]
fefresh_delay=500   ; sensor poll, ms
send_delay=500      ; HID frame interval, ms
IsLog=False
LogPath=
IsUdp=True
localPort=8890
StatusLogInterval=60 ; status line in journal, seconds (0 = off)
RpmMode=clock        ; "clock" = CPU MHz, "fan" = fan/pump RPM
```

Extra keys: `hidVid`, `hidPid`, `hidPath`, `LogLevel`, `handshakeAttempts`,
`StatusLogInterval`, `RpmMode`.

### RPM field

The panel has one field labelled `RPM`. With the stock firmware it is a fan
tachometer; the patched Windows build (and the default here) put the **CPU
frequency in MHz** there instead.

* `RpmMode=clock` (default) — show CPU clock, e.g. `5400`.
* `RpmMode=fan` — show a pump/fan tachometer in RPM. It picks the first
  hwmon fan whose name contains `cpu` or `pump`, otherwise the fastest one;
  GPU fan percentages are ignored. If the board exposes no fan sensor the
  field stays `0` (many boards need the `it87`/`nct6775` module loaded).

Switch without editing files:

```sh
sudo sed -i 's/^RpmMode=.*/RpmMode=fan/' /etc/xkwmon/config.ini
sudo systemctl restart xkwmon
```

Preview the frame without touching the panel:

```sh
xkwmon raw --rpm clock --hex
xkwmon raw --rpm fan --hex
```

## UDP API

Same protocol and port as the Windows original:

* `rundata` → `[{ "type": "Cpu", "data": "<json string>" }, ...]` with `Cpu`,
  `Gpu`, `Ram`, `Fan`, `Network` items;
* `staticdata` → `SystemOS`, `BaseBoard`, `Cpu`, `Gpu`, `Ram`, `HHD`,
  `Display`;
* `open` / `stop` → resume / pause the panel stream.

## Troubleshooting

| symptom | fix |
|---|---|
| `permission denied /dev/hidrawN` | run as root or install the udev rule |
| panel opened but handshake gives no answer | the MCU is asleep — unplug USB for ~30 s, plug it back |
| no `GPU` data | install the NVIDIA driver (NVML) or check `/sys/class/drm/*/device/hwmon` |
| CPU power is 0 | no RAPL (`/sys/class/powercap`) or not Intel |
| `RPM` shows 0 with `RpmMode=fan` | no fan sensor exported; load `it87`/`nct6775`, or use `RpmMode=clock` |
| UDP not answering | `ss -ulnp \| grep 8890`, open the firewall |

## Tests

```sh
python3 -m unittest discover -s tests -v
```
