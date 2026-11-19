# MADV Mini 360 — resurrection project

**Goal**: make the Madv Mini 360 camera work again, forever, without the dead
Android app, the dead cloud, or Google Play.

Status: **protocol reverse-engineered AND verified against real hardware. USB layer
100% working in both directions. Blocked on the image pipeline — the sensor never
produces data (see §10).**

---

## 1. What the device actually is

The marketing said "360° panorama camera, Xiaomi ecosystem". The truth from
the APK is more useful:

| Fact | Value | Source |
|---|---|---|
| SoC | **Sunplus / iCatch SPCA6350 family** | firmware strings `SPCA6350ALCv`, `spca6330 unsupported`, `customization/drivers/if/if_v35.c` |
| Board/ISP variant | **V35MA** (`V35MA.prm`) | `res/raw/fw11126.zip` |
| Firmware | **RTOS / bare-metal — no Linux anywhere** | 23k strings, zero `BusyBox`/`linux version`/`squashfs` hits |
| Video out | 1920×960 (also 1280×640, 3840×1920) | `C0105a.m1130a()` |
| Optics | **two fisheyes, side by side in one frame** (960×960 each) | `res/raw/video_stitch_fragment_shader_single.glsl` samples `(0.25,0.50)` and `(0.75,0.50)` |
| Host link | **USB-C tether only. No Wi-Fi, no Bluetooth.** | app connects as USB *host*; reviewer: "no WiFi network access needed" |
| Transport | **USB Mass Storage, Bulk-Only (BBB) with vendor SCSI opcodes** | `p031a/p034b/p035a/C0111b.java` |
| Live view | **H.264 Annex-B** elementary stream | `MediaCodec.createDecoderByType(MimeTypes.VIDEO_H264)` |
| Stitching | **done on the phone GPU**, not in the camera | `res/raw/*stitch*.glsl` |
| App | `com.madv.mini` v1.02.34.0, targetSdk 28 | `AndroidManifest.xml` |

So the "heavy Android dependency" is not exotic. The camera is a **USB SCSI
gadget**; Android just happens to be able to host it. Linux can host it too —
with `sg_raw`, `SG_IO`, or libusb. Nothing is lost.

> The iOS-port naming (`ui_Controller.ui_Liveview`, `libViewController.so`,
> `[lRecordStopCase]`) confirms this firmware/app pair was shipped by a small
> team building a phone app and a bare-metal camera — not a platform.

---

## 2. USB identity

Two pairs are accepted by the app (`C0111b.m1208i()`):

| VID | PID | Where |
|---|---|---|
| `0x464A` (17994) | `0x4D02` (19714) | `res/xml/usb_device_filter.xml` — the Android USB attach filter |
| `0x12D1` (4817) | `0x109B` (4251) | hard-coded fallback (`f896c`/`f895b`) |

**Which one the hardware actually reports is unverified** — confirm with
`tools/madv.py probe` once a unit is attached. Both are in the tool's accept list.

Interface requirements (`m1207h()`):
- one interface with **two bulk endpoints**, one IN one OUT
- the app sets 16384-byte transfer chunks
- Bulk-Only Mass Storage Reset: `controlTransfer(0x21, 0xFF, 0x0000, ifaceId)`

---

## 3. Transport: BBB + vendor SCSI

Standard Bulk-Only Transport, with two quirks worth remembering.

**CBW — 31 bytes**

| Offset | Field | Note |
|---|---|---|
| 0–3 | signature | `USBC` (`0x55534243`) |
| 4–7 | tag | random, big-endian |
| 8–11 | transfer length | **little-endian** (app does `Integer.reverseBytes()` then a BE `putInt`) — non-standard, replicate it |
| 12 | flags | `0x80` = data-in, `0x00` = data-out |
| 13 | LUN | 0 |
| 14 | CB length | 16 |
| 15–30 | **CDB** | 16-byte vendor command |

**CSW — 13 bytes**, signature `USBS` (`0x55534253`), tag echoed back.
The device may coalesce the CSW onto the end of the data payload; the app
detects it by checking whether the last 13 bytes of a read match
`USBS + tag`.

With the kernel's `usb-storage` driver bound, `/dev/sgN` appears and
**`SG_IO` handles all of the above for you** — which is why `tools/madv.py`
has no third-party dependencies.

---

## 4. Command set (16-byte CDB)

`CDB[0]` = opcode: **`0x7A`** = act/read, **`0x7B`** = write.
`CDB[1]` = category, `CDB[2]` = subcommand, `CDB[4]` = payload length
(write ops), `CDB[8..]` = parameters, `CDB[12..15]` = little-endian u32
where a 32-bit parameter is needed.

Response convention: `resp[0]` = status —
**`0` OK · `1` busy (retry) · `2` error · `0xFF` camera asleep, re-open session**.

### Session — category 0x00
| CDB | Meaning |
|---|---|
| `7A 00 01` `[8]=1` | **open communication** (reset to default effect) — required first |
| `7A 00 02` `[8]=1 [9]=0 [12..15]=300` | close communication |
| `7A 00 02` `[8]=1 [9]=1 [12..15]=256` | "app is in gallery" notify, every 150 s |

### Camera — category 0x01
| CDB | Meaning |
|---|---|
| `7A 01 01` `[9]=res` | start live view. res 9=1920×960, 10=1280×640, 11=3840×1920 |
| `7A 01 02` | stop live view |
| `7A 01 03` `[9]=res [10]=fps` | start recording |
| `7A 01 04` | stop recording |
| `7A 01 05` `[8]=orientation` | take picture (0=upright, 1/2/3 = 90/180/270) |
| `7A 01 F0` | power off |
| `7A 01 83` | enter firmware-upgrade mode |
| `7A 01 84` | examine firmware bin |
| `7A 01 81` | update camera firmware |
| `7A 01 85` | clear picture buffer |

### Poll — category 0x02 (check the async result)
`7A 02 01` live view · `02` stop live view · `03` start rec · `04` stop rec ·
`05` capture · `81` update fw · `82` reset · `83` enter fw mode · `84` exam fw

### Info — category 0x03
| CDB | Meaning | Reply layout |
|---|---|---|
| `7A 03 01` `[8..14]=app_version` | camera info | `[65:97]` hw_version, `[97:129]` fw_version |
| `7A 03 02` `[8..14]="v2.0000"` | SCSI protocol version | text at `[8:]` |
| `7A 03 30` | camera status | `[8]` execution, `[9]` thermal |
| `7A 03 34` | thermal status | |
| `7A 03 35` | remaining shot count | u32 at `[8]` |
| `7A 03 FF` | **keep-alive — every 500 ms, 10 misses = camera drops me** | |

### Settings — category 0x04 (get = `7A`, set = `7B` + payload)
| sub | Setting | Range |
|---|---|---|
| `01` | date/time | set payload 24 B: `[2:4]`=year LE, `[4]`mon `[5]`day `[6]`hr `[7]`min `[8]`sec `[10:12]`ms LE |
| `03` | photo resolution | 3, 4 |
| `04` | video resolution | 9, 10, 11 |
| `07` | EV bias | 0..12 |
| `08` | white balance | 0..4 |
| `09` | filter | 0..8 |
| `10` | shutter time | |
| `14` | logo type | 0..5 |
| `60` | **all settings** | set = 72-byte payload |
| `80` | firmware file info | 72 B: `[0:4]`=size, `[4:8]`=checksum, `[8:72]`=filename |

### Data — category 0x05
| CDB | Meaning | Reply layout |
|---|---|---|
| `7A 05 01` | live-view frame (H.264) | `[0]`status `[1]`1=final `[4]`res `[20]`thermal, data at `[20:]`; envelope ≤ 64 KiB |
| `7A 05 02` `[8..11]`=offset | **download stored photo**, paged | `[1]`transferType (1=done) `[8]`thermal `[16:20]`len LE `[20:]`data |
| `7A 05 82` | "picture done" ack — send after a completed download | |
| `7A 05 03` | thumbnail | 148480-byte envelope, `[16:20]`=len |

### Firmware upload — category 0x06
`7B 06 80` with a payload whose `[64]`,`[68]`,`[72]`,`[76]` are u32 LE
(chunk offsets/length) and data from `[80]`.

### Self-test — category 0xF0
`7A F0 10` read-throughput test, `7B F0 10` write-throughput test.

---

## 5. Firmware — I own it

`res/raw/fw11126.zip` inside the APK is the **camera's own firmware**, shipped
so the app could flash a bricked or old unit over USB. This is the insurance
policy for resurrection.

Unzips to:

- **`V35MA.prm`** (877 B) — DRAM parameters, starts `DRAMPAR1`
- **`FPUPDATE.DAT`** (39,633,077 B) — flash image

`FPUPDATE.DAT` structure (verified, **not encrypted** — low entropy, plain
zero padding):

| Offset | Content |
|---|---|
| `0x00000` | `SUNP BURN FILE` header. `u32@0x10` = total size, `u32@0x14` = `0x94000`, `u32@0x18` = `0x95000`, `u32@0x28` = `0x2258D48`, `u32@0x2C` = `0x22590B5` |
| `0x94000` | `SUNP BURN HDR 1` |
| `0x95000` | `SUNP BURN HDR 2` (followed by a `0x26 0x2F 0x3E 0x3C …` byte table, `0x7A`-padded — likely a de-scramble/offset table) |
| `0x2258D48` | `DRAMPAR1` — the same DRAM parameters as `V35MA.prm` |
| `0x22590B5` | trailer |

Not yet parsed: the region table that maps each image to a flash offset.
That is the next firmware task, and it only matters if I ever reflash.

---

## 6. Projection — the stream is ALREADY equirectangular

An earlier draft of this file called the live view "dual fisheye, stitched by
the app". **That was wrong**, and it is worth recording why: I built a
fisheye un-stitcher against that belief and produced garbage, then had to
throw it away.

The measurements that settle it, taken on a real 1920x960 frame:

| test | result | meaning |
|---|---|---|
| column 0 vs column 1919 | mean abs diff **3.5** | the image wraps horizontally — same direction at both edges |
| column 0 vs column 959 | mean abs diff **97** | the two halves are *not* the same scene |
| row 0 across its width | stdev **3.9** | a near-uniform pole, as an equirect zenith must be |

Then confirmed visually: rendering a rectilinear view with
`u = atan2(x,z)/2pi + 0.5`, `v = 0.5 - asin(y)/pi` produces an ordinary
photograph, not a warped mess. And 1920x960 is 2:1, the classic equirect aspect.

So **the camera stitches in-camera**. There is nothing to un-stitch, and the
app's GLSL is not the live-view path.

### Then what are the bundled shaders for?

They are still in `res/raw/` and they are not junk:

- `video_stitch_fragment_shader_single.glsl` — a **dual-fisheye to equirect**
  converter: two eyes at UV `(0.25,0.50)` and `(0.75,0.50)`, fisheye radius
  scaled `0.5` in u and `1.0` in v, seam blended with `smoothstep` between
  `cfLOWER=0.45` and `cfUPPER=0.55`, through a 3x3 `t_matrix`.
- `video_stitch_fragment_shader_twins.glsl` — a single-eye variant.
- `player_360_*_shader.glsl` — a trivial passthrough (`texture2D(sTexture, v)`),
  i.e. the equirect is sampled directly for viewing. More evidence for the
  conclusion above.

Most likely division of labour: the **camera** stitches live view, and the
**app** stitches saved stills/video — consistent with the photo path carrying a
vendor tag the app rewrites before saving.

The uniforms (`cfLensRatio`, `cfRatio`, `cfNormalizeX/Y`, `cfOffset0X/Y`,
`cfOffset1X/Y`, `cfRotate0/1`, `t_matrix`) have no assignments anywhere in the
Java layer; those values live in the native `libViewController.so` /
`libblender.so`. They only matter if the full-resolution still turns out to be
dual fisheye — which is still unknown, because that download never completes.

### Two projection bugs I shipped, recorded so they are not repeated

Both were in the web viewer, both are classic:

1. **Unnormalised view ray.** `atan2` is scale-invariant so longitude was fine,
   but `asin(d.y)` is not — off-axis the ray exceeds 1.0, the clamp bites, and
   the panorama stretches vertically away from the centre. This was a large
   part of the "heavily distorted" report.
2. **Wrong rotation order.** Yaw-then-pitch-about-world-X (`Rx*Ry`) instead of
   pitch-about-local-X-then-yaw (`Ry*Rx`). Measured with drag-down:

   | yaw | 0 | 90 | 180 | 270 |
   |---|---|---|---|---|
   | `Rx*Ry` | ok | y=0 | +0.48 | y=0 |
   | `Ry*Rx` | ok | ok | ok | ok |

   At 90/270 vertical motion vanished entirely and the view just circled; at
   180 it inverted, so dragging down moved the scene up. Any look-around camera
   needs `Ry*Rx`.

---

## 7. What is dead vs. what I do not need

Dead: `api.madv360.com:9999` (app update/EULA), Google Play listing, WeChat /
Weibo / Facebook share SDKs, Firebase.

Not needed for the camera to work: any of the above. The camera is a dumb USB
SCSI peripheral. **Nothing about the device was cloud-dependent.** The app was
simply the only host software.

---

## 8. Where this stands

Done and verified against hardware: enumeration and BBB transport, session
handling with automatic recovery, every settings read/write, date/time, live
view, thumbnail, still-capture trigger, the browser 360 viewer, idle/sleep
handling, and the zero-transcode relay.

Left, in rough order of value:

1. **Full-resolution stills.** `7A 05 02` answers busy, and the capture path has
   wedged the unit twice — it stops answering while still enumerating. Neither
   behaviour is understood. If the still turns out to be dual fisheye, the
   shader described in §6 is the tool for it; if it is already equirect, it is
   trivial. That question cannot be answered until a download actually works.
2. **Recording.** `7A 01 03` is accepted but nothing has been recorded
   end-to-end, and the unit's own storage is unproven: `7A 03 35` returns no
   count at all, which may mean there is none.
3. **Firmware recovery.** Image and protocol recovered; the `.prm` patch step
   is not. `docs/FIRMWARE-RECOVERY.md` is explicit that nothing has been
   flashed.
4. **Packaging.** The web demo is the interface today. A V4L2-style device node
   would let existing software use the camera, but the transport is vendor-class
   bulk SCSI, so that means a userspace bridge, not a kernel driver.

None of this is blocked on hardware I do not have — only on a working unit, a
replug away.

---

## 9. Artefacts

```
assets/Madv+Mini_1.02.34.0_APKPure.apk.zip   original APK, the only surviving doc
assets/work/                                 extracted APK (dex, libs, res/raw)
src/                                         jadx decompilation, --deobf (86 MB, regenerable)
tools/madv.py                                host driver / protocol tool
STATE.md                                     this file
```

Toolchain installed for this project, no root needed:
`/mnt/hdd/tools/jadx-dist/` — jadx + portable Temurin JRE 21.
Reproduce: `JAVA_HOME=/mnt/hdd/tools/jadx-dist/jdk-21.0.13.1+2-jre \
/mnt/hdd/tools/jadx-dist/jadx/bin/jadx --deobf -d src assets/Madv+*.zip`

Useful files to re-read first:
- `src/sources/p031a/p034b/p035a/C0111b.java` — USB/BBB transport
- `src/sources/p031a/p032a/p033a/C0105a.java` — every command (1820 lines)
- `assets/work/res/raw/video_stitch_fragment_shader_single.glsl` — the stitch
- `assets/work/res/xml/usb_device_filter.xml` — USB IDs

---

## 10. Verified on hardware (2026-11-19)

Unit attached: **`464a:4d02`** , manufacturer `ABILITY`, product `HA CAM`,
serial `123456789ABC`. Full speed 480 Mb/s.

- interface `0x00`: **class `0xff` / subclass `0x06` / protocol `0x50`**
  (vendor-specific *class*, Mass-Storage *BBB* subclass/protocol — deliberate,
  to stop the OS grabbing it)
- endpoints: bulk **OUT `0x03`**, bulk **IN `0x82`**, 512-byte packets
- **`usb-storage` does NOT bind it.** Use libusb and build CBW/CSW by hand.
- udev rule for unprivileged access: `tools/60-madv-mini.rules`
  (`SUBSYSTEM=="usb", ATTR{idVendor}=="464a", ATTR{idProduct}=="4d02",
  MODE="0660", GROUP="plugdev", TAG+="uaccess"`)

**Firmware on the unit: `v1.1126`** — exactly the version bundled in the APK
(`res/raw/fw11126.zip` = fw 1.1126). The unit is up to date; the firmware I
hold is authentic for this hardware.

### Reply layouts, corrected by experiment

`7A 03 01` camera info — **32-byte fields starting at offset 1**:

| offset | field | this unit |
|---|---|---|
| 1 | product code | `MVB010Z` |
| 33 | serial | `A0HBMA000190` |
| 65 | hw version | `Unknow` (yes, literally) / `C0` early in boot |
| 97 | fw version | `v1.1126` |
| 129 | product code | `MVB010Z` |
| 161 | manufacturer | `Ability` |
| 193 | slogan | `MakeMoney` |

(The Java offsets `65` and `97` were right all along.)

`7A 03 02` protocol version → `v2.0E00X`
`7A 03 30` / `7A 03 34` → **8-byte** replies, `[0]`=1, `[1]`=1, `[6:8]`=`"00"`
`7A 03 35` remaining → **1 byte only** (`00`), no count — likely no storage

**Single-setting get (`7A 04 xx`) replies carry the VALUE at `[0]` with no
status byte.** `0xFF` means "asleep, reopen".

`7A 04 60` all settings, 48 bytes:
`[0]`=0 status, `[2]`=photo_res, `[3]`=video_res, `[10:12]`=year LE,
`[12]`mon `[13]`day `[14]`hr `[15]`min `[16]`sec `[18:20]`ms LE.
This unit: photo_res=**3**, video_res=**9**, logo_type=**2**.

`7A 02 05` capture poll (72 bytes):
`[0]`=0 done / 1 busy / 3 error, `[1]`=?, `[2]`=0 when exposure ready,
`[3]`=**stored_pic_num**, `[8:]`=null-terminated filename.

### Session behaviour

- `7A 00 01` returns **0** on a fresh open, then **1** ("already open") on
  subsequent calls. Both mean the session is usable.
- `7A 00 02` with `[9]=0` is **close** — the camera drops to power save and
  every command then answers `0xFF`. With `[9]=1` it is the "app in gallery"
  notify. Do not confuse them.
- Bulk-Only reset (`ctrl 0x21/0xFF`) works and clears stale state.

### Write path: PROVEN

Set the clock (`7B 04 01`, 24-byte payload) from `2015-01-01 00:01:15` to
`2026-11-19 13:22:05`, read it back, exact match. So the OUT data phase,
payload assembly and big/little-endian quirks are all correct.

### *** SOLVED 2026-11-19: the camera WILL stream on Linux ***

**The missing step was `WRITE_SET_ALL_SETTING` (`7B 04 60`).** The camera
happily accepts `7A 01 01` (start live view) and then answers "busy" forever —
because it has never been told its configuration. The app always sends the
full 48-byte settings block first (`C2171b case 8732` →
`MSG_REMOTE_USB_CMD_WRITE_SET_ALL_SETTING`, which the log even names).

The real startup sequence is:

```
7A 03 01   get camera info (app_version string)      <- identity handshake
7A 01 85   clear picture buffer
7B 04 60   WRITE_SET_ALL_SETTING  (48 bytes)         <- THE GATE
7A 01 01   start live view   [8]=0 [9]=9|10 [10]=0
7A 02 01   poll: 0 = streaming ready
7A 05 01   get frame ...
```

`7B 04 60` payload (read-modify-write of what `7A 04 60` returns):

| off | field | app value |
|---|---|---|
| 2 | photo_resolution | 3 (or 4) |
| 3 | video_resolution | 9 (or 10) |
| 6 | evb | 0 |
| 7 | white_balance | 0 |
| 10:12 | year, LE | current |
| 12/13/14/15/16 | mon/day/hour/min/sec | current |
| 18:20 | ms, LE | 0 |
| 32 | filter | 0 |
| **35** | **bitrate** | **8** (100 in UI mode 448) |
| 39 | logo_type | 0..5 |

This does not look like a deliberate lock — it reads like firmware that keeps
its encoder unconfigured until a settings blob arrives. Either way: **write the
settings and it streams.**

### Live view: WORKING

- **H.264 High profile, 1920 x 960, ~30 fps**, decodes cleanly with ffmpeg.
- Measured: 129 access units / 2.53 MB in 4 s; 96-98 AUs per 3 s.
- Frame envelope (verified): **32-byte header**
  - `[0]` status (0 = ok)
  - `[1]` last-chunk flag
  - `[4]` resolution id (9/10/11)
  - `[28:32]` **payload length, LITTLE-endian**
  - `[32:]` H.264 Annex-B access unit (start code `00 00 00 01` at 32)
- 1920 x 960 is an **equirectangular panorama, stitched in-camera** — see §6.

### Video characteristics — measured, not guessed

| quantity | value |
|---|---|
| resolution | 1920x960, native (frame header `[4]` = 9) |
| frame rate | ~30 fps (96-98 access units per 3 s) |
| source bitrate | **~5.06 Mbit/s** (129 AUs / 2.53 MB in 4 s) |
| access unit size | ~19.6 KB average |
| keyframe cadence | IDR every ~16 AUs = **~0.53 s**, SPS sent with each |
| bitrate range in practice | **~4.8 Mbit/s** on a static scene, **~7.7 Mbit/s** with movement |

**1920x960 is the live-view maximum, confirmed by trying all three ids.** The
protocol enum advertises `11 = 3840x1920`, and the app maps UI index 1 to it —
but the camera accepts id 11 and then delivers **1280x640**, i.e. it silently
falls back rather than refusing. Measured:

| res id | decoded | bitrate |
|---|---|---|
| 9 | 1920x960 | 4.79 Mbit/s |
| 10 | 1280x640 | 3.68 Mbit/s |
| 11 | **1280x640** (not 3840x1920) | 3.93 Mbit/s |

So the 4K id in the enum is either for the still/record paths or not
implemented on fw v1.1126. Nobody should spend time chasing it expecting 4K.

### Where image artifacts come from

In `--mode fmp4` the stream is **`-c:v copy`** — not re-encoded at all, so the
viewer receives the camera's own bytes. Any artifact is therefore upstream of
this project:

| source | what it looks like |
|---|---|
| the camera's H.264 encoder, ~160 kbit/frame | blocking and smearing in motion and in low light |
| low-light sensor noise plus its denoiser | waxy, detail-less shadows |
| the in-camera stitch | the two bright arcs at the seam longitudes |
| 210-degree fisheye stretched to equirect | softness towards the poles |

`--mode mjpeg` is the one mode that **adds** a generation: ~5 Mbit/s of source
becomes ~17 Mbit/s of JPEG, lossy again. If a comparison looks worse in mjpeg
than in fmp4, that is the re-encode, not the camera.

`bitrate` in the settings block (`7B 04 60`, byte `[35]`) is **not** a usable
quality knob. Set it and streamed 3 s each time:

| set to | 8 | 20 | 50 | 100 | 200 |
|---|---|---|---|---|---|
| measured | 4.89 | 5.45 | 5.30 | **1.58** | 5.24 Mbit/s |

Non-monotonic — 100 collapses to a third of the rest. There is no reliable way
to raise the camera's own quality, so the app's value of 8 is as good as any,
and a quality slider would be a lie.

Cost of the two host-side paths on the same stream:

| path | bandwidth | server CPU |
|---|---|---|
| `--mode mjpeg` | **~17.4 Mbit/s** | ffmpeg decode + MJPEG encode |
| `--mode webcodecs` | **~5.06 Mbit/s** | copy bytes only |

MJPEG costs 3.5x the bandwidth *and* adds a second lossy generation. That is the
entire argument for `--mode webcodecs`.

### Thumbnail: WORKING

`7A 05 03` → envelope with `[16:20]` = payload length LE, `[20:]` = JPEG.
On this unit: **272 x 272**, ~16.3 KB. Valid JPEG.

### Still capture: partly working

- `7A 01 05 [8]=orientation` → `00` accepted
- `7A 02 05` poll: goes `[0]` 1 → 0, `[2]` 1 → 0 (exposure ready),
  `[3]` = `stored_pic_num` (9), `[8:]` = filename (empty here). Capture
  completes in ~1 s once the settings block has been written.
- **OPEN: the full-resolution download `7A 05 02` still answers status 1
  (busy) with chunk length 0**, even after: capture completed, thumbnail
  fetched first (the app's order, `8756` then `8760`), 5-30 s of retries,
  offsets 0 and 1, both photo resolutions, stream running and stopped, and a
  `7A 05 82` done-ACK. Not yet explained — next session's first task.
- For now the useful stills are: an H.264 frame from live view, or the 272x272
  thumbnail.

### App-side JPEG patch (worth knowing)

After downloading a still the app searches the JPEG for the marker
`31 01 02 00`, reads two little-endian u32s after it, and **zeroes** a region
(`C0044c` case 18465). It then writes the file as `IMG_yyyyMMdd_HHmmss.tmp`.
Reproduce that if a downloaded JPEG turns out to carry a vendor tag.

### Stability note

The camera wedges after a long run of experiments and bulk-only resets — it
stops answering entirely. It has no battery, so **a physical unplug/replug is
the only reset**. Do that before assuming a regression.

### Tool status

`tools/madv.py` now works for:
`probe`, `info`, `status`, `settings`, `dump`, `live` (**verified, 30 fps**),
`thumb` (**verified, JPEG**), `shoot` (**verified, capture completes**),
`record`, `raw`, `set_datetime`, `prepare`, `start_stream`.

Still open: `photo` (full-res still download).

Run it with the project venv: `./.venv/bin/python tools/madv.py live -n 5 -o out.h264`

### Heat, and the correct way to stop

The camera has no idle mode of its own. While live view is open the ISP and the
H.264 encoder run continuously and the unit gets properly warm — that is
expected, not a fault, and it is the single best argument for releasing the
camera deliberately.

Two levels, both confirmed to be accepted (`00`):

| Action | Effect |
|---|---|
| `7A 01 02` stop live view | encoder stops, camera stays on the USB bus — **use this to cool down** |
| `7A 00 02` close communication | session ends **and the camera powers down and leaves the USB bus** — needs a replug |
| `7A 01 F0` power off | camera shuts down |

That middle row cost real confusion. `7A 00 02` was assumed to be a light
"power save"; observed log:

```
[capture] no viewer for 60s - camera to power save
[capture] waking
[capture] USBError: [Errno 19] No such device (it may have been disconnected)
```

The device was gone from `lsusb` entirely. So an idle timeout that closed the
session effectively unplugged the camera. The web demo now only stops live
view when idle.

`tools/madv.py sleep` does stop-live-view + close-session, and `tools/madv-web.py`
does it automatically after `--idle-timeout` seconds with no viewer. Do not
leave a unit streaming with nobody watching.

### Low-CPU streaming: `--mode fmp4`, and why `--mode webcodecs` failed

The camera already emits H.264, so re-encoding it to MJPEG is wasted work — on
an SBC it is the whole CPU cost. MJPEG measured **~2.7 cores at 24 fps on an
H616**, against a source that needs only remuxing.

**`--mode fmp4` is the answer** (added after the first attempt at this failed):

- ffmpeg is told `-c:v copy` — no decode, no encode, just a remux
- its fragmented-MP4 output is split into the init segment (`ftyp`+`moov`,
  778 bytes) and whole `moof`+`mdat` fragments, ~200 ms each
- `GET /stream.mp4` sends `[u32 LE length][segment]` records over chunked HTTP,
  starting with the init segment
- the page feeds them to `MediaSource` and samples the `<video>` straight into
  the 360 shader. A late joiner's first fragment carries a large timestamp, so
  the client jumps `currentTime` to `buffered.start(0)` on the first append

MediaSource has **no secure-context requirement**, so this works over plain
`http://<lan-ip>` — which is the only thing phones and tablets will be using.

#### The failure that taught me this

`--mode webcodecs` shipped first: relay the access units, decode in the browser
with `VideoDecoder` (`avc1.640028`, `avc:{format:'annexb'}`). The relay was
verified (129 AUs / 2.53 MB in 4 s) and I called it done.

It was not done. **`VideoDecoder` is exposed only in a secure context.** Over
`http://<lan-ip>:8901` the constructor does not exist, the client hit a
`console.warn` and returned, and the page rendered **black with no visible
error** — on a tablet and a phone, which is exactly where it would be used.
`localhost` counts as secure; a LAN address does not.

That is the lesson worth keeping: "the server sends the right bytes" and "the
browser paints them" are two different claims, and I only verified the first.
The mode is kept because it is genuinely zero-cost where it works (localhost,
or behind HTTPS), but it is no longer the recommended path.

The client now shows an explicit on-screen message when the API is missing
instead of failing silently.

### Teardown must never close the session (fixed 2026-11-19)

The service sent `close_session` (`7A 00 02`) in its own teardown path — the
exact command this document warns powers the camera off the bus.

The chain: on `systemctl restart`, systemd kills the process group, so the
ffmpeg child dies first; the capture thread's `stdin.write` hits a broken pipe,
breaks out of `_session()`, and the `finally` called `close_session()`. Kernel
log agreed: `usb 3-1: USB disconnect` at the second of the restart.

So **every restart in a mode with an ffmpeg child (`mjpeg`, `fmp4`) stranded
the camera until someone physically replugged it.** A restart requiring desk
access is not a restart.

Fixes:
- the teardown calls `stop_liveview()` only
- `SIGTERM` gets a handler, so shutdown is deliberate and the capture loop gets
  a chance to stop the encoder instead of the process being yanked mid-command
- `main()` joins the capture thread for up to 5 s

Verified: SIGTERM exits in ~1 s, cleanly.

### Still capture wedges the unit (open)

On the current unit, `7A 01 05` + poll completed in ~1 s on the first attempt
(direct port). On two later attempts the same sequence wedged the camera: it
stays enumerated (`464a:4d02` still visible in `lsusb`) but every command times
out, and only a physical replug recovers it.

**The USB extension lead has been cleared.** It was the obvious suspect, but
the unit now runs behind that same extension at a steady 30 fps, and sleep/wake
works through it. So under-voltage during the capture spike is no longer a
convincing explanation, and the remaining candidate is a genuine firmware
wedge in the capture path.

Next attempt should be instrumented rather than hopeful: log the exact command
that stops answering, and try the capture with live view *not* running, since
every successful capture so far happened with a stream open and every failure
has too — that variable is still uncontrolled.

### Sleep/wake (verified 2026-11-19)

Confirmed against hardware through the web server's idle path:

```
[capture] no viewer for 1s - camera to power save
[capture] waking
```

and the next `/frame.jpg` returned a valid 64 KB frame immediately after.
`madv.py sleep` uses the same two commands (`7A 01 02` then `7A 00 02`).

### CPU cost, and why the capture loop was the bill (measured 2026-11-19)

On the H616 box (S905X-class), with one viewer attached and `--mode fmp4`:

| what | share of one core |
|---|---|
| python capture loop, before any tuning | **~81%** |
| ... after stopping the busy-spin | ~50% |
| ... after throttling the poll to the frame rate | ~28% |
| ... after reusing the USB read buffer, and waiting between frames | **~17%** |
| ffmpeg remux (`-c:v copy`) | **0.9%** |

Profiling the loop directly was the only way to find it. For a **32 fps**
stream it was issuing **368 `get_frame()` calls per second**, ~90% of them
answering `busy`, at **2.6 ms each** — 99% of wall time inside the calls. The
poll rate, not the data, was the cost.

Four fixes, in order of effect:

1. a 2 ms pause when a poll came back empty (stopped a pure busy-spin)
2. pause half an observed frame interval (EMA) when empty, clamped 4-20 ms —
   at 30 fps that is ~15 ms between polls instead of 2 ms
3. reuse one preallocated buffer for every bulk read. pyusb allocates a fresh
   array per `read()` when given a size; passing an array makes it return a
   byte count instead. This was worth more than the poll rate: profiling with
   `process_time` showed **2.24 ms of CPU per call**, i.e. the cost was ours,
   not USB latency
4. wait ~60% of the frame interval after a good frame, so the next poll lands
   on a frame instead of bouncing off `busy` first

Raising the bulk read from 16 KiB to 64 KiB made **no measurable difference**
(50% -> 52%, i.e. noise), so transfer count was never the problem. Worth
recording so nobody optimises it again.

For a box that does other work, the unit also carries `Nice=10` and
`CPUQuota=80%` so the camera can never starve its neighbours.
