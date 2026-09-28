#!/usr/bin/env bash
# Makes the install and uninstall scripts executable, so they can be run as ./install.sh.
# Copying the folder from Windows, a USB stick or a zip file usually drops that permission.
#
#   bash make_executable.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
shopt -s nullglob
scripts=(install*.sh uninstall*.sh "$(basename "${BASH_SOURCE[0]}")")

chmod +x "${scripts[@]}"
failed=0
for script in "${scripts[@]}"; do
  if [[ -x $script ]]; then
    echo "executable: $script"
  else
    echo "could not make executable: $script" >&2
    failed=1
  fi
done

if ((failed)); then
  echo "The drive this folder is on may not support Linux permissions (e.g. a FAT or NTFS USB stick)." >&2
  echo "Copy the folder to your home directory and try again, or run the scripts with 'bash install.sh'." >&2
  exit 1
fi
