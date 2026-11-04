#!/usr/bin/env bash
# One-line install:  curl -fsSL https://raw.githubusercontent.com/ma67-ex/ClaudeLamp/main/install.sh | bash
# Does three things: makes sure uv exists, builds /Applications/ClaudeLamp.app, and wires the Claude Code hooks.
# Usage from a checkout: ./install.sh [BLE_NAME] [IDLE_HEX]
set -euo pipefail
NAME="${1:-P065_17F3}"
IDLE="${2:-4b0082}"

if [ -f "$(dirname "$0")/app.py" ] 2>/dev/null; then
  HERE="$(cd "$(dirname "$0")" && pwd)"
else
  HERE="$HOME/ClaudeLamp"
  if [ -d "$HERE/.git" ]; then git -C "$HERE" pull -q; else git clone -q https://github.com/ma67-ex/ClaudeLamp "$HERE"; fi
fi

if ! command -v uv >/dev/null; then
  echo "Installing uv (Python runner)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  export PATH="$HOME/.local/bin:$PATH"
fi

mkdir -p ~/.claude/scripts
cp "$HERE/led.py" ~/.claude/scripts/led.py
chmod +x ~/.claude/scripts/led.py
[ -f ~/.claude/led-state.json ] || printf '{"name": "%s", "idle": "%s", "mode": "idle"}\n' "$NAME" "$IDLE" > ~/.claude/led-state.json

[ -f ~/.claude/settings.json ] || echo '{}' > ~/.claude/settings.json
cp ~/.claude/settings.json ~/.claude/settings.json.bak-claudelamp
python3 - "$HERE/hooks.json" <<'PY'
import json, os, sys
settings_path = os.path.expanduser("~/.claude/settings.json")
settings = json.load(open(settings_path))
hooks = settings.setdefault("hooks", {})
for event, entries in json.load(open(sys.argv[1])).items():
    existing = hooks.setdefault(event, [])
    for entry in entries:
        if not any("led.py" in h["command"] for e in existing for h in e["hooks"]):
            existing.append(entry)
json.dump(settings, open(settings_path, "w"), indent=2)
print("Claude Code hooks installed")
PY

"$HERE/build_app.sh"
LED_SELFTEST=1 ~/.claude/scripts/led.py >/dev/null
open -a /Applications/ClaudeLamp.app || true
echo "All done. ClaudeLamp is in your menu bar and in /Applications."
