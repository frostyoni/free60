# free60

Keeping a forgotten 360° camera alive, and giving it back to the world.

The **MADV Mini** (`com.madv.mini`) was a tiny USB-C panoramic camera from 2018.
It only ever worked through one Android app. The app is gone from Play, the
cloud endpoint (`api.madv360.com:9999`) is dead, and the hardware has been
abandoned. This project makes the camera work again — on Linux, with no app,
no cloud, and no Android.

> `free60` = **free** the **360** camera.

---

## What the device actually is

Marketing called it a 360 camera with "an app". The useful truth, recovered by
reverse-engineering the last surviving APK:

| | |
|---|---|
| SoC | Sunplus / iCatch **SPCA6350** family, RTOS (no Linux inside) |
| Host link | **USB-C tether only** — no Wi-Fi, no Bluetooth |
| USB class | Vendor-specific `ff/06/50` — Mass Storage **Bulk-Only Transport** |
| Command set | Vendor **SCSI** opcodes `0x7A` (act/read) and `0x7B` (write) |
| Live view | **H.264 High, 1920×960, ~30 fps** |
| Projection | **already-stitched equirectangular panorama (2:1)** |
| Firmware on unit | `v1.1126` — the exact image was shipped inside the APK |

The "heavy Android dependency" was never exotic. The camera is a USB SCSI
gadget; Android merely happened to be the only thing hosting it.

**Key discovery:** the camera accepts `start live view` and then answers "busy"
forever until it has been sent the full settings block (`7B 04 60`,
`WRITE_SET_ALL_SETTING`, bitrate 8). The app always sends it first. That single
message was the difference between a dead camera and a video stream.

Full protocol documentation: **[STATE.md](STATE.md)** — transport framing,
every opcode, reply layouts, and the mistakes made along the way.

---

## Status

Working, verified against real hardware:

- ✅ enumeration, session open/close, keep-alive, auto-recovery
- ✅ all settings read/write, date/time
- ✅ **live view — H.264 1920×960 @ ~30 fps**, decodes cleanly
- ✅ thumbnail download (272×272 JPEG)
- ⚠️ still capture **trigger** — accepted and completes in ~1 s, but has
  wedged the unit twice. See the open item below
- ✅ browser demo with a drag-to-look 360° viewer
- ✅ **idle handling** — with no viewer the encoder is stopped (cool) and the
  next request brings the stream straight back; the device stays on the bus
- ✅ **low-CPU mode (`--mode fmp4`)**: the camera's own H.264 is remuxed, not
  re-encoded, and played via MediaSource. Works over plain http.
- ⚠️ `--mode webcodecs` also exists and costs even less, but `VideoDecoder`
  requires a **secure context** — it is black over `http://<lan-ip>`. The page
  now says so out loud instead of failing silently.

Open:

- ⬜ full-resolution still download (`7A 05 02` still answers "busy"). Worse,
  on the current unit a still capture has twice **wedged the camera** part-way
  through — it stays on the bus (`lsusb` still shows it) but every command
  times out, and only a replug clears it. A USB extension lead was initially
  suspected and has since been cleared: the same unit streams at a steady
  30 fps through it. Treat the wedge as an unexplained firmware behaviour
  until someone reproduces it with instrumentation.
- ⬜ video recording (`7A 01 03`) — command accepted, untested end-to-end
- ⬜ firmware reflash path — I hold the exact image and the recovered
  protocol, but the `.prm` patch step is not reimplemented and nothing has
  been flashed. See [docs/FIRMWARE-RECOVERY.md](docs/FIRMWARE-RECOVERY.md)

---

## Requirements

Any Linux box with a **free USB host port**. That is the only hard constraint —
the camera is tether-only (no Wi-Fi, no Bluetooth, no battery), so it has to be
physically plugged into the machine running this.

| Need | Why | Notes |
|---|---|---|
| Python 3.8+ | the driver | distro `python3-usb` is enough; no venv required |
| `libusb-1.0` | USB access | vendor-class device, no kernel driver binds it |
| udev rule | unprivileged access | `tools/60-madv-mini.rules` |
| `ffmpeg` | **only** for the MJPEG web demo | not installed by default; `--with-ffmpeg` to add it. Other modes need none |

No compiler, no kernel module, no GPU, no bootloader work. x86_64, arm64 and
armv7 all work — the driver is pure Python over libusb.

Why Python rather than Rust: **[docs/DESIGN.md](docs/DESIGN.md)** — including
the measurements that say where Python is the wrong answer.

## Portability

| Platform | Status | Notes |
|---|---|---|
| Linux | **proven** | x86_64 (Debian/Ubuntu) and AArch64 (Armbian on S905X / S912 / H616 TV boxes) |
| macOS | expected, **untested** | needs `libusb` and pyusb. No driver binding should be needed — the camera is vendor-class, so macOS has nothing that would claim it |
| Windows | expected, **untested** | needs `libusb` plus a **WinUSB/libusbK driver bound to the camera** (Zadig). Windows has no driver for a class `0xff` device, so otherwise it sits there as an unknown device |

The driver itself is portable by construction: pure Python over pyusb, building
the USB Mass-Storage CBW/CSW envelope itself, with no kernel driver to satisfy.

**Linux-only pieces**, and nothing else:

- `tools/setup-host.sh` — apt and udev
- `tools/60-madv-mini.rules` — udev
- the `SG_IO` fallback transport — the default libusb transport is what
  everything actually uses

Trying it on Windows or macOS: install pyusb and libusb, run
`python tools/madv.py probe`, then the web demo. The first thing I would expect
to break is `os.read(ff.stdout.fileno())` in the capture loop — pipe semantics
differ on Windows — and `--mode mjpeg` would be the mode to start with.
Untested means untested, not "should be fine".

## Power and heat (read before trusting a port)

The camera's USB descriptors claim **Self Powered** and `bMaxPower = 100 mA`.
Do not believe them. There is **no battery**, and while live view is open the
ISP and the H.264 encoder run flat out.

**Actual current draw has not been measured** — that needs inline measurement
hardware this project does not have — but it is plainly well above 100 mA in
use, and the unit gets noticeably warm within minutes of streaming.

**A short USB extension lead is fine and often necessary.** The camera has to
stand upright to give a level panorama, and nobody has an upward-facing USB
port — so in practice it runs on a short passive extension. This project's unit
has been driven that way at a steady 30 fps without trouble. The risk is not
"an extension" as such, it is **weak or overloaded ports**: a busy unpowered
hub, a very long or thin cable, or a port already near its budget.

Failure is misleading. Under-volted, the camera still enumerates and still
acknowledges commands, but never produces a frame — or wedges part-way through
one. A brownout looks exactly like a firmware bug.
- If `probe` and `info` work but `live` stays silent, suspect power before you
  suspect code.
- It has no battery and no soft power switch, so **unplugging is the only hard
  reset** and the only way to fully discharge it.

## Quick start

```bash
git clone <this repo> free60 && cd free60

./tools/setup-host.sh --dry-run   # see what it would change
./tools/setup-host.sh             # libusb, pyusb, udev rule, then probe

<pybin> tools/madv.py info                # expect fw_version = v1.1126
<pybin> tools/madv.py live -n 5 -o live.h264
ffplay live.h264
```

`<pybin>` is whatever the setup script prints at the end: either `python3` or
`./.venv/bin/python`, depending on whether your distro ships pyusb.

- Full deployment checklist, services and firewall traps:
  **[docs/DEPLOY.md](docs/DEPLOY.md)**
- Agent-facing constraints and danger list: **[AGENTS.md](AGENTS.md)**

### Web demo

```bash
./.venv/bin/python tools/madv-web.py --port 8901 --bind <your-lan-ip>
# or, on a weak box, with no transcoding at all:
./.venv/bin/python tools/madv-web.py --port 8901 --bind <your-lan-ip> --mode webcodecs
```

Two modes:

| Mode | What runs | Cost |
|---|---|---|
| `mjpeg` (default) | server transcodes to MJPEG | works in any browser; the encode is the entire CPU cost |
| **`fmp4`** | server **remuxes** the camera's H.264 into fragmented MP4, browser plays it via MediaSource | near-zero CPU, works over plain http — **use this on an SBC** |
| `webcodecs` | server relays raw H.264, browser decodes | zero CPU, but **secure context only** — black screen over `http://<lan-ip>` |

The camera already emits H.264, so `webcodecs` is the right choice on an SBC.

Drag to look around, scroll/pinch to zoom, double-click to reset. There is also
a "flat panorama" tab showing the raw 2:1 equirectangular frame.

It stops streaming when nobody is watching (`--idle-timeout`, default 60 s) and
wakes on the next request — see the heat note below.

Press **Stats** (or `s`) for a transparent panel with the numbers: mode, codec,
server fps, frame count, a real byte-rate measured from what the server wrote,
resolution, readyState, buffered range, dropped frames, reconnect count — plus
sparklines for fps and bitrate. It reads exposed state rather than inventing
it, so it doubles as the debugging surface when something looks wrong and you
have no console. There is also a **Sleep
camera** button that does it immediately: its first job is to drop the page's
own stream, because the server counts any viewer traffic as a reason to wake.

**Firewall note:** if the host runs a firewall, the port has to be allowed in
before anything can reach it. On nftables, reload **with a delete first** —
`nft -f` on an existing table *appends* and silently duplicates every rule, so
packets end up both logged and allowed:

```bash
sudo nft delete table inet <table> && sudo nft -f <your-rules-file>.nft
```

Then verify from **another host**. A listener on localhost proves nothing about
the firewall or the LAN path.

### Tools

| File | Purpose |
|---|---|
| `tools/madv.py` | host driver + CLI (probe/info/live/photo/thumb/shoot/record/sleep/poweroff/raw/dump) |
| `tools/madv-web.py` | HTTP server + 360° browser demo |
| `tools/axml.py` | standalone Android binary-XML parser (used to read the APK manifest) |
| `tools/60-madv-mini.rules` | udev rule for `464a:4d02` |

`madv.py` deliberately has **no dependencies beyond pyusb** — it builds the
Bulk-Only CBW/CSW envelope by hand, because the camera declares a vendor class
and no kernel driver will bind it.

---

## Preservation

`firmware/fw11126.zip` is the camera's own firmware, extracted from
`res/raw/` inside the app. It is the insurance policy: if a unit bricks, this is
the image to restore it with. The unit in hand reports `v1.1126`, the exact
match.

`reference/shaders/` holds the app's original GLSL — the spherical projection is
documented there rather than lost.

The original APK is **not** committed (38 MB). Keep a copy; it is the only
surviving description of this device. APKPure hosts it (package com.madv.mini).

If a unit ever bricks, [docs/FIRMWARE-RECOVERY.md](docs/FIRMWARE-RECOVERY.md)
records the recovered flash protocol — read the warnings there first, it is a
last resort and has never been executed.

---

## Notes for the future

- **The camera runs hot.** Streaming keeps the ISP and the H.264 encoder
  running flat out, so a plug-in unit gets noticeably warm within minutes. It
  is not a fault. To cool it, **stop live view** (`7A 01 02`) — that stops the
  encoder and leaves the camera on the bus, ready to resume.
- **Do not `close_session` (`7A 00 02`) to save power.** It powers the camera
  down and it **leaves the USB bus**; getting it back needs a physical replug.
  `madv.py sleep` does this deliberately — use it when putting the unit away,
  not as an idle mode. The web demo only ever stops live view.
- The camera **wedges** after long experiment runs and stops answering. It has
  no battery, so a physical unplug/replug is the only reset.
- `0xFF` in a reply means "asleep" — re-send open communication. The driver
  does this automatically.
- A reply beginning `0x31 0x01 0x02 0x00` in a downloaded JPEG is a vendor tag
  the official app zeroes out before saving.
- Place the camera **upright** if you want the panorama horizon level; on its
  side the whole panorama is rolled 90°. Learned that the hard way because my workstation is upright, so the ports are sideways.

---

## License

[MIT](LICENSE) for the driver, the web demo and these docs — use it, ship it,
fork it.

That license does **not** extend to material recovered from the vendor's app,
which stays its owner's copyright and is included only for interoperability
and preservation:

- `firmware/fw11126.zip` — the camera's own firmware
- `reference/shaders/` — GLSL extracted from the APK
- `reference/usb_device_filter.xml`

The point of this project is that the hardware outlives the app. Take the code,
take the protocol, and go keep a forgotten camera alive.
