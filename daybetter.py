"""DayBetter "QC" BLE LED strip protocol (P065 family) and a small async client.

Frame: A0 <cmd> <len=payload+3> <payload...> <crc16-modbus lo> <crc16-modbus hi>
Reply: same frame with A1 as first byte.

Reverse engineered from the DayBetter Android app (com.th.daybetter 1.9.4,
class com.th.qc.command.CommandUtil, assets/qc_default_mode_data.json).
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

# BLE
NAME_PREFIX = "P065"
WRITE_UUID = "0000a065-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000f065-0000-1000-8000-00805f9b34fb"

# Commands (CommandUtil decimal ids converted to hex)
CMD_READ_STATE = 0x10
CMD_SWITCH = 0x11
CMD_MODE = 0x12
CMD_BRIGHT = 0x13
CMD_COLOR = 0x15
CMD_SPEED = 0x16
CMD_SENSITIVITY = 0x17
CMD_READ_RHYTHM = 0x1B
CMD_WHITE = 0x20
CMD_RHYTHM_START = 0x21
CMD_PEAK_STAY = 0x2B
CMD_SET_ALARM = 0x34
CMD_READ_ALARM = 0x35
CMD_RHYTHM_BRIGHT = 0x51
CMD_READ_SUPPORT = 0x58
CMD_POWER_ON_REACTION = 0x5C
CMD_CUSTOM_SCENE_COLOR = 0xA1
CMD_CUSTOM_SCENE_ALL = 0xA3

# Modes (QCConstants)
MODE_WHITE = 0
MODE_COLOR = 1
MODE_DEVICE_MIC = 0xA0
MODE_PHONE_MIC = 0xA1
MODE_MUSIC_RHYTHM = 0xB0
MODE_DIY = 0xC2
DIY_DYNAMIC, DIY_BREATHE, DIY_STROBE = 0xC1, 0xC2, 0xC3
DIY_RGB_MARKER = 101  # 4th byte of an RGB colour slot; a white slot carries its warmth instead
MIC_SUBMODES = ["Classic", "Soft", "Dynamic", "Disco"]
POWER_ON = {0: "Off", 1: "On", 2: "Last state"}

# Built-in scenes for P065: (mode id, name, animated). Sent as A0 12 05 <id> FF crc.
SCENES = [
    (0x02, "Static Red", False), (0x03, "Static Blue", False), (0x04, "Static Green", False),
    (0x05, "Static Cyan", False), (0x06, "Static Yellow", False), (0x07, "Static Purple", False),
    (0x08, "Static White", False),
    (0x0B, "Three-colour Gradient", True), (0x0C, "Seven-colour Gradient", True),
    (0x14, "Red Green Gradient", True), (0x15, "Red Blue Gradient", True), (0x16, "Green Blue Gradient", True),
    (0x09, "Three-colour Jump", True), (0x0A, "Seven-colour Jump", True),
    (0x0D, "Red Breath", True), (0x0E, "Green Breath", True), (0x0F, "Blue Breath", True),
    (0x10, "Yellow Breath", True), (0x11, "Cyan Breath", True), (0x12, "Purple Breath", True),
    (0x13, "White Breath", True), (0x24, "Three-colour Breath", True), (0x23, "Multi-colour Breath", True),
    (0x17, "Seven-colour Strobe", True), (0x18, "Red Strobe", True), (0x19, "Green Strobe", True),
    (0x1A, "Blue Strobe", True), (0x1B, "Yellow Strobe", True), (0x1C, "Cyan Strobe", True),
    (0x1D, "Purple Strobe", True), (0x1E, "White Strobe", True),
]
SCENE_NAMES = {sid: name for sid, name, _ in SCENES}


def crc16_modbus(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "little")


def pack(cmd: int, *payload: int) -> bytes:
    body = bytes([0xA0, cmd & 0xFF, len(payload) + 3, *[p & 0xFF for p in payload]])
    return body + crc16_modbus(body)


# Frame builders
def switch(on: bool) -> bytes:
    return pack(CMD_SWITCH, 1 if on else 0)


def mode(m: int, sub: int = 0) -> bytes:
    return pack(CMD_MODE, m, sub)


def brightness(pct: int) -> bytes:
    return pack(CMD_BRIGHT, max(1, min(100, pct)))


def speed(pct: int) -> bytes:
    return pack(CMD_SPEED, max(1, min(100, pct)))


def sensitivity(pct: int) -> bytes:
    return pack(CMD_SENSITIVITY, max(1, min(100, pct)))


def color(r: int, g: int, b: int) -> list[bytes]:
    """Static colour. The app switches to MODE_COLOR first, then sends RGB + 0xFF."""
    return [mode(MODE_COLOR, 0), pack(CMD_COLOR, r, g, b, 255)]


def white(warmth: int) -> list[bytes]:
    """Tunable white, warmth 0 (cool) .. 255 (warm)."""
    warmth = max(0, min(255, warmth))
    return [mode(MODE_WHITE, 0), pack(CMD_WHITE, 0xFF, 0xFF, 255 - warmth, warmth)]


def scene(scene_id: int) -> bytes:
    return mode(scene_id, 0xFF)


def power_on_reaction(value: int) -> bytes:
    return pack(CMD_POWER_ON_REACTION, value)


def rhythm(on: bool) -> bytes:
    return pack(CMD_RHYTHM_START, 1 if on else 0)


def alarm(number: int, enable: bool, week: int, hour: int, minute: int, second: int = 0, action: int = 0) -> bytes:
    """number 1 = turn-on timer, 2 = turn-off timer. week bits: bit0 Mon .. bit6 Sun, 0x80 = once.
    action 1 = on, 0 = off."""
    return pack(CMD_SET_ALARM, number, 1 if enable else 0, week, hour, minute, second, action)


def once_week(hour: int, minute: int) -> int:
    """Week byte the app sends for a one-shot timer: the next matching day, flagged with 0x80."""
    t = time.localtime()
    weekday = t.tm_wday  # Mon=0
    if (t.tm_hour, t.tm_min) >= (hour, minute):
        weekday = (weekday + 1) % 7
    return 0x80 | (1 << weekday)


def diy(colors: list[tuple[int, int, int] | int], pattern: int = DIY_DYNAMIC, bright: int = 50,
        spd: int = 50, rgb_index: bool = False) -> list[bytes]:
    """Custom scene. Each colour is an (r, g, b) tuple or an int warmth for a white slot.
    Chunked as A0 A3 <len> <i+1> <total> <i> <12 bytes> crc, like CommandUtil.packCustomScene."""
    data = [MODE_DIY, pattern, bright, spd]
    for c in colors:
        if isinstance(c, int):
            data += [0xFF, 0xFF, 0xFF, c & 0xFF]
        else:
            data += [c[0], c[1], c[2], DIY_RGB_MARKER]
    if rgb_index:
        data.append(0)
    chunks = [data[i:i + 12] for i in range(0, len(data), 12)]
    out = []
    for i, chunk in enumerate(chunks):
        body = bytes([0xA0, CMD_CUSTOM_SCENE_ALL, len(chunk) + 6, i + 1, len(chunks), i, *chunk])
        out.append(body + crc16_modbus(body))
    return out


@dataclass
class State:
    on: bool = False
    mode: int = MODE_COLOR
    sub: int = 0
    bright: int = 100
    speed: int = 50
    sensitivity: int = 50
    r: int = 0
    g: int = 0
    b: int = 0
    w: int = 0
    device_mic: bool = False
    power_on_reaction: int | None = None
    peak_stay: int | None = None
    timers: list[dict] = field(default_factory=list)
    raw: str = ""

    def describe(self) -> str:
        if not self.on:
            return "Off"
        if self.mode == MODE_COLOR:
            return f"Colour #{self.r:02x}{self.g:02x}{self.b:02x}"
        if self.mode == MODE_WHITE:
            return f"White, warmth {self.w}"
        if self.mode == MODE_DIY:
            return "DIY scene"
        if self.mode in (MODE_DEVICE_MIC, MODE_PHONE_MIC, MODE_MUSIC_RHYTHM):
            return "Music"
        return SCENE_NAMES.get(self.mode, f"Mode {self.mode:#x}")

    def restore_frames(self) -> list[bytes]:
        """Frames that put the strip back into this state."""
        if not self.on:
            return [switch(False)]
        out = [switch(True)]
        if self.mode == MODE_COLOR:
            out += color(self.r, self.g, self.b)
        elif self.mode == MODE_WHITE:
            out += white(self.w)
        else:
            out.append(mode(self.mode, self.sub))
        out.append(brightness(self.bright))
        return out


def parse_state(frame: bytes) -> State:
    """Parse the A1 10 reply (QCLightProtocol.readLight)."""
    d = frame[3:-2]
    st = State(on=d[0] == 1, mode=d[1], sub=d[2], bright=max(1, d[3]), speed=max(1, d[4]),
               sensitivity=max(1, d[5]), r=d[6], g=d[7], b=d[8], w=d[9], device_mic=len(d) > 13 and d[13] == 1,
               raw=frame.hex())
    if len(d) >= 40:
        st.power_on_reaction = d[24]
        st.timers = parse_timers(d[25:39])
    if len(d) >= 41 and d[40] != 255:
        st.peak_stay = d[40]
    return st


def parse_timers(d: bytes) -> list[dict]:
    """Two 7-byte records: number, enable, week, hour, minute, second, action."""
    out = []
    for i in range(0, len(d) - 6, 7):
        n, en, wk, h, m, s, act = d[i:i + 7]
        out.append({"number": n, "enable": en == 1, "week": wk, "hour": h, "minute": m, "second": s, "action": act})
    return out


class Strip:
    """One BLE connection. Disconnects after `idle` seconds without traffic so the hook script
    (direct BLE fallback) and the phone app can use the radio. Not thread-safe: call from one loop,
    and serialise callers with an asyncio.Lock."""

    def __init__(self, name_prefix: str = NAME_PREFIX, idle: float = 3.0):
        self.name_prefix = name_prefix
        self.idle = idle
        self.client = None
        self.lock = asyncio.Lock()
        self._last = 0.0
        self._replies: asyncio.Queue = asyncio.Queue()
        self._idle_task = None

    async def connect(self):
        from bleak import BleakClient, BleakScanner
        if self.client and self.client.is_connected:
            return
        dev = await BleakScanner.find_device_by_filter(
            lambda d, a: (a.local_name or d.name or "").lower().startswith(self.name_prefix.lower()), timeout=15)
        if dev is None:
            raise RuntimeError(f"{self.name_prefix} not found. Close the DayBetter phone app and power the strip.")
        self.client = BleakClient(dev, timeout=20)
        await self.client.connect()
        await self.client.start_notify(NOTIFY_UUID, lambda _h, data: self._replies.put_nowait(bytes(data)))
        self._touch()

    def _touch(self):
        self._last = time.monotonic()
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_watch())

    async def _idle_watch(self):
        while True:
            await asyncio.sleep(0.5)
            async with self.lock:
                if time.monotonic() - self._last >= self.idle:
                    await self._disconnect()
                    return

    async def disconnect(self):
        async with self.lock:
            await self._disconnect()

    async def _disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    async def send(self, frames: bytes | list[bytes]):
        async with self.lock:
            await self._send(frames)

    async def _send(self, frames):
        await self.connect()
        for fr in ([frames] if isinstance(frames, bytes) else frames):
            await self.client.write_gatt_char(WRITE_UUID, fr, response=True)
            await asyncio.sleep(0.08)
        self._touch()

    async def query(self, frame: bytes, reply_cmd: int, timeout: float = 2.0) -> bytes:
        async with self.lock:
            return await self._query(frame, reply_cmd, timeout)

    async def _query(self, frame: bytes, reply_cmd: int, timeout: float = 2.0) -> bytes:
        await self.connect()
        while not self._replies.empty():
            self._replies.get_nowait()
        await self.client.write_gatt_char(WRITE_UUID, frame, response=True)
        self._touch()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                reply = await asyncio.wait_for(self._replies.get(), deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            if len(reply) > 2 and reply[0] == 0xA1 and reply[1] == reply_cmd:
                return reply
        raise RuntimeError("no reply from strip")

    async def read_state(self) -> State:
        async with self.lock:
            st = parse_state(await self._query(pack(CMD_READ_STATE, 1), CMD_READ_STATE))
            if not st.timers:
                try:
                    st.timers = parse_timers((await self._query(pack(CMD_READ_ALARM, 1), CMD_READ_ALARM))[3:-2])
                except RuntimeError:
                    pass
            return st


if __name__ == "__main__":
    assert pack(CMD_SWITCH, 1).hex() == "a0110401b121"
    assert pack(CMD_COLOR, 0xFF, 0, 0).hex() == "a01506ff000025c0"
    assert scene(0x0B).hex() == "a012050bfff610"
    assert scene(0x23).hex() == "a0120523ffe810"
    assert brightness(100).hex() == "a0130464d0ca"
    st = parse_state(bytes.fromhex("a110110101416432324b00820000000000277a"))
    assert (st.on, st.mode, st.bright, st.speed, (st.r, st.g, st.b)) == (True, 1, 100, 50, (0x4B, 0x00, 0x82)), st
    assert st.describe() == "Colour #4b0082"
    t = parse_timers(bytes.fromhex("01000000000001" "02000000000000"))
    assert t[0]["number"] == 1 and t[0]["action"] == 1 and t[1]["number"] == 2
    frames = diy([(255, 0, 0), (0, 0, 255)], DIY_BREATHE, 50, 50)
    assert frames[0][:6].hex() == "a0a312010100" and len(frames) == 1, frames
    assert once_week(0, 0) & 0x80
    print("daybetter selftest ok")
