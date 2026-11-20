#!/usr/bin/env bash
# recover.sh - try to bring the camera back without touching the cable.
#
# The camera resets its USB connection under load. Sometimes it re-enumerates
# by itself and the service reattaches within a second or two; sometimes it
# comes back but the running service is left holding a dead handle. This walks
# the escalating options, cheapest first.
#
#   ./tools/recover.sh                      # reset it, report what happened
#   ./tools/recover.sh --service free60     # ... and restart that unit after
#   ./tools/recover.sh --dry-run
#
# NOTE: nothing here can fix a camera that is enumerated but genuinely wedged
# (answers nothing). No root hub on these boxes has per-port power switching,
# so if all of this fails the cable is the only answer.

set -euo pipefail
VID=464a; PID=4d02
SERVICE=""
DRY=0

say()  { printf '  %s\n' "$*"; }
step() { printf '\n%s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --service) SERVICE="$2"; shift 2 ;;
    --dry-run|-n) DRY=1; shift ;;
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

find_sysfs() {
  for d in /sys/bus/usb/devices/*; do
    [ -f "$d/idVendor" ] || continue
    [ "$(cat "$d/idVendor")" = "$VID" ] || continue
    [ "$(cat "$d/idProduct")" = "$PID" ] || continue
    basename "$d"; return 0
  done
  return 1
}

PY=$(command -v python3 || true)

step "1. is it on the bus?"
if NODE=$(find_sysfs); then
  say "present at $NODE (bus $(cat /sys/bus/usb/devices/$NODE/busnum) dev $(cat /sys/bus/usb/devices/$NODE/devnum))"
else
  say "NOT on the bus. Nothing software-side can help - unplug and replug it."
  exit 1
fi

step "2. does it answer?"
ANSWER=0
if [ -n "$PY" ]; then
  PYTHONPATH="${FREEB0_TOOLS:-$(dirname "$0")}" "$PY" - <<'EOF'
import sys
try:
    import madv
    t = madv.UsbTransport()
except Exception as e:
    print("  cannot open it: %s" % e); sys.exit(2)
try:
    c = madv.Camera(t)
    c.open_session()
    info = c.info()
    t.close()
except Exception as e:
    print("  opened but did not answer: %s" % e); sys.exit(1)
print("  answers, fw=%s" % info.get("fw_version"))
sys.exit(0 if info.get("fw_version") else 1)
EOF
  RC=$?
  case $RC in
    0) ANSWER=1; say "responsive" ;;
    2) say "busy - probably already held by a running service (that is fine)" ;;
    *) say "not answering" ;;
  esac
fi

step "3. libusb device reset"
say "(this is the gentle one - it resets the port, not the power)"
if [ "$DRY" -eq 0 ] && [ -n "$PY" ]; then
  "$PY" - <<'EOF' || true
import usb.core
d = usb.core.find(idVendor=0x464a, idProduct=0x4d02)
if d is None:
    print("  gone before we could reset it")
else:
    try:
        d.reset(); print("  reset ok")
    except Exception as e:
        print("  reset failed:", e)
EOF
  sleep 3
fi

step "4. unbind / bind (software replug)"
say "forces a full re-enumeration without moving the cable"
NODE=$(find_sysfs || true)
if [ -z "$NODE" ]; then
  say "device vanished - it may have re-enumerated; skipping"
elif [ "$DRY" -eq 1 ]; then
  say "would: echo $NODE > /sys/bus/usb/drivers/usb/unbind"
  say "       echo $NODE > /sys/bus/usb/drivers/usb/bind"
else
  if echo -n "$NODE" > /sys/bus/usb/drivers/usb/unbind 2>/dev/null; then
    say "unbound"
    sleep 3
    if echo -n "$NODE" > /sys/bus/usb/drivers/usb/bind 2>/dev/null; then
      say "bound"
      sleep 4
    else
      say "bind failed - the kernel may re-add it on its own; watch dmesg"
    fi
  else
    say "unbind failed (needs root?)"
  fi
fi

step "5. service"
if [ -n "$SERVICE" ]; then
  say "restarting $SERVICE (required after unbind/bind: the running process"
  say "keeps a stale handle and reports streaming=true with 0 fps)"
  [ "$DRY" -eq 0 ] && systemctl restart "$SERVICE" && sleep 5
  systemctl is-active "$SERVICE" 2>/dev/null || true
else
  say "no --service given. If you did step 4, restart the service yourself -"
  say "a stale handle otherwise reports streaming with no frames."
fi

step "result"
if NODE=$(find_sysfs); then
  say "on the bus at $NODE"
  say "check it with: madv.py info"
else
  say "still absent. Replug the camera - that is the only remaining option."
  exit 1
fi
