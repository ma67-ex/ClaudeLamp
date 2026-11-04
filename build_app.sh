#!/usr/bin/env bash
# Builds dist/ClaudeLamp.app and installs it to /Applications. Needs uv on the machine (the bundle runs the
# project through uv, so first launch downloads bleak, pywebview, sounddevice and numpy).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/dist/ClaudeLamp.app"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE"/app.py "$HERE"/daybetter.py "$HERE"/ui.html "$APP/Contents/Resources/"
cat > "$APP/Contents/MacOS/ClaudeLamp" <<'SH'
#!/bin/bash
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
exec uv run --quiet --script "$RES/app.py"
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
# icon: dark rounded tile with a glowing purple LED strip
uv run --quiet --with pillow python3 - "$APP/Contents/Resources" <<'PY'
import sys, os
from PIL import Image, ImageDraw, ImageFilter
out = sys.argv[1]; S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((60, 60, S-60, S-60), radius=220, fill=(12, 12, 18, 255))
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0)); g = ImageDraw.Draw(glow)
for i in range(7):
    x = 200 + i * 104
    g.ellipse((x-60, S//2-60, x+60, S//2+60), fill=(120, 60, 255, 255))
glow = glow.filter(ImageFilter.GaussianBlur(70))
img.alpha_composite(glow)
d = ImageDraw.Draw(img)
d.line((150, S//2, S-150, S//2), fill=(40, 40, 52, 255), width=44)
for i in range(7):
    x = 200 + i * 104
    d.ellipse((x-34, S//2-34, x+34, S//2+34), fill=(190, 150, 255, 255))
    d.ellipse((x-14, S//2-22, x+6, S//2-6), fill=(255, 255, 255, 220))
mask = Image.new("L", (S, S), 0); ImageDraw.Draw(mask).rounded_rectangle((60, 60, S-60, S-60), radius=220, fill=255)
img.putalpha(mask)
iconset = os.path.join(out, "ClaudeLamp.iconset"); os.makedirs(iconset, exist_ok=True)
for n in (16, 32, 64, 128, 256, 512, 1024):
    im = img.resize((n, n), Image.LANCZOS)
    im.save(os.path.join(iconset, f"icon_{n}x{n}.png"))
    if n >= 32: im.save(os.path.join(iconset, f"icon_{n//2}x{n//2}@2x.png"))
print("iconset ok")
PY
iconutil -c icns "$APP/Contents/Resources/ClaudeLamp.iconset" -o "$APP/Contents/Resources/ClaudeLamp.icns"
rm -rf "$APP/Contents/Resources/ClaudeLamp.iconset"
rm -rf /Applications/ClaudeLamp.app && cp -R "$APP" /Applications/ClaudeLamp.app
echo "installed /Applications/ClaudeLamp.app"
