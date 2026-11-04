# PLAN.md: rebuild AiLamp from scratch

This document is written for an AI agent or a developer who has never seen this project. Follow it top to bottom and you will have a Bluetooth LED strip that mirrors Claude Code's state within a few minutes. Everything that was learned while building the first version is recorded here, including the dead ends, so you do not repeat them.

## 1. Goal

A Bluetooth Low Energy (BLE) LED strip in the user's room should:

1. turn red when Claude Code asks the user a question (the `AskUserQuestion` tool) or waits for a permission prompt,
2. turn green when Claude Code finishes a turn,
3. return to the user's normal colour when the user sends the next message, or when a permission is granted and Claude continues working.

## 2. Hardware and environment

- Strip: DayBetter RGB LED strip, advertises over BLE as `P065_17f3` (the DayBetter app shows it as `P065_17F3`, uppercase; match case-insensitively).
- Host: macOS, Apple Silicon. CoreBluetooth is used through the Python `bleak` library. The terminal app needs Bluetooth permission; macOS prompts on first scan.
- Tooling: `uv` (for `uv run --script` with inline dependencies), Python 3.11 or newer, `bleak>=0.22`. No other dependencies.
- Claude Code with hooks support in `~/.claude/settings.json`.

## 3. Discovering the protocol (what was done, in order)

### 3.1 Search for prior art

The only public code for DayBetter strips was `shindekokoro/homebridge-daybetter` (Node, `@abandonware/noble`). It writes to the second characteristic of service `0000e026-0000-1000-8000-00805f9b34fb` and sends these frames:

| Purpose | Bytes (hex) |
|---|---|
| Power on | `A0 11 04 01 B1 21` |
| Power off | `A0 11 04 00 70 E1` |
| Brightness 100 % | `A0 13 04 64 D0 CA` |
| Colour red | `A0 15 06 FF 00 00 25 C0` |
| Colour green | `A0 15 06 00 FF 00 54 00` |
| Colour blue | `A0 15 06 00 00 FF 55 B0` |

That project hard-coded the last two bytes per colour and called them "unidentified". They are a checksum.

### 3.2 Identify the checksum

Brute force with the `crcmod` library: compute every predefined CRC-16 variant over the frame without its last two bytes and compare to the observed tail. Only one variant matches every sample: **CRC-16/MODBUS** (poly `0x8005` reflected as `0xA001`, init `0xFFFF`, no final xor), appended **little-endian**.

```python
def crc16_modbus(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "little")

assert crc16_modbus(bytes.fromhex("A0110401")) == bytes.fromhex("B121")
assert crc16_modbus(bytes.fromhex("A01506FF0000")) == bytes.fromhex("25C0")
```

Frame layout: `A0 <cmd> <len> <payload...> <crc_lo> <crc_hi>`.

| cmd | payload | meaning |
|---|---|---|
| `11` | `01` / `00` | power on / off |
| `13` | `00`..`64` | brightness 0 to 100 |
| `15` | `RR GG BB` | colour |
| `12` | `C0 00` / `C1 00` | scene night / reading (not used) |

The strip replies on its notify characteristic with the same frame, first byte changed to `A1`, for example `a1110401b0dd` after power on and `a1150401f11c` after a colour write. Use this ACK to confirm the protocol without looking at the strip.

### 3.3 Find the strip

Scan with `bleak`:

```python
import asyncio
from bleak import BleakScanner
async def main():
    for dev, adv in (await BleakScanner.discover(timeout=20, return_adv=True)).values():
        if adv.local_name:
            print(adv.rssi, dev.address, adv.local_name, adv.service_uuids)
asyncio.run(main())
```

Dead end 1: the strip did not appear in several scans. Cause: the DayBetter phone app was connected to it. A BLE peripheral stops advertising while it has a connection. Fix: close the app or turn the phone's Bluetooth off. After that the strip appeared as `P065_17f3` advertising service `0000c065-...`.

Dead end 2: a device named `MELK-OA21 E3` was visible at similar signal strength. It is an ELK-BLEDOM family controller and accepted writes, but it belonged to someone else. Do not write to a device until the user confirms it is theirs. Blink it and ask.

### 3.4 Enumerate the strip

```
SVC 0000e065-0000-1000-8000-00805f9b34fb
   CH 0000a065-0000-1000-8000-00805f9b34fb ['write']
   CH 0000f065-0000-1000-8000-00805f9b34fb ['notify', 'read']
```

Different UUIDs from the homebridge project (`e065` instead of `e026`) but the same frame format. Therefore the script does not look for a specific service; it takes the first writable characteristic and sends `A0` frames to it. The `a065` characteristic is `write` (with response), not `write-without-response`; the script sets `response=True` when the characteristic has the `write` property.

### 3.5 Confirm by probing

Subscribe to the notify characteristic, then send candidate frames. ELK frames (`7e 00 04 01 00 00 00 00 ef`) produced no reply. `A0` frames produced `A1` ACKs. Protocol confirmed. A later blue, then red, then green sequence confirmed the channel order is plain RGB.

### 3.6 Full command set (from the decompiled app)

Decompile: `brew install jadx`, download `com.th.daybetter` 1.9.4 from Aptoide, `jadx --no-res -d out app.apk`, read `com/th/qc/command/CommandUtil.java`, `com/th/common/protocol/qc/QCLightProtocol.java` and `assets/qc_default_mode_data.json`. All frames use `pack(cmd, payload...)` = `A0 cmd len payload crc`.

| cmd | payload | meaning |
|---|---|---|
| `10` | `01` | read state; reply `A1 10 ...` = on, mode, sub, bright, speed, sensitivity, r, g, b, w, orderR, orderG, orderB, hasMic |
| `11` | `01` / `00` | power on / off |
| `12` | `mode sub` | mode. `01 00` colour, `00 00` white, `<scene> FF` built-in scene, `C2 <C1\|C2\|C3>` DIY dynamic / breathe / strobe, `A0 <0-3>` device mic |
| `13` | `1..100` | brightness |
| `15` | `r g b FF` | static colour (send mode `01 00` first) |
| `16` | `1..100` | effect speed |
| `17` | `1..100` | microphone sensitivity |
| `20` | `FF FF (255-w) w` | tunable white, w = warmth (send mode `00 00` first) |
| `21` | `01` / `00` | start / stop music reaction |
| `2B` | `n` | peak stay (gradual peak firmware only) |
| `34` | `num en week h m s act` | timer. num 1 = on, 2 = off. week bits Mon=1 .. Sun=64, 0x80 = once. act 1 = on, 0 = off |
| `35` | `01` | read timers; reply holds two 7-byte records |
| `5C` | `0 / 1 / 2` | after power cut: off / on / last state |
| `A3` | chunked | DIY: data = `C2 pattern bright speed (r g b 65)*` split in 12-byte chunks, each `A0 A3 len idx total idx-1 chunk crc` |

Built-in scene ids for P065 (31): statics `02`..`08`, jumps `09` `0A`, gradients `0B` `0C` `14` `15` `16`, breaths `0D`..`13` `23` `24`, strobes `17`..`1E`. Names are in `daybetter.py`.

The strip answers every frame with the same frame prefixed `A1`; the state query answers with the state record. This strip's state record is 14 bytes (no power-on-reaction or timer bytes), so timers are read with `35`.

## 4. The script: `led.py`

Install location: `~/.claude/scripts/led.py`, executable. It is a `uv` inline script; the shebang `#!/usr/bin/env -S uv run --quiet --script` plus the `# /// script` block make `uv` create and cache a venv with `bleak` on first run.

Behaviour:

- Reads and writes `~/.claude/led-state.json`: `{"name": "P065_17F3", "idle": "ff8c3c", "mode": "idle"}`.
  - `name`: BLE advertised name prefix, matched case-insensitively.
  - `idle`: colour to restore, or `"off"`.
  - `mode`: last colour the script set (`idle`, `red`, `green`). Used by `unred`.
- Commands: `red`, `green`, `restore`, `unred` (restore only if mode is red), `set RRGGBB` (also updates idle), `idle RRGGBB|off`, `on`, `off`, `scan`.
- Takes an exclusive `fcntl.flock` on `~/.claude/led-state.json.lock` for the whole operation. Hooks fire in the background and can overlap; two simultaneous connections to one BLE peripheral fail, so they are serialised.
- Connects by scanning for the name (15 s timeout), picks the protocol (ELK if characteristic `fff3` exists, otherwise DayBetter `A0` on the first writable characteristic), sends power-on then colour, sleeps 150 ms between writes, disconnects.
- `LED_SELFTEST=1 led.py` runs assertions on the CRC and frame builders.

The full source is in `led.py` next to this file. Copy it verbatim.

## 5. Claude Code hooks

Add to `~/.claude/settings.json` under `"hooks"` (the installer merges `hooks.json` for you). Each command is wrapped as `( ... >/dev/null 2>&1 &)` so the hook returns immediately and Claude is not blocked by the two-second BLE connect.

| Event | Matcher | Command | Why |
|---|---|---|---|
| `PreToolUse` | `AskUserQuestion` | `led.py red` | Claude is about to ask a question |
| `Notification` | `permission_prompt` | `led.py red` | Claude is waiting for a permission |
| `Stop` | none | `led.py green` | Claude finished its turn |
| `UserPromptSubmit` | none | `led.py restore` | user replied |
| `PostToolUse` | none | `led.py unred` | a tool ran after a red state, meaning the permission was granted; go back to idle. No-op otherwise. |

## 5b. The desktop app: `app.py` + `daybetter.py`

`daybetter.py` holds the protocol (frame builders, state parser, scene table) and `Strip`, an async bleak client that disconnects after 3 s idle. `app.py` is a pywebview window (WKWebView, HTML inline) whose JavaScript calls Python methods on `Api`. BLE work runs on one asyncio loop in a background thread, serialised by an `asyncio.Lock`.

While the app runs it listens on the unix socket `~/.claude/ailamp.sock`. `led.py` checks for that socket first and, if present, sends `{"cmd", "rgb", "snapshot", "prev"}` as one JSON line and lets the app do the BLE work. That avoids two processes fighting over one peripheral and a deadlock that appeared when both also shared the file lock: the hook holds the lock while it waits for the app, so the app must never take it.

## 6. Install steps for a fresh machine

1. `brew install uv`
2. Clone the repo, run `./install.sh <BLE_NAME> <IDLE_HEX>`.
3. Close the vendor app on the phone.
4. `~/.claude/scripts/led.py scan` and confirm the strip name appears.
5. `~/.claude/scripts/led.py red`, then `restore`. Watch the strip.
6. Start a Claude Code session. Ask it anything. When it finishes, the strip turns green. Reply, and it returns to idle.

## 7. Known limits and gotchas

- The strip has no readable colour state. "Restore" means the configured idle colour, not a true previous value.
- About two seconds of latency per change (scan, connect, write, disconnect each time). Keeping a persistent connection would be faster but would block the phone app permanently.
- If the vendor app is open on the phone, every hook fails silently with "device not found".
- The `Stop` hook fires after every Claude turn, including quick diagnostic turns. While testing colours by hand the strip goes green two seconds after each reply, which looks like a bug. Test sequences inside one shell command.
- `set RRGGBB` also updates `idle`. Run `idle RRGGBB` afterwards if you only wanted a temporary colour.
- Multiple Claude sessions share one strip and one state file. The last hook wins.
- Adapting to another strip: run `scan`, then connect and dump characteristics. If you see `fff3`, it is ELK-BLEDOM and already supported. If you see one write and one notify characteristic under a vendor service, try the `A0` frames and look for an `A1` ACK. Anything else needs a BLE sniff of the vendor app (Android `btsnoop_hci.log`, or macOS PacketLogger with an iPhone).
