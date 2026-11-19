#!/usr/bin/env python3
"""
madv.py - open-source host driver for the MADV Mini 360 camera (Sunplus SPCA6350).

The camera declares itself as a vendor-specific interface (class 0xFF) but
speaks USB Mass Storage Bulk-Only Transport (subclass 0x06, protocol 0x50)
with a vendor SCSI command set: opcode 0x7A = act/read, 0x7B = write.

Everything here was reverse-engineered from com.madv.mini v1.02.34.0.
See STATE.md for the full protocol description.

Two transports are supported:
  usb   raw libusb (pyusb), builds CBW/CSW by hand - needed because the
        vendor class means no kernel driver attaches
  sg    /dev/sgN + SG_IO, for the case where usb-storage is force-bound

Usage:
  madv.py probe                  list USB devices, identify the camera
  madv.py info                   open session, read versions/status
  madv.py status                 execution + thermal status
  madv.py settings               read all settings
  madv.py live  [-n SECS] [-o f] capture live view H.264
  madv.py photo [-o f]           download one stored photo
  madv.py thumb [-o f]           download the last thumbnail
  madv.py shoot                  take a picture
  madv.py record start|stop      start/stop video recording
  madv.py dump  [-o DIR]         save every readable command reply
  madv.py raw HEX                send a raw CDB (32 hex chars) and dump reply
"""

import argparse
import ctypes
import fcntl
import glob
import os
import struct
import sys
import time

# --- identity -----------------------------------------------------------
VID_PID_KNOWN = [
    (0x464A, 0x4D02),   # confirmed on hardware: "HA CAM ABILITY"
    (0x12D1, 0x109B),   # fallback pair hard-coded in C0111b.m1208i()
]

USB_SIG_CBW = b"USBC"
USB_SIG_CSW = b"USBS"

# --- protocol constants -------------------------------------------------
OP_CMD = 0x7A
OP_SET = 0x7B
CAT_SESSION, CAT_CAMERA, CAT_POLL = 0x00, 0x01, 0x02
CAT_INFO, CAT_SETTINGS, CAT_STREAM, CAT_FIRMWARE = 0x03, 0x04, 0x05, 0x06

ST_OK, ST_BUSY, ST_ERR, ST_POWERSAVE = 0, 1, 2, 0xFF

LIVEVIEW_RES = {9: (1920, 960), 10: (1280, 640), 11: (3840, 1920)}
KEEPALIVE_CDB = bytes([0x7A, 0x03, 0xFF])


# =======================================================================
# transport: raw libusb + hand-rolled Bulk-Only Transport
# =======================================================================
class UsbTransport:
    """Speaks BBB directly. Mirrors C0111b/C0110a byte for byte."""

    EP_OUT = 0x03
    EP_IN = 0x82
    CHUNK = 16384

    def __init__(self, vid=None, pid=None, bus=None, addr=None, timeout_ms=2000):
        import usb.core
        import usb.util
        self._usb = usb.util
        self.timeout_ms = timeout_ms
        self.tag = 0x12345678

        if bus is not None and addr is not None:
            self.dev = usb.core.find(bus=bus, address=addr)
        else:
            self.dev = None
            for v, p in ([(vid, pid)] if vid else []) + VID_PID_KNOWN:
                self.dev = usb.core.find(idVendor=v, idProduct=p)
                if self.dev:
                    break
        if self.dev is None:
            raise RuntimeError("MADV Mini not found on USB")

        try:
            self.dev.set_configuration()
        except Exception:
            pass                      # already configured / transiently busy
        cfg = self.dev.get_active_configuration()
        self.intf = cfg[(0, 0)]
        self.iface_num = self.intf.bInterfaceNumber

        # detach a kernel driver if anything grabbed it
        try:
            if self.dev.is_kernel_driver_active(self.iface_num):
                self.dev.detach_kernel_driver(self.iface_num)
        except (NotImplementedError, Exception):
            pass
        usb.util.claim_interface(self.dev, self.iface_num)
        self._claimed = True

    # -- low level --
    def _write(self, data, timeout=None):
        return self.dev.write(self.EP_OUT, data,
                              timeout=timeout or self.timeout_ms)

    def _read(self, size, timeout=None):
        return bytes(self.dev.read(self.EP_IN, size,
                                   timeout=timeout or self.timeout_ms))

    def reset(self):
        """Bulk-Only Mass Storage Reset: C0111b.m1212m()."""
        try:
            self.dev.ctrl_transfer(0x21, 0xFF, 0x0000, self.iface_num, None,
                                   self.timeout_ms)
            return True
        except Exception:
            return False

    def _cbw(self, cdb, length, flags):
        self.tag = (self.tag + 0x9E3779B9) & 0xFFFFFFFF
        return (
            USB_SIG_CBW
            + struct.pack(">I", self.tag)      # tag: big-endian
            + struct.pack("<I", length)        # length: LITTLE-endian (app quirk)
            + bytes([flags, 0x00, 0x10])       # flags, LUN 0, CB length 16
            + bytes(cdb).ljust(16, b"\x00")[:16]
        )

    def _read_csw(self, acc, timeout):
        """Return (payload, status) once the 13-byte CSW appears at the tail."""
        for _ in range(4000):
            if len(acc) >= 13 and bytes(acc[-13:-9]) == USB_SIG_CSW:
                tag = struct.unpack(">I", bytes(acc[-9:-5]))[0]
                status = acc[-1]
                if tag == self.tag:
                    return bytes(acc[:-13]), status
            try:
                chunk = self._read(self.CHUNK, timeout)
            except Exception as e:
                if len(acc) >= 13 and bytes(acc[-13:-9]) == USB_SIG_CSW:
                    return bytes(acc[:-13]), acc[-1]
                raise IOError(f"bulk-in failed after {len(acc)} bytes: {e}")
            if not chunk:
                continue
            acc += chunk
            if len(acc) > 4 * 1024 * 1024:
                raise IOError("response overflow")
        raise IOError("no CSW from device")

    def command(self, cdb, rx_len=0, tx=None, timeout_ms=None):
        """Issue a 16-byte vendor CDB. Returns (payload, status)."""
        t = timeout_ms or self.timeout_ms
        if tx is not None:
            self._write(self._cbw(cdb, len(tx), 0x00), t)
            for off in range(0, len(tx), self.CHUNK):
                self._write(bytes(tx[off:off + self.CHUNK]), t)
            acc = bytearray()
            try:
                payload, st = self._read_csw(acc, t)
            except IOError:
                st = 0
            return b"", st
        length = rx_len if rx_len else 512
        self._write(self._cbw(cdb, length, 0x80), t)
        acc = bytearray()
        return self._read_csw(acc, t)

    def close(self):
        try:
            if self._claimed:
                self._usb.release_interface(self.dev, self.iface_num)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# =======================================================================
# transport: /dev/sgN + SG_IO (kernel builds the CBW/CSW for us)
# =======================================================================
SG_IO = 0x2285


class sg_io_hdr(ctypes.Structure):
    _fields_ = [
        ("interface_id", ctypes.c_int), ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte), ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort), ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p), ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p), ("timeout", ctypes.c_uint),
        ("dxfer_len_used", ctypes.c_uint), ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte), ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte), ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort), ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint), ("info", ctypes.c_uint),
    ]


class SgTransport:
    SG_DXFER_NONE, SG_DXFER_TO_DEV, SG_DXFER_FROM_DEV = -1, -2, -3

    def __init__(self, path, timeout_ms=2000):
        self.path, self.timeout_ms = path, timeout_ms
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def reset(self):
        return False

    def _io(self, cdb, buf, direction):
        hdr = sg_io_hdr()
        hdr.interface_id = ord("S")
        hdr.dxfer_direction = direction
        hdr.cmd_len = 16
        hdr.mx_sb_len = 64
        hdr.dxfer_len = len(buf) if buf else 0
        hdr.dxferp = ctypes.cast(buf, ctypes.c_void_p) if buf else None
        hdr.cmdp = ctypes.cast(cdb, ctypes.c_void_p)
        sb = ctypes.create_string_buffer(64)
        hdr.sbp = ctypes.cast(sb, ctypes.c_void_p)
        hdr.timeout = self.timeout_ms
        fcntl.ioctl(self.fd, SG_IO, hdr, True)
        return hdr, sb.raw

    def command(self, cdb, rx_len=0, tx=None, timeout_ms=None):
        if timeout_ms:
            self.timeout_ms = timeout_ms
        cdb = bytes(cdb).ljust(16, b"\x00")[:16]
        cb = ctypes.create_string_buffer(cdb, 16)
        if tx is not None:
            buf = ctypes.create_string_buffer(bytes(tx), len(tx))
            hdr, _ = self._io(cb, buf, self.SG_DXFER_TO_DEV)
            return b"", 0
        if rx_len:
            buf = ctypes.create_string_buffer(rx_len)
            hdr, sense = self._io(cb, buf, self.SG_DXFER_FROM_DEV)
            got = rx_len - max(0, hdr.resid)
            return buf.raw[:got], (0 if hdr.status == 0 else 2)
        hdr, _ = self._io(cb, None, self.SG_DXFER_NONE)
        return b"", 0

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# =======================================================================
# discovery
# =======================================================================
def find_devices():
    out = []
    for d in sorted(glob.glob("/sys/bus/usb/devices/*")):
        try:
            vid = int(open(f"{d}/idVendor").read().strip(), 16)
            pid = int(open(f"{d}/idProduct").read().strip(), 16)
        except OSError:
            continue
        name = ""
        for f in ("product", "manufacturer"):
            try:
                name += open(f"{d}/{f}").read().strip() + " "
            except OSError:
                pass
        out.append((os.path.basename(d), vid, pid, name.strip(), d))
    return out


def sg_for_usb(sysdir):
    base = os.path.basename(sysdir)
    for sgdir in glob.glob("/sys/class/scsi_generic/sg*"):
        try:
            real = os.path.realpath(sgdir)
        except OSError:
            continue
        if f"/{base}/" in real + "/" or os.path.realpath(sysdir) in real:
            return "/dev/" + os.path.basename(sgdir)
    return None


def cmd_probe(args):
    print("USB devices:")
    hit = None
    for dev, vid, pid, name, sysdir in find_devices():
        mark = ""
        if (vid, pid) in VID_PID_KNOWN:
            mark = "   <-- MADV Mini"
            hit = sysdir
        print(f"  {dev:<10} {vid:04x}:{pid:04x}  {name}{mark}")
    print()
    print("SCSI generic nodes:", ", ".join(sorted(glob.glob("/dev/sg*"))) or "none")
    if not hit:
        print("\nNo MADV Mini found (looking for 464a:4d02 / 12d1:109b).")
        return 1
    base = os.path.basename(hit)
    ints = glob.glob(f"{hit}/{base}:*")
    for i in ints:
        cls = open(f"{i}/bInterfaceClass").read().strip()
        sub = open(f"{i}/bInterfaceSubClass").read().strip()
        prt = open(f"{i}/bInterfaceProtocol").read().strip()
        eps = sorted(os.path.basename(e) for e in glob.glob(f"{i}/ep_*"))
        print(f"\ninterface {os.path.basename(i)}: class={cls} sub={sub} proto={prt}")
        for e in eps:
            p = f"{i}/{e}"
            print(f"  {e}: {open(p+'/type').read().strip():<5} "
                  f"{open(p+'/direction').read().strip():<3} "
                  f"maxpkt={int(open(p+'/wMaxPacketSize').read().strip(),16)}")
    node = sg_for_usb(hit)
    print(f"\nkernel driver bound: {'usb-storage -> ' + node if node else 'none (vendor class - use libusb)'}")
    bus = int(open(f"{hit}/busnum").read().strip())
    devnum = int(open(f"{hit}/devnum").read().strip())
    devnode = f"/dev/bus/usb/{bus:03d}/{devnum:03d}"
    print(f"usbfs node: {devnode}")
    try:
        print("writable:", os.access(devnode, os.W_OK))
    except OSError:
        pass
    print("\nready: python3 tools/madv.py info")
    return 0


def open_transport(args):
    t = getattr(args, "transport", "auto")
    if t in ("auto", "sg"):
        if getattr(args, "dev", None):
            return SgTransport(args.dev)
        for dev, vid, pid, name, sysdir in find_devices():
            if (vid, pid) in VID_PID_KNOWN:
                node = sg_for_usb(sysdir)
                if node:
                    return SgTransport(node)
        if t == "sg":
            raise SystemExit("no /dev/sgN for the camera")
    return UsbTransport()


# =======================================================================
# protocol
# =======================================================================
def _cdb(op, cat, sub, params=None, payload_len=0):
    c = bytearray(16)
    c[0] = op
    c[1] = cat
    c[2] = sub & 0xFF
    if payload_len:
        c[4] = payload_len & 0xFF
    for off, val in (params or {}).items():
        c[off] = val & 0xFF
    return bytes(c)


def _u32le_params(base, value):
    return {base + i: (value >> (8 * i)) & 0xFF for i in range(4)}


class Camera:
    def __init__(self, transport):
        self.t = transport
        self._last_ka = 0.0
        self.already_open = False

    def _recover(self):
        """One-shot recovery: bulk-only reset, then re-open the session directly.

        Deliberately bypasses _call() so recovery can never recurse into itself.
        """
        try:
            self.t.reset()
        except Exception:
            pass
        try:
            self.t.command(_cdb(OP_CMD, CAT_SESSION, 0x01, {8: 1}),
                           rx_len=4096, timeout_ms=2000)
            self._last_ka = time.time()
            return True
        except Exception:
            return False

    def _call(self, cdb, rx_len=65536, tx=None, timeout_ms=None, _retry=True):
        try:
            payload, st = self.t.command(cdb, rx_len=rx_len, tx=tx,
                                         timeout_ms=timeout_ms)
        except (IOError, OSError):
            if not _retry or not self._recover():
                raise
            return self._call(cdb, rx_len=rx_len, tx=tx,
                              timeout_ms=timeout_ms, _retry=False)
        # 0xFF: the camera dropped into power save.  Re-open and retry once,
        # exactly as the app does in C0105a.m1132a().
        if (_retry and payload and payload[0] == ST_POWERSAVE
                and cdb[0] == OP_CMD and cdb[1:3] != b"\x00\x01"):
            if self._recover():
                return self._call(cdb, rx_len=rx_len, tx=tx,
                                  timeout_ms=timeout_ms, _retry=False)
        return payload, st

    def open_session(self, retries=20, reset_effect=True):
        cdb = _cdb(OP_CMD, CAT_SESSION, 0x01, {8: 1 if reset_effect else 0})
        for attempt in range(retries):
            p, _ = self._call(cdb, rx_len=4096, _retry=False)
            st = p[0] if p else None
            if st == ST_OK:
                self._last_ka = time.time()
                return True
            if st == ST_BUSY and attempt >= 1:
                # already open - the camera is happy to serve us
                self._last_ka = time.time()
                self.already_open = True
                return True
            if st in (ST_BUSY, None):
                time.sleep(0.1)
                continue
            if st == ST_POWERSAVE:
                time.sleep(0.1)
                continue
            raise IOError(f"open communication failed, status={st}")
        raise IOError("open communication timed out")

    def close_session(self):
        params = {8: 1, 9: 0}
        params.update(_u32le_params(12, 300))
        p, _ = self._call(_cdb(OP_CMD, CAT_SESSION, 0x02, params), rx_len=4096)
        return p[0] if p else None

    def keepalive(self, force=False):
        if not force and time.time() - self._last_ka < 0.45:
            return None
        self._last_ka = time.time()
        p, _ = self._call(KEEPALIVE_CDB, rx_len=4096)
        return p[0] if p else None

    # ---- info ----
    def info(self, app_version="v2.00.0"):
        cdb = bytearray(_cdb(OP_CMD, CAT_INFO, 0x01))
        av = app_version.encode()[:7]
        cdb[8:8 + len(av)] = av
        p, _ = self._call(bytes(cdb), rx_len=4096)
        txt = lambda b: b.split(b"\x00")[0].decode("ascii", "replace")
        return {"status": p[0] if p else None,
                "hw_version": txt(p[65:97]) if len(p) >= 97 else "",
                "fw_version": txt(p[97:129]) if len(p) >= 129 else "",
                "raw": p[:160].hex()}

    def scsi_version(self):
        cdb = bytearray(_cdb(OP_CMD, CAT_INFO, 0x02))
        cdb[8:8 + 7] = b"v2.0000"
        p, _ = self._call(bytes(cdb), rx_len=4096)
        return {"status": p[0] if p else None,
                "version": p[8:40].split(b"\x00")[0].decode("ascii", "replace"),
                "raw": p[:48].hex()}

    def status(self):
        p, _ = self._call(_cdb(OP_CMD, CAT_INFO, 0x30), rx_len=4096)
        return {"status": p[0] if p else None,
                "value": p[1] if len(p) > 1 else None,
                "raw": p[:32].hex()}

    def thermal(self):
        p, _ = self._call(_cdb(OP_CMD, CAT_INFO, 0x34), rx_len=4096)
        return {"status": p[0] if p else None,
                "value": p[1] if len(p) > 1 else None,
                "raw": p[:32].hex()}

    def remained_pics(self):
        p, _ = self._call(_cdb(OP_CMD, CAT_INFO, 0x35), rx_len=4096)
        return struct.unpack_from("<I", p, 8)[0] if len(p) >= 12 else None

    # ---- settings ----
    def get_setting(self, sub):
        """Single-setting get replies carry the VALUE at [0] - no status byte.

        A reply of 0xFF means the camera is asleep and the call must be retried.
        """
        p, _ = self._call(_cdb(OP_CMD, CAT_SETTINGS, sub), rx_len=4096)
        return (p[0] if p else None)

    def all_settings(self):
        p, _ = self._call(_cdb(OP_CMD, CAT_SETTINGS, 0x60), rx_len=4112)
        return p

    def write_all_settings(self, photo_res=3, video_res=9, evb=0, wb=0,
                           filter_=0, bitrate=8, logo_type=None, when=None):
        """OP 7B/04/60 - write the 48-byte settings block.

        THIS IS MANDATORY BEFORE LIVE VIEW. The camera accepts `7A 01 01`
        but never produces a frame until this has been written; that is why
        the app always sends WRITE_SET_ALL_SETTING first (C2171b case 8732).

        Payload layout (identical to what 7A 04 60 reads back):
          [2]=photo_res [3]=video_res [6]=evb [7]=white_balance
          [10:12]=year LE [12]=mon [13]=day [14]=hour [15]=min [16]=sec
          [18:20]=ms LE [32]=filter [35]=bitrate [39]=logo_type
        The app uses bitrate 8 (100 in mode 448).
        """
        cur, _ = self._call(_cdb(OP_CMD, CAT_SETTINGS, 0x60), rx_len=4096)
        pl = bytearray(bytes(cur[:48]).ljust(48, b"\x00"))
        t = when or time.localtime()
        pl[2] = photo_res
        pl[3] = video_res
        pl[6] = evb
        pl[7] = wb
        struct.pack_into("<H", pl, 10, t.tm_year)
        pl[12], pl[13] = t.tm_mon, t.tm_mday
        pl[14], pl[15], pl[16] = t.tm_hour, t.tm_min, t.tm_sec
        struct.pack_into("<H", pl, 18, 0)
        pl[32] = filter_
        pl[35] = bitrate
        if logo_type is not None:
            pl[39] = logo_type
        p, _ = self._call(_cdb(OP_SET, CAT_SETTINGS, 0x60, {4: 48}),
                          rx_len=4096, tx=bytes(pl))
        return p[0] if p else None

    def prepare(self, res=9, bitrate=8, app_version="1.02.34"):
        """Everything the app does between connect and using the camera."""
        self.info(app_version=app_version)
        self.clear_picture_buffer()
        return self.write_all_settings(video_res=res, bitrate=bitrate)

    def start_stream(self, res=9, app_version="1.02.34", bitrate=8):
        """The app's real startup sequence (C2171b 8732 -> 18519 -> 18449)."""
        self.prepare(res=res, bitrate=bitrate, app_version=app_version)
        return self._act(0x01, {8: 0, 9: res, 10: 0})

    def set_setting(self, sub, value):
        p, _ = self._call(_cdb(OP_SET, CAT_SETTINGS, sub, {4: 1}),
                          rx_len=4096, tx=bytes([value & 0xFF]))
        return p[0] if p else None

    def set_datetime(self, t=None):
        t = t or time.localtime()
        pl = bytearray(24)
        struct.pack_into("<H", pl, 2, t.tm_year)
        pl[4], pl[5] = t.tm_mon, t.tm_mday
        pl[6], pl[7], pl[8] = t.tm_hour, t.tm_min, t.tm_sec
        p, _ = self._call(_cdb(OP_SET, CAT_SETTINGS, 0x01, payload_len=24),
                          rx_len=4096, tx=bytes(pl))
        return p[0] if p else None

    # ---- camera actions ----
    def _act(self, sub, params=None, poll=None, tries=15):
        p, _ = self._call(_cdb(OP_CMD, CAT_CAMERA, sub, params), rx_len=4096)
        st = p[0] if p else None
        if st != ST_OK or poll is None:
            return st
        for _ in range(tries):
            self.keepalive()
            q, _ = self._call(_cdb(OP_CMD, CAT_POLL, poll), rx_len=4096)
            s2 = q[0] if q else None
            if s2 == ST_OK:
                return ST_OK
            if s2 != ST_BUSY:
                return s2
            time.sleep(0.15)
        return ST_BUSY

    def start_liveview(self, res=9):
        return self._act(0x01, {8: 0, 9: res, 10: 0}, poll=0x01)

    def stop_liveview(self):
        return self._act(0x02, poll=0x02)

    def start_recording(self, res=9, fps=0):
        return self._act(0x03, {8: 0, 9: res, 10: fps}, poll=0x03)

    def stop_recording(self):
        return self._act(0x04, poll=0x04)

    def take_picture(self, orientation=0):
        return self._act(0x05, {8: orientation}, poll=0x05)

    def power_off(self):
        p, _ = self._call(_cdb(OP_CMD, CAT_CAMERA, 0xF0), rx_len=4096)
        return p[0] if p else None

    def clear_picture_buffer(self):
        p, _ = self._call(_cdb(OP_CMD, CAT_CAMERA, 0x85), rx_len=4096)
        return p[0] if p else None

    # ---- data ----
    def get_frame(self, want=65536):
        """OP 7A/05/01 - one live-view access unit.

        Envelope (verified): 32-byte header
          [0]=status [1]=last-chunk flag [4]=resolution id
          [28:32]=payload length, LITTLE-endian
          [32:]=H.264 Annex-B access unit (start code 00 00 00 01)
        """
        p, _ = self._call(_cdb(OP_CMD, CAT_STREAM, 0x01, {8: 0}),
                          rx_len=want, timeout_ms=4000)
        if len(p) < 32 or p[0] != ST_OK:
            return None
        n = struct.unpack_from("<I", p, 28)[0]
        data = p[32:32 + n] if 0 < n <= len(p) - 32 else p[32:]
        return {"status": p[0], "last": p[1], "res": p[4],
                "thermal": p[8], "data": data, "raw_len": len(p)}

    def get_picture(self, stall_limit=64):
        out = bytearray()
        offset, stalls = 0, 0
        while stalls < stall_limit:
            self.keepalive()
            p, _ = self._call(
                _cdb(OP_CMD, CAT_STREAM, 0x02, _u32le_params(8, offset)),
                rx_len=327680, timeout_ms=6000)
            if len(p) < 20 or p[0] != ST_OK:
                stalls += 1
                time.sleep(0.05)
                continue
            n = struct.unpack_from("<I", p, 16)[0]
            if n == 0 or 20 + n > len(p):
                stalls += 1
                continue
            out += p[20:20 + n]
            offset += n
            stalls = 0
            if p[1] == 1:
                self._ack_picture()
                return bytes(out)
        raise IOError(f"picture download stalled at {len(out)} bytes")

    def _ack_picture(self):
        p, _ = self._call(_cdb(OP_CMD, CAT_STREAM, 0x82), rx_len=4096)
        return p[0] if p else None

    def get_thumbnail(self):
        p, _ = self._call(_cdb(OP_CMD, CAT_STREAM, 0x03), rx_len=148480)
        if len(p) < 20 or p[0] != ST_OK:
            return None
        n = struct.unpack_from("<I", p, 16)[0]
        return p[20:20 + n] if 0 < n <= len(p) - 20 else p[20:]


# =======================================================================
# CLI
# =======================================================================
def _say(label, val):
    print(f"{label:<16}: {val}")


def cmd_info(args):
    with open_transport(args) as t:
        cam = Camera(t)
        _say("open session", cam.open_session())
        _say("info", cam.info())
        _say("scsi version", cam.scsi_version())
        _say("status", cam.status())
        _say("thermal", cam.thermal())
        _say("remaining", cam.remained_pics())
        for name, sub in [("photo_res", 0x03), ("video_res", 0x04),
                          ("evb", 0x07), ("white_balance", 0x08),
                          ("filter", 0x09), ("shutter_time", 0x10),
                          ("logo_type", 0x14)]:
            try:
                _say("  " + name, cam.get_setting(sub))
            except IOError as e:
                _say("  " + name, f"error {e}")
        _say("close session", cam.close_session())


def cmd_status(args):
    with open_transport(args) as t:
        cam = Camera(t)
        cam.open_session()
        _say("status", cam.status())
        _say("thermal", cam.thermal())
        _say("remaining", cam.remained_pics())
        cam.close_session()


def cmd_settings(args):
    with open_transport(args) as t:
        cam = Camera(t)
        cam.open_session()
        p = cam.all_settings()
        print(f"all-settings: {len(p)} bytes, status={p[0] if p else None}")
        for i in range(0, min(len(p), 64), 16):
            print(f"  [{i:02d}] {p[i:i+16].hex(' ')}")
        cam.close_session()


def cmd_live(args):
    with open_transport(args) as t:
        cam = Camera(t)
        cam.open_session()
        st = cam.start_stream(res=args.res)
        print(f"start live view -> {st}  ({LIVEVIEW_RES.get(args.res)})")
        frames = chunks = total = 0
        deadline = time.time() + args.secs
        out = open(args.out, "wb")
        try:
            while time.time() < deadline:
                cam.keepalive()
                fr = cam.get_frame()
                if not fr or not fr["data"]:
                    continue
                out.write(fr["data"])
                chunks += 1
                total += len(fr["data"])
                if fr["last"] == 1:
                    frames += 1
        except KeyboardInterrupt:
            pass
        finally:
            out.close()
            cam.stop_liveview()
            cam.close_session()
        print(f"wrote {chunks} chunks ({frames} frames, {total} bytes) "
              f"to {args.out}")


def cmd_photo(args):
    with open_transport(args) as t:
        cam = Camera(t)
        cam.open_session()
        cam.prepare()
        data = cam.get_picture()
        open(args.out, "wb").write(data)
        cam.close_session()
    print(f"wrote {len(data)} bytes to {args.out}")
    print("JPEG:", data[:2] == b"\xff\xd8", " SOI/EOI:",
          data[:2].hex(), data[-2:].hex())


def cmd_thumb(args):
    with open_transport(args) as t:
        cam = Camera(t)
        cam.open_session()
        cam.prepare()
        data = cam.get_thumbnail() or b""
        open(args.out, "wb").write(data)
        cam.close_session()
    print(f"wrote {len(data)} bytes to {args.out}")
    print("JPEG:", data[:2] == b"\xff\xd8")


def cmd_shoot(args):
    with open_transport(args) as t:
        cam = Camera(t)
        cam.open_session()
        cam.prepare()
        _say("take picture", cam.take_picture(args.orientation))
        cam.close_session()


def cmd_record(args):
    with open_transport(args) as t:
        cam = Camera(t)
        cam.open_session()
        cam.prepare()
        if args.action == "start":
            _say("start recording", cam.start_recording(args.res))
        else:
            _say("stop recording", cam.stop_recording())
        cam.close_session()


def cmd_dump(args):
    os.makedirs(args.out, exist_ok=True)
    probes = [
        ("session.open", _cdb(OP_CMD, CAT_SESSION, 0x01, {8: 1}), 4096),
        ("info.03_01", _cdb(OP_CMD, CAT_INFO, 0x01), 4096),
        ("info.03_02_scsiver", _cdb(OP_CMD, CAT_INFO, 0x02), 4096),
        ("info.03_30_status", _cdb(OP_CMD, CAT_INFO, 0x30), 4096),
        ("info.03_34_thermal", _cdb(OP_CMD, CAT_INFO, 0x34), 4096),
        ("info.03_35_remain", _cdb(OP_CMD, CAT_INFO, 0x35), 4096),
        ("set.04_01_datetime", _cdb(OP_CMD, CAT_SETTINGS, 0x01), 4096),
        ("set.04_03_photo", _cdb(OP_CMD, CAT_SETTINGS, 0x03), 4096),
        ("set.04_04_video", _cdb(OP_CMD, CAT_SETTINGS, 0x04), 4096),
        ("set.04_07_evb", _cdb(OP_CMD, CAT_SETTINGS, 0x07), 4096),
        ("set.04_08_wb", _cdb(OP_CMD, CAT_SETTINGS, 0x08), 4096),
        ("set.04_09_filter", _cdb(OP_CMD, CAT_SETTINGS, 0x09), 4096),
        ("set.04_10_shutter", _cdb(OP_CMD, CAT_SETTINGS, 0x10), 4096),
        ("set.04_14_logo", _cdb(OP_CMD, CAT_SETTINGS, 0x14), 4096),
        ("set.04_60_all", _cdb(OP_CMD, CAT_SETTINGS, 0x60), 4112),
        ("stream.05_03_thumb", _cdb(OP_CMD, CAT_STREAM, 0x03), 148480),
    ]
    with open_transport(args) as t:
        cam = Camera(t)
        for name, cdb, rx in probes:
            try:
                p, st = cam._call(cdb, rx_len=rx, timeout_ms=5000)
            except Exception as e:
                print(f"{name:<22} ERROR {e}")
                continue
            fn = os.path.join(args.out, name + ".bin")
            open(fn, "wb").write(p)
            print(f"{name:<22} {len(p):>7} bytes  status={p[0] if p else None}")
            print(f"    {p[:32].hex(' ')}")
        cam.close_session()


def cmd_raw(args):
    cdb = bytes.fromhex(args.hex.replace(" ", ""))
    with open_transport(args) as t:
        payload, st = t.command(cdb, rx_len=args.rx, timeout_ms=args.timeout)
    print(f"status={st} got={len(payload)}")
    print(payload[:512].hex(" "))
    if args.out and payload:
        open(args.out, "wb").write(payload)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-d", "--dev", help="explicit /dev/sgN node")
    ap.add_argument("-t", "--transport", choices=["auto", "usb", "sg"],
                    default="auto")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe").set_defaults(func=cmd_probe)
    sub.add_parser("info").set_defaults(func=cmd_info)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("settings").set_defaults(func=cmd_settings)

    p = sub.add_parser("live")
    p.add_argument("-n", "--secs", type=float, default=5)
    p.add_argument("-o", "--out", default="live.h264")
    p.add_argument("-r", "--res", type=int, default=9)
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("photo")
    p.add_argument("-o", "--out", default="photo.jpg")
    p.set_defaults(func=cmd_photo)

    p = sub.add_parser("thumb")
    p.add_argument("-o", "--out", default="thumb.jpg")
    p.set_defaults(func=cmd_thumb)

    p = sub.add_parser("shoot")
    p.add_argument("-O", "--orientation", type=int, default=0)
    p.set_defaults(func=cmd_shoot)

    p = sub.add_parser("record")
    p.add_argument("action", choices=["start", "stop"])
    p.add_argument("-r", "--res", type=int, default=9)
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("dump")
    p.add_argument("-o", "--out", default="dump")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("raw")
    p.add_argument("hex")
    p.add_argument("--rx", type=int, default=4096)
    p.add_argument("--timeout", type=int, default=2000)
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_raw)

    args = ap.parse_args()
    try:
        return args.func(args) or 0
    except SystemExit:
        raise
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
