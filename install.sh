#!/usr/bin/env bash
# LinuxComm installer for Ubuntu 23.04+, Debian 12+ and Raspberry Pi OS Bookworm or newer.
#
#   sudo bash install.sh                            install or update
#   sudo bash install.sh --autostart                ...and start LinuxComm when anyone logs in
#   sudo bash install.sh --autostart --fullscreen   ...fullscreen, for wall-mounted displays
#   sudo bash install.sh --no-autostart             stop starting LinuxComm at login
#   sudo bash install.sh --no-speech                without speech-to-text captions (saves ~110 MB)
set -euo pipefail

APP_ID=io.github.itoleck.LinuxComm
PREFIX=/opt/linuxcomm
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOSTART_FILE=/etc/xdg/autostart/$APP_ID.desktop
SYSCTL_FILE=/etc/sysctl.d/60-linuxcomm.conf
# shellcheck source=data/installer-common.sh
source "$SRC/data/installer-common.sh"

autostart=keep
exec_args=""
speech=1
for arg in "$@"; do
  case "$arg" in
    --autostart) autostart=on ;;
    --no-autostart) autostart=off ;;
    --fullscreen) exec_args=" --fullscreen" ;;
    --no-speech) speech=0 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

# Most of these ship with Ubuntu Desktop already, so this is usually a no-op.
PACKAGES=(
  python3 python3-gi
  gir1.2-gtk-4.0 gir1.2-adw-1
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good
  pulseaudio-utils  # pactl, to mute other apps during calls (works with PipeWire too)
)
if ((speech)); then
  PACKAGES+=(python3-venv python3-cffi)  # for Vosk, installed below
fi

is_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }

echo "==> Checking required packages"
missing=()
for p in "${PACKAGES[@]}"; do is_installed "$p" || missing+=("$p"); done
if ((${#missing[@]})); then
  echo "    installing: ${missing[*]}"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}"
else
  echo "    all present"
fi
# Optional: PipeWire's own GStreamer plugin, used if the PulseAudio layer is missing.
if ! is_installed gstreamer1.0-pipewire; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends gstreamer1.0-pipewire \
    >/dev/null 2>&1 || true
fi
# Optional: weather symbols fall back to emoji when the icon theme has no weather icons.
if ! fc-list : family 2>/dev/null | grep -i emoji >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends fonts-noto-color-emoji \
    >/dev/null 2>&1 || true
fi

echo "==> Checking library versions"
adw_version=$(dpkg-query -W -f='${Version}\n' libadwaita-1-0 2>/dev/null | head -n1 || true)
gtk_version=$(dpkg-query -W -f='${Version}\n' libgtk-4-1 2>/dev/null | head -n1 || true)
if ! dpkg --compare-versions "${adw_version:-0}" ge 1.2 || ! dpkg --compare-versions "${gtk_version:-0}" ge 4.8; then
  echo "LinuxComm needs libadwaita 1.2 and GTK 4.8 or newer, but this system has" >&2
  echo "libadwaita ${adw_version:-(none)} and GTK ${gtk_version:-(none)}." >&2
  echo "Supported: Ubuntu 23.04+, Debian 12+ and Raspberry Pi OS Bookworm or newer." >&2
  exit 1
fi
echo "    libadwaita $adw_version, GTK $gtk_version"

echo "==> Installing LinuxComm to $PREFIX"
install -d "$PREFIX"
rm -rf "$PREFIX/linuxcomm"
cp -r "$SRC/linuxcomm" "$PREFIX/"
find "$PREFIX/linuxcomm" -name __pycache__ -prune -exec rm -rf {} +
python3 -m compileall -q "$PREFIX/linuxcomm" >/dev/null || true
write_launcher "$PREFIX"
remove_old_data_folder "$PREFIX"

install -Dm644 "$SRC/data/$APP_ID.desktop" "/usr/share/applications/$APP_ID.desktop"
install -Dm644 "$SRC/data/$APP_ID.svg" "/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true

if ((speech)); then
  setup_speech "$PREFIX" || echo "    Could not set up speech to text (no internet?). LinuxComm works" \
                                  "without captions; run the installer again to add them."
else
  remove_speech "$PREFIX"
fi

echo "==> Allowing LinuxComm to use HTTP port 80 without root"
# Ports below 1024 normally need root. Lowering the threshold to 80 lets the
# intercom (running as the logged-in user) listen on port 80.
current=$(sysctl -n net.ipv4.ip_unprivileged_port_start)
if ((current > 80)); then
  echo "net.ipv4.ip_unprivileged_port_start=80" > "$SYSCTL_FILE"
  sysctl -q -p "$SYSCTL_FILE"
  echo "    done (was $current)"
else
  echo "    already allowed"
fi

echo "==> Firewall"
# The rule is only removed by uninstall.sh if this installer added it.
if command -v ufw >/dev/null && ufw status | grep "Status: active" >/dev/null; then
  if ufw status | grep -E '^80/tcp[[:space:]]+ALLOW' >/dev/null; then
    echo "    TCP port 80 is already open in ufw"
  else
    ufw allow 80/tcp comment "LinuxComm intercom" >/dev/null
    touch "$PREFIX/.ufw-rule-added"
    echo "    opened TCP port 80 in ufw"
  fi
else
  echo "    ufw is not active, nothing to open"
fi

case "$autostart" in
  on)
    install -d "$(dirname "$AUTOSTART_FILE")"
    sed "s|^Exec=.*|Exec=linuxcomm$exec_args|" "$SRC/data/$APP_ID.desktop" > "$AUTOSTART_FILE"
    echo "X-GNOME-Autostart-enabled=true" >> "$AUTOSTART_FILE"
    echo "==> LinuxComm will start at login${exec_args:+ (fullscreen)}"
    ;;
  off)
    rm -f "$AUTOSTART_FILE"
    echo "==> LinuxComm will no longer start at login"
    ;;
esac

echo
version=$(PYTHONPATH="$PREFIX" python3 -c 'import linuxcomm; print(linuxcomm.__version__)' 2>/dev/null || true)
echo "LinuxComm ${version:-} is installed. Open it from the app grid, or run: linuxcomm"
echo "This station's address for other stations: $(hostname -I 2>/dev/null | awk '{print $1}') ($(hostname))"
