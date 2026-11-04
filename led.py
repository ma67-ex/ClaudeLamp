#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=0.22"]
# ///
"""Claude Code status light for a Bluetooth LED strip.

Usage: led.py red|green|restore|unred|on|off|set RRGGBB|idle RRGGBB|idle off|scan

State + config live in ~/.claude/led-state.json:
  {"name": "P065_17F3", "idle": "ff8c3c", "mode": "idle"}
"name" is the BLE advertised name, "idle" is the colour to restore to
("off" to switch the strip off). The strip cannot report its own colour,
so "restore" means "go back to the configured idle colour".
"""
import asyncio, fcntl, json, os, sys

STATE = os.path.expanduser("~/.claude/led-state.json")
DEFAULT = {"name": "P065_17F3", "idle": "ff8c3c", "mode": "idle"}
RED, GREEN = "ff0000", "00ff00"

# Protocol A: ELK-BLEDOM / MELK family. Service fff0, write char fff3, frames 7e..ef.
ELK_CHAR = "0000fff3-0000-1000-8000-00805f9b34fb"
# Protocol B: DayBetter (homebridge-daybetter). Vendor service (e026 / e065 ...), one write char
# (a065 ...), one notify char. Frames: A0 CMD LEN payload + CRC16-MODBUS (little-endian). Strip ACKs with A1.


def crc16_modbus(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "little")


def frames(proto: str, cmd: str, rgb: str | None):
    if proto == "elk":
        if cmd == "off":
            return [bytes.fromhex("7e00040000000000ef")]
        return [bytes.fromhex("7e00040100000000ef"), bytes.fromhex("7e000503" + rgb + "00ef")]
    body = bytes.fromhex("A0110400") if cmd == "off" else bytes.fromhex("A0110401")
    out = [body + crc16_modbus(body)]
    if cmd != "off":
        colour = bytes.fromhex("A01506" + rgb)
        out.append(colour + crc16_modbus(colour))
    return out


def load():
    try:
        with open(STATE) as f:
            return {**DEFAULT, **json.load(f)}
    except FileNotFoundError:
        return dict(DEFAULT)


def save(st):
    with open(STATE, "w") as f:
        json.dump(st, f)


async def send(name: str, cmd: str, rgb: str | None):
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
        for fr in frames(proto, cmd, rgb):
            await c.write_gatt_char(char, fr, response="write" in char.properties)
            await asyncio.sleep(0.15)


async def scan():
    from bleak import BleakScanner
    for dev, adv in (await BleakScanner.discover(timeout=10, return_adv=True)).values():
        if adv.local_name:
            print(adv.rssi, adv.local_name, adv.service_uuids)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd == "scan":
        return asyncio.run(scan())
    if cmd == "help":
        return print(__doc__)
    # ponytail: one global lock so overlapping hooks (Stop then UserPromptSubmit) serialise on the radio
    with open(STATE + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        st = load()
        if cmd == "idle":
            st["idle"] = argv[2].lower()
            save(st)
            cmd = "restore"
        if cmd == "unred":
            if st["mode"] != "red":
                return
            cmd = "restore"
        if cmd == "red":
            rgb, mode = RED, "red"
        elif cmd == "green":
            rgb, mode = GREEN, "green"
        elif cmd == "set":
            rgb, mode = argv[2].lower(), "idle"
            st["idle"] = rgb
        elif cmd == "restore":
            rgb, mode = st["idle"], "idle"
        elif cmd == "on":
            rgb, mode = st["idle"] if st["idle"] != "off" else "ffffff", "idle"
        elif cmd == "off":
            rgb, mode = "off", "idle"
        else:
            sys.exit(__doc__)
        if rgb == "off":
            cmd = "off"
        asyncio.run(send(st["name"], cmd, rgb))
        st["mode"] = mode
        save(st)


if __name__ == "__main__":
    if os.environ.get("LED_SELFTEST"):
        assert crc16_modbus(bytes.fromhex("A0110401")) == bytes.fromhex("B121")
        assert crc16_modbus(bytes.fromhex("A01506FF0000")) == bytes.fromhex("25C0")
        assert frames("elk", "set", "ff0000")[1].hex() == "7e000503ff000000ef"
        assert frames("daybetter", "set", "00ff00")[1].hex() == "a0150600ff005400"
        print("selftest ok")
    else:
        main(sys.argv)
