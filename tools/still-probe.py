#!/usr/bin/env python3
"""
still-probe.py - one shot at the whole still path, with no clear in between.

The still path has two independent problems that have never been tested
together:

  1. capture (7A 01 05) resets the USB link on some power setups
  2. the download (7A 05 02) answers "busy, 0 bytes" in every state I can
     construct without capturing first

Because nothing has ever been downloaded, it is also possible that there has
simply been nothing *to* download: the picture buffer is RAM, and every
prepare() we run sends 7A 01 85 (clear picture buffer). So any photo from an
earlier capture is long gone.

This script does the whole sequence in one uninterrupted run and reports at each
step, so one execution on a good power supply answers both questions:

    capture -> poll to completion -> download, no clear, no restart

Run it on the camera host with the service stopped:

    sudo systemctl stop free60
    python3 tools/still-probe.py
    # or, from a machine with the repo but no pyusb: see docs/DEPLOY.md

It never calls 7A 01 85. If the camera resets mid-capture, it says so and
stops, because there is nothing useful to do after a reset.
"""

import sys, os, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import madv
from madv import _cdb, OP_CMD


def main():
    print("still-probe: capture and download in one pass, no clear\n")

    try:
        t = madv.UsbTransport()
    except Exception as e:
        print(f"  cannot open the camera: {e}")
        return 1
    cam = madv.Camera(t)

    try:
        print("1. open session")
        cam.open_session()
        print("   live view status:", cam._call(_cdb(OP_CMD, 0x02, 0x01), rx_len=4096)[0][:4].hex(" "))

        print("\n2. configure (settings only - deliberately no 7A 01 85)")
        cam.info()
        cam.write_all_settings(video_res=9, bitrate=8)

        print("\n3. capture")
        r = cam._call(_cdb(OP_CMD, 0x01, 0x05, {8: 0}), rx_len=4096)
        print("   shutter reply:", r[0][:2].hex(" "))

        done = False
        for i in range(80):
            q, _ = cam._call(_cdb(OP_CMD, 0x02, 0x05), rx_len=4096)
            if i < 3 or (q and q[0] == 0):
                print(f"   poll {i}: status={q[0]} exposure={q[2] if len(q)>2 else '-'} "
                      f"stored_pic_num={q[3] if len(q)>3 else '-'}")
            if q and q[0] == 0:
                done = True
                break
            time.sleep(0.25)

        if not done:
            print("\n   capture never completed. If the camera also vanished from")
            print("   lsusb, this is the reset - try a powered hub or a direct port.")
            return 2

        print("\n4. try the download immediately, in the same session")
        for attempt in range(3):
            p, _ = cam._call(_cdb(OP_CMD, 0x05, 0x02,
                                  {8: 0, 9: 0, 10: 0, 11: 0}),
                             rx_len=327680, timeout_ms=5000)
            ln = int.from_bytes(p[16:20], "little") if len(p) >= 20 else -1
            print(f"   attempt {attempt}: len={len(p)} status={p[0] if p else '-'} "
                  f"transferType={p[1] if len(p) > 1 else '-'} chunk={ln}")
            if ln > 0:
                out = "still-chunk.bin"
                open(out, "wb").write(p[20:20 + ln])
                print(f"   *** DATA *** first bytes: {p[20:36].hex(' ')}")
                print(f"   wrote first chunk to {out}")
                print("   Tell whoever maintains this: 7A 05 02 WORKS. Record the")
                print("   state it was in, because nothing else about this matters.")
                return 0
            time.sleep(0.4)

        print("\n   download still refused after a completed capture.")
        print("   Next: copy the app's state - close the session, send the gallery")
        print("   notify (7A 00 02 with [9]=1), reopen, and try again. That")
        print("   variant lives in docs/FIRMWARE-RECOVERY.md's sibling notes in")
        print("   STATE.md, under 'How the official app retrieved a photo'.")
        return 3
    finally:
        try:
            t.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
