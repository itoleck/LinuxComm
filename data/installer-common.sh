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

# Text to speech, for talking with a synthetic voice: pyttsx3 with eSpeak NG voices (small)
# always, and Coqui TTS's natural voices where the computer can run them (64-bit, 4 GB of
# memory). The Coqui versions are pinned to ones tested together: newer transformers and
# PyTorch releases have broken coqui-tts before.
COQUI_PACKAGES=("coqui-tts==0.27.5" "transformers>=4.57,<5" "torch<2.15" "torchaudio<2.12" "torchcodec<0.17")

setup_voices() {
  local prefix=$1 coqui=$2 venv=$1/venv
  echo "==> Text to speech (talking with a synthetic voice)"
  if "$venv/bin/python3" -c 'import pyttsx3' 2>/dev/null; then
    echo "    pyttsx3 (eSpeak voices) is installed"
  else
    echo "    installing pyttsx3 (eSpeak voices)"
    "$venv/bin/python3" -m pip install --quiet --disable-pip-version-check pyttsx3 || return 1
  fi
  save_voice_list "$prefix" pyttsx3 || echo "    the eSpeak voices don't work; is espeak-ng installed?"
  if ((coqui)); then
    setup_coqui "$prefix"
  else
    remove_coqui "$prefix"
  fi
}

setup_coqui() {
  local prefix=$1 venv=$1/venv arch mem_kb free_kb
  arch=$(uname -m)
  mem_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
  if [[ $arch != x86_64 && $arch != aarch64 ]]; then
    echo "    natural voices (Coqui TTS) need a 64-bit system; LinuxComm uses the eSpeak voices"
    return 0
  fi
  if ((mem_kb < 3500000)); then
    echo "    natural voices (Coqui TTS) need 4 GB of memory; LinuxComm uses the eSpeak voices"
    return 0
  fi
  if "$venv/bin/python3" -c 'import importlib.metadata as m; assert m.version("coqui-tts") == "0.27.5"' 2>/dev/null; then
    echo "    natural voices (Coqui TTS) are installed"
  else
    free_kb=$(df -Pk "$prefix" | awk 'NR == 2 {print $4}')
    if ((free_kb < 4000000)); then
      echo "    natural voices (Coqui TTS) need 4 GB of free disk space; LinuxComm uses the eSpeak voices"
      return 0
    fi
    echo "    installing natural voices (Coqui TTS: about 1 GB to download, a few minutes)"
    local index=()
    if [[ $arch == x86_64 ]]; then
      index=(--extra-index-url https://download.pytorch.org/whl/cpu)  # PyTorch without the CUDA gigabytes
    fi
    if ! "$venv/bin/python3" -m pip install --quiet --disable-pip-version-check \
         ${index[@]+"${index[@]}"} "${COQUI_PACKAGES[@]}"; then
      echo "    Coqui TTS could not be installed; LinuxComm uses the eSpeak voices"
      return 0
    fi
  fi
  echo "    preparing the natural voices (a 150 MB download the first time)"
  save_voice_list "$prefix" coqui \
    || echo "    the natural voices didn't start (see $prefix/models/tts-coqui.log); LinuxComm uses the eSpeak voices"
}

# Start a text-to-speech engine once (Coqui: downloads its model) and save its list of voices,
# so Preferences can show them without starting the engine.
save_voice_list() {
  local prefix=$1 engine=$2 out
  mkdir -p "$prefix/models"
  out=$(PYTHONPATH="$prefix" "$prefix/venv/bin/python3" -m linuxcomm.tts_worker "$engine" \
          --models "$prefix/models" --prepare 2>"$prefix/models/tts-$engine.log") || true
  if [[ $out == *'"ready": true'* ]]; then
    printf '%s\n' "$out" > "$prefix/models/voices-$engine.json"
    return 0
  fi
  rm -f "$prefix/models/voices-$engine.json"
  return 1
}

remove_coqui() {
  local prefix=$1 venv=$1/venv
  rm -rf "$prefix/models/coqui" "$prefix/models/voices-coqui.json" "$prefix/models/tts-coqui.log"
  if "$venv/bin/python3" -m pip show coqui-tts >/dev/null 2>&1; then
    # Coqui brings in dozens of packages; rebuilding the environment without it frees them all.
    echo "    removing the natural voices (Coqui TTS)"
    rm -rf "$venv"
    if ! python3 -m venv --system-site-packages "$venv" \
       || ! "$venv/bin/python3" -m pip install --quiet --disable-pip-version-check vosk pyttsx3; then
      rm -rf "$venv"
      return 1
    fi
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
