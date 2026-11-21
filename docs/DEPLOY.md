# Deploying free60 on a Linux host

> Linux is the proven platform. macOS and Windows are expected to work but
> untested — see the Portability table in the [README](../README.md).

A checklist for a human **or** an agent. If you are an agent, also read
[`AGENTS.md`](../AGENTS.md) — it lists the constraints you must not work around.

## 0. Precondition you cannot skip

**The camera must be physically plugged into the host you are setting up.**
It is USB-tethered: no Wi-Fi, no Bluetooth, no battery. There is no remote
path worth using — usbip round-trips badly enough to ruin a 30 fps bulk stream.

Check it is there at all. Use the driver's own probe rather than `lsusb`,
because **`lsusb` is often not installed on minimal Armbian/TVBox images**:

```bash
python3 tools/madv.py probe      # works with pyusb only, no usbutils needed
```

If that reports nothing found, stop and plug the camera in.

## 1. Get the repo onto the host

```bash
git clone <this repo> free60 && cd free60
```

## 2. Run the setup script

```bash
./tools/setup-host.sh --dry-run   # read this first
./tools/setup-host.sh
```

It installs libusb, makes pyusb importable (distro package, venv fallback),
installs the udev rule, and probes for the camera. It never touches the
camera's firmware.

It does **not** install ffmpeg by default. Only `--mode mjpeg` needs it, and on
a TVBox that is ~115 MB of packages on a small eMMC/SD root for a path you
probably will not use. Add `--with-ffmpeg` if you want the mjpeg fallback.

## 3. Prove you can talk to it

```bash
<pybin> tools/madv.py probe    # shows the device and whether it is reachable
<pybin> tools/madv.py info     # expect fw_version = v1.1126
```

`<pybin>` is whatever the setup script printed at the end — either
`python3` or `./.venv/bin/python`.

If `probe` works but `info` fails, see *Troubleshooting* below.

## 4. Run the demo

```bash
# weak host (TV-box SoC): remux only, near-zero CPU, works over plain http
<pybin> tools/madv-web.py --port 8901 --bind <lan-ip> --mode fmp4

# simplest, and it works in any browser - but it transcodes
<pybin> tools/madv-web.py --port 8901 --bind <lan-ip>            # mjpeg

# localhost or HTTPS ONLY - see the warning below
<pybin> tools/madv-web.py --port 8901 --bind <lan-ip> --mode webcodecs
```

Open `http://<lan-ip>:8901/`.

| mode | server cost | browser needs | works over plain http? |
|---|---|---|---|
| `fmp4` | remux only (`-c:v copy`) | MediaSource (everywhere) | **yes** |
| `mjpeg` | full decode + JPEG encode | nothing | yes |
| `webcodecs` | none | WebCodecs **and a secure context** | **no** |

### WebCodecs needs HTTPS — this bites everyone

`VideoDecoder` is exposed only in a **secure context**. On
`http://<lan-ip>:8901` the API does not exist at all: the page renders black
and only a console warning explains why. `localhost` counts as secure; a LAN
address does not, and neither do phones or tablets on that address.

Two ways out:

1. **`--https`** — the server generates a self-signed certificate on first run
   (SANs for every local address, key chmod 600, in `certs/`). The browser warns
   once, then WebCodecs works. This is the portable answer: it helps any device
   on the LAN, unlike a tailnet certificate which only helps tailnet members.
2. **`--mode fmp4`** — hands the camera's own H.264 to MediaSource instead.
   Near-identical CPU cost, no certificate, no warning.

`fmp4` is the better default. `webcodecs --https` is worth it only if you want
ffmpeg out of the pipeline entirely.

MJPEG is the fallback that always works, at roughly 3x the bandwidth and real
CPU cost — on an H616 that measured around 2.7 cores at 24 fps.

## 5. Open the port — and prove it from another host

If the host runs a firewall, the port is not reachable no matter what `ss`
says. On nftables, reload **with a delete first**, because `nft -f` on an
existing table *appends* and silently duplicates every rule:

```bash
sudo nft delete table inet <your-table> && sudo nft -f /etc/nftables.d/<your-file>.nft
```

Then verify **from a different machine**, never from the host itself:

```bash
python3 -c "import socket;s=socket.socket();s.settimeout(4);s.connect(('<lan-ip>',8901));print('ok')"
```

A listener on localhost proves nothing about the firewall or the LAN path.

## 5b. Playing nicely with other services

The capture loop is the only real CPU consumer (the ffmpeg remux is under 1%).
On a shared box, cap it rather than letting it compete:

```ini
[Service]
Nice=10
CPUQuota=80%
```

Measured on an S905X-class TV box with one viewer, `--mode fmp4`: **~17% of one
core**, with ffmpeg under 1%. If you see substantially more, something is
polling the camera faster than it delivers frames — check before assuming the
box is too slow. An earlier build sat at 81% purely because of a busy poll
loop.

## 6. Run it as a service (optional)

```ini
[Unit]
Description=free60 MADV Mini 360 web demo
After=network-online.target

[Service]
User=<a user in plugdev>
WorkingDirectory=/opt/free60
ExecStart=/usr/bin/python3 tools/madv-web.py \
  --port 8901 --bind <lan-ip> --mode webcodecs --idle-timeout 60
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Two things that catch people:

- **udev `uaccess` does not apply to services.** The rule grants access to an
  interactive seat only. A service needs its user in `plugdev` (the rule's
  `MODE`/`GROUP`), or needs to run as root.
- The server retries internally when the camera is absent, so `Restart=always`
  is only a crash guard.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `probe` finds nothing | not plugged into **this** host, or a dead port |
| `probe` finds it, no `/dev/sgN` | expected — vendor class, no kernel driver binds it |
| `probe` finds it, `info` times out | camera is wedged → unplug/replug (no battery, no soft reset) |
| `info` works, `live` silent | the camera is under-volted, or was never sent the settings block. Suspect the port first |
| Device vanished mid-run | `close_session` was sent, which powers it **off the bus**. Replug |
| Page loads, no video | firewall (test from another host) or the browser lacks WebCodecs → use `mjpeg` |
| Stream is choppy | bandwidth. `--scale 1280` for mjpeg, or switch to `webcodecs` |

## Hardware notes

- **Power:** the descriptors claim *Self Powered, 100 mA*. Do not believe them;
  there is no battery and the encoder draws far more in use. A short USB
  extension is fine and often necessary to stand the camera upright — the risk
  is weak or overloaded ports (busy unpowered hubs, long thin cables).
- **Heat:** streaming runs the ISP and encoder flat out. Use `--idle-timeout`
  or the Sleep button; do not leave a stream open with nobody watching.
- **Do not flash firmware.** `docs/FIRMWARE-RECOVERY.md` explains why, and is
  deliberately marked unproven.
