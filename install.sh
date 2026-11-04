#!/usr/bin/env bash
# Installs led.py into ~/.claude/scripts, writes a default state file,
# and merges hooks.json into ~/.claude/settings.json (idempotent).
# Usage: ./install.sh [BLE_NAME] [IDLE_HEX]
set -euo pipefail
NAME="${1:-P065_17F3}"
IDLE="${2:-ff8c3c}"
HERE="$(cd "$(dirname "$0")" && pwd)"

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/ (brew install uv)"; exit 1; }

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
        already = any("led.py" in h["command"] for e in existing for h in e["hooks"])
        if not already:
            existing.append(entry)
json.dump(settings, open(settings_path, "w"), indent=2)
print("hooks merged into", settings_path)
PY

LED_SELFTEST=1 ~/.claude/scripts/led.py
echo "Installed. Test with: ~/.claude/scripts/led.py scan   then   ~/.claude/scripts/led.py red"
