#!/usr/bin/env python3
"""
madv-web.py - live web demo for the MADV Mini 360 camera on Linux.

Runs a small HTTP server that pulls the camera's H.264 live view over USB,
transcodes it to MJPEG with ffmpeg, and serves:

    /                 demo page (live view + 360 look-around viewer)
    /stream.mjpg      multipart JPEG stream
    /frame.jpg        latest raw dual-fisheye frame
    /api/info         camera identity / status JSON
    /api/shutter      POST - trigger a still capture
    /api/snapshot.jpg latest frame as a download

Usage:
    ./.venv/bin/python tools/madv-web.py --port 8901
then open http://<this-host>:8901/
"""

import argparse
import json
import signal
import os
import shutil
import socket as _socket
import ssl
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import madv


# =======================================================================
# capture: USB -> ffmpeg -> latest MJPEG frame
# =======================================================================
class Capture:
    def __init__(self, scale_width=1920, quality=5, idle_timeout=60,
                 mode="mjpeg"):
        self.mode = mode              # 'mjpeg' | 'webcodecs' | 'fmp4'
        self.mp4_init = b""
        self._mp4_ready = False
        self.subs = []                # queues for raw H.264 relays
        self.subs_lock = threading.Lock()
        self.scale_width = scale_width
        self.idle_timeout = idle_timeout
        self.quality = quality
        self.latest = None            # latest JPEG bytes
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.frames = 0
        self.bytes_out = 0      # bytes actually written to viewers
        self.fps = 0.0
        self._fps_t0 = None
        self._fps_n = 0
        self.info = {}
        self.error = None
        self.running = True
        self._pending = None
        self.last_request = time.time()
        self.streaming = False
        self._last_frame_t = 0.0
        self._frame_dt = 0.033      # EMA of the gap between frames
        self.sleep_now = False
        self.latest_thumb = None
        self.last_shutter = None
        self.ff = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def subscribe(self):
        q = deque(maxlen=120)
        with self.subs_lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.subs_lock:
            if q in self.subs:
                self.subs.remove(q)

    def publish(self, au):
        with self.subs_lock:
            subs = list(self.subs)
        for q in subs:
            q.append(au)
        now = time.time()
        self.frames += 1
        if self._fps_t0 is None:
            self._fps_t0, self._fps_n = now, 0
        self._fps_n += 1
        if now - self._fps_t0 >= 2.0:
            self.fps = self._fps_n / (now - self._fps_t0)
            self._fps_t0, self._fps_n = now, 0

    # -- the ffmpeg encoder (H.264 in on stdin, JPEGs out on stdout) --
    def _spawn_ffmpeg(self):
        vf = f"scale={self.scale_width}:-2" if self.scale_width else None
        # NOTE: -vsync 0 is required (image2pipe mjpeg dedupes to ~2 frames
        # without it).  Do not use -probesize/-analyzeduration here: I join
        # the H.264 stream mid-flight and a tiny probe size makes ffmpeg
        # give up.  -err_detect ignore_err keeps it alive across the
        # occasional dropped access unit.
        # -r on the INPUT side gives ffmpeg a frame rate to synthesise
        # timestamps from. Raw H.264 on a pipe has none, and if the stream is
        # joined mid-GOP some ffmpeg builds then produce degenerate tfdt
        # (observed: stuck at ~2.0 s advancing 15 microseconds per fragment).
        base = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
                "-err_detect", "ignore_err", "-fflags", "+genpts",
                "-r", "30",
                "-f", "h264", "-i", "pipe:0", "-an"]
        if self.mode == "fmp4":
            # copy, don't transcode: fragmented MP4 that MediaSource can eat.
            # MSE does not need a secure context, unlike WebCodecs.
            # +default_base_moof is REQUIRED. Without it ffmpeg writes tfhd
            # with base-data-offset-present (flags 0x39) and an absolute
            # offset from the start of the stream, so any client that did not
            # receive the stream from byte 0 gets a fragment pointing ~64-100
            # MB past what it has, and MediaSource silently paints nothing.
            cmd = base + ["-c:v", "copy",
                          "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                          "-frag_duration", "200000",
                          "-f", "mp4", "pipe:1"]
        else:
            cmd = list(base)
            if vf:
                cmd += ["-vf", vf]
            cmd += ["-vsync", "0", "-c:v", "mjpeg", "-q:v", str(self.quality),
                    "-f", "image2pipe", "pipe:1"]
        self._fflog = open("/tmp/madv-ffmpeg.log", "ab", buffering=0)
        self.ff = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,
                                   stderr=self._fflog, bufsize=0)
        return self.ff

    def _read_jpegs(self, ff):
        """ffmpeg emits bare JPEGs back-to-back: split on SOI/EOI."""
        buf = b""
        while self.running:
            chunk = os.read(ff.stdout.fileno(), 65536)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                if start < 0:
                    buf = b""
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end < 0:
                    buf = buf[start:]
                    break
                jpg = buf[start:end + 2]
                buf = buf[end + 2:]
                now = time.time()
                with self.cond:
                    self.latest = jpg
                    self.frames += 1
                    if self._fps_t0 is None:
                        self._fps_t0, self._fps_n = now, 0
                    self._fps_n += 1
                    if now - self._fps_t0 >= 2.0:
                        self.fps = self._fps_n / (now - self._fps_t0)
                        self._fps_t0, self._fps_n = now, 0
                    self.cond.notify_all()

    def _read_mp4(self, ff):
        """Split ffmpeg's fMP4 output into init segment + whole fragments."""
        import struct as _s
        buf = bytearray()
        pending_moof = None
        while self.running:
            try:
                chunk = os.read(ff.stdout.fileno(), 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            pos = 0
            while True:
                if len(buf) - pos < 8:
                    break
                size = int.from_bytes(buf[pos:pos + 4], "big")
                typ = bytes(buf[pos + 4:pos + 8])
                if size == 1:
                    if len(buf) - pos < 16:
                        break
                    size = int.from_bytes(buf[pos + 8:pos + 16], "big")
                if size < 8 or pos + size > len(buf):
                    break
                box = bytes(buf[pos:pos + size])
                if typ == b"moof":
                    self._mp4_ready = True
                    pending_moof = box
                elif typ == b"mdat":
                    if pending_moof is not None:
                        self._publish_fragment(pending_moof + box)
                        pending_moof = None
                else:
                    if not self._mp4_ready:
                        self.mp4_init += box      # ftyp + moov
                pos += size
            del buf[:pos]

    @staticmethod
    def _fragment_samples(frag):
        """Frame count in a moof+mdat fragment, read from trun.sample_count."""
        i = frag.find(b"trun")
        if i < 0 or i + 12 > len(frag):
            return 0
        return int.from_bytes(frag[i + 8:i + 12], "big")

    def _publish_fragment(self, frag):
        with self.subs_lock:
            subs = list(self.subs)
        for q in subs:
            q.append(frag)
        # count frames, not fragments: a 200 ms fragment holds ~6 samples, and
        # an earlier guess of "5 fragments/sec" reported 2 or 10 fps on a
        # perfectly healthy stream
        now = time.time()
        n = self._fragment_samples(frag) or 0
        self.frames += n
        if self._fps_t0 is None:
            self._fps_t0, self._fps_n = now, 0
        self._fps_n += n
        if now - self._fps_t0 >= 2.0:
            self.fps = self._fps_n / (now - self._fps_t0)
            self._fps_t0, self._fps_n = now, 0

    def _run(self):
        while self.running:
            try:
                self._session()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:      # anything at all: keep retrying
                self.error = f"{type(e).__name__}: {e}"
                print(f"[capture] {self.error} -- retrying in 2s", flush=True)
                time.sleep(2)

    def _session(self):
        dev = madv.UsbTransport()
        cam = madv.Camera(dev)
        cam.open_session()
        info = cam.info(app_version="1.02.34")
        self.info = {"fw_version": info.get("fw_version"),
                     "hw_version": info.get("hw_version")}
        st = cam.start_stream(res=9)
        print(f"[capture] fw={self.info.get('fw_version')} start_stream={st}",
              flush=True)
        self.error = None
        ff = None
        if self.mode == "mjpeg":
            ff = self._spawn_ffmpeg()
            threading.Thread(target=self._read_jpegs, args=(ff,),
                             daemon=True).start()
        elif self.mode == "fmp4":
            ff = self._spawn_ffmpeg()
            threading.Thread(target=self._read_mp4, args=(ff,),
                             daemon=True).start()
            print("[capture] mode=fmp4: remuxing H.264 (copy), no re-encode",
                  flush=True)
        else:
            print("[capture] mode=webcodecs: relaying access units, "
                  "no transcoding", flush=True)

        t_fps = time.time()
        frames_at = 0
        self.last_request = time.time()
        self.streaming = True          # _session() already started the stream
        try:
            while self.running:
                if self._command(cam):
                    continue

                idle = time.time() - self.last_request

                if not self.streaming:
                    if self.idle_timeout and idle > self.idle_timeout:
                        time.sleep(0.2)
                        continue
                    print("[capture] waking", flush=True)
                    cam.open_session()
                    cam.start_stream(res=9)
                    self.streaming = True
                    frames_at = self.frames
                    t_fps = time.time()
                    continue

                # nobody looking for a while (or asked): stop the encoder so it cools down
                if self.sleep_now or (self.idle_timeout and idle > self.idle_timeout):
                    why = ("sleep requested" if self.sleep_now
                           else f"no viewer for {self.idle_timeout}s")
                    print(f"[capture] {why} - encoder off "
                          f"(session kept open)", flush=True)
                    try:
                        # stop live view ONLY.  close_session (7A 00 02) makes
                        # the camera power down and drop off the USB bus
                        # entirely - it then needs a physical replug, which is
                        # much worse than a warm camera.
                        cam.stop_liveview()
                    except Exception as e:
                        print(f"[capture] idle stop failed: {e}", flush=True)
                    self.streaming = False
                    self.sleep_now = False
                    continue

                try:
                    fr = cam.get_frame()
                except Exception:
                    fr = None            # transient; keep going
                if not (fr and fr["data"]):
                    # The camera answers "busy" most of the time. Polling flat
                    # out measured 368 calls/s for a 32 fps stream, at ~2.6 ms
                    # per call - that poll loop, not the data, was the CPU.
                    # Wait roughly half an observed frame interval instead.
                    time.sleep(max(0.004, min(0.020, self._frame_dt * 0.5)))
                if fr and fr["data"]:
                    now_f = time.time()
                    if self._last_frame_t:
                        dt = now_f - self._last_frame_t
                        if 0 < dt < 1.0:
                            self._frame_dt = self._frame_dt * 0.8 + dt * 0.2
                    self._last_frame_t = now_f
                    if ff is not None:
                        try:
                            ff.stdin.write(fr["data"])
                            ff.stdin.flush()
                        except (BrokenPipeError, OSError):
                            break
                    else:
                        self.publish(fr["data"])
                    # The next frame is roughly _frame_dt away. Waiting most of
                    # that here means the following poll lands on a frame
                    # instead of bouncing off "busy" first.
                    time.sleep(max(0.002, min(0.030, self._frame_dt * 0.6)))
                now = time.time()
                if now - t_fps >= 2.0:
                    self.fps = (self.frames - frames_at) / (now - t_fps)
                    frames_at = self.frames
                    t_fps = now
        finally:
            if ff is not None:
                try:
                    ff.kill()
                except Exception:
                    pass
            # Stop the encoder so it cools down, but DO NOT close the session.
            # 7A 00 02 powers the camera off and it leaves the USB bus, and this
            # finally runs on every teardown - including a service restart,
            # where systemd kills the ffmpeg child first and the resulting
            # broken stdin drops us straight through here. That turned every
            # `systemctl restart` into a trip to the desk to replug the camera.
            try:
                cam.stop_liveview()
            except Exception:
                pass
            dev.close()

    # -- one camera at a time: actions are serviced between frames --
    def _command(self, cam):
        with self.lock:
            job = self._pending
            self._pending = None
        if job is None:
            return False
        try:
            if job == "shutter":
                st = cam.take_picture()          # sends + polls to completion
                thumb = cam.get_thumbnail()
                if thumb:
                    with self.cond:
                        self.latest_thumb = thumb
                        self.cond.notify_all()
                self.last_shutter = {"status": st, "at": time.time()}
                cam.start_stream(res=9)          # re-arm the stream
        except Exception as e:
            self.error = f"command failed: {e}"
        return True

    def request(self, job):
        with self.lock:
            self._pending = job
        return True

    def wait_frame(self, timeout=5.0):
        with self.cond:
            if self.latest is None:
                self.cond.wait(timeout)
            return self.latest


# =======================================================================
# HTTP
# =======================================================================
PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>MADV Mini 360 - live on Linux</title>
<style>
 :root{--bg:#0b0d10;--fg:#e8eef5;--dim:#7d8896;--acc:#4fd1c5;--card:#12161d}
 *{box-sizing:border-box}
 html,body{height:100%}
 body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,sans-serif;
      display:flex;flex-direction:column;overflow:hidden;touch-action:none}
 header{padding:10px 14px;border-bottom:1px solid #1e2530;display:flex;gap:10px;align-items:center;flex-wrap:wrap;flex:0 0 auto}
 h1{font-size:14px;margin:0;font-weight:600;white-space:nowrap}
 .pill{font:11px/1 ui-monospace,monospace;padding:4px 8px;border-radius:99px;background:#1b222c;color:var(--dim)}
 .pill b{color:var(--acc);font-weight:600}
 .spacer{flex:1}
 button{background:#1d2733;color:var(--fg);border:1px solid #2b3746;padding:6px 12px;
        border-radius:8px;cursor:pointer;font-size:12px}
 button:hover{background:#25313f;border-color:#3a4a5c}
 button.on{background:#16343a;border-color:var(--acc);color:var(--acc)}
 #stage{position:relative;flex:1 1 auto;min-height:0;background:#000}
 #gl{width:100%;height:100%;display:block;cursor:grab;touch-action:none}
 #gl.drag{cursor:grabbing}
 #flat{width:100%;height:100%;object-fit:contain;display:none;background:#000}
 #hud{position:absolute;left:12px;bottom:12px;font:11px/1.4 ui-monospace,monospace;color:#9fb0c0;
      background:rgba(10,14,20,.55);padding:7px 10px;border-radius:8px;pointer-events:none}
 #err{color:#ff7b7b}
 #banner{position:absolute;left:50%;bottom:14px;transform:translateX(-50%);display:none;z-index:7;
         background:rgba(110,38,38,.92);color:#ffe9e9;padding:7px 14px;border-radius:8px;
         font:11px/1.4 ui-monospace,monospace;pointer-events:none;max-width:calc(100% - 20px);
         text-align:center}
 #stats{position:absolute;top:10px;right:10px;display:none;z-index:6;max-width:min(260px,calc(100% - 20px));
        background:rgba(8,12,18,.80);border:1px solid #1e2530;border-radius:10px;padding:10px 12px;
        font:11px/1.55 ui-monospace,monospace;color:#b9c6d4;pointer-events:none;
        -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
 #stats .row{display:flex;justify-content:space-between;gap:14px}
 #stats b{color:var(--acc);font-weight:600}
 #stats .lbl,#stats .g .lbl{color:#6f7d8c;font-size:9px;letter-spacing:.08em;text-transform:uppercase}
 #stats .row .lbl{color:#8b99a8;font-size:11px;letter-spacing:0;text-transform:none}
 #stats .g{margin-top:7px}
 #stats canvas{display:block;width:100%;height:26px;background:rgba(255,255,255,.03);border-radius:4px}
 #vid{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;
      opacity:0;pointer-events:none;background:#000}
 #veil{position:absolute;inset:0;display:none;align-items:center;justify-content:center;
       text-align:center;background:rgba(6,9,13,.86);color:#9fb0c0;
       font:13px/1.7 ui-monospace,monospace;cursor:pointer;z-index:5}
 .bar{display:flex;gap:10px;align-items:center;padding:8px 14px;border-top:1px solid #1e2530;
       flex:0 0 auto;flex-wrap:wrap}
 label.s{display:flex;gap:6px;align-items:center;color:var(--dim);font-size:12px}
 input[type=range]{width:110px;accent-color:var(--acc)}
 @media (max-width: 640px){
   header{padding:8px 10px;gap:6px}
   h1{display:none}                       /* the tabs already say what this is */
   .pill{font-size:10px;padding:3px 6px}
   /* one scrollable row instead of five: keeps the viewport for the picture */
   .bar{padding:6px 8px;gap:6px;flex-wrap:nowrap;overflow-x:auto;overflow-y:hidden;
        -webkit-overflow-scrolling:touch;scrollbar-width:none}
   .bar::-webkit-scrollbar{display:none}
   .bar > *{flex:0 0 auto}
   .bar button{padding:6px 10px;font-size:12px}
   label.s{font-size:11px}
   input[type=range]{width:84px}
   #hud{font-size:10px;padding:5px 7px;left:8px;bottom:8px;max-width:calc(100% - 16px)}
   #veil{font-size:12px}
 }
 @media (max-width: 380px){
   .pill{font-size:9px}
   .bar button{padding:5px 8px;font-size:11px}
 }
</style></head><body>

<header>
  <h1>MADV Mini 360</h1>
  <span class="pill">fw <b id="fw">-</b></span>
  <span class="pill">hw <b id="hw">-</b></span>
  <span class="pill">fps <b id="fps">-</b></span>
  <span class="pill" id="err" style="display:none"></span>
  <span class="spacer"></span>
  <button id="tab_360" class="on">360 look-around</button>
  <button id="tab_flat">flat panorama</button>
</header>

<div id="stage">
  <canvas id="gl"></canvas>
  <img id="flat" alt="panorama">
  <canvas id="flatwc" style="display:none;width:100%;height:100%;object-fit:contain;background:#000"></canvas>
  <canvas id="wc" style="display:none"></canvas>
  <video id="vid" muted playsinline></video>
  <div id="veil"><div>camera asleep &mdash; encoder off<br><span style="opacity:.7">click anywhere to wake</span></div></div>
  <div id="stats">
    <div class="row"><span class="lbl">stream</span><b id="s_mode">-</b></div>
    <div class="row"><span>codec</span><b id="s_codec">-</b></div>
    <div class="row"><span>server fps</span><b id="s_sfps">-</b></div>
    <div class="row"><span>frames</span><b id="s_frames">-</b></div>
    <div class="row"><span>bitrate</span><b id="s_rate">-</b></div>
    <div class="row"><span>video</span><b id="s_res">-</b></div>
    <div class="row"><span>readyState</span><b id="s_rs">-</b></div>
    <div class="row"><span>buffered</span><b id="s_buf">-</b></div>
    <div class="row"><span>dropped</span><b id="s_drop">-</b></div>
    <div class="row"><span>sessions</span><b id="s_gen">-</b></div>
    <div class="row"><span class="lbl">server</span><b>-</b></div>
    <div class="row"><span>subscribers</span><b id="s_subs">-</b></div>
    <div class="row"><span>streaming</span><b id="s_strm">-</b></div>
    <div class="row"><span>canvas</span><b id="s_cv">-</b></div>
    <div class="g"><div class="lbl">fps</div><canvas id="g_fps" width="240" height="26"></canvas></div>
    <div class="g"><div class="lbl">mbit/s</div><canvas id="g_mb" width="240" height="26"></canvas></div>
  </div>
  <div id="banner"></div>
  <div id="hud"><span id="diag" style="display:block;opacity:.65;margin-bottom:3px"></span>drag or arrow-key to look &middot; scroll / pinch / +- to zoom &middot; double-click to reset</div>
</div>

<div class="bar">
  <button id="shutter" title="capture writes into the camera; full-resolution download is not implemented, so this cannot give you a usable photo yet">Take picture</button>
  <button id="snap">Save panorama</button>
  <button id="saveview">Save view</button>
  <button id="fs">Fullscreen</button>
  <button id="sleep">Sleep camera</button>
  <button id="statsBtn">Stats</button>
  <button id="spin">Auto-rotate</button>
  <button id="reset">Reset view</button>
  <button id="inv">Invert Y</button>
  <span class="spacer"></span>
  <label class="s">zoom <input id="fov" type="range" min="30" max="120" value="75"></label>
</div>

<script>
const $ = id => document.getElementById(id);
let mode = '360';

$('tab_360').onclick = () => { mode='360'; $('tab_360').classList.add('on'); $('tab_flat').classList.remove('on');
  $('gl').style.display='block';
  $('flat').style.display='none';
  $('flatwc').style.display='none';
  // #vid is full-bleed over the canvas in fmp4 mode; leaving it at opacity 1
  // made the 360 view look unreachable because it was painting on top of it
  $('vid').style.opacity='0'; };
$('tab_flat').onclick = () => { mode='flat'; $('tab_flat').classList.add('on'); $('tab_360').classList.remove('on');
  $('gl').style.display='none';
  $('flat').style.display   = (srcMode==='mjpeg')?'block':'none';
  $('flatwc').style.display = (srcMode==='webcodecs')?'block':'none';
  $('vid').style.opacity    = (srcMode==='fmp4')?'1':'0';
  if(srcMode==='webcodecs'){ flatwc.width=wcCanvas.width||1; flatwc.height=wcCanvas.height||1; } };

$('shutter').onclick = async () => {
  const b=$('shutter');
  if(b.disabled) return;
  b.disabled = true; b.textContent = 'Capturing...';
  try { await fetch('/api/shutter', {method:'POST'}); } catch(e){}
  // capture resets the camera on marginal power, so give it a cooldown rather
  // than letting it be pressed repeatedly while the link is down
  setTimeout(()=>{ b.textContent='Take picture'; b.disabled=false; }, 15000);
};
$('snap').onclick = () => { window.location = '/api/snapshot.jpg?t=' + Date.now(); };
// save the current rectilinear view, not the raw equirect - this is what people
// actually want to keep ("the thing I was looking at")
$('saveview').onclick = () => {
  try{
    const a=document.createElement('a');
    a.href=canvas.toDataURL('image/jpeg',0.92);
    a.download='madv-view-'+new Date().toISOString().replace(/[:.]/g,'-')+'.jpg';
    a.click();
  }catch(e){ console.warn('save view failed', e); }
};
$('fs').onclick = () => {
  if(document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen();
};
$('reset').onclick = () => { yaw=Math.PI; pitch=0; fovT=75; velYaw=0; velPitch=0; $('fov').value=75; };
let invertY=false;
try{ invertY = localStorage.getItem('madv-invertY')==='1'; }catch(e){}
function syncInv(){ $('inv').classList.toggle('on', invertY); }
$('inv').onclick = () => { invertY=!invertY;
  try{ localStorage.setItem('madv-invertY', invertY?'1':'0'); }catch(e){}
  syncInv(); };
syncInv();
let spin=false;

// ---- sleep / wake ----
// Stopping the stream client-side first is essential: the server treats any
// /frame.jpg or /stream.mjpg traffic as a viewer and would wake the camera
// straight back up.
let asleep = false;
let veilMode = null;          // null | 'asleep' | 'paused'
function showVeil(m, html){
  if(m === 'paused' && asleep) return;   // never stomp the sleep veil
  veilMode = m;
  $('veil').firstElementChild.innerHTML = html;
  $('veil').style.display = 'flex';
}
function hideVeil(onlyIf){
  if(onlyIf && veilMode !== onlyIf) return;
  veilMode = null;
  $('veil').style.display = 'none';
}
function setAsleep(v){
  asleep = v;
  $('sleep').textContent = v ? 'Wake camera' : 'Sleep camera';
  $('sleep').classList.toggle('on', v);
  if(v) showVeil('asleep', 'camera asleep &mdash; encoder off<br>'
                + '<span style="opacity:.7">click anywhere to wake</span>');
  else hideVeil('asleep');
}
async function goSleep(){
  setAsleep(true);
  $('flat').removeAttribute('src');          // drop the mjpeg connection
  if(h264Abort){ h264Abort.abort(); h264Abort=null; }
  if(fmp4Abort){ fmp4Abort.abort(); fmp4Abort=null; }
  if(decoder){ try{ decoder.close(); }catch(e){} decoder=null; }
  try{ vid.pause(); vid.removeAttribute('src'); vid.load(); }catch(e){}
  try{ await fetch('/api/sleep', {method:'POST'}); }catch(e){}
}
async function wakeUp(){
  setAsleep(false);
  try{ await fetch('/frame.jpg?t='+Date.now()); }catch(e){}   // triggers the wake
  if(srcMode==='mjpeg'){ $('flat').src='/stream.mjpg'; grab(); }
  else { initStream().catch(()=>{}); }
}
$('sleep').onclick = () => { asleep ? wakeUp() : goSleep(); };
$('veil').onclick  = () => {
  if(asleep){ wakeUp(); return; }
  hideVeil('paused');                 // don't leave a dead overlay behind
  vid.play().catch(()=>{});
};
$('spin').onclick = () => { spin=!spin; $('spin').classList.toggle('on', spin); };

// ---------- status ----------
let LAST=null, lastBytes=0, lastBytesT=0, CUR_MBPS=0;
async function poll(){
  try{
    const j = await (await fetch('/api/info')).json();
    $('fw').textContent=j.fw_version||'-'; $('hw').textContent=j.hw_version||'-';
    $('fps').textContent=(j.fps||0).toFixed(0);
    if(j.error){ $('err').style.display=''; $('err').textContent=j.error; } else $('err').style.display='none';
    // bitrate from bytes the server actually wrote, not an estimate
    const now=performance.now(), b=j.bytes_out||0;
    if(lastBytesT && now>lastBytesT){
      CUR_MBPS=(b-lastBytes)*8/((now-lastBytesT)/1000)/1e6;
      pushHist(histMb, CUR_MBPS);
    }
    lastBytes=b; lastBytesT=now; LAST=j;
    pushHist(histFps, j.fps||0);
  }catch(e){}
}

// ---------- geek stats overplain ----------
let statsOn=false;
const HMAX=60, histFps=[], histMb=[];
function pushHist(a,v){ a.push(v); if(a.length>HMAX) a.shift(); }
function spark(cv, arr, stroke, fill){
  const c=cv.getContext('2d'), w=cv.width, h=cv.height;
  c.clearRect(0,0,w,h);
  if(arr.length<2) return;
  const mx=Math.max(1e-6, ...arr);
  c.beginPath();
  for(let i=0;i<arr.length;i++){
    const x=(i/(HMAX-1))*w, y=h-1-(arr[i]/mx)*(h-4);
    i?c.lineTo(x,y):c.moveTo(x,y);
  }
  c.strokeStyle=stroke; c.lineWidth=1.5; c.stroke();
  c.lineTo(((arr.length-1)/(HMAX-1))*w, h); c.lineTo(0,h); c.closePath();
  c.fillStyle=fill; c.fill();
}
function fmtN(v,d){ return (v===null||v===undefined||isNaN(v))?'-':Number(v).toFixed(d); }

$('statsBtn').onclick = () => {
  statsOn = !statsOn;
  $('stats').style.display = statsOn ? 'block' : 'none';
  $('statsBtn').classList.toggle('on', statsOn);
};
window.addEventListener('keydown', e => {
  if((e.key==='s'||e.key==='S') && !e.metaKey && !e.ctrlKey) $('statsBtn').click();
});

function statsTick(){
  if(!statsOn) return;
  const j = LAST;
  if(j){
    $('s_mode').textContent   = j.mode || '-';
    $('s_codec').textContent  = j.codec || '-';
    $('s_sfps').textContent   = fmtN(j.fps,1);
    $('s_frames').textContent = (j.frames ?? '-');
    $('s_rate').textContent   = fmtN(CUR_MBPS,2) + ' Mbit/s';
  }
  $('s_res').textContent = vid.videoWidth ? vid.videoWidth+'\u00d7'+vid.videoHeight : '-';
  $('s_rs').textContent  = vid.readyState + (vid.paused ? ' paused' : ' playing');
  if(vid.buffered.length){
    const s0=vid.buffered.start(0), e0=vid.buffered.end(vid.buffered.length-1);
    $('s_buf').textContent = (e0-s0).toFixed(1)+'s  '+s0.toFixed(1)+'\u2192'+e0.toFixed(1);
  } else $('s_buf').textContent = 'empty';
  const q = vid.getVideoPlaybackQuality ? vid.getVideoPlaybackQuality() : null;
  $('s_drop').textContent = q ? (q.droppedVideoFrames+' / '+q.totalVideoFrames) : '-';
  $('s_gen').textContent  = fmp4Gen;
  if(j){
    $('s_subs').textContent = (j.subscribers ?? '-');
    $('s_strm').textContent = j.streaming ? 'yes' : 'idle';
  }
  $('s_cv').textContent   = canvas.width+'\u00d7'+canvas.height+' @'+window.devicePixelRatio;
  spark($('g_fps'), histFps, '#4fd1c5', 'rgba(79,209,197,.13)');
  spark($('g_mb'),  histMb,  '#ffb86b', 'rgba(255,184,107,.13)');
}
setInterval(statsTick, 700);

setInterval(() => {
  const stale = (performance.now() - lastFrameAt) > 2500;
  const b = $('banner');
  if(stale && !asleep){
    b.textContent = 'camera not sending frames — reconnecting…';
    b.style.display = 'block';
  } else {
    b.style.display = 'none';
  }
}, 500);
setInterval(poll,1000); poll();

// ---------- equirectangular panorama viewer ----------
const canvas=$('gl');
const gl=canvas.getContext('webgl',{antialias:false,preserveDrawingBuffer:true});
const VS = `attribute vec2 a; varying vec2 vuv;
void main(){ vuv=a*0.5+0.5; gl_Position=vec4(a,0.0,1.0); }`;
const FS = `
precision highp float;
varying vec2 vuv;
uniform sampler2D tex;
uniform vec2 res;
uniform float yaw, pitch, fovYv;
const float TAU = 6.28318530718;
const float PI  = 3.14159265359;
void main(){
  vec2 p = vuv*2.0 - 1.0;          // -1..1 both axes
  float aspect = res.x / res.y;
  // fovYv is the FOV of the WIDER axis (horizontal on landscape screens).
  // Taking it as vertical on a 2.5:1 canvas produced a ~125 deg horizontal
  // FOV, which is what made everything smear at the edges.
  float tx = tan(fovYv*0.5);
  float ty = tx / aspect;
  if (aspect < 1.0) { ty = tx; tx = ty * aspect; }
  // BUGFIX 1: this ray must be normalised. atan2 is scale-invariant so the
  // longitude survived, but asin(d2.y) does not - an unnormalised ray made the
  // clamp bite at the edges and stretched the panorama vertically off-centre.
  vec3 d = normalize(vec3(p.x*tx, p.y*ty, 1.0));

  // BUGFIX 2: pitch about the camera's LOCAL X first, THEN yaw about world Y.
  // The old order (yaw then pitch-about-world-X) meant that at yaw=180 the
  // vertical axis flipped - drag down moved the scene up - and at yaw=90 pitch
  // degenerated into roll, so the view circled instead of tilting.
  float cp=cos(pitch), sp=sin(pitch);
  vec3 d1 = vec3(d.x,  cp*d.y - sp*d.z, sp*d.y + cp*d.z);
  float cy=cos(yaw),  sy=sin(yaw);
  vec3 d2 = vec3(cy*d1.x + sy*d1.z, d1.y, -sy*d1.x + cy*d1.z);

  float u = fract(atan(d2.x, d2.z)/TAU + 0.5);
  float v = 0.5 - asin(clamp(d2.y,-1.0,1.0))/PI;
  gl_FragColor = vec4(texture2D(tex, vec2(u, v)).rgb, 1.0);
}`;

function sh(t,src){ const s=gl.createShader(t); gl.shaderSource(s,src); gl.compileShader(s);
  if(!gl.getShaderParameter(s,gl.COMPILE_STATUS)) console.error(gl.getShaderInfoLog(s)); return s; }
const prog=gl.createProgram();
gl.attachShader(prog, sh(gl.VERTEX_SHADER,VS));
gl.attachShader(prog, sh(gl.FRAGMENT_SHADER,FS));
gl.linkProgram(prog);
if(!gl.getProgramParameter(prog,gl.LINK_STATUS)) console.error(gl.getProgramInfoLog(prog));
gl.useProgram(prog);

const qb=gl.createBuffer();
gl.bindBuffer(gl.ARRAY_BUFFER,qb);
gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,1,1]),gl.STATIC_DRAW);
const aLoc=gl.getAttribLocation(prog,'a');
gl.enableVertexAttribArray(aLoc);
gl.vertexAttribPointer(aLoc,2,gl.FLOAT,false,0,0);

gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
const tex=gl.createTexture();
gl.bindTexture(gl.TEXTURE_2D,tex);
gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);
gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);
gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);
gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
gl.texImage2D(gl.TEXTURE_2D,0,gl.RGB,1,1,0,gl.RGB,gl.UNSIGNED_BYTE,new Uint8Array([0,0,0]));

const U=n=>gl.getUniformLocation(prog,n);
const uYaw=U('yaw'), uPitch=U('pitch'), uFov=U('fovYv'), uRes=U('res');

let yaw=Math.PI, pitch=0, fovT=75;
const VSENS = 1.9;      // vertical drag boost: 1:1 is far too slow on a wide canvas
const PITCH_MAX = 1.55; // ~89 deg, so the nadir is actually reachable
let velYaw=0, velPitch=0;
let dragging=false, lx=0, ly=0, lmove=0, pinch=null;

// radians per pixel such that dragging one canvas-height == one vertical FOV
function dragScale(){
  // radians per pixel on each axis, from the on-screen FOV of that axis
  const w = canvas.clientWidth || 960, h = canvas.clientHeight || 480;
  const tx = Math.tan(fovT*Math.PI/360);
  let ty = tx / (w/h);
  if (w < h) { ty = tx; }
  const fovX = 2*Math.atan(tx), fovY = 2*Math.atan(ty);
  return { kx: fovX/w, ky: (fovY/h)*VSENS };
}

canvas.addEventListener('pointerdown', e=>{
  dragging=true; lx=e.clientX; ly=e.clientY; lmove=performance.now();
  velYaw=0; velPitch=0;
  canvas.classList.add('drag'); canvas.setPointerCapture(e.pointerId);
});
canvas.addEventListener('pointerup', ()=>{ dragging=false; canvas.classList.remove('drag'); });
canvas.addEventListener('pointercancel', ()=>{ dragging=false; canvas.classList.remove('drag'); });
canvas.addEventListener('pointermove', e=>{
  if(!dragging) return;
  const now = performance.now();
  const dts = Math.max(8, now - lmove) / 1000;   // seconds
  lmove = now;
  const dx=e.clientX-lx, dy=e.clientY-ly; lx=e.clientX; ly=e.clientY;
  const {kx, ky} = dragScale();
  const sy = invertY ? -1 : 1;          // invert-Y: drag up to look down
  yaw   -= dx*kx;
  pitch += dy*ky*sy;
  pitch = Math.max(-PITCH_MAX, Math.min(PITCH_MAX, pitch));
  velYaw   = (-dx*kx)/dts;      // radians per second
  velPitch = ( dy*ky*sy)/dts;
});
canvas.addEventListener('wheel', e=>{ e.preventDefault();
  fovT = Math.max(30, Math.min(120, fovT + (e.deltaY>0?4:-4)));
  $('fov').value = fovT;
},{passive:false});
window.addEventListener('keydown', e=>{
  const step = 0.09;
  if(e.key==='ArrowLeft'){ yaw += step; velYaw=velPitch=0; e.preventDefault(); }
  else if(e.key==='ArrowRight'){ yaw -= step; velYaw=velPitch=0; e.preventDefault(); }
  else if(e.key==='ArrowUp'){ pitch = Math.max(-PITCH_MAX, pitch-step); velYaw=velPitch=0; e.preventDefault(); }
  else if(e.key==='ArrowDown'){ pitch = Math.min(PITCH_MAX, pitch+step); velYaw=velPitch=0; e.preventDefault(); }
  else if(e.key==='+'||e.key==='='){ fovT=Math.max(30,fovT-4); $('fov').value=fovT; }
  else if(e.key==='-'){ fovT=Math.min(120,fovT+4); $('fov').value=fovT; }
});
canvas.addEventListener('dblclick', ()=>{ yaw=Math.PI; pitch=0; velYaw=0; velPitch=0; fovT=75; $('fov').value=75; });
$('fov').oninput = e => { fovT = +e.target.value; };

// pinch zoom
canvas.addEventListener('touchmove', e=>{
  if(e.touches.length!==2) return;
  e.preventDefault();
  const d=Math.hypot(e.touches[0].clientX-e.touches[1].clientX, e.touches[0].clientY-e.touches[1].clientY);
  if(pinch!=null){ fovT = Math.max(30, Math.min(120, fovT - (d-pinch)*0.12)); $('fov').value=fovT; }
  pinch=d;
},{passive:false});
canvas.addEventListener('touchend', ()=>{ pinch=null; });

const img=new Image();
let ready=false, busy=false, srcMode='mjpeg', frames=0;
img.onload = ()=>{ ready=true; busy=false; noteFrame(); };
img.onerror = ()=>{ busy=false; };
function grab(){ if(asleep||srcMode!=='mjpeg'||mode!=='360'||busy) return; busy=true; img.src='/frame.jpg?t='+Date.now(); }

// ---- WebCodecs path: the camera already sends H.264, so let the browser decode it ----
const wcCanvas=document.getElementById('wc');
const wcCtx=wcCanvas.getContext('2d',{willReadFrequently:false});
const flatwc=document.getElementById('flatwc');
const flatwcCtx=flatwc.getContext('2d');
let decoder=null, CODEC='avc1.640028', h264Abort=null;

function isKeyFrame(au){
  for(let i=0;i+4<au.length;i++){
    if(au[i]===0&&au[i+1]===0&&au[i+2]===0&&au[i+3]===1){
      if((au[i+4]&0x1f)===5) return true; i+=4;
    } else if(au[i]===0&&au[i+1]===0&&au[i+2]===1){
      if((au[i+3]&0x1f)===5) return true; i+=3;
    }
  }
  return false;
}

function unsupported(msg){
  $('veil').style.display='flex';
  $('veil').firstElementChild.innerHTML =
    msg + '<br><span style="opacity:.7">restart the server with --mode fmp4 or --mode mjpeg</span>';
}

const vid = document.getElementById('vid');
let fmp4Abort = null;

let fmp4Gen = 0;
function stopFmp4(){
  fmp4Gen++;
  if(fmp4Abort){ try{ fmp4Abort.abort(); }catch(e){} fmp4Abort = null; }
}

async function fmp4Session(myGen){
  // One MediaSource + one HTTP stream, resolved when the stream ends.
  return new Promise((resolve) => {
    const ms = new MediaSource();
    const url = URL.createObjectURL(ms);
    let done = false;
    const finish = () => {
      if(done) return;
      done = true;
      try{ URL.revokeObjectURL(url); }catch(e){}
      resolve();
    };
    vid.src = url;
    ms.addEventListener('sourceopen', async () => {
      let sb;
      try{ sb = ms.addSourceBuffer('video/mp4; codecs="' + CODEC + '"'); }
      catch(e){ unsupported('this browser cannot play ' + CODEC); finish(); return; }
      const queue = [];
      let busy = false;
      const pump = () => {
        if(busy || !queue.length || sb.updating) return;
        busy = true;
        try{ sb.appendBuffer(queue.shift()); }
        catch(e){ busy = false; queue.length = 0; }
      };
      sb.addEventListener('updateend', () => {
        busy = false;
        if(myGen !== fmp4Gen) return;              // stale session
        // Only claim readiness once there is real media.  The first updateend
        // follows the INIT segment, when buffered.length is 0 and readyState
        // is 0 - calling play() there rejects, and the old code swallowed that
        // and never tried again, leaving a decoded but paused element.
        if(vid.buffered.length){
          if(vid.currentTime < vid.buffered.start(0)){
            vid.currentTime = vid.buffered.start(0);   // late join
          }
          ready = true;
          if(vid.paused) vid.play().catch(()=>{});
        }
        pump();
      });
      fmp4Abort = new AbortController();
      const mine = fmp4Abort;
      try{
        const res = await fetch('/stream.mp4', {signal: mine.signal});
        const reader = res.body.getReader();
        let buf = new Uint8Array(0);
        while(true){
          const {done: d, value} = await reader.read();
          if(d) break;
          if(myGen !== fmp4Gen) break;
          const nb = new Uint8Array(buf.length + value.length);
          nb.set(buf); nb.set(value, buf.length); buf = nb;
          while(buf.length >= 4){
            const len = new DataView(buf.buffer, buf.byteOffset).getUint32(0, true);
            if(buf.length < 4 + len) break;
            queue.push(buf.slice(4, 4+len));
            buf = buf.slice(4+len);
            pump();
          }
        }
      }catch(e){ /* aborted */ }
      finish();
    });
  });
}

async function startFmp4(){
  // Only ever one chain of sessions. Re-entering here (wake, tab switch) used
  // to stack loops and leave sockets that nothing could abort.
  stopFmp4();
  const myGen = fmp4Gen;
  while(!asleep && myGen === fmp4Gen){
    await fmp4Session(myGen);
    if(asleep || myGen !== fmp4Gen) break;
    await new Promise(r => setTimeout(r, 1000));
  }
}

async function initStream(){
  let j = {};
  try{ j = await (await fetch('/api/info')).json(); }catch(e){}
  srcMode = j.mode || 'mjpeg';
  CODEC = j.codec || CODEC;
  if(srcMode==='mjpeg'){
    // only the mjpeg path needs the multipart stream.  Opening it in another
    // mode used to leave a server thread blocked on frames that never come.
    if(!asleep) $('flat').src = '/stream.mjpg';
    return;
  }
  if(srcMode==='fmp4'){
    startFmp4();
    // some browsers refuse autoplay; offer a tap rather than a frozen frame
    clearInterval(window._playWatch);
    window._playWatch = setInterval(() => {
      if(asleep || srcMode!=='fmp4') return;
      if(vid.buffered.length && vid.paused){
        if(veilMode !== 'paused') showVeil('paused',
          'video is paused<br><span style="opacity:.7">tap to start</span>');
      } else {
        hideVeil('paused');             // playing again -> get out of the way
      }
    }, 1000);
    return;
  }
  if(!('VideoDecoder' in window)){ unsupported('this browser has no WebCodecs'); return; }
  decoder=new VideoDecoder({
    output: f => {
      if(wcCanvas.width!==f.displayWidth){ wcCanvas.width=f.displayWidth; wcCanvas.height=f.displayHeight; }
      wcCtx.drawImage(f,0,0);
      ready=true;
      noteFrame();
      if(mode==='flat'){ flatwc.width=wcCanvas.width; flatwc.height=wcCanvas.height;
                         flatwcCtx.drawImage(wcCanvas,0,0); }
      f.close();
    },
    error: e => console.error('VideoDecoder error', e)
  });
  decoder.configure({codec:CODEC, avc:{format:'annexb'}, optimizeForLatency:true});
  pumpH264();
}

async function pumpH264(){
  if(h264Abort) h264Abort.abort();
  h264Abort = new AbortController();
  let res;
  try{ res = await fetch('/stream.h264', {signal:h264Abort.signal}); }
  catch(e){ return; }
  const reader=res.body.getReader();
  let buf=new Uint8Array(0), ts=0;
  while(true){
    let chunk;
    try{ chunk = await reader.read(); }catch(e){ break; }
    const {done,value} = chunk;
    if(done) break;
    const nb=new Uint8Array(buf.length+value.length); nb.set(buf); nb.set(value,buf.length); buf=nb;
    while(buf.length>=4){
      const len=new DataView(buf.buffer,buf.byteOffset).getUint32(0,true);
      if(buf.length < 4+len) break;
      const au=buf.slice(4,4+len);
      buf=buf.slice(4+len);
      try{
        decoder.decode(new EncodedVideoChunk({
          type: isKeyFrame(au)?'key':'delta', timestamp:(ts+=33333), data:au }));
      }catch(e){ /* not configured yet - drop */ }
    }
  }
}

let last=performance.now(), lastDiag=0;
let lastFrameAt=performance.now(), lastVidT=-1;
function noteFrame(){ lastFrameAt=performance.now(); }
function draw(now){
  requestAnimationFrame(draw);
  const dt=Math.min(0.05,(now-last)/1000); last=now;
  if(!dragging){
    if(velYaw || velPitch){
      yaw   += velYaw*dt;
      pitch += velPitch*dt;
      const decay = Math.pow(0.02, dt);      // ~0.25 s to almost stop
      velYaw *= decay; velPitch *= decay;
      if(Math.abs(velYaw)   < 0.02) velYaw = 0;
      if(Math.abs(velPitch) < 0.02) velPitch = 0;
      pitch=Math.max(-PITCH_MAX,Math.min(PITCH_MAX,pitch));
    }
    if(spin) yaw += dt*0.25;
  }
  if(srcMode==='fmp4' && vid.currentTime !== lastVidT){
    lastVidT = vid.currentTime; noteFrame();
  }
  if(!asleep) grab();           // keep fetching even before the first frame lands
  if(now - lastDiag > 500){
    lastDiag = now;
    $('diag').textContent = (srcMode==='fmp4')
      ? 'fmp4 paused=' + vid.paused + ' readyState=' + vid.readyState +
        ' t=' + vid.currentTime.toFixed(1) + ' buffered=' + vid.buffered.length +
        (vid.buffered.length ? ' [' + vid.buffered.start(0).toFixed(1) + '-' +
                               vid.buffered.end(0).toFixed(1) + ']' : '')
      : srcMode + ' frames=' + frames;
  }
  if(mode!=='360'||!ready) return;
  const w=canvas.clientWidth||960, h=canvas.clientHeight||480;
  if(canvas.width!==w||canvas.height!==h){ canvas.width=w; canvas.height=h; }
  gl.viewport(0,0,w,h);
  gl.bindTexture(gl.TEXTURE_2D,tex);
  const src = (srcMode==='webcodecs') ? wcCanvas
            : (srcMode==='fmp4')      ? vid
            : img;
  if(srcMode==='fmp4' && vid.readyState < 2) return;
  gl.texImage2D(gl.TEXTURE_2D,0,gl.RGB,gl.RGB,gl.UNSIGNED_BYTE,src);
  gl.uniform2f(uRes,w,h);
  gl.uniform1f(uYaw,yaw);
  gl.uniform1f(uPitch,pitch);
  gl.uniform1f(uFov,fovT*Math.PI/180);
  gl.drawArrays(gl.TRIANGLE_STRIP,0,4);
}
requestAnimationFrame(draw);
initStream().catch(e=>console.warn('stream init failed', e));
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    capture = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        cap = self.capture

        if path in ("/", "/index.html"):
            return self._send(200, "text/html; charset=utf-8", PAGE.encode())

        if path == "/api/info":
            body = json.dumps({
                "fw_version": cap.info.get("fw_version"),
                "hw_version": cap.info.get("hw_version"),
                "fps": round(cap.fps, 1),
                "mode": cap.mode,
                "bytes_out": cap.bytes_out,
                "idle_timeout": cap.idle_timeout,
                "streaming": cap.streaming,
                "subscribers": len(cap.subs),
                "codec": "avc1.640028",
                "frames": cap.frames,
                "error": cap.error,
            }).encode()
            return self._send(200, "application/json", body)

        if path == "/frame.jpg":
            cap.last_request = time.time()
            jpg = cap.wait_frame(3.0)
            if not jpg:
                return self._send(503, "text/plain", b"no frame yet")
            return self._send(200, "image/jpeg", jpg)

        if path == "/api/snapshot.jpg":
            cap.last_request = time.time()
            jpg = cap.wait_frame(3.0) or b""
            return self._send(200, "image/jpeg", jpg,
                              {"Content-Disposition": 'attachment; filename="madv-frame.jpg"'})

        if path == "/stream.mjpg":
            cap.last_request = time.time()
            # HTTP/1.1 streaming needs explicit chunked framing, otherwise
            # clients sit waiting for a Content-Length that never comes.
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def chunk(data):
                self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                cap.bytes_out += len(data)

            print(f"[stream] client attached, latest={'yes' if cap.latest else 'no'}",
                  flush=True)
            sent = 0
            try:
                last = None
                last_send = time.time()
                while True:
                    with cap.cond:
                        if cap.latest is last:
                            cap.cond.wait(2.0)
                        jpg = cap.latest
                    if jpg is None or jpg is last:
                        if not cap.streaming and time.time() - last_send > 5:
                            return
                        continue
                    last = jpg
                    sent += 1
                    if sent == 1 or sent % 60 == 0:
                        print(f"[stream] sent {sent} frames ({len(jpg)} B)",
                              flush=True)
                    chunk(b"--frame\r\nContent-Type: image/jpeg\r\n"
                          b"Content-Length: " + str(len(jpg)).encode() +
                          b"\r\n\r\n" + jpg + b"\r\n")
                    self.wfile.flush()
                    cap.last_request = time.time()
                    last_send = time.time()
            except (BrokenPipeError, ConnectionResetError):
                return
        if path == "/stream.mp4":
            cap.last_request = time.time()
            q = cap.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def chunk(data):
                self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                cap.bytes_out += len(data)

            try:
                import struct as _s
                sent_init = False
                last_send = time.time()
                while True:
                    if not sent_init and cap.mp4_init:
                        chunk(_s.pack("<I", len(cap.mp4_init)) + cap.mp4_init)
                        self.wfile.flush()
                        sent_init = True
                        cap.last_request = time.time()
                        last_send = time.time()
                    if not q:
                        time.sleep(0.004)
                        if not cap.running:
                            break
                        # The encoder was stopped (idle). Sit here forever and
                        # the browser never sees EOF and never reconnects - that
                        # is how one client ended up with three zombie sockets.
                        if not cap.streaming and time.time() - last_send > 5:
                            break
                        continue
                    frag = q.popleft()
                    chunk(_s.pack("<I", len(frag)) + frag)
                    self.wfile.flush()
                    # delivering a fragment IS a viewer. Refreshing only when
                    # the request starts made every stream die at exactly
                    # --idle-timeout seconds.
                    cap.last_request = time.time()
                    last_send = time.time()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                cap.unsubscribe(q)
            return

        if path == "/live.mp4":
            # Standard-consumable fragmented MP4: init segment then fragments,
            # verbatim, no framing. /stream.mp4 is length-prefixed because the
            # page needs to split records; anything else - ffmpeg, VLC, OBS, a
            # <video src=...> - wants the plain stream.
            cap.last_request = time.time()
            q = cap.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def chunk(data):
                self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                cap.bytes_out += len(data)

            try:
                sent_init = False
                last_send = time.time()
                while True:
                    if not sent_init and cap.mp4_init:
                        chunk(cap.mp4_init); self.wfile.flush()
                        sent_init = True
                        cap.last_request = time.time(); last_send = time.time()
                    if not q:
                        time.sleep(0.004)
                        if not cap.running:
                            break
                        if not cap.streaming and time.time() - last_send > 5:
                            break
                        continue
                    frag = q.popleft()
                    chunk(frag); self.wfile.flush()
                    cap.last_request = time.time(); last_send = time.time()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                cap.unsubscribe(q)
            return

        if path == "/stream.h264":
            cap.last_request = time.time()
            q = cap.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def chunk(data):
                self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                cap.bytes_out += len(data)

            try:
                import struct as _s
                last_send = time.time()
                while True:
                    if not q:
                        time.sleep(0.004)
                        if not cap.running:
                            break
                        if not cap.streaming and time.time() - last_send > 5:
                            break
                        continue
                    au = q.popleft()
                    chunk(_s.pack("<I", len(au)) + au)
                    self.wfile.flush()
                    cap.last_request = time.time()
                    last_send = time.time()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                cap.unsubscribe(q)
            return

        if path == "/thumb.jpg":
            t = cap.latest_thumb
            if not t:
                return self._send(404, "text/plain", b"no thumbnail yet")
            return self._send(200, "image/jpeg", t)

        return self._send(404, "text/plain", b"not found")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/sleep":
            self.capture.sleep_now = True    # one-shot; does not touch the config
            return self._send(200, "application/json", b'{"ok":true}')
        if path == "/api/shutter":
            self.capture.last_request = time.time()
            self.capture.request("shutter")
            return self._send(200, "application/json", b'{"ok":true}')
        return self._send(404, "text/plain", b"not found")


def ensure_cert(bind_addr, certdir):
    """Return (cert, key), generating a self-signed pair with SANs if needed.

    WebCodecs (`VideoDecoder`) is only exposed in a secure context, so
    `--mode webcodecs` cannot work over plain http on a LAN address. A
    self-signed certificate is enough: the browser warns once, the user
    accepts, and the page is then a secure context. No CA, no tailnet, and it
    works for any device on the LAN - which is the point of shipping it.
    """
    os.makedirs(certdir, exist_ok=True)
    cert = os.path.join(certdir, "cert.pem")
    key = os.path.join(certdir, "key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key

    if not shutil.which("openssl"):
        raise SystemExit(
            "openssl not found - needed once to generate a self-signed "
            "certificate. Install openssl, or pass --cert/--key.")

    sans = ["DNS:localhost", "IP:127.0.0.1"]
    host = _socket.gethostname()
    if host and host != "localhost":
        sans.append("DNS:" + host)
    if bind_addr not in ("0.0.0.0", "::", ""):
        sans.append("IP:" + bind_addr)
    # every local IPv4, so whichever address the user actually types matches
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True,
                             text=True, timeout=3).stdout
        for ip in out.split():
            if ip.count(".") == 3:
                sans.append("IP:" + ip)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        for info in _socket.getaddrinfo(host, None, _socket.AF_INET):
            sans.append("IP:" + info[4][0])
    except OSError:
        pass
    san = ",".join(dict.fromkeys(sans))

    print(f"[tls] generating a self-signed certificate for {san}", flush=True)
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", cert, "-days", "825",
         "-subj", "/CN=free60 camera",
         "-addext", "subjectAltName=" + san],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.chmod(key, 0o600)
    except OSError:
        pass
    return cert, key


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--https", action="store_true",
                    help="serve HTTPS with a self-signed certificate. Required "
                         "for --mode webcodecs: VideoDecoder only exists in a "
                         "secure context")
    ap.add_argument("--cert", help="certificate to use with --https")
    ap.add_argument("--key", help="private key to use with --https")
    ap.add_argument("--cert-dir", default="certs",
                    help="where a generated certificate is kept (default: certs/)")
    ap.add_argument("--scale", type=int, default=1920,
                    help="downscale width for the web preview (0 = native)")
    ap.add_argument("--quality", type=int, default=5)
    ap.add_argument("--mode", choices=["mjpeg", "fmp4", "webcodecs"], default="mjpeg",
                    help="mjpeg: server transcodes, any browser. "
                         "fmp4: remux only (copy), near-zero CPU, browser uses "
                         "MediaSource - works over plain http. "
                         "webcodecs: relay raw H.264, secure context only")
    ap.add_argument("--idle-timeout", type=int, default=60,
                    help="seconds with no viewer before the camera is put to "
                         "sleep; 0 disables (the camera runs HOT while streaming)")
    args = ap.parse_args()

    cap = Capture(scale_width=args.scale, quality=args.quality,
                  idle_timeout=args.idle_timeout, mode=args.mode).start()
    Handler.capture = cap
    # SIGTERM (systemctl stop/restart) must shut down deliberately: without a
    # handler Python dies instantly and the capture loop never gets to stop
    # live view. It must not close the session either - see _session's finally.
    def _on_signal(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_signal)

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)

    if args.https or args.cert:
        if args.cert and args.key:
            cert, key = args.cert, args.key
        else:
            cert, key = ensure_cert(args.bind, args.cert_dir)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        scheme = "https"
        print(f"[tls] serving {cert} (accept the browser warning once)",
              flush=True)
    else:
        scheme = "http"
    srv.daemon_threads = True
    print(f"serving on {scheme}://{args.bind}:{args.port}/  mode={args.mode}", flush=True)
    if args.mode == "webcodecs" and scheme != "https":
        print("[warn] webcodecs needs a secure context: browsers will show a", flush=True)
        print("       black canvas on http://<lan-ip>. Add --https, or use", flush=True)
        print("       --mode fmp4 which has no such requirement.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cap.running = False
        # give the capture thread a moment to stop the encoder cleanly rather
        # than having the process yanked mid-command
        try:
            cap.thread.join(timeout=5)
        except Exception:
            pass


if __name__ == "__main__":
    main()
