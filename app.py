#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=0.22", "pywebview>=5"]
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


class Api:
    """Methods callable from the page via window.pywebview.api.<name>(...)."""

    def __init__(self):
        self.cfg = load_cfg()
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

    def chaos(self, on):
        """Host-driven disco: random colour every acknowledged BLE write (13.6 per second measured)."""
        if on:
            self._stop_chaos()
            self._chaos = asyncio.run_coroutine_threadsafe(self._chaos_loop(), self.loop)
            return {"ok": True}
        self._stop_chaos()
        return {"ok": True}

    def _stop_chaos(self):
        task = getattr(self, "_chaos", None)
        if task and not task.done():
            task.cancel()
        self._chaos = None

    async def _chaos_loop(self):
        import random
        strip = await self._strip()
        await strip.send([db.switch(True), db.brightness(100)] + db.color(255, 0, 0))
        while True:
            r, g, b = random.choice([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255), (255, 255, 255)])
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


HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>AiLamp</title>
<style>
:root{--bg:#0f1014;--panel:#181a21;--line:#262932;--fg:#e8e9ee;--mut:#8a8f9c;--acc:#7c5cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,Inter,system-ui,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:14px 18px;border-bottom:1px solid var(--line)}
header h1{font-size:16px;margin:0;font-weight:600}#status{color:var(--mut);font-size:13px;flex:1}
#dot{width:10px;height:10px;border-radius:50%;background:#333;box-shadow:0 0 12px currentColor}
nav{display:flex;gap:4px;padding:10px 18px 0}nav button{background:none;border:0;color:var(--mut);padding:8px 12px;border-radius:8px;cursor:pointer;font-size:14px}
nav button.on{background:var(--panel);color:var(--fg)}
main{padding:14px 18px 24px}section{display:none}section.on{display:block}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:12px}
.row{display:flex;align-items:center;gap:12px;margin:8px 0}.row label{min-width:110px;color:var(--mut)}
input[type=range]{flex:1;accent-color:var(--acc)}input[type=color]{width:56px;height:36px;border:0;background:none;padding:0;cursor:pointer}
input[type=time],input[type=text],select{background:#0f1014;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:7px 9px;font-size:14px}
button.b{background:#23262f;color:var(--fg);border:1px solid var(--line);border-radius:9px;padding:8px 12px;cursor:pointer;font-size:13px}
button.b:hover{border-color:var(--acc)}button.b.on{background:var(--acc);border-color:var(--acc);color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.sw{display:flex;gap:8px;flex-wrap:wrap}.sw span{width:30px;height:30px;border-radius:50%;cursor:pointer;border:2px solid transparent}.sw span:hover{border-color:#fff}
.slots{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.slot{position:relative}.slot b{position:absolute;top:-6px;right:-6px;background:#000;border-radius:50%;width:18px;height:18px;font-size:11px;text-align:center;line-height:18px;cursor:pointer;color:#fff}
.week{display:flex;gap:4px}.week button{width:38px}
.err{color:#ff6b6b;font-size:13px;min-height:18px;margin-top:6px}.hint{color:var(--mut);font-size:12.5px}
.toggle{width:46px;height:26px;border-radius:13px;background:#333;position:relative;cursor:pointer;border:0}.toggle::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:.15s}
.toggle.on{background:#2ecc71}.toggle.on::after{left:23px}
</style></head><body>
<header><div id="dot"></div><h1>AiLamp</h1><div id="status">connecting…</div><button class="b" onclick="refresh()">Refresh</button><button id="pw" class="toggle" onclick="togglePower()"></button></header>
<nav><button class="on" data-t="light">Light</button><button data-t="scenes">Scenes</button><button data-t="diy">DIY</button><button data-t="music">Music</button><button data-t="timer">Timer</button><button data-t="settings">Settings</button></nav>
<main>
<section id="light" class="on">
 <div class="card"><div class="row"><label>Brightness</label><input id="bright" type="range" min="1" max="100" value="100"><span id="brightv">100%</span></div></div>
 <div class="card"><div class="row"><label>Colour</label><input id="col" type="color" value="#4b0082"><span id="colv" class="hint">#4b0082</span></div>
  <div class="sw" id="sw"></div></div>
 <div class="card"><div class="row"><label>White</label><input id="warm" type="range" min="0" max="255" value="128"><span class="hint">cool ⟷ warm</span></div></div>
 <div class="err" id="e-light"></div>
</section>
<section id="scenes">
 <div class="card"><div class="row"><label>Party</label><button class="b on" onclick="disco()">Ultimate Disco</button><button id="chaosb" class="b" onclick="chaos()">Chaos (random colours, host-driven)</button></div>
 <p class="hint">Ultimate Disco = firmware seven-colour strobe at max speed. Chaos streams a new random colour on every Bluetooth write, about 13 per second measured; stop it before using other controls.</p></div>
 <div class="card"><div class="row"><label>Speed</label><input id="speed" type="range" min="1" max="100" value="50"><span id="speedv">50%</span></div>
 <div class="row"><label>Brightness</label><input id="bright2" type="range" min="1" max="100" value="100"><span id="bright2v">100%</span></div></div>
 <div class="card"><div class="grid" id="scenegrid"></div></div><div class="err" id="e-scenes"></div>
</section>
<section id="diy">
 <div class="card"><div class="row"><label>Pattern</label><button class="b on" data-p="193">Dynamic</button><button class="b" data-p="194">Breathe</button><button class="b" data-p="195">Strobe</button></div>
 <div class="row"><label>Colours</label><div class="slots" id="slots"></div><button class="b" onclick="addSlot()">+ colour</button><button class="b" onclick="addWhite()">+ white</button></div>
 <div class="row"><label>Speed</label><input id="dspeed" type="range" min="1" max="100" value="50"></div>
 <div class="row"><label>Brightness</label><input id="dbright" type="range" min="1" max="100" value="50"></div>
 <div class="row"><label></label><button class="b on" onclick="sendDiy()">Apply scene</button></div>
 <p class="hint">Strobe only works on firmware that reports scene storage; this strip may ignore it.</p></div><div class="err" id="e-diy"></div>
</section>
<section id="music">
 <div class="card"><p class="hint" id="michint">Checking whether this strip has a microphone…</p>
 <div class="row"><label>Style</label><select id="micmode"><option value="0">Classic</option><option value="1">Soft</option><option value="2">Dynamic</option><option value="3">Disco</option></select></div>
 <div class="row"><label>Sensitivity</label><input id="sens" type="range" min="1" max="100" value="50"></div>
 <div class="row"><label></label><button class="b on" onclick="music(true)">Start</button><button class="b" onclick="music(false)">Stop</button></div></div><div class="err" id="e-music"></div>
</section>
<section id="timer">
 <div class="card"><h3 style="margin:0 0 8px">Turn on</h3><div class="row"><label>Time</label><input type="time" id="t1" value="07:00"><button id="t1en" class="toggle" onclick="this.classList.toggle('on')"></button></div>
  <div class="row"><label>Repeat</label><div class="week" id="w1"></div></div><div class="row"><label></label><button class="b on" onclick="saveTimer(1)">Save</button></div></div>
 <div class="card"><h3 style="margin:0 0 8px">Turn off</h3><div class="row"><label>Time</label><input type="time" id="t2" value="23:00"><button id="t2en" class="toggle" onclick="this.classList.toggle('on')"></button></div>
  <div class="row"><label>Repeat</label><div class="week" id="w2"></div></div><div class="row"><label></label><button class="b on" onclick="saveTimer(2)">Save</button></div></div>
 <p class="hint">No repeat days selected = fire once at the next matching time.</p><div class="err" id="e-timer"></div>
</section>
<section id="settings">
 <div class="card"><h3 style="margin:0 0 8px">Strip</h3>
  <div class="row"><label>BLE name</label><input type="text" id="sname"></div>
  <div class="row"><label>After power cut</label><select id="pon"><option value="0">Stay off</option><option value="1">Turn on</option><option value="2">Last state</option></select><button class="b" onclick="setPon()">Apply</button></div>
  <div class="row"><label>DIY RGB index</label><input type="checkbox" id="rgbidx"><span class="hint">tick if DIY scenes come out wrong</span></div></div>
 <div class="card"><h3 style="margin:0 0 8px">Claude Code status light</h3>
  <div class="row"><label>Idle colour</label><input type="color" id="idle"><span class="hint">shown while Claude works and after the 5 s green</span></div>
  <div class="row"><label>Try it</label><button class="b" onclick="hook('attention')">Needs you</button><button class="b" onclick="hook('done')">Done</button><button class="b" onclick="hook('working')">Working</button><span id="hookstat" class="hint"></span></div></div>
 <div class="row"><label></label><button class="b on" onclick="saveSettings()">Save settings</button></div>
 <div class="card"><p class="hint" id="raw"></p></div><div class="err" id="e-settings"></div>
</section>
</main>
<script>
const $=s=>document.querySelector(s);const api=()=>window.pywebview.api;
let pattern=193, slots=["#ff0000","#00ff00","#0000ff"], weeks={1:0,2:0}, power=false;
const DAYS=["Mo","Tu","We","Th","Fr","Sa","Su"];
const SWATCH=["#ff0000","#ff8000","#ffff00","#00ff00","#00ffff","#0000ff","#8000ff","#ff00ff","#ffffff","#4b0082","#ff8c3c","#00ff80"];
function err(id,r){$('#'+id).textContent=r&&r.ok===false?r.error:'';}
function call(id,fn){return fn().then(r=>{err(id,r);return r;}).catch(e=>{$('#'+id).textContent=String(e);});}
function throttle(fn,ms){let t,last=0,a;return(...args)=>{a=args;const now=Date.now();if(now-last>=ms){last=now;fn(...a);}else{clearTimeout(t);t=setTimeout(()=>{last=Date.now();fn(...a);},ms-(now-last));}};}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('nav button,section').forEach(x=>x.classList.remove('on'));b.classList.add('on');$('#'+b.dataset.t).classList.add('on');});
function setPower(on){power=on;$('#pw').classList.toggle('on',on);$('#dot').style.background=on?$('#col').value:'#333';$('#dot').style.color=on?$('#col').value:'#000';}
function togglePower(){setPower(!power);call('e-light',()=>api().power(power));}
const sendBright=throttle(v=>call('e-light',()=>api().brightness(v)),150);
['bright','bright2'].forEach(id=>{$('#'+id).oninput=e=>{$('#'+id+'v').textContent=e.target.value+'%';$('#bright').value=$('#bright2').value=e.target.value;sendBright(e.target.value);};});
const sendColor=throttle(v=>{setPower(true);call('e-light',()=>api().color(v));},150);
$('#col').oninput=e=>{$('#colv').textContent=e.target.value;sendColor(e.target.value);};
SWATCH.forEach(c=>{const s=document.createElement('span');s.style.background=c;s.onclick=()=>{$('#col').value=c;$('#colv').textContent=c;sendColor(c);};$('#sw').appendChild(s);});
const sendWarm=throttle(v=>call('e-light',()=>api().white(v)),150);$('#warm').oninput=e=>sendWarm(e.target.value);
const sendSpeed=throttle(v=>call('e-scenes',()=>api().speed(v)),150);$('#speed').oninput=e=>{$('#speedv').textContent=e.target.value+'%';sendSpeed(e.target.value);};
let chaosOn=false;
function disco(){chaosOn=false;$('#chaosb').classList.remove('on');setPower(true);$('#speed').value=100;$('#speedv').textContent='100%';document.querySelectorAll('#scenegrid .b').forEach(b=>b.classList.remove('on'));call('e-scenes',()=>api().disco());}
function chaos(){chaosOn=!chaosOn;$('#chaosb').classList.toggle('on',chaosOn);setPower(true);call('e-scenes',()=>api().chaos(chaosOn));}
function buildScenes(list){const g=$('#scenegrid');g.innerHTML='';list.forEach(s=>{const b=document.createElement('button');b.className='b';b.textContent=s.name+(s.animated?'':' ·');b.onclick=()=>{document.querySelectorAll('#scenegrid .b').forEach(x=>x.classList.remove('on'));b.classList.add('on');setPower(true);call('e-scenes',()=>api().scene(s.id));};g.appendChild(b);});}
document.querySelectorAll('#diy [data-p]').forEach(b=>b.onclick=()=>{document.querySelectorAll('#diy [data-p]').forEach(x=>x.classList.remove('on'));b.classList.add('on');pattern=+b.dataset.p;});
function renderSlots(){const h=$('#slots');h.innerHTML='';slots.forEach((c,i)=>{const d=document.createElement('div');d.className='slot';if(typeof c==='string'){const inp=document.createElement('input');inp.type='color';inp.value=c;inp.oninput=e=>slots[i]=e.target.value;d.appendChild(inp);}else{const inp=document.createElement('input');inp.type='range';inp.min=0;inp.max=255;inp.value=c;inp.style.width='90px';inp.title='white warmth';inp.oninput=e=>slots[i]=+e.target.value;d.appendChild(inp);}const x=document.createElement('b');x.textContent='×';x.onclick=()=>{slots.splice(i,1);renderSlots();};d.appendChild(x);h.appendChild(d);});}
function addSlot(){if(slots.length<8)slots.push('#ffffff');renderSlots();}function addWhite(){if(slots.length<8)slots.push(128);renderSlots();}
function sendDiy(){if(!slots.length)return;setPower(true);call('e-diy',()=>api().diy(slots,pattern,$('#dbright').value,$('#dspeed').value));}
function music(on){call('e-music',()=>api().music(on,$('#micmode').value,$('#sens').value));}
function buildWeek(n){const w=$('#w'+n);w.innerHTML='';DAYS.forEach((d,i)=>{const b=document.createElement('button');b.className='b';b.textContent=d;b.onclick=()=>{weeks[n]^=(1<<i);b.classList.toggle('on',!!(weeks[n]&(1<<i)));};w.appendChild(b);});}
function setWeek(n,mask){weeks[n]=mask&0x7f;[...$('#w'+n).children].forEach((b,i)=>b.classList.toggle('on',!!(weeks[n]&(1<<i))));}
function saveTimer(n){const [h,m]=$('#t'+n).value.split(':').map(Number);call('e-timer',()=>api().timer(n,$('#t'+n+'en').classList.contains('on'),weeks[n],h,m));}
function setPon(){call('e-settings',()=>api().power_on($('#pon').value));}
function saveSettings(){call('e-settings',()=>api().set_settings($('#sname').value,$('#idle').value,$('#rgbidx').checked));}
function hook(w){$('#hookstat').textContent='…';api().hook_test(w).then(r=>{$('#hookstat').textContent=r.ok?'sent':r.error;});}
function applyState(s){setPower(s.on);$('#bright').value=$('#bright2').value=s.bright;$('#brightv').textContent=$('#bright2v').textContent=s.bright+'%';$('#speed').value=s.speed;$('#speedv').textContent=s.speed+'%';$('#sens').value=s.sensitivity;
 if(s.mode===1){$('#col').value=s.rgb;$('#colv').textContent=s.rgb;}if(s.mode===0)$('#warm').value=s.w;
 document.querySelectorAll('#scenegrid .b').forEach(b=>b.classList.remove('on'));
 $('#status').textContent=s.label;$('#michint').textContent=s.device_mic?'Built-in microphone detected.':'This strip reports no built-in microphone. Music mode is sent anyway but may do nothing.';
 if(s.power_on!==null&&s.power_on!==undefined)$('#pon').value=s.power_on;
 (s.timers||[]).forEach(t=>{const n=t.number;if(n!==1&&n!==2)return;$('#t'+n).value=String(t.hour).padStart(2,'0')+':'+String(t.minute).padStart(2,'0');$('#t'+n+'en').classList.toggle('on',t.enable);setWeek(n,t.week&0x80?0:t.week);});
 $('#raw').textContent='state frame: '+s.raw;}
function refresh(){$('#status').textContent='reading…';api().get_state().then(r=>{if(r.ok)applyState(r.result);else $('#status').textContent=r.error;});}
window.addEventListener('pywebviewready',()=>{buildWeek(1);buildWeek(2);renderSlots();api().scenes().then(buildScenes);api().get_settings().then(s=>{$('#sname').value=s.name;$('#idle').value='#'+s.idle;$('#rgbidx').checked=s.diy_rgb_index;});refresh();});
</script></body></html>"""


def main():
    import webview
    api = Api()
    webview.create_window("AiLamp", html=HTML, js_api=api, width=760, height=720, min_size=(640, 560))
    try:
        webview.start()
    finally:
        if os.path.exists(SOCK):
            os.remove(SOCK)


if __name__ == "__main__":
    main()
