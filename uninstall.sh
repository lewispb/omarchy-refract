#!/bin/bash
set -euo pipefail

rm -f "$HOME/.config/omarchy/hooks/theme-set.d/rgb-sync"
rm -f "$HOME/.config/omarchy/hooks/post-boot.d/rgb-sync"

rm -rf "$HOME/.config/omarchy/plugins/io.github.lewispb.rgb-sync"
omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
rm -rf "$HOME/.local/state/omarchy-rgb-sync"

systemctl --user disable --now openrgb-server.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/openrgb-server.service"
systemctl --user daemon-reload

echo "Uninstalled. Devices return to their onboard lighting on next reconnect or reboot."
echo "The openrgb package was left installed; remove it with: sudo pacman -R openrgb"
