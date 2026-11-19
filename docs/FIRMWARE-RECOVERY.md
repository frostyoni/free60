# Firmware recovery — what we know, and what we do not

**Status: INCOMPLETE. Do not follow this as a procedure yet.**

This page exists because we *hold* the camera's firmware, and holding it is
worthless if nobody records how it is meant to be used. It documents what has
been recovered from the official app and what has been verified on hardware,
and it is explicit about the gap.

---

## Read this first

Reflashing is a **last resort**. Only consider it when the unit is already dead:
live view never starts *and* the original Android app also fails on the same
camera. If the app still works, the camera is fine and the problem is on the
host side — see `STATE.md`.

Nothing in this file has been executed. The flash protocol was recovered from
the app; the image was never sent to a camera by this project. A wrong flash
can leave a unit that will not enumerate, which is much harder to recover than
a camera that merely refuses to stream.

---

## What we hold

`firmware/fw11126.zip` — extracted from `res/raw/fw11126.zip` inside the last
APK. The app shipped this so it could reflash a stale or bricked camera over
USB. It contains:

| File | Size | What it is |
|---|---|---|
| `FPUPDATE.DAT` | 39,633,077 B | the flash image, `SUNP BURN FILE` container |
| `V35MA.prm` | 877 B | **board-specific DRAM parameters**, starts `DRAMPAR1` |

The unit in hand reports firmware `v1.1126`, which is exactly this image — so
the copy is authentic for this hardware, not a random archive.

`STATE.md` §5 has the container layout parsed so far (`SUNP BURN HDR 1` at
`0x94000`, `HDR 2` at `0x95000`, `DRAMPAR1` at `0x2258D48`).

---

## Verified on hardware

Only these two things:

- `7A 01 83` (enter firmware-upgrade mode) is accepted, and polling
  `7A 02 83` returns `0`. The camera will enter the update state.
- The unit reports `v1.1126`, matching the image we hold.

Everything else below is **recovered from the app's code, not yet proven**.

---

## The step that is easy to miss: the image is patched before it is sent

The app does **not** transmit `FPUPDATE.DAT` as-is.

`GeneralFunction.C0034b.m366a(FPUPDATE.DAT, FPUPDATE.tmp, V35MA.prm)` rewrites
it first:

1. parse the SUNP burn header (`lFileSize`, `lFw2Offset`, `lDramParamOffset`,
   `lChecksum` — little-endian throughout)
2. seek to `lDramParamOffset` and replace that region with the contents of
   `V35MA.prm`
3. fix up `lFileSize` and `lChecksum` (and `lFw2Offset` when a second image is
   present)
4. write the result to `FPUPDATE.tmp`, which replaces `FPUPDATE.DAT`

So the DRAM parameters carried inside the burn file are overridden by the
per-board `.prm`. Sending the raw file would flash the wrong DRAM setup.

**This patcher is not reimplemented yet.** Until it is, we do not have a
byte-identical equivalent of what the official app sends.

---

## The flash protocol, as recovered

Little-endian for every multi-byte field. `resp[0]` = status
(`0` ok, `1` busy, `2` error, `0xFF` asleep).

```
 1. 7A 01 83                 enter firmware-upgrade mode
    poll 7A 02 83            until 0

 2. 7B 04 80   (72-byte payload)
      [0:4]   data_size       = size of the patched FPUPDATE.DAT
      [4:8]   check_sum       = (sum of all file bytes) % 256      <-- NOT a CRC
      [8:72]  file_name       = "FPUPDATE.DAT", NUL-padded to 64

 3. 7A 01 84                 examine / validate the binary
    poll 7A 02 84            until 0

 4. 7A 01 81                 start updating camera firmware
    poll 7A 02 81            until 0

 5. 7B 06 80   (80 + n byte payload, n <= 64512, repeated per part)
      CDB[4:8] = n                     transfer size, u32 LE
      [0:64]   header blob             (64 bytes, reused every part)
      [64:68]  part_index              starting at 0, increments
      [68:72]  transfer_size           = n
      [72:76]  total_file_size
      [76:80]  total_part_count
      [80:]    chunk data

 6. poll 7A 02 81            until 0
 7. 7A 02 82                 check reset status after the camera reboots
```

The app shows progress as `(part_index * 80 / total_parts) + 5` percent, and
treats the run as done once `sent_bytes >= file_size`.

---

## What is still needed

1. Reimplement `C0034b` (the `.prm` patch) and prove it reproduces the app's
   `FPUPDATE.tmp` for the image we hold.
2. Confirm the 64-byte "header blob" in the `7B 06 80` payload — where the app
   gets it is not yet pinned down.
3. Actually run it — on a unit that is already dead, never on a working one.
4. Record the outcome here either way.

---

## If it never gets finished

The honest fallback: the archive is preserved, the protocol is documented, and
anyone with a dead unit and more appetite for risk can finish the job. That is
still a better outcome than the device disappearing with its app.
