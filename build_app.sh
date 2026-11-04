#!/usr/bin/env bash
# Builds dist/ClaudeLamp.app and installs it to /Applications. Needs uv on the machine (the bundle runs the
# project through uv, so first launch downloads bleak, pywebview, sounddevice and numpy).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/dist/ClaudeLamp.app"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE"/app.py "$HERE"/daybetter.py "$HERE"/ui.html "$HERE"/popover.html "$APP/Contents/Resources/"
cat > "$APP/Contents/MacOS/ClaudeLamp" <<'SH'
#!/bin/bash
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
mkdir -p "$HOME/Library/Logs"
exec uv run --quiet --script "$RES/app.py" >> "$HOME/Library/Logs/ClaudeLamp.log" 2>&1
SH
chmod +x "$APP/Contents/MacOS/ClaudeLamp"
cat > "$APP/Contents/Info.plist" <<'PL'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>ClaudeLamp</string>
  <key>CFBundleDisplayName</key><string>ClaudeLamp</string>
  <key>CFBundleIdentifier</key><string>dev.ma67.claudelamp</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>ClaudeLamp</string>
  <key>CFBundleIconFile</key><string>ClaudeLamp</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSBluetoothAlwaysUsageDescription</key><string>ClaudeLamp talks to your LED strip over Bluetooth.</string>
  <key>NSMicrophoneUsageDescription</key><string>Music mode listens to the room and drives the strip.</string>
</dict></plist>
PL
uv run --quiet --with pillow python3 "$HERE/make_icon.py" "$APP/Contents/Resources"
iconutil -c icns "$APP/Contents/Resources/ClaudeLamp.iconset" -o "$APP/Contents/Resources/ClaudeLamp.icns"
rm -rf "$APP/Contents/Resources/ClaudeLamp.iconset"
rm -rf /Applications/ClaudeLamp.app && cp -R "$APP" /Applications/ClaudeLamp.app
echo "installed /Applications/ClaudeLamp.app"
