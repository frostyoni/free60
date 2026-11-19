# A camera nobody can use, and how it came back

In 2018 a company called MADV shipped a tiny USB-C 360 camera. You plugged it
into your phone, an Android app appeared, and it worked. It was reviewed kindly
and then it was gone.

Today the app is not on Play. The cloud endpoint it talked to,
`api.madv360.com:9999`, does not answer. There is no Linux driver, no macOS
support, no community, no wiki. If you own one it is a small piece of plastic
with a lens in it.

Except it isn't. It is a USB peripheral, and USB peripherals do not stop
working because a company stopped caring. What was lost was not the hardware's
capability — it was **the only software that knew how to ask**.

This is the story of getting that back.

---

## The only surviving document was the app

The last APK was the whole archive: no spec, no SDK, no vendor contact. So the
APK had to be read like a source of truth, because it was the only one.

Decompiling it was the easy part. The useful discoveries were less obvious:

- The camera is **not** a Wi-Fi camera. It is USB-C tethered. No Wi-Fi radio, no
  Bluetooth, not even a battery.
- It declares a **vendor-specific USB class** — `ff/06/50` — which is Mass
  Storage Bulk-Only Transport wearing a disguise, specifically to stop the
  operating system from grabbing it.
- On top of that transport it speaks a **vendor SCSI command set**: opcode
  `0x7A` acts, `0x7B` writes.
- The live view is **already a stitched equirectangular panorama** at
  1920x960. The camera does the stitching; the app never did.

And inside `res/raw/` of the APK sat `fw11126.zip` — the camera's own firmware,
shipped so the app could reflash a bricked unit. The single most valuable file
in the whole archive, quietly included for a support case that never came.

## The camera that answered every question and did nothing

With the protocol mapped, the driver worked almost immediately. Session open.
Settings read and written. The clock — stuck in January 2015 — corrected over
USB and read back. Every command acknowledged.

And no picture ever appeared.

`start live view` returned success. Polling its status returned `busy`. Forever.
Every resolution, every retry, every session variant. The camera was polite and
completely inert.

The cause was one message the app always sent and I never did:

```
7B 04 60   WRITE_SET_ALL_SETTING   (48 bytes, bitrate = 8)
```

The camera will accept "start live view" and then sit there, because it has
never been told its encoder configuration. Nothing in the protocol says this.
It is not a permission gate or a lock — it reads like firmware that simply
waits for a settings blob before doing anything. Send it, and the first frame
arrives in under a second, 1920x960, thirty times a second.

**The whole device hinged on one message that nothing documented.**

---

## Three times I was confidently wrong

The corrections matter more than the successes, because they are what the next
person will trip over.

**1. "It's dual fisheye, so I need to un-stitch it."** The app ships GLSL that
converts dual fisheye to equirectangular, so I built an un-stitcher and got
garbage. The stream wraps horizontally — column 0 and column 1919 differ by
3.5 out of 255, while column 0 and column 959 differ by 97. It is already a
panorama. The shader is for the saved-photo path. Measure before building.

**2. "WebCodecs works, I verified it."** I verified the *server* relayed
correct bytes. `VideoDecoder` is only exposed in a **secure context**, so over
`http://<lan-ip>` the API simply does not exist. The page rendered black, on a
tablet and a phone, which is exactly where it would be watched. The replacement
uses MediaSource, which has no such requirement.

**3. "Idle timeout keeps it cool."** The teardown path called `close_session`
— the one command that powers the camera off — so **every service restart
dropped the camera off the USB bus** and required physically unplugging it. A
restart that needs desk access is not a restart.

None of these were subtle protocol errors. They were assumptions that survived
right up until someone tested the thing I had actually claimed.

---

## What it does now

- **Live view**: H.264 1920x960 at 30 fps, in the browser, with drag-to-look
  360 navigation — or pulled by ffmpeg, VLC, OBS, anything.
- **A virtual camera**: the panorama projected to an ordinary flat view and
  published as a normal V4L2 webcam, so it works in a meeting app.
- **Low cost**: 17% of one core on a cheap TV box, because the camera's own
  H.264 is remuxed rather than re-encoded.
- **A real record**: every opcode, every reply layout, every mistake, in
  `STATE.md`.

What it does not do: full-resolution stills. The camera resets its USB
connection when asked to capture on the test unit, and the download command
answers `busy` even when capture completes. That is documented as unsolved
rather than quietly omitted.

## Why Python

Because the work was **discovery**, not throughput. Every fact here came from
sending candidate bytes and reading the reply — thousands of hypotheses, each
one a one-line change and a re-run. The camera moves 5 Mbit/s; the heavy stages
belong to libusb, ffmpeg and the viewer's GPU. And a stranger has to be able to
open this, understand it, and patch it after everyone involved has moved on.

There is a longer argument, including where Python is the wrong answer, in
`DESIGN.md`.

## Why this matters

The thing worth keeping is not the camera. It is that **the hardware outlives
the app**, and usually the app is the only part that dies.

Every abandoned device with a USB port is potentially recoverable by exactly
this process: find the last copy of the software, read it, measure what the
hardware actually does, and write down everything — including the parts you got
wrong.

If you have one of these in a drawer, it works again. Start with the README.
