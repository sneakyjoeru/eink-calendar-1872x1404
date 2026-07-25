#!/bin/bash
# install.sh — Set up the E-Ink Calendar app on Orange Pi Zero 2W
set -e

echo "=== E-Ink Calendar Installer ==="

# Check for IT8951 C driver
if [ ! -f /home/orangepi/it8951-epaper-c/it8951 ]; then
    echo "⚠️  IT8951 C driver not found at /home/orangepi/it8951-epaper-c/it8951"
    echo "   Install it first: https://github.com/sneakyjoeru/it8951-epaper-c-orangepi-zero-2w"
    echo "   Run: sudo ./it8951 --setup"
    exit 1
fi

# Install Python deps
echo "1. Installing Python dependencies..."
sudo apt update
sudo apt install -y python3-pip python3-venv
pip3 install -r requirements.txt

# Create config dir
echo "2. Creating config directory..."
mkdir -p config

# Copy .env
if [ ! -f .env ]; then
    cp .env.example .env
    echo "   Created .env from .env.example — edit if needed"
fi

# Install systemd service
echo "3. Installing systemd service..."
sudo cp eink-calendar.service /etc/systemd/system/
sudo systemctl daemon-reload

echo ""
echo "=== Installation complete! ==="
echo ""
echo "Next steps:"
echo "  1. Place your Google OAuth client_secret.json in config/"
echo "  2. Start the app: sudo systemctl start eink-calendar"
echo "  3. Or run manually: sudo python3 -m app.main"
echo "  4. The e-ink will show a QR code — scan it to open settings"
echo "  5. Log in with Google, select calendars, choose a view mode"
echo ""
echo "To enable auto-start on boot: sudo systemctl enable eink-calendar"