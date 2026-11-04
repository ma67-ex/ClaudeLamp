#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=0.22", "pywebview>=5", "sounddevice>=0.5", "numpy>=1.26"]
# ///
"""AiLamp desktop app: every DayBetter control for the P065 strip, plus Claude Code status light settings.

Run:  ./app.py        (needs uv; first start downloads bleak + pywebview)
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daybetter as db  # noqa: E402

STATE = os.path.expanduser("~/.claude/led-state.json")
SOCK = os.path.expanduser("~/.claude/ailamp.sock")  # led.py hooks talk to the running app through this
LED_PY = os.path.expanduser("~/.claude/scripts/led.py")


def load_cfg():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"name": "P065_17F3", "idle": "4b0082", "mode": "idle"}


def save_cfg(cfg):
    with open(STATE, "w") as f:
        json.dump(cfg, f)


class AudioMeter:
    """Default input device -> loudness in 0..1 and a bass-onset flag, updated ~40 times a second."""

    def __init__(self, api):
        self.api, self.level, self.beat = api, 0.0, False
        self._avg = 1e-4
        self._bass_avg = 1e-4
        self.stream = None

    def start(self):
        import numpy as np
        import sounddevice as sd
        self.np = np
        self.stream = sd.InputStream(channels=1, samplerate=44100, blocksize=1024, callback=self._cb)
        self.stream.start()

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def _cb(self, data, frames, t, status):
        np = self.np
        x = data[:, 0]
        rms = float(np.sqrt(np.mean(x * x)) + 1e-9)
        spec = np.abs(np.fft.rfft(x))
        bass = float(spec[1:8].mean() + 1e-9)          # roughly 40..300 Hz at 44.1 kHz / 1024
        gain = 1.0 + self.api.sens / 25.0                # sensitivity slider 1..100 -> gain 1..5
        self._avg = 0.98 * self._avg + 0.02 * rms
        self._bass_avg = 0.95 * self._bass_avg + 0.05 * bass
        lvl = min(1.0, (rms / (self._avg * 2.0 + 1e-6)) * 0.5 * gain * min(1.0, rms * 40 * gain))
        self.level = 0.6 * self.level + 0.4 * lvl
        self.api._level = self.level
        if bass > self._bass_avg * 1.6 and bass > 0.02 / gain:
            self.beat = True


class Api:
    """Methods callable from the page via window.pywebview.api.<name>(...)."""

    def __init__(self):
        self.cfg = load_cfg()
        self.sens = 50
        self._level = 0.0
        self._audio = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.strip = None
        asyncio.run_coroutine_threadsafe(self._serve_hooks(), self.loop)

    async def _serve_hooks(self):
        """Unix socket server so ~/.claude/scripts/led.py can use our BLE connection instead of fighting for the radio."""
        if os.path.exists(SOCK):
            os.remove(SOCK)
        await asyncio.start_unix_server(self._hook_conn, SOCK)

    async def _hook_conn(self, reader, writer):
        """led.py sends {"frames": [hex, ...]} as one line; we write them on our connection and answer {"ok": true}."""
        try:
            req = json.loads((await reader.readline()).decode())
            self._stop_chaos()  # a status change from Claude beats the party
            self.music_host(False, self.sens)
            await (await self._strip()).send([bytes.fromhex(h) for h in req["frames"]])
            writer.write((json.dumps({"ok": True}) + "\n").encode())
        except Exception as e:
            writer.write((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
        try:
            await writer.drain()
        except OSError:
            pass
        writer.close()

    def _run(self, coro):
        try:
            return {"ok": True, "result": asyncio.run_coroutine_threadsafe(coro, self.loop).result(30)}
        except Exception as e:  # surfaced in the UI
            return {"ok": False, "error": str(e)}

    async def _strip(self):
        if self.strip is None or self.strip.name_prefix != self.cfg["name"]:
            if self.strip:
                await self.strip.disconnect()
            self.strip = db.Strip(self.cfg["name"])
        return self.strip

    async def _send(self, frames):
        await (await self._strip()).send(frames)
        return True

    async def _state(self):
        st = await (await self._strip()).read_state()
        return {"on": st.on, "mode": st.mode, "sub": st.sub, "bright": st.bright, "speed": st.speed,
                "sensitivity": st.sensitivity, "rgb": f"#{st.r:02x}{st.g:02x}{st.b:02x}", "w": st.w,
                "device_mic": st.device_mic, "power_on": st.power_on_reaction, "timers": st.timers,
                "label": st.describe(), "raw": st.raw}

    # Light
    def get_state(self):
        return self._run(self._state())

    def power(self, on):
        return self._run(self._send(db.switch(bool(on))))

    def brightness(self, pct):
        return self._run(self._send(db.brightness(int(pct))))

    def color(self, hexrgb):
        h = hexrgb.lstrip("#")
        return self._run(self._send(db.color(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))))

    def white(self, warmth):
        return self._run(self._send(db.white(int(warmth))))

    # Scenes
    def scenes(self):
        return [{"id": sid, "name": name, "animated": anim} for sid, name, anim in db.SCENES]

    def scene(self, sid):
        return self._run(self._send(db.scene(int(sid))))

    def speed(self, pct):
        return self._run(self._send(db.speed(int(pct))))

    def disco(self):
        """Ultimate disco: firmware seven-colour strobe at full speed and brightness. Fastest the strip can do on its own."""
        self._stop_chaos()
        return self._run(self._send([db.switch(True), db.brightness(100), db.scene(0x17), db.speed(100)]))

    def police(self):
        """Firmware DIY scene cycling blue, white, red at full speed and brightness."""
        self._stop_chaos()
        frames = [db.switch(True)] + db.diy([(0, 0, 255), (255, 255, 255), (255, 0, 0)], db.DIY_DYNAMIC, 100, 100,
                                            bool(self.cfg.get("diy_rgb_index", False)))
        return self._run(self._send(frames))

    def chaos(self, on, palette=None):
        """Host-driven cycling: one acknowledged BLE write per colour (13.6 per second measured).
        palette: list of hex colours played in order, or None for random disco."""
        self._stop_chaos()
        if on:
            seq = None
            if palette:
                seq = [(int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)) for h in palette]
            self._chaos = asyncio.run_coroutine_threadsafe(self._chaos_loop(seq), self.loop)
        return {"ok": True}

    def _stop_chaos(self):
        task = getattr(self, "_chaos", None)
        if task and not task.done():
            task.cancel()
        self._chaos = None

    async def _chaos_loop(self, seq=None):
        import random
        disco = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255), (255, 255, 255)]
        strip = await self._strip()
        await strip.send([db.switch(True), db.brightness(100)] + db.color(255, 0, 0))
        i = 0
        while True:
            r, g, b = seq[i % len(seq)] if seq else random.choice(disco)
            i += 1
            await strip.send(db.pack(db.CMD_COLOR, r, g, b, 255), gap=0)  # one acknowledged write per colour

    # DIY
    def diy(self, colors, pattern, bright, spd):
        slots = []
        for c in colors:
            if isinstance(c, str):
                h = c.lstrip("#")
                slots.append((int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))
            else:
                slots.append(int(c))
        frames = db.diy(slots, int(pattern), int(bright), int(spd), bool(self.cfg.get("diy_rgb_index", False)))
        return self._run(self._send(frames))

    # Music (device mic only; this strip reports whether it has one)
    def music(self, on, submode, sens):
        frames = [db.mode(db.MODE_DEVICE_MIC, int(submode)), db.sensitivity(int(sens)), db.rhythm(bool(on))]
        return self._run(self._send(frames))

    # Music, Mac microphone: Python captures audio, maps it to colour frames, and feeds the level back to the page
    def music_host(self, on, sens):
        self.sens = int(sens)
        if on and not getattr(self, "_audio", None):
            self._stop_chaos()
            self._level = 0.0
            self._audio = AudioMeter(self)
            try:
                self._audio.start()
            except Exception as e:
                self._audio = None
                return {"ok": False, "error": f"microphone: {e}"}
            self._music_task = asyncio.run_coroutine_threadsafe(self._music_loop(), self.loop)
        elif not on and getattr(self, "_audio", None):
            self._audio.stop()
            self._audio = None
            t = getattr(self, "_music_task", None)
            if t:
                t.cancel()
        return {"ok": True}

    def music_level(self):
        return float(getattr(self, "_level", 0.0))

    async def _music_loop(self):
        import colorsys
        strip = await self._strip()
        await strip.send([db.switch(True), db.brightness(100)] + db.color(255, 0, 255))
        hue, last_beat = 0.0, 0.0
        while True:
            m = self._audio
            if m is None:
                return
            level = m.level
            if m.beat and time.monotonic() - last_beat > 0.12:   # new bass hit: jump hue
                hue = (hue + 0.17 + 0.2 * level) % 1.0
                last_beat = time.monotonic()
                m.beat = False
            else:
                hue = (hue + 0.002) % 1.0
            v = 0.12 + 0.88 * level
            r, g, b = (int(c * 255 * v) for c in colorsys.hsv_to_rgb(hue, 1.0, 1.0))
            await strip.send(db.pack(db.CMD_COLOR, r, g, b, 255), gap=0)
            await asyncio.sleep(0.02)

    def log(self, msg):
        print("[ui]", msg, file=sys.stderr)
        return True

    # Timers
    def timer(self, number, enable, week, hour, minute):
        number, week, hour, minute = int(number), int(week), int(hour), int(minute)
        if week == 0:
            week = db.once_week(hour, minute)
        action = 1 if number == 1 else 0
        return self._run(self._send(db.alarm(number, bool(enable), week, hour, minute, 0, action)))

    # Settings
    def power_on(self, value):
        return self._run(self._send(db.power_on_reaction(int(value))))

    def get_settings(self):
        self.cfg = load_cfg()
        return {"name": self.cfg.get("name"), "idle": self.cfg.get("idle"),
                "diy_rgb_index": self.cfg.get("diy_rgb_index", False), "hooks": os.path.exists(LED_PY)}

    def set_settings(self, name, idle, diy_rgb_index):
        self.cfg = load_cfg()
        self.cfg.update({"name": name.strip(), "idle": idle.lstrip("#").lower(), "diy_rgb_index": bool(diy_rgb_index)})
        save_cfg(self.cfg)
        return {"ok": True}

    def hook_test(self, what):
        """Run the Claude Code hook script so the user can see red / green / restore."""
        if not os.path.exists(LED_PY):
            return {"ok": False, "error": "led.py not installed; run install.sh"}
        p = subprocess.run([LED_PY, what], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        return {"ok": p.returncode == 0, "error": (p.stderr or p.stdout).strip()}




# ---------- menu bar (macOS status item) ----------
MENU = [
    ("Open AiLamp", "open"), None,
    ("Power on", "on"), ("Power off", "off"), None,
    ("Purple", "c:4b0082"), ("Red", "c:ff0000"), ("Blue", "c:0040ff"), ("Green", "c:00ff40"),
    ("Pink", "c:ff2bd6"), ("White", "c:ffffff"), ("Warm white", "w:220"), None,
    ("Brightness 100%", "b:100"), ("Brightness 50%", "b:50"), ("Brightness 20%", "b:20"), None,
    ("Ultimate Disco", "disco"), ("Police", "police"), ("Seven-colour Gradient", "s:12"), ("Purple Breath", "s:18"), None,
    ("Off in 15 min", "t:15"), ("Off in 30 min", "t:30"), ("Off in 1 hour", "t:60"), ("Cancel off timer", "t:0"), None,
    ("Quit AiLamp", "quit"),
]


def install_menubar(api, window):
    """NSStatusItem with a preset menu. Must run on the main (Cocoa) thread."""
    import datetime
    import objc
    from AppKit import NSImage, NSMenu, NSMenuItem, NSObject, NSStatusBar, NSVariableStatusItemLength, NSApplication

    class Target(NSObject):
        def act_(self, sender):
            tag = sender.representedObject()
            threading.Thread(target=run_action, args=(tag,), daemon=True).start()

    def run_action(tag):
        try:
            if tag == "open":
                from Foundation import NSOperationQueue
                NSOperationQueue.mainQueue().addOperationWithBlock_(lambda: (window.show(), NSApplication.sharedApplication().activateIgnoringOtherApps_(True)))
            elif tag == "quit":
                os._exit(0)
            elif tag == "on":
                api.power(True)
            elif tag == "off":
                api.power(False)
            elif tag.startswith("c:"):
                api.power(True); api.color(tag[2:])
            elif tag.startswith("w:"):
                api.power(True); api.white(int(tag[2:]))
            elif tag.startswith("b:"):
                api.brightness(int(tag[2:]))
            elif tag.startswith("s:"):
                api.power(True); api.scene(int(tag[2:]))
            elif tag == "disco":
                api.disco()
            elif tag == "police":
                api.police()
            elif tag.startswith("t:"):
                mins = int(tag[2:])
                if mins == 0:
                    api.timer(2, False, 0x80, 0, 0)
                else:
                    when = datetime.datetime.now() + datetime.timedelta(minutes=mins)
                    api.timer(2, True, 0, when.hour, when.minute)
        except Exception as e:
            print("[menu]", tag, e, file=sys.stderr)

    target = Target.alloc().init()
    menu = NSMenu.alloc().init()
    for entry in MENU:
        if entry is None:
            menu.addItem_(NSMenuItem.separatorItem())
            continue
        title, tag = entry
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "act:", "")
        item.setTarget_(target)
        item.setRepresentedObject_(tag)
        menu.addItem_(item)
    item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
    icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_("lightbulb.led.fill", "AiLamp")
    if icon is None:
        icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_("lightbulb.fill", "AiLamp")
    icon.setTemplate_(True)
    item.button().setImage_(icon)
    item.setMenu_(menu)
    api._status_item = item   # keep a reference or Cocoa releases it
    api._menu_target = target


def main():
    import webview
    api = Api()
    ui = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
    window = webview.create_window("AiLamp", url=ui, js_api=api, width=1180, height=760, min_size=(820, 600))

    def on_closing():
        window.hide()      # closing the window keeps the app alive in the menu bar
        return False

    window.events.closing += on_closing

    def after_start():
        from PyObjCTools import AppHelper
        AppHelper.callAfter(install_menubar, api, window)

    try:
        webview.start(after_start)
    finally:
        if os.path.exists(SOCK):
            os.remove(SOCK)


if __name__ == "__main__":
    main()
