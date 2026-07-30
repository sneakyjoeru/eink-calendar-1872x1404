#!/bin/bash
# build-and-deploy.sh — Build the IT8951 C driver on the Orange Pi and deploy it.
#
# Run ON the Orange Pi Zero 2W (aarch64), from the repo root:
#   cd eink-calendar-1872x1404
#   sudo bash bin/build-and-deploy.sh
#
# Requires: gcc, libgpiod-dev, libfreetype-dev, make (installed by `it8951 --setup`).
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
DRIVER_DIR="$HERE/c-driver"
DEST="/opt/eink-calendar/bin/it8951"

echo "==> Building IT8951 driver in $DRIVER_DIR"
cd "$DRIVER_DIR"
make clean
make build-pi

echo "==> Installing to $DEST"
sudo mkdir -p "$(dirname "$DEST")"
sudo cp it8951 "$DEST"
sudo chmod +x "$DEST"

echo "==> Verifying"
"$DEST" --help 2>&1 | head -1 || true
echo "Done. Restart the calendar service: sudo systemctl restart eink-calendar"