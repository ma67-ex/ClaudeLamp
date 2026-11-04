#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=0.22"]
# ///
"""Claude Code status light for a Bluetooth LED strip.

  led.py working        idle colour (Claude is busy)             UserPromptSubmit, after a permission is granted
  led.py attention      red strobe at max speed (Claude waits)   AskUserQuestion, permission prompt
  led.py done           green strobe (max speed) for 5 s, then idle          Stop. Reads the hook JSON on stdin; if Claude's
                                                                 last message ends with "?", shows attention instead
  led.py unattention    idle colour, only if currently attention PostToolUse (permission was granted)
  led.py set RRGGBB | idle RRGGBB|off | on | off | scan

State + config: ~/.claude/led-state.json  {"name": "P065_17F3", "idle": "4b0082", "mode": "idle", "effects": {...}}
"effects" lets you choose what each state looks like (edit the file or use Settings in the app):
  {"working":   {"type": "colour", "rgb": "4b0082"},
   "attention": {"type": "scene", "scene": 24, "speed": 100, "seconds": 300},
   "done":      {"type": "scene", "scene": 25, "speed": 100, "seconds": 5}}
type: "colour" (rgb, optional bright 1..100), "scene" (DayBetter scene id, speed 1..100, optional bright), or "off".
seconds: how long attention / done lasts before the working look returns.
If the ClaudeLamp desktop app is running it owns the Bluetooth link; frames are handed to it over ~/.claude/claudelamp.sock.
"""
import asyncio, fcntl, json, os, sys, time

STATE = os.path.expanduser("~/.claude/led-state.json")
SOCK = os.path.expanduser("~/.claude/claudelamp.sock")
DEFAULT = {"name": "P065_17F3", "idle": "4b0082", "mode": "idle"}
DEFAULT_EFFECTS = {
    "working":   {"type": "colour", "rgb": "4b0082"},
    "attention": {"type": "scene", "scene": 0x18, "speed": 100, "seconds": 300},   # Red Strobe
    "done":      {"type": "scene", "scene": 0x19, "speed": 100, "seconds": 5},     # Green Strobe
}
ELK_BLINK = {0x18: "96", 0x19: "97", 0x1A: "98", 0x1B: "99", 0x1C: "9a", 0x1D: "9b", 0x1E: "9c", 0x17: "95"}  # scene id -> ELK effect

# Protocol A: ELK-BLEDOM / MELK family. Service fff0, write char fff3, frames 7e..ef.
ELK_CHAR = "0000fff3-0000-1000-8000-00805f9b34fb"
# Protocol B: DayBetter QC family (P065). Vendor service, one write char, one notify char.
# Frames: A0 cmd (payload+3) payload crc16-modbus little-endian.


def crc16_modbus(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "little")


def pack(cmd: int, *payload: int) -> bytes:
    body = bytes([0xA0, cmd, len(payload) + 3, *payload])
    return body + crc16_modbus(body)


def effect_frames(proto: str, spec: dict) -> list[bytes]:
    """Frames for one configured effect: {"type": "colour"|"scene"|"off", ...}."""
    t = spec.get("type", "colour")
    if t == "off":
        return frames(proto, "off", None)
    bright = spec.get("bright")
    if t == "scene":
        sid, spd = int(spec.get("scene", 0x18)), int(spec.get("speed", 100))
        if proto == "elk":
            out = [bytes.fromhex("7e00040100000000ef"), bytes.fromhex("7e0003" + ELK_BLINK.get(sid, "87") + "03000000ef"),
                   bytes.fromhex("7e0002%02x00000000ef" % spd)]
        else:
            out = [pack(0x11, 1), pack(0x12, sid, 0xFF), pack(0x16, spd)]
    else:
        out = frames(proto, "colour", spec.get("rgb", "4b0082"))
    if bright:
        out.append(bytes.fromhex("7e0001%02x00000000ef" % int(bright)) if proto == "elk" else pack(0x13, int(bright)))
    return out


def frames(proto: str, cmd: str, rgb: str | None) -> list[bytes]:
    """cmd: "colour" (static rgb), "attention" (red strobe, max speed), "off"."""
    if proto == "elk":
        if cmd == "off":
            return [bytes.fromhex("7e00040000000000ef")]
        on = bytes.fromhex("7e00040100000000ef")
        if cmd == "attention":
            return [on, bytes.fromhex("7e00039603000000ef"), bytes.fromhex("7e00026400000000ef")]  # blink_r, speed 100
        if cmd == "done":
            return [on, bytes.fromhex("7e00039703000000ef"), bytes.fromhex("7e00026400000000ef")]  # blink_g, speed 100
        return [on, bytes.fromhex("7e000503" + rgb + "00ef")]
    if cmd == "off":
        return [pack(0x11, 0)]
    if cmd == "attention":
        return [pack(0x11, 1), pack(0x12, 0x18, 0xFF), pack(0x16, 100)]  # scene Red Strobe, speed 100
    if cmd == "done":
        # ponytail: user asked for 1000 blinks in 5 s (200 Hz); BLE writes take ~50 ms and the firmware strobe
        # tops out well below that, so this is the fastest green blink the strip can do
        return [pack(0x11, 1), pack(0x12, 0x19, 0xFF), pack(0x16, 100)]  # scene Green Strobe, speed 100
    r, g, b = bytes.fromhex(rgb)
    return [pack(0x11, 1), pack(0x12, 1, 0), pack(0x15, r, g, b, 0xFF)]


def load():
    try:
        with open(STATE) as f:
            st = {**DEFAULT, **json.load(f)}
    except FileNotFoundError:
        st = dict(DEFAULT)
    eff = {k: {**v, **st.get("effects", {}).get(k, {})} for k, v in DEFAULT_EFFECTS.items()}
    if "effects" not in st or "working" not in st.get("effects", {}):
        eff["working"] = {"type": "off" if st["idle"] == "off" else "colour", "rgb": st["idle"]}   # old config: idle colour
    st["effects"] = eff
    return st


def effect(st, state: str) -> dict:
    return st["effects"].get(state) or DEFAULT_EFFECTS[state]


def save(st):
    with open(STATE, "w") as f:
        json.dump(st, f)


HOOK_STATE = {"state": "idle"}   # set by main() before send(); forwarded to the app for its Claude badge


def send_via_app(daybetter_frames: list[bytes]) -> bool:
    """Desktop app running: let it write the frames. Returns False when it is not running."""
    import socket
    if not os.path.exists(SOCK):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sk:
            sk.settimeout(30)
            sk.connect(SOCK)
            sk.sendall((json.dumps({"frames": [f.hex() for f in daybetter_frames], "state": HOOK_STATE["state"]}) + "\n").encode())
            reply = json.loads(sk.makefile().readline())
    except (OSError, ValueError):
        return False  # stale socket; fall back to direct BLE
    if not reply.get("ok"):
        sys.exit("led.py (via app): " + reply.get("error", "?"))
    return True


async def send_ble(name: str, cmd: str, rgb: str | None, spec: dict | None = None):
    from bleak import BleakClient, BleakScanner
    dev = await BleakScanner.find_device_by_filter(
        lambda d, a: (a.local_name or d.name or "").lower().startswith(name.lower()), timeout=15)
    if dev is None:
        sys.exit(f"led.py: device {name!r} not found (phone app connected? strip off?)")
    async with BleakClient(dev, timeout=20) as c:
        chars = {ch.uuid: ch for s in c.services for ch in s.characteristics}
        if ELK_CHAR in chars:
            proto, char = "elk", chars[ELK_CHAR]
        else:
            writable = [ch for ch in chars.values() if "write" in ch.properties or "write-without-response" in ch.properties]
            if not writable:
                sys.exit("led.py: no writable characteristic, services: " + ", ".join(s.uuid for s in c.services))
            proto, char = "daybetter", writable[0]
        for fr in (effect_frames(proto, spec) if spec else frames(proto, cmd, rgb)):
            await c.write_gatt_char(char, fr, response="write" in char.properties)
            await asyncio.sleep(0.12)


def send(name: str, cmd: str, rgb: str | None, spec: dict | None = None):
    if not send_via_app(effect_frames("daybetter", spec) if spec else frames("daybetter", cmd, rgb)):
        asyncio.run(send_ble(name, cmd, rgb, spec))


def send_effect(st, state: str):
    send(st["name"], "effect", None, effect(st, state))


def claude_asked_question() -> bool:
    """Stop hook: inspect the transcript; a final assistant message ending in '?' means Claude wants an answer."""
    try:
        hook = json.loads(sys.stdin.read() or "{}")
        path = hook.get("transcript_path")
        if not path:
            return False
        last = ""
        with open(path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                content = rec.get("message", {}).get("content", [])
                texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                if texts:
                    last = texts[-1]
        return last.rstrip().rstrip("*_`").endswith("?")
    except Exception:
        return False


async def scan():
    from bleak import BleakScanner
    for dev, adv in (await BleakScanner.discover(timeout=10, return_adv=True)).values():
        if adv.local_name:
            print(adv.rssi, adv.local_name, adv.service_uuids)


def idle_cmd(st):
    return ("off", None) if st["idle"] == "off" else ("colour", st["idle"])


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd == "scan":
        return asyncio.run(scan())
    if cmd == "help":
        return print(__doc__)
    if cmd == "done" and claude_asked_question():
        cmd = "attention"
    if cmd == "attention-expire":
        time.sleep(int(os.environ.get("LED_ATTENTION_SECONDS", effect(load(), "attention").get("seconds", 300))))   # outside the lock
        with open(STATE + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            st = load()
            if st.get("mode") == "attention" and str(st.get("attention_at")) == argv[2]:
                HOOK_STATE["state"] = "idle"
                send_effect(st, "working")
                st["mode"] = "idle"
                save(st)
        return
    # ponytail: one global lock so overlapping hooks (Stop then UserPromptSubmit) run in order
    with open(STATE + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        st = load()
        if st.get("sleep") and cmd in ("working", "attention", "done", "unattention", "restore"):
            return                                   # strip was switched off on purpose; hooks stay quiet until "on"
        if cmd in ("on", "set", "idle"):
            st.pop("sleep", None)
        if cmd == "idle":
            st["idle"] = argv[2].lower()
            st["effects"]["working"] = {"type": "off" if st["idle"] == "off" else "colour", "rgb": st["idle"]}
            cmd = "working"
        if cmd == "unattention":
            if st["mode"] != "attention":
                return
            cmd = "working"
        if cmd == "set":
            st["idle"] = argv[2].lower()
            st["effects"]["working"] = {"type": "colour", "rgb": st["idle"]}
            cmd = "working"
        HOOK_STATE["state"] = {"attention": "needs you", "done": "done"}.get(cmd, "working")
        if cmd in ("working", "restore", "on"):
            send_effect(st, "working")
            st["mode"] = "idle"
        elif cmd == "attention":
            send_effect(st, "attention")
            st["mode"] = "attention"
            st["attention_at"] = time.time()
            # a detached watcher puts the strip back to idle if nobody answers, so it does not strobe all night
            import subprocess
            subprocess.Popen([os.path.abspath(__file__), "attention-expire", str(st["attention_at"])],
                             start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif cmd == "done":
            send_effect(st, "done")
            st["mode"] = "done"
            save(st)
            time.sleep(int(effect(st, "done").get("seconds", 5)))
            HOOK_STATE["state"] = "idle"
            send_effect(st, "working")
            st["mode"] = "idle"
        elif cmd == "off":
            send(st["name"], "off", None)
            st["mode"] = "idle"
            st["sleep"] = True
        else:
            sys.exit(__doc__)
        save(st)


if __name__ == "__main__":
    if os.environ.get("LED_SELFTEST"):
        assert pack(0x11, 1).hex() == "a0110401b121"
        assert frames("daybetter", "colour", "00ff00")[2].hex() == "a0150700ff00ff" + crc16_modbus(bytes.fromhex("a0150700ff00ff")).hex()
        assert frames("daybetter", "attention", None)[1].hex() == "a0120518ff" + crc16_modbus(bytes.fromhex("a0120518ff")).hex()
        assert frames("elk", "colour", "ff0000")[1].hex() == "7e000503ff000000ef"
        assert frames("elk", "attention", None)[1].hex() == "7e00039603000000ef"
        assert frames("daybetter", "done", None)[1].hex() == "a0120519ff" + crc16_modbus(bytes.fromhex("a0120519ff")).hex()
        assert effect_frames("daybetter", {"type": "scene", "scene": 0x0B, "speed": 50})[1].hex() == "a012050bff" + crc16_modbus(bytes.fromhex("a012050bff")).hex()
        assert effect_frames("daybetter", {"type": "colour", "rgb": "00ff00", "bright": 40})[-1].hex() == "a0130428" + crc16_modbus(bytes.fromhex("a0130428")).hex()
        assert effect_frames("elk", {"type": "scene", "scene": 0x18, "speed": 100})[1].hex() == "7e00039603000000ef"
        assert effect_frames("daybetter", {"type": "off"})[0].hex() == "a011040070e1"
        print("selftest ok")
    else:
        main(sys.argv)
