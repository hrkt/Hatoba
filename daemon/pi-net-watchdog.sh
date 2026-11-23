#!/bin/sh
# pi-net-watchdog.sh -- Pi network auto-recovery (approved 2026-11-24,
# starts in dry-run; see Environment= in the .service unit).
#
# Purpose: the Pi itself keeps dropping off the network (invisible on
# Tailscale: 2026-11-18/20/21/22, twice on 2026-11-23), so the Hatoba
# daemon watchdog + systemd Restart=always cannot help: they only
# restart the daemon process on a Pi that is still reachable. This
# script runs from a systemd timer on the Pi and escalates locally:
#   1) tailscaled restart, 2) Wi-Fi link down/up,
#   3) USB re-enumeration of the Wi-Fi dongle (authorized 0 -> 1),
#   4) reboot (only when daemon AND network are both down).
# Every check that fails leaves a diagnostic snapshot in the log, so
# the next fall shows whether the dongle vanished from USB or not.
#
# Limits (honest): a warm reboot does NOT cut USB VBUS on the Pi 2B,
# and neither does the authorized toggle remove power. If the dongle
# is electrically latched up, only a real power cycle helps (an
# external relay / smart plug on the Pi's PSU). This watchdog covers
# the software-recoverable cases and gathers evidence for the rest.
#
# Install (on the Pi; files arrive with git pull in ~/myos/Hatoba):
#   sudo systemctl enable --now \
#     /home/hiroki/myos/Hatoba/daemon/pi-net-watchdog.timer
#   tail -f ~/myos/Hatoba/daemon/net-watchdog.log
# Go live: remove the Environment=NET_WATCHDOG_DRY_RUN line from
# pi-net-watchdog.service, then
#   sudo systemctl daemon-reload
#   sudo systemctl restart pi-net-watchdog.timer
# Sudo: the unit runs as User=hiroki and the script sudoes for
# systemctl / ip / tee / reboot. Check `sudo -n true` on the Pi; if
# it needs a password, add a scoped /etc/sudoers.d entry.
#
# Safety notes:
# - Interface is detected from the default route, never hardcoded
#   (the Wi-Fi dongle moved ports on 2026-11-18; wlan0/wlan1 varies).
#   With no default route, the first wlan* interface is used.
# - Reboot needs 4 consecutive failed checks (~20 min) AND daemon
#   health and network both down, so a flash in progress (daemon
#   busy, network up) or a brief dropout cannot trigger it.
# - Dry-run mode: NET_WATCHDOG_DRY_RUN=1 logs what it would do.
set -eu
LOG=/home/hiroki/myos/Hatoba/daemon/net-watchdog.log
STATE=/tmp/pi-net-watchdog.state
DRY_RUN=${NET_WATCHDOG_DRY_RUN:-0}
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
say() { echo "$(date '+%Y-%m-%d %H:%M:%S') [net-watchdog] $*" | tee -a "$LOG" || true; }
run() { if [ "$DRY_RUN" = 1 ]; then say "dry-run: $*"; else say "run: $*"; "$@" || true; fi; }
snapshot() {
  say "snapshot usb: $(lsusb 2>/dev/null | tr '\n' ';')"
  say "snapshot links: $(ip -brief link 2>/dev/null | tr '\n' ';')"
  (journalctl -k -n 12 --no-pager 2>/dev/null || dmesg 2>/dev/null | tail -12) \
    | while IFS= read -r line; do say "  klog: $line"; done || true
}
usb_authorized_toggle() {
  echo 0 | sudo tee "$1/authorized" >/dev/null 2>&1 || true
  sleep 2
  echo 1 | sudo tee "$1/authorized" >/dev/null 2>&1 || true
}
fails=0
[ -f "$STATE" ] && fails=$(cat "$STATE" 2>/dev/null || echo 0)
case "$fails" in *[!0-9]*|'') fails=0 ;; esac
# --- checks: daemon health (local) and network (gateway + tailscale) ---
daemon_ok=0; net_ok=0
curl -s --max-time 5 http://127.0.0.1:8766/health >/dev/null 2>&1 && daemon_ok=1 || true
gw=$(ip route 2>/dev/null | awk '/^default/ {print $3; exit}')
if [ -n "$gw" ]; then ping -c 1 -W 3 "$gw" >/dev/null 2>&1 && net_ok=1 || true; fi
if [ "$net_ok" = 0 ]; then
  tailscale status >/dev/null 2>&1 && net_ok=1 || true
fi
if [ "$daemon_ok" = 1 ] && [ "$net_ok" = 1 ]; then
  [ "$fails" != 0 ] && say "recovered (fails reset from $fails)"
  echo 0 > "$STATE"; exit 0
fi
fails=$((fails + 1)); echo "$fails" > "$STATE"
say "unhealthy ($fails/4): daemon_ok=$daemon_ok net_ok=$net_ok gw=${gw:-none}"
snapshot
# --- escalation ---
iface=$(ip route 2>/dev/null | awk '/^default/ {print $5; exit}')
if [ -z "$iface" ]; then
  iface=$(ls /sys/class/net 2>/dev/null | grep '^wlan' | head -1 || true)
fi
usbdev=""
if [ -n "$iface" ]; then
  usbdev=$(readlink -f "/sys/class/net/$iface/device" 2>/dev/null || true)
  [ -n "$usbdev" ] && [ -f "$usbdev/authorized" ] || usbdev=""
fi
case "$fails" in
  1) run sudo systemctl restart tailscaled ;;
  2) if [ -n "$iface" ]; then
       run sudo ip link set "$iface" down; sleep 5
       run sudo ip link set "$iface" up
     else say "no interface to bounce"; fi ;;
  3) if [ -n "$usbdev" ]; then
       run usb_authorized_toggle "$usbdev"
     else say "no USB device path for iface=${iface:-none}; cannot re-enumerate"; fi ;;
  *) if [ "$daemon_ok" = 0 ] && [ "$net_ok" = 0 ]; then
       say "still down after 4 checks and both sides down; rebooting Pi"
       run sudo reboot
     else
       say "holding off reboot: only one side is down (daemon_ok=$daemon_ok net_ok=$net_ok)"
       echo 3 > "$STATE"
     fi ;;
esac
