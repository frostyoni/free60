# Why Python, and when that stops being the right answer

The obvious question for a USB driver: why not Rust (or C)? This is the honest
version, including the part where Python is the wrong tool.

## What this code actually has to do

The camera sends **~5 Mbit/s** at 1920x960. That is 600 KB/s. The heavy work is
never in this repository:

| Stage | Who does it |
|---|---|
| USB bulk transfers | pyusb → libusb (C) |
| H.264 decode | ffmpeg, or the browser via MediaSource / WebCodecs |
| H.264 remux for `--mode fmp4` | ffmpeg with `-c:v copy` — no transcode |
| 360 projection | the viewer's GPU |
| Nothing at all | the Python |

The Python writes CBW envelopes, reads CSW trailers, and moves buffers around.
There is no inner loop worth optimising at this scale.

## Why Python was the right call here

**1. The work was discovery, not implementation.** Every fact in `STATE.md` —
opcodes `0x7A`/`0x7B`, the 32-byte frame header, the little-endian CBW length
quirk, the `WRITE_SET_ALL_SETTING` gate that made the camera look dead — came
from sending candidate bytes and reading the reply. Thousands of hypotheses,
each needing a round trip. Python turned each one into a one-line change and a
re-run. A compile step in that loop would have cost more than it saved.

**2. The deployment target has no toolchain.** The test box is an S905X
consuming `apt install python3 python3-usb`. Rust would mean cross-compiling
per architecture (x86_64, armv7, arm64), linking libusb per target, and
shipping binaries. On a device that boots from eMMC and has no compiler, that
is a real cost, paid every time someone wants to try it.

**3. A preservation project needs to be readable by strangers.** The premise is
that the vendor is gone and nobody maintains this. A single annotated file that
a stranger can open, understand, and patch beats a binary whose source may not
even build in five years. `madv.py` is readable end to end.

**4. One dependency.** pyusb. The alternative pulls a crate graph whose
reproducibility matters more precisely because nobody is around to fix it.

**5. The protocol documentation is language-agnostic.** `STATE.md` describes the
wire format completely. That was the hard part, and it is already done — which
is exactly what makes a port cheap if anyone wants one.

## Where Python loses

Being straight about the costs:

**The capture loop is the CPU cost.** Measured on an H616: ffmpeg's copy remux
took **3.1% of one core**, while the Python capture path took **~81%** — before
the busy-spin fix. The per-command overhead (build CBW, write, N bulk reads,
parse CSW, per frame at 30 fps) is pure interpreter cost. Rust would plausibly
cut that by 5-10x.

**Distribution to normal humans is worse.** `pip install pyusb` plus a WinUSB
driver bind via Zadig is not a friendly install. A signed binary would be.

**If this ever becomes a product or a kernel driver, rewrite it.** A V4L2
module, or something multiple applications depend on, wants Rust or C for
reasons that have nothing to do with this project's current shape.

## The trigger to rewrite

Not "Rust is faster". The trigger is one of:

- the capture loop's CPU prevents a second concurrent viewer on a cheap box
- the project ships to people who cannot run `pip`
- it stops being a viewer and becomes something other software links against

Until one of those is true, Python buys iteration speed and auditability, and
pays for it in CPU that the hardware so far has to spare.
