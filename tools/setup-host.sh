#!/usr/bin/env bash
# setup-host.sh - prepare a Linux host to drive the MADV Mini 360 camera.
#
# Idempotent: safe to re-run. Nothing here touches the camera's firmware.
#
#   ./tools/setup-host.sh --dry-run        show what would happen
#   ./tools/setup-host.sh                  do it (no ffmpeg)
#   ./tools/setup-host.sh --with-ffmpeg    also install ffmpeg (~115 MB)
#
#   ./tools/setup-host.sh --install-service \
#       [--service-bind 0.0.0.0] [--service-port 8901] \
#       [--service-mode fmp4] [--service-user <user>]
#                                          install + enable a systemd unit
#   ./tools/setup-host.sh --uninstall-service
#
# What it does:
#   1. checks python3 (>=3.8) and libusb
#   2. makes pyusb importable (distro package preferred, venv as fallback)
#   3. installs the udev rule so a normal user can reach the camera
#   4. notes whether ffmpeg is present. It is NOT installed by default: only
#      --mode mjpeg needs it. Pass --with-ffmpeg to install it anyway.
#   5b. optionally install + enable a systemd unit (--install-service).
#   5. probes for the camera and prints the exact command to run next

set -euo pipefail

DRY=0
ASSUME_YES=0
WITH_FFMPEG=0
INSTALL_SERVICE=0
UNINSTALL_SERVICE=0
SERVICE_BIND=0.0.0.0
SERVICE_PORT=8901
SERVICE_MODE=fmp4
SERVICE_USER=${SUDO_USER:-$(id -un)}
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|-n)       DRY=1; shift ;;
    --yes|-y)           ASSUME_YES=1; shift ;;
    --with-ffmpeg)      WITH_FFMPEG=1; shift ;;
    --install-service)  INSTALL_SERVICE=1; shift ;;
    --uninstall-service) UNINSTALL_SERVICE=1; shift ;;
    --service-bind)     SERVICE_BIND="$2"; shift 2 ;;
    --service-port)     SERVICE_PORT="$2"; shift 2 ;;
    --service-mode)     SERVICE_MODE="$2"; shift 2 ;;
    --service-user)     SERVICE_USER="$2"; shift 2 ;;
    -h|--help)          sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RULES_SRC="$HERE/tools/60-madv-mini.rules"
RULES_DST="/etc/udev/rules.d/60-madv-mini.rules"
VENV="$HERE/.venv"

ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mfail\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
run()  { if [ "$DRY" -eq 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

SUDO=""
need_root() {
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
}

echo "free60 host setup"
echo "repo: $HERE"
[ "$DRY" -eq 1 ] && echo "(dry run - nothing will be changed)"

# ---------------------------------------------------------------- platform
step "1. platform"
if [ "$(uname -s)" != "Linux" ]; then
  fail "this needs Linux (found $(uname -s))"; exit 1
fi
ok "Linux $(uname -r) / $(uname -m)"

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 not found"; exit 1
fi
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
if python3 -c 'import sys;raise SystemExit(0 if sys.version_info>=(3,8) else 1)'; then
  ok "python3 $PYV"
else
  fail "python3 $PYV is too old (need >= 3.8)"; exit 1
fi

# ---------------------------------------------------------------- libusb
step "2. libusb"
if ldconfig -p 2>/dev/null | grep 'libusb-1.0\.so\.0' >/dev/null 2>&1; then
  ok "libusb-1.0 present"
else
  warn "libusb-1.0 not found in the linker cache"
  if command -v apt-get >/dev/null 2>&1; then
    need_root; run $SUDO apt-get install -y libusb-1.0-0
  else
    fail "install libusb-1.0-0 with your package manager first"; exit 1
  fi
fi

# ---------------------------------------------------------------- pyusb
step "3. pyusb"
PYBIN="python3"
if python3 -c 'import usb.core' 2>/dev/null; then
  ok "pyusb already importable by python3"
else
  HAVE_APT=0
  if command -v apt-get >/dev/null 2>&1 && apt-cache show python3-usb >/dev/null 2>&1; then
    HAVE_APT=1
  fi
  if [ "$HAVE_APT" -eq 1 ]; then
    need_root
    run $SUDO apt-get install -y python3-usb
    if [ "$DRY" -eq 0 ] && python3 -c 'import usb.core' 2>/dev/null; then
      ok "installed distro python3-usb"
    elif [ "$DRY" -eq 1 ]; then
      echo "  [dry-run] if that fails, fall back to a venv at $VENV"
    fi
  fi
  if [ "$DRY" -eq 0 ] && ! python3 -c 'import usb.core' 2>/dev/null; then
    warn "distro package unavailable or ineffective - using a venv"
    [ -x "$VENV/bin/python" ] || run python3 -m venv "$VENV"
    run "$VENV/bin/pip" install --quiet --upgrade pyusb || {
      fail "pip install pyusb failed (offline?)"; exit 1; }
    PYBIN="$VENV/bin/python"
    ok "pyusb installed in venv"
  elif [ "$DRY" -eq 0 ]; then
    PYBIN="python3"
  fi
fi
# if we are already inside a venv with pyusb, prefer it
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'import usb.core' 2>/dev/null; then
  PYBIN="$VENV/bin/python"
fi

# ---------------------------------------------------------------- ffmpeg
step "4. ffmpeg"
if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"
elif [ "$WITH_FFMPEG" -eq 1 ]; then
  if command -v apt-get >/dev/null 2>&1; then
    need_root; run $SUDO apt-get install -y ffmpeg
    [ "$DRY" -eq 0 ] && ok "installed ffmpeg"
  else
    warn "install ffmpeg with your package manager"
  fi
else
  warn "ffmpeg not installed, and not installed by default"
  say  "  only --mode mjpeg needs it: it decodes the camera's H.264 and"
  say  "  re-encodes to JPEG, which costs CPU and ~3.5x the bandwidth."
  say  "  --mode fmp4 and --mode webcodecs need no ffmpeg at all."
  say  "  re-run with --with-ffmpeg if you want the mjpeg fallback."
fi

# ---------------------------------------------------------------- udev
step "5. udev rule"
if [ ! -f "$RULES_SRC" ]; then
  fail "missing $RULES_SRC"; exit 1
fi
if [ -f "$RULES_DST" ] && cmp -s "$RULES_SRC" "$RULES_DST"; then
  ok "$RULES_DST already installed"
else
  need_root
  run $SUDO install -m 644 "$RULES_SRC" "$RULES_DST"
  run $SUDO udevadm control --reload-rules
  run $SUDO udevadm trigger --subsystem-match=usb
  [ "$DRY" -eq 0 ] && ok "installed and reloaded udev"
fi

# GRP is referenced by the rule; tell the operator if it is missing
if ! getent group plugdev >/dev/null 2>&1; then
  warn "group 'plugdev' does not exist on this system."
  warn "either create it or edit $RULES_DST to use an existing group."
fi
if id -nG "$(id -un)" 2>/dev/null | tr ' ' '\n' | grep '^plugdev$' >/dev/null 2>&1; then
  ok "user $(id -un) is in plugdev"
else
  warn "user $(id -un) is NOT in plugdev - services and shells may get EPERM"
fi

# ---------------------------------------------------------------- probe
step "6. camera"
if [ "$DRY" -eq 1 ]; then
  echo "  [dry-run] $PYBIN tools/madv.py probe"
else
  "$PYBIN" "$HERE/tools/madv.py" probe || {
    warn "camera not found - is it plugged into THIS host?"
    warn "the camera is USB-tethered: no Wi-Fi, no Bluetooth, no battery."
  }
fi

# ---------------------------------------------------------------- service
UNIT=/etc/systemd/system/free60.service
if [ "$UNINSTALL_SERVICE" -eq 1 ]; then
  step "7. remove service"
  need_root
  run $SUDO systemctl disable --now free60
  run $SUDO rm -f "$UNIT"
  run $SUDO systemctl daemon-reload
  [ "$DRY" -eq 0 ] && ok "removed free60.service"
elif [ "$INSTALL_SERVICE" -eq 1 ]; then
  step "7. install service"
  need_root
  case "$SERVICE_MODE" in
    fmp4|mjpeg|webcodecs) ;;
    *) fail "--service-mode must be fmp4, mjpeg or webcodecs"; exit 2 ;;
  esac
  # systemd needs an absolute interpreter path
  case "$PYBIN" in
    /*) SERVICE_PY="$PYBIN" ;;
    *)  SERVICE_PY=$(command -v "$PYBIN" || echo /usr/bin/python3) ;;
  esac
  SUPGRP=""
  getent group plugdev >/dev/null 2>&1 && SUPGRP="SupplementaryGroups=plugdev"

  if [ "$DRY" -eq 1 ]; then
    echo "  [dry-run] would write $UNIT:"
    sed 's/^/    /' <<EOF
[Unit]
Description=free60 MADV Mini 360 web demo
After=network-online.target
Wants=network-online.target

[Service]
Nice=10
CPUQuota=80%
User=$SERVICE_USER
$SUPGRP
WorkingDirectory=$HERE
ExecStart=$SERVICE_PY tools/madv-web.py --port $SERVICE_PORT --bind $SERVICE_BIND --mode $SERVICE_MODE --idle-timeout 60
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  else
    TMP=$(mktemp)
    cat > "$TMP" <<EOF
[Unit]
Description=free60 MADV Mini 360 web demo
After=network-online.target
Wants=network-online.target

[Service]
Nice=10
CPUQuota=80%
User=$SERVICE_USER
$SUPGRP
WorkingDirectory=$HERE
ExecStart=$SERVICE_PY tools/madv-web.py --port $SERVICE_PORT --bind $SERVICE_BIND --mode $SERVICE_MODE --idle-timeout 60
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    if [ -f "$UNIT" ] && cmp -s "$TMP" "$UNIT"; then
      ok "free60.service unchanged - not restarting a running service"
      rm -f "$TMP"
    else
      $SUDO install -m 644 "$TMP" "$UNIT"
      rm -f "$TMP"
      $SUDO systemctl daemon-reload
      $SUDO systemctl enable free60
      # restart, not 'enable --now': an active unit is left alone by the latter
      $SUDO systemctl restart free60
      ok "free60.service written and (re)started"
    fi
    ok "  http://$SERVICE_BIND:$SERVICE_PORT/   mode=$SERVICE_MODE"
    if ! id -nG "$SERVICE_USER" 2>/dev/null | tr ' ' '\n' | grep '^plugdev$' >/dev/null 2>&1; then
      warn "service user $SERVICE_USER is NOT in plugdev - it may not reach the camera."
      warn "udev uaccess does not apply to services; add the user to the group."
    fi
    systemctl is-active free60 >/dev/null 2>&1 && ok "active" || warn "not active - check: journalctl -u free60 -n 30"
  fi
fi

# ---------------------------------------------------------------- next
step "next"
cat <<EOF
  $PYBIN tools/madv.py info          # expect fw = v1.1126

  # strong box / any browser:
  $PYBIN tools/madv-web.py --port 8901 --bind <lan-ip>

  # weak box (TV-box SoC): no transcoding, browser decodes
  $PYBIN tools/madv-web.py --port 8901 --bind <lan-ip> --mode webcodecs

  then open http://<lan-ip>:8901/

  If a firewall is present, open the port from ANOTHER host to prove it.
EOF

[ "$DRY" -eq 1 ] && echo && echo "(dry run complete - nothing changed)"
exit 0
