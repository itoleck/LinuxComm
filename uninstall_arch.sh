#!/usr/bin/env bash
# Removes LinuxComm from Arch Linux / CachyOS. Settings in ~/.config/linuxcomm are kept.
#
#   sudo bash uninstall_arch.sh
set -euo pipefail

APP_ID=io.github.itoleck.LinuxComm
PREFIX=/opt/linuxcomm
SHARE=/usr/local/share
SYSCTL_FILE=/etc/sysctl.d/60-linuxcomm.conf

if [[ $EUID -ne 0 ]]; then
  if command -v sudo >/dev/null; then
    exec sudo bash "$0" "$@"
  elif command -v doas >/dev/null; then
    exec doas bash "$0" "$@"
  fi
  echo "Please run this script as root." >&2
  exit 1
fi

# Only remove firewall rules that install_arch.sh added.
if [[ -f "$PREFIX/.ufw-rule-added" ]] && command -v ufw >/dev/null; then
  ufw delete allow 80/tcp >/dev/null 2>&1 || true
  echo "Closed TCP port 80 in ufw"
fi
if [[ -f "$PREFIX/.firewalld-rule-added" ]] && command -v firewall-cmd >/dev/null; then
  firewall-cmd --quiet --permanent --remove-port=80/tcp 2>/dev/null || true
  firewall-cmd --quiet --reload 2>/dev/null || true
  echo "Closed TCP port 80 in firewalld"
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
      "$SHARE/applications/$APP_ID.desktop" \
      "$SHARE/icons/hicolor/scalable/apps/$APP_ID.svg" \
      "/etc/xdg/autostart/$APP_ID.desktop"
if [[ -f $SHARE/icons/hicolor/icon-theme.cache ]]; then
  for tool in gtk-update-icon-cache gtk4-update-icon-cache; do
    if command -v "$tool" >/dev/null; then
      "$tool" -q -t -f "$SHARE/icons/hicolor" || true
      break
    fi
  done
fi
if command -v update-desktop-database >/dev/null; then
  update-desktop-database -q "$SHARE/applications" 2>/dev/null || true
fi

echo "LinuxComm removed. Your settings (~/.config/linuxcomm) and saved transcripts (~/linuxcomm/data) are kept."
echo "Packages such as gtk4, libadwaita and GStreamer were left installed because other applications use them."
