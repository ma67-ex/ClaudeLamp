#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=0.22", "pywebview>=5", "sounddevice>=0.5", "numpy>=1.26"]
# ///
"""ClaudeLamp desktop app: every DayBetter control for the P065 strip, plus Claude Code status light settings.

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
SOCK = os.path.expanduser("~/.claude/claudelamp.sock")  # led.py hooks talk to the running app through this
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
        self._claude_state, self._claude_at = "idle", 0
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
            if "js" in req:                                   # local debugging aid: run JS in the main window
                self._main_window.evaluate_js(req["js"])
                writer.write(b'{"ok": true}\n')
                await writer.drain()
                writer.close()
                return
            self._claude_state = req.get("state", self._claude_state)
            self._claude_at = time.time()
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

    def claude_state(self):
        """Last state the Claude Code hook reported: working / needs you / done / idle."""
        return {"state": getattr(self, "_claude_state", "idle"), "at": getattr(self, "_claude_at", 0)}

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

    def _main_window_visible(self):
        from AppKit import NSApplication
        for w in NSApplication.sharedApplication().windows():
            if w.title() == "ClaudeLamp":
                return bool(w.isVisible())
        return False

    def open_full(self):
        from Foundation import NSOperationQueue
        from AppKit import NSApplication

        def show():
            if getattr(self, "_hide_pop", None):
                self._hide_pop()
            self._own_activate_at = time.monotonic()
            self._main_window.show()
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        NSOperationQueue.mainQueue().addOperationWithBlock_(show)
        return True

    def quit_app(self):
        os._exit(0)

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




# ---------- menu bar: status item + native NSPopover holding a WKWebView ----------
POP_W, POP_H = 372, 640


def install_menubar(api, window):
    try:
        _install_menubar(api, window)
    except Exception:
        import traceback
        traceback.print_exc()


def _install_menubar(api, window):
    """Bulb in the menu bar. Click opens a transient NSPopover whose content is popover.html in a WKWebView.
    The page calls Python through window.webkit.messageHandlers.api; replies come back via __reply(id, json)."""
    import objc
    from AppKit import (NSImage, NSMenu, NSMenuItem, NSObject, NSStatusBar, NSVariableStatusItemLength, NSApplication,
                        NSPopover, NSViewController, NSAppearance, NSMakeRect, NSEvent)
    from Foundation import NSURL, NSOperationQueue
    import WebKit

    here = os.path.dirname(os.path.abspath(__file__))

    class Bridge(NSObject):
        def userContentController_didReceiveScriptMessage_(self, ucc, msg):
            body = msg.body()
            call_id, method, args = body["id"], body["method"], list(body.get("args") or [])

            def work():
                try:
                    result = getattr(api, method)(*args)
                    payload = json.dumps({"ok": True, "result": result})
                except Exception as e:
                    payload = json.dumps({"ok": False, "error": str(e)})
                NSOperationQueue.mainQueue().addOperationWithBlock_(
                    lambda: api._pop_web.evaluateJavaScript_completionHandler_(f"__reply({int(call_id)},{payload})", None))
            threading.Thread(target=work, daemon=True).start()

    class Target(NSObject):
        def toggle_(self, sender):
            try:
                pop = api._popover
                if pop.isShown():
                    pop.close()
                    return
                api._main_window.hide()                                            # one surface at a time
                btn = api._status_item.button()
                pop.showRelativeToRect_ofView_preferredEdge_(btn.bounds(), btn, 1)   # 1 = NSMinYEdge, below the bar
                api._own_activate_at = time.monotonic()                            # this activation is ours, not a Dock click
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                api._pop_web.evaluateJavaScript_completionHandler_("typeof refresh==='function'&&refresh()", None)
            except Exception:
                import traceback
                traceback.print_exc()

        def quit_(self, sender):
            os._exit(0)

        def open_(self, sender):
            api.open_full()

        def activated_(self, note):
            """App became active. Our own activations (popover, open_full) are ignored for a second;
            anything else is a Dock click or reopen and brings the full window."""
            if time.monotonic() - getattr(api, "_own_activate_at", 0) < 1.0:
                return
            if time.monotonic() - getattr(api, "_install_at", 0) < 3.0:            # launch activation, not a Dock click
                return
            print("[menubar] external activation -> full window", file=sys.stderr)
            if api._popover.isShown() or not api._main_window_visible():
                api.open_full()

    bridge = Bridge.alloc().init()
    cfg = WebKit.WKWebViewConfiguration.alloc().init()
    cfg.userContentController().addScriptMessageHandler_name_(bridge, "api")
    web = WebKit.WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, POP_W, POP_H), cfg)
    web.setValue_forKey_(False, "drawsBackground")
    web.loadFileURL_allowingReadAccessToURL_(NSURL.fileURLWithPath_(os.path.join(here, "popover.html")), NSURL.fileURLWithPath_(here))
    vc = NSViewController.alloc().init()
    vc.setView_(web)
    pop = NSPopover.alloc().init()
    pop.setContentViewController_(vc)
    pop.setContentSize_((POP_W, POP_H))
    pop.setBehavior_(0 if os.environ.get("CLAUDELAMP_SHOT") else 1)      # transient: closes on outside click
    pop.setAnimates_(True)
    pop.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameVibrantDark"))

    target = Target.alloc().init()
    item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
    icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_("lightbulb.led.fill", "ClaudeLamp") \
        or NSImage.imageWithSystemSymbolName_accessibilityDescription_("lightbulb.fill", "ClaudeLamp")
    icon.setTemplate_(True)
    item.button().setImage_(icon)
    item.button().setTarget_(target)
    item.button().setAction_("toggle:")
    menu = NSMenu.alloc().init()                                         # kept for a right-click fallback
    for title, sel in (("Open ClaudeLamp", "open:"), ("Quit ClaudeLamp", "quit:")):
        mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, "")
        mi.setTarget_(target)
        menu.addItem_(mi)
    api._status_item, api._menu_target, api._popover, api._pop_web, api._bridge, api._status_menu = item, target, pop, web, bridge, menu
    api._hide_pop = lambda: pop.close()
    api._own_activate_at, api._install_at = 0.0, time.monotonic()
    from Foundation import NSNotificationCenter
    NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(target, "activated:", "NSApplicationDidBecomeActiveNotification", None)
    # Dock click / reopen while already active sends no activation notification; hook the delegate method instead
    try:
        delegate = NSApplication.sharedApplication().delegate()

        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
            print("[menubar] reopen -> full window", file=sys.stderr)
            api.open_full()
            return False
        objc.classAddMethods(type(delegate), [applicationShouldHandleReopen_hasVisibleWindows_])
    except Exception as e:
        print("[menubar] reopen hook failed:", e, file=sys.stderr)
    print("[menubar] status item + popover installed", file=sys.stderr)


def main():
    import webview
    api = Api()
    here = os.path.dirname(os.path.abspath(__file__))
    window = webview.create_window("ClaudeLamp", url=os.path.join(here, "ui.html"), js_api=api, width=1180, height=760,
                                   min_size=(820, 600), hidden=not os.environ.get("CLAUDELAMP_SHOW_MAIN"))  # launch goes to the popover
    api._main_window = window

    def on_closing():
        window.hide()      # closing the window keeps the app alive in the menu bar
        return False

    window.events.closing += on_closing

    def after_start():
        from PyObjCTools import AppHelper
        AppHelper.callAfter(install_menubar, api, window)
        if not os.environ.get("CLAUDELAMP_SHOW_MAIN"):
            for _ in range(40):                             # wait until the status button has a real frame
                time.sleep(0.25)
                item = getattr(api, "_status_item", None)
                try:
                    if item and item.button().window() and item.button().window().frame().size.width > 0:
                        break
                except Exception:
                    pass
            time.sleep(0.5)
            AppHelper.callAfter(lambda: api._menu_target.toggle_(api._status_item.button()))

    try:
        webview.start(after_start)
    finally:
        if os.path.exists(SOCK):
            os.remove(SOCK)


if __name__ == "__main__":
    main()
