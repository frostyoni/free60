# AGENTS.md — free60

Instructions for an agent asked to deploy or work on this repository.
Humans: `README.md` first, then `docs/DEPLOY.md`.

## What this project is

A host driver for the **MADV Mini** USB-C 360 camera (vendor id `464a`,
product `4d02`), recovered by reverse-engineering its dead Android app.
The camera speaks USB Mass Storage Bulk-Only Transport with a **vendor SCSI**
command set. `tools/madv.py` builds the CBW/CSW envelope by hand over libusb.

The protocol is documented in `STATE.md`. Read it before changing the driver —
much of it was hard-won and some of it corrects mistakes that look reasonable.

## Deploying — do this

```bash
./tools/setup-host.sh --dry-run   # show what it would do
./tools/setup-host.sh             # packages, udev rule, pyusb, probe
./.venv/bin/python tools/madv.py probe
./.venv/bin/python tools/madv.py info     # expect fw = v1.1126
```

Then choose a mode:

```bash
# weak box (TV-box SoC): ship the camera's own H.264, browser decodes
./.venv/bin/python tools/madv-web.py --port 8901 --bind <lan-ip> --mode webcodecs

# any box / old browsers: server transcodes to MJPEG
./.venv/bin/python tools/madv-web.py --port 8901 --bind <lan-ip>
```

## Hard constraints — do not work around these

1. **The camera must be physically attached to the host you are configuring.**
   It is USB tether only: no Wi-Fi, no Bluetooth, no battery. Do not reach for
   usbip — round-trip latency makes a 30 fps bulk stream unusable.
2. **Never flash firmware.** `docs/FIRMWARE-RECOVERY.md` is deliberately marked
   unproven; the `.prm` patch step is not implemented. A wrong flash is not
   recoverable over USB.
3. **If a firewall is present, open the port.** On nftables reload with a delete
   first — `nft -f` on an existing table *appends* and silently duplicates
   every rule:
   ```bash
   sudo nft delete table inet <table> && sudo nft -f /etc/nftables.d/<table>.nft
   ```
4. **Verify from another host, not locally.** A listener proves nothing about
   the firewall or the LAN path.
5. **udev `uaccess` does not apply to systemd services.** If running as a
   service, the service user must be in the rule's group (`plugdev`), or run
   as root.

## Device behaviour that will mislead you

- **`close_session` (`7A 00 02`) powers the camera off and it leaves the USB
  bus.** It then needs a physical replug. It is not an idle mode. To cool the
  camera, stop live view only (`7A 01 02`).
- **A reply of `0xFF` means the camera is asleep** — re-send open communication.
  The driver already does this automatically.
- **The camera wedges.** It can stop answering while still enumerating
  (`lsusb` shows it). Only a replug clears it. It has no battery and no soft
  power switch, so unplugging is the only hard reset.
- **Under-volted reads like a firmware bug**: the camera still enumerates and
  still acknowledges commands but never produces a frame. Suspect the port
  before suspecting the code.
- **It gets hot.** Streaming runs the ISP and H.264 encoder flat out. Do not
  leave a stream open with nobody watching; use `--idle-timeout` (default 60 s)
  or the Sleep button.

## The stream is already stitched

The live view is an **equirectangular panorama** (1920x960, 2:1) produced in
camera. Do not un-stitch it. `STATE.md` §6 has the measurements that prove it,
and the two projection bugs that belief caused.

## Current gaps (do not pretend otherwise)

Full-resolution still download, end-to-end recording, and the firmware burn
format are all unresolved and documented as such. If asked to implement one,
read the corresponding section of `STATE.md` first — the constraints there are
real, not guesses.

## House rules for this repo

- Documentation is first person singular ("I"), not "we".
- Anything measured goes into `STATE.md`; anything unproven is labelled
  unproven; anything found to be wrong stays visible as a correction.
- `*.local.md` is gitignored and must never be committed — it carries fleet
  specifics.
