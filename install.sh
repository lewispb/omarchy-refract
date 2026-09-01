#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v openrgb >/dev/null 2>&1; then
  omarchy pkg add openrgb
fi

mkdir -p "$HOME/.config/systemd/user"
cp openrgb-server.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now openrgb-server.service

omarchy hook install theme-set rgb-sync
omarchy hook install post-boot rgb-sync

# Give the server time to detect devices on a first start, then apply the
# current theme's colors.
sleep 5
"$HOME/.config/omarchy/hooks/theme-set.d/rgb-sync"

echo "Installed. RGB devices now follow the Omarchy theme."
echo "Optional configuration: ~/.config/omarchy/rgb-sync.conf (see README)."
