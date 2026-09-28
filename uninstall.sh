#!/usr/bin/env bash
# Removes LinuxComm. Settings in ~/.config/linuxcomm are kept.
#
#   sudo bash uninstall.sh
set -euo pipefail

APP_ID=io.github.itoleck.LinuxComm
PREFIX=/opt/linuxcomm
SYSCTL_FILE=/etc/sysctl.d/60-linuxcomm.conf

if [[ $EUID -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

# Only remove the firewall rule if the installer added it.
if [[ -f "$PREFIX/.ufw-rule-added" ]] && command -v ufw >/dev/null; then
  ufw delete allow 80/tcp >/dev/null 2>&1 || true
  echo "Closed TCP port 80 in ufw"
fi

if [[ -f "$SYSCTL_FILE" ]]; then
  rm -f "$SYSCTL_FILE"
  sysctl -q -w net.ipv4.ip_unprivileged_port_start=1024 || true
  echo "Port 80 requires root again"
fi

# Version 0.0.8 created /data for saved transcripts (they now go to ~/linuxcomm/data).
# Remove it only if that installer created it and it is empty.
if [[ -f "$PREFIX/.data-folder-created" ]]; then
  if rmdir /data 2>/dev/null; then
    echo "Removed the empty /data folder"
  elif [[ -d /data ]]; then
    echo "Kept /data: it contains saved transcripts"
  fi
fi

rm -rf "$PREFIX"
rm -f /usr/local/bin/linuxcomm \
      "/usr/share/applications/$APP_ID.desktop" \
      "/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg" \
      "/etc/xdg/autostart/$APP_ID.desktop"
gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true

echo "LinuxComm removed. Your settings (~/.config/linuxcomm) and saved transcripts (~/linuxcomm/data) are kept."
