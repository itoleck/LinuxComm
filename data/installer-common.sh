# Shared by install.sh and install_arch.sh (sourced, not run directly).

VOSK_MODEL_URL=https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip

# The `linuxcomm` command. It uses the private Python environment (which adds speech to
# text) when that still works with the system's GTK bindings, and the system Python
# otherwise, e.g. after a distribution upgrade replaced Python; LinuxComm then runs
# without captions until the installer is run again.
write_launcher() {
  local prefix=$1
  install -d /usr/local/bin
  cat > /usr/local/bin/linuxcomm <<EOF
#!/bin/sh
export PYTHONPATH="$prefix\${PYTHONPATH:+:\$PYTHONPATH}"
if "$prefix/venv/bin/python3" -c "import gi" 2>/dev/null; then
  exec "$prefix/venv/bin/python3" -m linuxcomm "\$@"
fi
exec /usr/bin/python3 -m linuxcomm "\$@"
EOF
  chmod 755 /usr/local/bin/linuxcomm
}

# Speech to text: Vosk in a private Python environment (the distributions don't package
# it) that also sees the system's GTK bindings, plus a small English model (~40 MB).
setup_speech() {
  local prefix=$1 venv=$1/venv models=$1/models
  echo "==> Speech to text (captions)"
  local system_python venv_python=""
  system_python=$(python3 -c 'import sys; print(sys.version_info[:2])')
  if [[ -x $venv/bin/python3 ]]; then
    venv_python=$("$venv/bin/python3" -c 'import sys, vosk; print(sys.version_info[:2])' 2>/dev/null || true)
  fi
  if [[ $venv_python == "$system_python" ]]; then
    echo "    Vosk is installed"
  else
    echo "    installing Vosk"
    rm -rf "$venv"
    if ! python3 -m venv --system-site-packages "$venv" \
       || ! "$venv/bin/python3" -m pip install --quiet --disable-pip-version-check vosk; then
      rm -rf "$venv"  # don't leave a half-made environment behind
      return 1
    fi
  fi
  if compgen -G "$models/*/am" >/dev/null; then
    echo "    a speech model is installed"
  else
    echo "    downloading the English speech model (about 40 MB)"
    python3 - "$models" "$VOSK_MODEL_URL" <<'PY' || return 1
import shutil, sys, tempfile, urllib.request, zipfile
dest, url = sys.argv[1], sys.argv[2]
with tempfile.TemporaryFile() as tmp:
    with urllib.request.urlopen(url, timeout=60) as response:
        shutil.copyfileobj(response, tmp)
    zipfile.ZipFile(tmp).extractall(dest)
PY
  fi
}

# Version 0.0.8 created /data for saved transcripts; they now go to ~/linuxcomm/data.
# Remove that /data if it is still empty; never touch one with files in it.
remove_old_data_folder() {
  local prefix=$1
  [[ -f $prefix/.data-folder-created ]] || return 0
  if rmdir /data 2>/dev/null; then
    echo "==> Removed the empty /data folder made by version 0.0.8"
  elif [[ -d /data ]]; then
    echo "==> Left /data in place because it contains files; transcripts are now saved in ~/linuxcomm/data"
  fi
  rm -f "$prefix/.data-folder-created"
}

remove_speech() {
  local prefix=$1
  if [[ -e $prefix/venv || -e $prefix/models ]]; then
    rm -rf "$prefix/venv" "$prefix/models"
    echo "==> Removed speech to text"
  fi
}
