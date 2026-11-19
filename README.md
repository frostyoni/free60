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
- ✅ still capture trigger (completes in ~1 s)
- ✅ browser demo with a drag-to-look 360° viewer

Open:

- ⬜ full-resolution still download (`7A 05 02` still answers "busy")
- ⬜ video recording (`7A 01 03`) — command accepted, untested end-to-end
- ⬜ firmware reflash path — we hold the exact image, but the burn format is
  only partly mapped

---

## Quick start

Requires Linux, `ffmpeg`, and Python 3. The camera needs unprivileged USB
access via the included udev rule.

```bash
# 1. let your user talk to the camera
sudo install -m644 tools/60-madv-mini.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb

# 2. set up the venv (pyusb)
python3 -m venv .venv && ./.venv/bin/pip install pyusb

# 3. is it there?
./.venv/bin/python tools/madv.py probe
./.venv/bin/python tools/madv.py info

# 4. record 5 seconds of live view
./.venv/bin/python tools/madv.py live -n 5 -o live.h264
ffplay live.h264
```

### Web demo

```bash
./.venv/bin/python tools/madv-web.py --port 8901 --bind <your-lan-ip>
# then open http://<your-lan-ip>:8901/
```

Drag to look around, scroll/pinch to zoom, double-click to reset. There is also
a "flat panorama" tab showing the raw 2:1 equirectangular frame.

**Firewall note:** this fleet runs `nftables` (`inet <table>`). The port must be
added to the input allow-list in `/etc/nftables.d/<table>.nft`, and the table
reloaded **with a delete first** — `nft -f` on an existing table *appends* and
silently duplicates every rule:

```bash
sudo nft delete table inet <table> && sudo nft -f /etc/nftables.d/<table>.nft
```

### Tools

| File | Purpose |
|---|---|
| `tools/madv.py` | host driver + CLI (probe/info/live/photo/thumb/shoot/record/raw/dump) |
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
surviving description of this device.

---

## Notes for whoever picks this up

- The camera **wedges** after long experiment runs and stops answering. It has
  no battery, so a physical unplug/replug is the only reset.
- `0xFF` in a reply means "asleep" — re-send open communication. The driver
  does this automatically.
- A reply beginning `0x31 0x01 0x02 0x00` in a downloaded JPEG is a vendor tag
  the official app zeroes out before saving.
- Place the camera **upright** if you want the panorama horizon level; on its
  side the whole panorama is rolled 90°.

---

License: TBD.
