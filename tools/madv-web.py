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
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import madv


# =======================================================================
# capture: USB -> ffmpeg -> latest MJPEG frame
# =======================================================================
class Capture:
    def __init__(self, scale_width=1920, quality=5, idle_timeout=60):
        self.scale_width = scale_width
        self.idle_timeout = idle_timeout
        self.quality = quality
        self.latest = None            # latest JPEG bytes
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.frames = 0
        self.fps = 0.0
        self._fps_t0 = None
        self._fps_n = 0
        self.info = {}
        self.error = None
        self.running = True
        self._pending = None
        self.last_request = time.time()
        self.streaming = False
        self.latest_thumb = None
        self.last_shutter = None
        self.ff = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    # -- the ffmpeg encoder (H.264 in on stdin, JPEGs out on stdout) --
    def _spawn_ffmpeg(self):
        vf = f"scale={self.scale_width}:-2" if self.scale_width else None
        # NOTE: -vsync 0 is required (image2pipe mjpeg dedupes to ~2 frames
        # without it).  Do not use -probesize/-analyzeduration here: we join
        # the H.264 stream mid-flight and a tiny probe size makes ffmpeg
        # give up.  -err_detect ignore_err keeps it alive across the
        # occasional dropped access unit.
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
               "-err_detect", "ignore_err",
               "-f", "h264", "-i", "pipe:0", "-an"]
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
        ff = self._spawn_ffmpeg()
        reader = threading.Thread(target=self._read_jpegs, args=(ff,), daemon=True)
        reader.start()

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

                # nobody looking for a while: stop the encoder so it cools down
                if self.idle_timeout and idle > self.idle_timeout:
                    print(f"[capture] no viewer for {self.idle_timeout}s - "
                          f"camera to power save", flush=True)
                    try:
                        cam.sleep()
                    except Exception as e:
                        print(f"[capture] sleep failed: {e}", flush=True)
                    self.streaming = False
                    continue

                try:
                    fr = cam.get_frame()
                except Exception:
                    fr = None            # transient; keep going
                if fr and fr["data"]:
                    try:
                        ff.stdin.write(fr["data"])
                        ff.stdin.flush()
                    except (BrokenPipeError, OSError):
                        break
                now = time.time()
                if now - t_fps >= 2.0:
                    self.fps = (self.frames - frames_at) / (now - t_fps)
                    frames_at = self.frames
                    t_fps = now
        finally:
            self.running_to_restart = True
            try:
                ff.kill()
            except Exception:
                pass
            try:
                cam.stop_liveview()
                cam.close_session()
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
 .bar{display:flex;gap:10px;align-items:center;padding:8px 14px;border-top:1px solid #1e2530;flex:0 0 auto}
 label.s{display:flex;gap:6px;align-items:center;color:var(--dim);font-size:12px}
 input[type=range]{width:110px;accent-color:var(--acc)}
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
  <img id="flat" src="/stream.mjpg" alt="panorama">
  <div id="hud">drag to look &middot; scroll / pinch to zoom &middot; double-click to reset</div>
</div>

<div class="bar">
  <button id="shutter">Take picture</button>
  <button id="snap">Save frame</button>
  <button id="spin">Auto-rotate</button>
  <button id="reset">Reset view</button>
  <span class="spacer"></span>
  <label class="s">zoom <input id="fov" type="range" min="30" max="120" value="75"></label>
</div>

<script>
const $ = id => document.getElementById(id);
let mode = '360';

$('tab_360').onclick = () => { mode='360'; $('tab_360').classList.add('on'); $('tab_flat').classList.remove('on');
  $('gl').style.display='block'; $('flat').style.display='none'; };
$('tab_flat').onclick = () => { mode='flat'; $('tab_flat').classList.add('on'); $('tab_360').classList.remove('on');
  $('flat').style.display='block'; $('gl').style.display='none'; };

$('shutter').onclick = async () => {
  const b=$('shutter'); const t=b.textContent; b.textContent='Capturing...';
  try { await fetch('/api/shutter', {method:'POST'}); } catch(e){}
  setTimeout(()=>{ b.textContent=t; }, 1400);
};
$('snap').onclick = () => { window.location = '/api/snapshot.jpg?t=' + Date.now(); };
$('reset').onclick = () => { yaw=Math.PI; pitch=0; fovT=75; velYaw=0; velPitch=0; $('fov').value=75; };
let spin=false;
$('spin').onclick = () => { spin=!spin; $('spin').classList.toggle('on', spin); };

// ---------- status ----------
async function poll(){
  try{
    const j = await (await fetch('/api/info')).json();
    $('fw').textContent=j.fw_version||'-'; $('hw').textContent=j.hw_version||'-';
    $('fps').textContent=(j.fps||0).toFixed(0);
    if(j.error){ $('err').style.display=''; $('err').textContent=j.error; } else $('err').style.display='none';
  }catch(e){}
}
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
  vec3 d = vec3(p.x*tx, p.y*ty, 1.0);

  float cy=cos(yaw),  sy=sin(yaw);
  float cp=cos(pitch), sp=sin(pitch);
  vec3 d1 = vec3(cy*d.x + sy*d.z, d.y, -sy*d.x + cy*d.z);
  vec3 d2 = vec3(d1.x, cp*d1.y - sp*d1.z, sp*d1.y + cp*d1.z);

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
  return { kx: fovX/w, ky: fovY/h };
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
  yaw   -= dx*kx;
  pitch += dy*ky;
  pitch = Math.max(-1.45, Math.min(1.45, pitch));
  velYaw   = (-dx*kx)/dts;      // radians per second
  velPitch = ( dy*ky)/dts;
});
canvas.addEventListener('wheel', e=>{ e.preventDefault();
  fovT = Math.max(30, Math.min(120, fovT + (e.deltaY>0?4:-4)));
  $('fov').value = fovT;
},{passive:false});
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
let ready=false, busy=false;
img.onload = ()=>{ ready=true; busy=false; };
img.onerror = ()=>{ busy=false; };
function grab(){ if(mode!=='360'||busy) return; busy=true; img.src='/frame.jpg?t='+Date.now(); }

let last=performance.now();
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
      pitch=Math.max(-1.45,Math.min(1.45,pitch));
    }
    if(spin) yaw += dt*0.25;
  }
  grab();                       // keep fetching even before the first frame lands
  if(mode!=='360'||!ready) return;
  const w=canvas.clientWidth||960, h=canvas.clientHeight||480;
  if(canvas.width!==w||canvas.height!==h){ canvas.width=w; canvas.height=h; }
  gl.viewport(0,0,w,h);
  gl.bindTexture(gl.TEXTURE_2D,tex);
  gl.texImage2D(gl.TEXTURE_2D,0,gl.RGB,gl.RGB,gl.UNSIGNED_BYTE,img);
  gl.uniform2f(uRes,w,h);
  gl.uniform1f(uYaw,yaw);
  gl.uniform1f(uPitch,pitch);
  gl.uniform1f(uFov,fovT*Math.PI/180);
  gl.drawArrays(gl.TRIANGLE_STRIP,0,4);
}
requestAnimationFrame(draw);
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

            print(f"[stream] client attached, latest={'yes' if cap.latest else 'no'}",
                  flush=True)
            sent = 0
            try:
                last = None
                while True:
                    cap.last_request = time.time()
                    with cap.cond:
                        if cap.latest is last:
                            cap.cond.wait(2.0)
                        jpg = cap.latest
                    if jpg is None or jpg is last:
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
            except (BrokenPipeError, ConnectionResetError):
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
            self.capture.idle_timeout = 1   # force the idle path
            self.capture.last_request = 0
            return self._send(200, "application/json", b'{"ok":true}')
        if path == "/api/shutter":
            self.capture.last_request = time.time()
            self.capture.request("shutter")
            return self._send(200, "application/json", b'{"ok":true}')
        return self._send(404, "text/plain", b"not found")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--scale", type=int, default=1920,
                    help="downscale width for the web preview (0 = native)")
    ap.add_argument("--quality", type=int, default=5)
    ap.add_argument("--idle-timeout", type=int, default=60,
                    help="seconds with no viewer before the camera is put to "
                         "sleep; 0 disables (the camera runs HOT while streaming)")
    args = ap.parse_args()

    cap = Capture(scale_width=args.scale, quality=args.quality,
                  idle_timeout=args.idle_timeout).start()
    Handler.capture = cap
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    srv.daemon_threads = True
    print(f"serving on http://{args.bind}:{args.port}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cap.running = False


if __name__ == "__main__":
    main()
