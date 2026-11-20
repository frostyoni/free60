#!/usr/bin/env bash
# virtual-cam.sh - present the MADV Mini as an ordinary V4L2 webcam.
#
# Run this on the machine that wants a camera, NOT necessarily the one with the
# camera plugged in: it pulls the HTTP stream and re-publishes it through
# v4l2loopback. Zoom, OBS, browsers and anything else then see a normal camera.
#
#   ./tools/virtual-cam.sh                                  # flat view, defaults below
#   ./tools/virtual-cam.sh --src http://<camera-host>:8901/live.mp4
#   ./tools/virtual-cam.sh --device /dev/video10 --fov 100
#   ./tools/virtual-cam.sh --raw                            # the panorama, unprojected
#   ./tools/virtual-cam.sh --dry-run                        # print, do nothing
#
# The camera sends an equirectangular panorama, which looks wrong in a webcam
# window. By default this projects it to a flat, ordinary camera view with
# ffmpeg's v360 filter. --raw skips that if you actually want 360 footage.

set -euo pipefail

SRC=${SRC:-}            # required: http://<camera-host>:8901/live.mp4
DEVICE=${DEVICE:-/dev/video10}
W=${W:-1280}
H=${H:-720}
FOV=${FOV:-95}          # horizontal field of view of the flat view
RAW=0
DRY=0

say()  { printf '  %s\n' "$*"; }
step() { printf '\n%s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --src)      SRC="$2"; shift 2 ;;
    --device)   DEVICE="$2"; shift 2 ;;
    --size)     W="${2%x*}"; H="${2#*x}"; shift 2 ;;
    --fov)      FOV="$2"; shift 2 ;;
    --raw)      RAW=1; shift ;;
    --dry-run|-n) DRY=1; shift ;;
    -h|--help)  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$SRC" ]; then
  echo "error: no source given." >&2
  echo "  --src http://<camera-host>:8901/live.mp4   (or export SRC=...)" >&2
  exit 2
fi

step "1. v4l2loopback"
if ! modinfo v4l2loopback >/dev/null 2>&1; then
  say "module not found on this machine."
  say "install it first:"
  say "  Debian/Ubuntu: sudo apt install v4l2loopback-dkms v4l2loopback-utils"
  say "  then:          sudo modprobe v4l2loopback video_nr=10 card_label='MADV Mini 360' exclusive_caps=1"
  say ""
  say "This box has no kernel build tree, so it cannot build the module at all."
  say "Run this script on a normal Linux desktop instead - the camera host does"
  say "not need it, only the machine that wants a webcam."
  [ "$DRY" -eq 1 ] || exit 1
  say ""
  say "(continuing anyway because this is a dry run)"
fi
if lsmod | grep -q '^v4l2loopback'; then
  say "already loaded"
else
  say "would load: modprobe v4l2loopback video_nr=${DEVICE##*/video} " \
      "card_label='MADV Mini 360' exclusive_caps=1"
  if [ "$DRY" -eq 0 ]; then
    sudo modprobe v4l2loopback "video_nr=${DEVICE##*/video}" \
         card_label="MADV Mini 360" exclusive_caps=1
  fi
fi
[ -e "$DEVICE" ] && say "device: $DEVICE" || say "device $DEVICE does not exist yet"

step "2. ffmpeg"
NO_PROJ=""
[ "$RAW" -eq 0 ] && NO_PROJ="-vf v360=input=e:output=flat:h_fov=${FOV}:v_fov=$((FOV*H/W)):w=${W}:h=${H}"

cmd=(ffmpeg -hide_banner -nostdin -loglevel warning
     -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5
     -i "$SRC")
[ -n "$NO_PROJ" ] && cmd+=($NO_PROJ)
cmd+=(-pix_fmt yuv420p -f v4l2 "$DEVICE")

say "source : $SRC"
say "output : $DEVICE  ${W}x${H}  $([ "$RAW" -eq 1 ] && echo 'panorama (raw)' || echo "flat ${FOV} deg")"
say ""
printf '  %q ' "${cmd[@]}"; printf '\n'

if [ "$DRY" -eq 1 ]; then
  printf '\n(dry run - nothing started)\n'
  exit 0
fi

step "3. running (ctrl-c to stop)"
exec "${cmd[@]}"
