#!/usr/bin/env bash
# LinuxComm installer for Arch Linux, CachyOS and other Arch-based distributions.
#
#   sudo bash install_arch.sh                            install or update
#   sudo bash install_arch.sh --autostart                ...and start LinuxComm when anyone logs in
#   sudo bash install_arch.sh --autostart --fullscreen   ...fullscreen, for wall-mounted displays
#   sudo bash install_arch.sh --no-autostart             stop starting LinuxComm at login
#   sudo bash install_arch.sh --no-speech                without speech-to-text captions (saves ~110 MB)
set -euo pipefail

APP_ID=io.github.itoleck.LinuxComm
PREFIX=/opt/linuxcomm
SHARE=/usr/local/share   # pacman owns /usr/share, so locally installed files go here
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
  if command -v sudo >/dev/null; then
    exec sudo bash "$0" "$@"
  elif command -v doas >/dev/null; then
    exec doas bash "$0" "$@"
  fi
  echo "Please run this installer as root." >&2
  exit 1
fi

if ! command -v pacman >/dev/null; then
  echo "pacman was not found. This installer is for Arch Linux and CachyOS; on Ubuntu use install.sh." >&2
  exit 1
fi

PACKAGES=(
  python python-gobject
  gtk4 libadwaita adwaita-icon-theme
  gstreamer gst-plugins-base gst-plugins-base-libs gst-plugins-good
  libpulse  # pactl, to mute other apps during calls (works with PipeWire too)
)
if ((speech)); then
  PACKAGES+=(python-cffi)  # for Vosk, installed below
fi
# PipeWire's own GStreamer plugin, used if PipeWire's PulseAudio layer (pipewire-pulse) is missing.
if pacman -Q pipewire >/dev/null 2>&1; then
  PACKAGES+=(gst-plugin-pipewire)
fi
# Weather symbols fall back to emoji when the icon theme has no weather icons (e.g. Breeze on KDE).
if ! fc-list : family 2>/dev/null | grep -i emoji >/dev/null; then
  PACKAGES+=(noto-fonts-emoji)
fi

echo "==> Checking required packages"
mapfile -t missing < <(pacman -T "${PACKAGES[@]}" || true)
if ((${#missing[@]})); then
  echo "    installing: ${missing[*]}"
  if ! pacman -S --needed --noconfirm "${missing[@]}"; then
    echo >&2
    echo "Package installation failed. If pacman could not find or download packages, the package" >&2
    echo "database is probably out of date: run 'sudo pacman -Syu', then run this installer again." >&2
    exit 1
  fi
else
  echo "    all present"
fi

echo "==> Installing LinuxComm to $PREFIX"
install -d "$PREFIX"
rm -rf "$PREFIX/linuxcomm"
cp -r "$SRC/linuxcomm" "$PREFIX/"
find "$PREFIX/linuxcomm" -name __pycache__ -prune -exec rm -rf {} +
python3 -m compileall -q "$PREFIX/linuxcomm" >/dev/null || true
write_launcher "$PREFIX"
remove_old_data_folder "$PREFIX"

install -Dm644 "$SRC/data/$APP_ID.desktop" "$SHARE/applications/$APP_ID.desktop"
install -Dm644 "$SRC/data/$APP_ID.svg" "$SHARE/icons/hicolor/scalable/apps/$APP_ID.svg"
# Only refresh an icon cache that already exists; a stale cache would hide the new icon.
if [[ -f $SHARE/icons/hicolor/icon-theme.cache ]]; then
  for tool in gtk-update-icon-cache gtk4-update-icon-cache; do
    if command -v "$tool" >/dev/null; then
      "$tool" -q -t -f "$SHARE/icons/hicolor" || true
      break
    fi
  done
fi
if command -v update-desktop-database >/dev/null; then
  update-desktop-database -q "$SHARE/applications" || true
fi

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
  install -d "$(dirname "$SYSCTL_FILE")"
  echo "net.ipv4.ip_unprivileged_port_start=80" > "$SYSCTL_FILE"
  sysctl -q -p "$SYSCTL_FILE"
  echo "    done (was $current)"
else
  echo "    already allowed"
fi

echo "==> Firewall"
# Rules are only removed by uninstall_arch.sh if this installer added them.
firewall_found=0
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep "Status: active" >/dev/null; then
  firewall_found=1
  if ufw status | grep -E '^80/tcp[[:space:]]+ALLOW' >/dev/null; then
    echo "    TCP port 80 is already open in ufw"
  else
    ufw allow 80/tcp comment "LinuxComm intercom" >/dev/null
    touch "$PREFIX/.ufw-rule-added"
    echo "    opened TCP port 80 in ufw"
  fi
fi
if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  firewall_found=1
  if firewall-cmd --permanent --query-port=80/tcp >/dev/null 2>&1 \
     || firewall-cmd --permanent --query-service=http >/dev/null 2>&1; then
    echo "    TCP port 80 is already open in firewalld"
  else
    firewall-cmd --quiet --permanent --add-port=80/tcp
    firewall-cmd --quiet --reload
    touch "$PREFIX/.firewalld-rule-added"
    echo "    opened TCP port 80 in firewalld"
  fi
fi
if ((firewall_found == 0)); then
  echo "    no active ufw or firewalld; if you use another firewall, allow incoming TCP port 80"
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

address=$(ip -4 route get 1.1.1.1 2>/dev/null \
  | awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }' || true)
echo
version=$(PYTHONPATH="$PREFIX" python3 -c 'import linuxcomm; print(linuxcomm.__version__)' 2>/dev/null || true)
echo "LinuxComm ${version:-} is installed. Open it from the application menu, or run: linuxcomm"
echo "This station's address for other stations: ${address:-unknown} ($(cat /proc/sys/kernel/hostname))"
