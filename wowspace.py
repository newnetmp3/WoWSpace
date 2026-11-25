#!/usr/bin/env python3
"""WowSpace: a WOW Thing-style sound enhancer for PipeWire.

Creates a "WowSpace" output device using PipeWire's built-in filter-chain
(no plugins to compile) and gives it live controls: 3D Space, 3D Center,
TruBass (resonance plus generated bass harmonics), Focus and a headphone
crossfeed. Turning it on makes WowSpace your default output; turning it off
switches back to your real device.
"""

from __future__ import annotations

import cmath
import ctypes
import json
import math
import os
import signal
import subprocess
import sys
from pathlib import Path

APP_ID = "wowspace"
SINK = "effect_input.wowspace"
SOURCE = "effect_output.wowspace"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_ID
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_ID
AUTOSTART = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "wowspace.desktop"

SPEAKERS = [40, 60, 80, 100, 150, 200, 300, 400]
PRESETS = {
    "Classic":   dict(space=60, center=50, trubass=50, speaker=100, focus=30, headphones=False, crossfeed=0),
    "Headphones": dict(space=55, center=55, trubass=45, speaker=80, focus=25, headphones=True, crossfeed=40),
    "Max WOW":   dict(space=100, center=45, trubass=80, speaker=100, focus=50, headphones=False, crossfeed=0),
    "Bass Head": dict(space=40, center=55, trubass=90, speaker=60, focus=15, headphones=True, crossfeed=25),
    "Subtle":    dict(space=30, center=50, trubass=25, speaker=150, focus=15, headphones=False, crossfeed=0),
}
DEFAULTS = dict(PRESETS["Classic"], gain=0.0, auto=True, enabled=False, device="", preset="Classic", user_presets={})
SETTING_KEYS = ("space", "center", "trubass", "speaker", "focus", "headphones", "crossfeed", "gain", "auto")


# --------------------------------------------------------------------------- DSP
def db(x: float) -> float:
    return 10 ** (x / 20)


def rbj(kind: str, f: float, fs: float, q: float, g: float = 0.0):
    """RBJ cookbook biquad, normalised (b0, b1, b2, a1, a2). Matches PipeWire's builtins."""
    a = 10 ** (g / 40)
    w = 2 * math.pi * min(f, fs * 0.45) / fs
    c, s = math.cos(w), math.sin(w)
    al = s / (2 * q)
    if kind == "peak":
        b = (1 + al * a, -2 * c, 1 - al * a); den = (1 + al / a, -2 * c, 1 - al / a)
    elif kind in ("lowshelf", "highshelf"):
        al = s / 2 * math.sqrt(2)  # shelf slope 1, which is what PipeWire uses
        r = 2 * math.sqrt(a) * al
        if kind == "lowshelf":
            b = (a * ((a + 1) - (a - 1) * c + r), 2 * a * ((a - 1) - (a + 1) * c), a * ((a + 1) - (a - 1) * c - r))
            den = ((a + 1) + (a - 1) * c + r, -2 * ((a - 1) + (a + 1) * c), (a + 1) + (a - 1) * c - r)
        else:
            b = (a * ((a + 1) + (a - 1) * c + r), -2 * a * ((a - 1) + (a + 1) * c), a * ((a + 1) + (a - 1) * c - r))
            den = ((a + 1) - (a - 1) * c + r, 2 * ((a - 1) - (a + 1) * c), (a + 1) - (a - 1) * c - r)
    elif kind == "hp":
        b = ((1 + c) / 2, -(1 + c), (1 + c) / 2); den = (1 + al, -2 * c, 1 - al)
    else:  # lp
        b = ((1 - c) / 2, 1 - c, (1 - c) / 2); den = (1 + al, -2 * c, 1 - al)
    return tuple(x / den[0] for x in b) + (den[1] / den[0], den[2] / den[0])


def resp(co, f: float, fs: float) -> complex:
    b0, b1, b2, a1, a2 = co
    z1 = cmath.exp(-1j * 2 * math.pi * f / fs)
    z2 = z1 * z1
    return (b0 + b1 * z1 + b2 * z2) / (1 + a1 * z1 + a2 * z2)


# Filter blocks: (node name, PipeWire label, rbj kind, (freq, Q, gain) from settings)
def filter_blocks(p: dict):
    d, fo, tb, sp = p["space"] / 100, p["focus"] / 100, p["trubass"] / 100, p["speaker"]
    focus = [("peaking", "peak", 3200, 0.9, 6 * fo), ("highshelf", "highshelf", 9000, 0.707, 2.5 * fo)]
    mid = [("mTB", "bq_peaking", "peak", sp, 1.1, 14 * tb),
           ("mHP", "bq_highpass", "hp", sp * 0.3 if tb > 0 else 5, 0.707, 0)]
    mid += [(f"mF{i}", "bq_" + lab, kind, f, q, g) for i, (lab, kind, f, q, g) in enumerate(focus, 1)]
    side = [("sHP", "bq_highpass", "hp", 20 + 70 * d if d > 0 else 5, 0.707, 0),
            ("s1", "bq_peaking", "peak", 350, 0.7, 6 * d),
            ("s2", "bq_peaking", "peak", 3000, 1.0, -5 * d),
            ("s3", "bq_highshelf", "highshelf", 7000, 0.707, 5 * d)]
    side += [(f"sF{i}", "bq_" + lab, kind, f, q, g) for i, (lab, kind, f, q, g) in enumerate(focus, 1)]
    harm = [("hLP1", "bq_lowpass", "lp", sp, 0.707, 0), ("hLP2", "bq_lowpass", "lp", sp, 0.707, 0),
            ("hHP1", "bq_highpass", "hp", sp, 0.707, 0), ("hHP2", "bq_highpass", "hp", sp, 0.707, 0),
            ("hLP3", "bq_lowpass", "lp", min(sp * 4, 16000), 0.707, 0)]
    xf = []
    for ch in ("L", "R"):
        xf += [(f"x{ch}1", "bq_lowpass", "lp", 750, 0.6, 0), (f"x{ch}2", "bq_lowpass", "lp", 1400, 0.6, 0)]
    return mid, side, harm, xf


def gain_db(p: dict, fs: float = 48000) -> float:
    """Output trim. With auto headroom, the loudest frequency of the linear path sits at -0.5 dB."""
    if not p["auto"]:
        return p["gain"]
    mid, side, _, xf = filter_blocks(p)
    cg, gw = db((p["center"] - 50) / 50 * 6), db(8 * p["space"] / 100)
    g = 0.45 * p["crossfeed"] / 100 if p["headphones"] else 0.0
    peak = 1e-9
    for i in range(320):
        f = 15 * (20000 / 15) ** (i / 319)
        m = cg * math.prod(resp(rbj(k, fr, fs, q, gn), f, fs) for _, _, k, fr, q, gn in mid)
        s = gw * math.prod(resp(rbj(k, fr, fs, q, gn), f, fs) for _, _, k, fr, q, gn in side)
        ll, lr = (m + s) / 2, (m - s) / 2
        if g:
            lp = math.prod(resp(rbj(k, fr, fs, q, gn), f, fs) for _, _, k, fr, q, gn in xf[:2])
            lr += g * lp * cmath.exp(-1j * 2 * math.pi * f * 0.00028) * ll
            ll *= 1 - 0.35 * g
        peak = max(peak, abs(ll + lr), abs(ll - lr))
    return -0.5 - 20 * math.log10(peak) + p["gain"]


def controls(p: dict) -> dict[str, float]:
    """Every live control value in the graph, keyed 'node:Control'."""
    mid, side, harm, xf = filter_blocks(p)
    out: dict[str, float] = {}
    for name, _, _, f, q, g in mid + side + harm + xf:
        out[f"{name}:Freq"], out[f"{name}:Q"], out[f"{name}:Gain"] = float(f), float(q), float(g)
    cg, gw = db((p["center"] - 50) / 50 * 6), db(8 * p["space"] / 100)
    hg = 2.0 * p["trubass"] / 100
    g = 0.45 * p["crossfeed"] / 100 if p["headphones"] else 0.0
    trim = db(gain_db(p))
    out.update({"mid:Gain 1": 0.5 * cg, "mid:Gain 2": 0.5 * cg, "side:Gain 1": 0.5, "side:Gain 2": 0.5,
                "hAbs:Gain 1": 2.0, "hAbs:Gain 2": 1.0, "hClamp:Min": 0.0, "hClamp:Max": 100.0})
    for ch in ("L", "R"):
        out.update({f"pre{ch}:Gain 1": 1.0, f"pre{ch}:Gain 2": gw, f"pre{ch}:Gain 3": hg,
                    f"x{ch}d:Delay (s)": 0.00028,
                    f"out{ch}:Gain 1": (1 - 0.35 * g) * trim, f"out{ch}:Gain 2": g * trim,
                    f"cl{ch}:Min": -1.0, f"cl{ch}:Max": 1.0})
    return out


def graph_conf(p: dict) -> str:
    """A standalone PipeWire config that runs the WowSpace filter-chain."""
    mid, side, harm, xf = filter_blocks(p)
    ctl = controls(p)
    nodes, links = [], []

    def node(name, label, extra=""):
        mine = {k.split(":", 1)[1]: v for k, v in ctl.items() if k.split(":", 1)[0] == name}
        c = " ".join(f'"{k}" = {v:.6g}' for k, v in mine.items())
        nodes.append(f'{{ type = builtin label = {label} name = {name} {extra}control = {{ {c} }} }}')

    def link(a, b):
        links.append(f'{{ output = "{a}" input = "{b}" }}')

    def chain(blocks, src):
        for name, label, *_ in blocks:
            node(name, label)
            link(src, f"{name}:In")
            src = f"{name}:Out"
        return src

    for n in ("inL", "inR"):
        nodes.append(f"{{ type = builtin label = copy name = {n} }}")
    nodes.append("{ type = builtin label = invert name = invR }")
    link("inR:Out", "invR:In")
    node("mid", "mixer"); link("inL:Out", "mid:In 1"); link("inR:Out", "mid:In 2")
    node("side", "mixer"); link("inL:Out", "side:In 1"); link("invR:Out", "side:In 2")
    m_out = chain(mid, "mid:Out")
    s_out = chain(side, "side:Out")
    # TruBass harmonics: |x| of the low-passed bass = 2*max(x,0) - x, then keep the harmonics.
    h_src = chain(harm[:2], "mid:Out")
    node("hClamp", "clamp"); link(h_src, "hClamp:In")
    nodes.append("{ type = builtin label = invert name = hInv }"); link(h_src, "hInv:In")
    node("hAbs", "mixer"); link("hClamp:Out", "hAbs:In 1"); link("hInv:Out", "hAbs:In 2")
    h_out = chain(harm[2:], "hAbs:Out")
    nodes.append("{ type = builtin label = invert name = sInv }"); link(s_out, "sInv:In")
    for ch, s in (("L", s_out), ("R", "sInv:Out")):
        node(f"pre{ch}", "mixer")
        link(m_out, f"pre{ch}:In 1"); link(s, f"pre{ch}:In 2"); link(h_out, f"pre{ch}:In 3")
    # Headphone crossfeed: low-passed, delayed copy of each side into the other ear.
    for ch, other in (("L", "R"), ("R", "L")):
        x = chain([b for b in xf if b[0].startswith(f"x{ch}")], f"pre{ch}:Out")
        node(f"x{ch}d", "delay", 'config = { "max-delay" = 0.01 } ')
        link(x, f"x{ch}d:In")
        node(f"out{other}", "mixer") if not any(f"name = out{other} " in n for n in nodes) else None
        link(f"x{ch}d:Out", f"out{other}:In 2")
    for ch in ("L", "R"):
        if not any(f"name = out{ch} " in n for n in nodes):
            node(f"out{ch}", "mixer")
        link(f"pre{ch}:Out", f"out{ch}:In 1")
        node(f"cl{ch}", "clamp"); link(f"out{ch}:Out", f"cl{ch}:In")

    nl = "\n          "
    return f"""# Generated by WowSpace. Edit the settings in the app, not here.
context.properties = {{ log.level = 2 }}
context.spa-libs = {{
  audio.convert.* = audioconvert/libspa-audioconvert
  support.*       = support/libspa-support
}}
context.modules = [
  {{ name = libpipewire-module-rt flags = [ ifexists nofail ] }}
  {{ name = libpipewire-module-protocol-native }}
  {{ name = libpipewire-module-client-node }}
  {{ name = libpipewire-module-adapter }}
  {{ name = libpipewire-module-filter-chain
    args = {{
      node.description = "WowSpace"
      media.name       = "WowSpace"
      filter.graph = {{
        nodes = [
          {nl.join(nodes)}
        ]
        links = [
          {nl.join(links)}
        ]
        inputs  = [ "inL:In" "inR:In" ]
        outputs = [ "clL:Out" "clR:Out" ]
      }}
      capture.props = {{
        node.name      = "{SINK}"
        media.class    = Audio/Sink
        audio.channels = 2
        audio.position = [ FL FR ]
      }}
      playback.props = {{
        node.name      = "{SOURCE}"
        node.passive   = true
        audio.channels = 2
        audio.position = [ FL FR ]
      }}
    }}
  }}
]
"""


# ------------------------------------------------------------------------ engine
def _die_with_parent():
    try:
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


def _run(args: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


class Engine:
    """Runs the filter-chain in its own pipewire client process and talks to it with pw-cli/wpctl."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.sink_id: int | None = None

    def start(self, p: dict) -> bool:
        self.stop()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        conf = STATE_DIR / "wowspace.conf"
        conf.write_text(graph_conf(p))
        log = open(STATE_DIR / "pipewire.log", "w")
        self.proc = subprocess.Popen(["pipewire", "-c", str(conf)], stdout=log, stderr=log,
                                     preexec_fn=_die_with_parent)
        for _ in range(50):
            if self.proc.poll() is not None:
                return False
            if self.node_id(SINK) is not None and self.node_id(SOURCE) is not None:
                self.sink_id = self.node_id(SINK)
                return True
            subprocess.run(["sleep", "0.1"])
        return False

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc, self.sink_id = None, None

    @staticmethod
    def dump() -> list:
        try:
            return json.loads(_run(["pw-dump"]) or "[]")
        except json.JSONDecodeError:
            return []

    def node_id(self, name: str, objs: list | None = None) -> int | None:
        for o in objs if objs is not None else self.dump():
            props = (o.get("info") or {}).get("props") or {}
            if o.get("type", "").endswith(":Node") and props.get("node.name") == name:
                return o["id"]
        return None

    def sinks(self) -> list[tuple[str, str]]:
        out = []
        for o in self.dump():
            props = (o.get("info") or {}).get("props") or {}
            if props.get("media.class") == "Audio/Sink" and props.get("node.name") != SINK:
                out.append((props.get("node.name", ""), props.get("node.description") or props.get("node.nick") or props.get("node.name", "")))
        return out

    def default_sink(self) -> str:
        for o in self.dump():
            if o.get("type", "").endswith(":Metadata") and ((o.get("props") or {}).get("metadata.name") == "default"):
                for m in o.get("metadata") or []:
                    if m.get("key") == "default.audio.sink":
                        v = m.get("value")
                        return (v or {}).get("name", "") if isinstance(v, dict) else str(v)
        return ""

    def set_default(self, name: str) -> bool:
        nid = self.node_id(name)
        return nid is not None and subprocess.run(["wpctl", "set-default", str(nid)], capture_output=True).returncode == 0

    def route_to(self, device: str):
        """Send WowSpace's processed audio to a specific device (empty = let WirePlumber choose)."""
        oid = self.node_id(SOURCE)
        if oid is None:
            return
        if device:
            _run(["pw-metadata", str(oid), "target.object", device])
        else:
            _run(["pw-metadata", "-d", str(oid), "target.object"])

    def set_controls(self, ctl: dict[str, float]) -> bool:
        if self.sink_id is None:
            return False
        body = " ".join(f'"{k}" {v:.6g}' for k, v in ctl.items())
        r = subprocess.run(["pw-cli", "s", str(self.sink_id), "Props", f"{{ params = [ {body} ] }}"],
                           capture_output=True, text=True)
        return r.returncode == 0


# --------------------------------------------------------------------- settings
def load_settings() -> dict:
    s = dict(DEFAULTS)
    try:
        s.update(json.loads((CONFIG_DIR / "settings.json").read_text()))
    except (OSError, json.JSONDecodeError):
        pass
    return s


def save_settings(s: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_DIR / "settings.json.tmp"
    tmp.write_text(json.dumps(s, indent=2))
    tmp.replace(CONFIG_DIR / "settings.json")


# --------------------------------------------------------------------------- GUI
def run_gui(start_hidden: bool):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QAction, QIcon, QActionGroup
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
                                   QInputDialog, QLabel, QMenu, QPushButton, QSlider, QSystemTrayIcon,
                                   QVBoxLayout, QWidget, QDoubleSpinBox)

    app = QApplication(sys.argv)
    app.setApplicationName("WowSpace")
    app.setDesktopFileName("wowspace")
    app.setQuitOnLastWindowClosed(False)

    # Single instance: a second launch just shows the running window.
    sock = QLocalSocket()
    sock.connectToServer(APP_ID)
    if sock.waitForConnected(300):
        sock.write(b"show"); sock.flush(); sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(APP_ID)
    server = QLocalServer()
    server.listen(APP_ID)

    icon_path = next((p for p in (Path(__file__).with_name("wowspace.svg"),
                                  Path("/usr/share/icons/hicolor/scalable/apps/wowspace.svg")) if p.exists()), None)
    icon = QIcon(str(icon_path)) if icon_path else QIcon.fromTheme("audio-card")
    app.setWindowIcon(icon)

    S = load_settings()
    engine = Engine()

    win = QWidget()
    win.setWindowTitle("WowSpace")
    root = QVBoxLayout(win)

    top = QHBoxLayout()
    power = QPushButton("Off")
    power.setCheckable(True)
    power.setMinimumHeight(40)
    power.setMinimumWidth(110)
    preset = QComboBox()
    save_btn = QPushButton("Save…")
    top.addWidget(power)
    top.addWidget(QLabel("Preset"))
    top.addWidget(preset, 1)
    top.addWidget(save_btn)
    root.addLayout(top)

    sliders: dict[str, tuple[QSlider, QLabel]] = {}

    def slider_row(form, key, label, lo=0, hi=100, tip=""):
        row = QHBoxLayout()
        sl = QSlider(Qt.Horizontal)
        sl.setRange(lo, hi)
        sl.setToolTip(tip)
        val = QLabel()
        val.setMinimumWidth(42)
        val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(sl, 1)
        row.addWidget(val)
        form.addRow(label, row)
        sliders[key] = (sl, val)

    g3d = QGroupBox("3D")
    f = QFormLayout(g3d)
    slider_row(f, "space", "Space", tip="Widens the stereo image (L−R perspective curve)")
    slider_row(f, "center", "Center", tip="Level of vocals, kick and bass in the middle. 50 is neutral.")
    root.addWidget(g3d)

    gb = QGroupBox("TruBass")
    f = QFormLayout(gb)
    slider_row(f, "trubass", "Amount", tip="Bass resonance plus generated bass harmonics")
    speaker = QComboBox()
    for hz in SPEAKERS:
        speaker.addItem({40: "40 Hz · Large", 100: "100 Hz · Medium", 200: "200 Hz · Small", 400: "400 Hz · Tiny"}.get(hz, f"{hz} Hz"), hz)
    f.addRow("Speaker size", speaker)
    root.addWidget(gb)

    gf = QGroupBox("Focus && headphones")
    f = QFormLayout(gf)
    slider_row(f, "focus", "Focus", tip="Presence around 3 kHz and air above 9 kHz")
    hp = QCheckBox("Headphone mode")
    f.addRow("", hp)
    slider_row(f, "crossfeed", "Crossfeed", tip="Blends a little of each side into the other ear")
    root.addWidget(gf)

    go = QGroupBox("Output")
    f = QFormLayout(go)
    device = QComboBox()
    f.addRow("Play through", device)
    trim = QDoubleSpinBox()
    trim.setRange(-12, 6); trim.setSingleStep(0.5); trim.setSuffix(" dB")
    auto = QCheckBox("Auto headroom")
    trow = QHBoxLayout(); trow.addWidget(trim); trow.addWidget(auto); trow.addStretch(1)
    f.addRow("Trim", trow)
    autostart = QCheckBox("Start WowSpace when I log in")
    f.addRow("", autostart)
    root.addWidget(go)

    status = QLabel()
    status.setWordWrap(True)
    root.addWidget(status)

    # ---- state <-> widgets
    loading = False

    def refresh_presets():
        preset.blockSignals(True)
        preset.clear()
        for n in list(PRESETS) + list(S["user_presets"]):
            preset.addItem(n)
        preset.addItem("Custom")
        preset.setCurrentText(S.get("preset") if preset.findText(S.get("preset", "")) >= 0 else "Custom")
        preset.blockSignals(False)

    def refresh_devices():
        device.blockSignals(True)
        device.clear()
        device.addItem("Your previous default output", "")
        for name, desc in engine.sinks():
            device.addItem(desc, name)
        i = device.findData(S["device"])
        device.setCurrentIndex(max(i, 0))
        device.blockSignals(False)

    def to_widgets():
        nonlocal loading
        loading = True
        for k, (sl, val) in sliders.items():
            sl.setValue(int(S[k]))
            val.setText(f"{int(S[k])}%")
        speaker.setCurrentIndex(max(speaker.findData(int(S["speaker"])), 0))
        hp.setChecked(bool(S["headphones"]))
        sliders["crossfeed"][0].setEnabled(bool(S["headphones"]))
        trim.setValue(float(S["gain"]))
        auto.setChecked(bool(S["auto"]))
        autostart.setChecked(AUTOSTART.exists())
        loading = False

    def set_status(msg: str = ""):
        if msg:
            status.setText(msg)
        elif not engine.running():
            status.setText("The WowSpace device isn't running. Check that PipeWire and WirePlumber are running, then reopen WowSpace.")
        else:
            status.setText(f"Trim {gain_db(S):+.1f} dB · " + ("On: WowSpace is your default output." if S["enabled"] else "Off: your audio bypasses WowSpace."))

    push_timer = QTimer(); push_timer.setSingleShot(True); push_timer.setInterval(40)
    save_timer = QTimer(); save_timer.setSingleShot(True); save_timer.setInterval(500)
    push_timer.timeout.connect(lambda: (engine.set_controls(controls(S)), set_status()))
    save_timer.timeout.connect(lambda: save_settings(S))

    def changed(mark_custom=True):
        if loading:
            return
        if mark_custom and S.get("preset") != "Custom":
            S["preset"] = "Custom"
            refresh_presets()
        push_timer.start(); save_timer.start()

    for k, (sl, val) in sliders.items():
        def on(v, k=k, val=val):
            S[k] = v; val.setText(f"{v}%"); changed()
        sl.valueChanged.connect(on)
    speaker.currentIndexChanged.connect(lambda _: (S.__setitem__("speaker", speaker.currentData()), changed()))
    def on_hp(on):
        S["headphones"] = on; sliders["crossfeed"][0].setEnabled(on); changed()
    hp.toggled.connect(on_hp)
    trim.valueChanged.connect(lambda v: (S.__setitem__("gain", v), changed()))
    auto.toggled.connect(lambda v: (S.__setitem__("auto", v), changed()))

    def apply_preset(name):
        if name == "Custom":
            return
        vals = PRESETS.get(name) or S["user_presets"].get(name)
        if not vals:
            return
        S.update(vals); S["preset"] = name
        to_widgets(); refresh_presets(); sync_tray()
        changed(mark_custom=False)
    preset.textActivated.connect(apply_preset)

    def save_preset():
        name, ok = QInputDialog.getText(win, "Save preset", "Preset name:")
        name = name.strip()
        if ok and name and name not in PRESETS and name != "Custom":
            S["user_presets"][name] = {k: S[k] for k in SETTING_KEYS}
            S["preset"] = name
            refresh_presets(); rebuild_tray_presets(); save_settings(S)
    save_btn.clicked.connect(save_preset)

    def on_device(_):
        S["device"] = device.currentData() or ""
        if S["enabled"]:
            engine.route_to(S["device"] or S.get("previous", ""))
        save_settings(S)
    device.currentIndexChanged.connect(on_device)

    def on_autostart(on):
        if on:
            AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
            AUTOSTART.write_text("[Desktop Entry]\nType=Application\nName=WowSpace\nExec=wowspace --hidden\n"
                                 "Icon=wowspace\nX-GNOME-Autostart-enabled=true\n")
        elif AUTOSTART.exists():
            AUTOSTART.unlink()
    autostart.toggled.connect(on_autostart)

    # ---- on / off
    def enable(on: bool):
        if on:
            if not engine.running() and not engine.start(S):
                set_status("Couldn't start the WowSpace device. Details are in " + str(STATE_DIR / "pipewire.log"))
                on = False
            else:
                cur = engine.default_sink()
                if cur and cur != SINK:
                    S["previous"] = cur
                engine.route_to(S["device"] or S.get("previous", ""))
                engine.set_default(SINK)
        else:
            back = S["device"] or S.get("previous", "")
            if not (back and engine.set_default(back)):
                others = engine.sinks()
                if others:
                    engine.set_default(others[0][0])
        S["enabled"] = on
        power.blockSignals(True); power.setChecked(on); power.setText("On" if on else "Off"); power.blockSignals(False)
        sync_tray(); set_status(); save_settings(S)
    power.toggled.connect(enable)

    # ---- tray
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("WowSpace")
    menu = QMenu()
    act_on = QAction("WowSpace on", checkable=True)
    act_on.toggled.connect(lambda v: v != S["enabled"] and enable(v))
    menu.addAction(act_on)
    preset_menu = menu.addMenu("Preset")
    group = QActionGroup(preset_menu)

    def rebuild_tray_presets():
        preset_menu.clear()
        for n in list(PRESETS) + list(S["user_presets"]):
            a = QAction(n, preset_menu, checkable=True)
            a.setChecked(n == S.get("preset"))
            a.triggered.connect(lambda _=False, n=n: apply_preset(n))
            group.addAction(a); preset_menu.addAction(a)

    def sync_tray():
        act_on.blockSignals(True); act_on.setChecked(S["enabled"]); act_on.blockSignals(False)
        for a in preset_menu.actions():
            a.setChecked(a.text() == S.get("preset"))
        tray.setToolTip("WowSpace: " + ("on" if S["enabled"] else "off") + f" · {S.get('preset', 'Custom')}")

    def show_win():
        refresh_devices(); win.show(); win.raise_(); win.activateWindow()

    menu.addSeparator()
    menu.addAction("Open WowSpace", show_win)
    def quit_app():
        if S["enabled"]:
            enable(False)
        engine.stop(); save_settings(S); app.quit()
    menu.addAction("Quit", quit_app)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda r: (win.hide() if win.isVisible() else show_win()) if r == QSystemTrayIcon.Trigger else None)
    has_tray = QSystemTrayIcon.isSystemTrayAvailable()
    if has_tray:
        tray.show()

    def close_event(e):
        if has_tray:
            e.ignore(); win.hide()
        else:
            e.accept(); quit_app()
    win.closeEvent = close_event
    server.newConnection.connect(lambda: (server.nextPendingConnection(), show_win()))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: quit_app())
    keepalive = QTimer(); keepalive.start(500); keepalive.timeout.connect(lambda: None)  # lets Python see signals

    # ---- boot
    refresh_presets(); rebuild_tray_presets(); to_widgets()
    ok = engine.start(S)
    refresh_devices()
    if ok and S["enabled"]:
        enable(True)
    sync_tray(); set_status()
    if not (start_hidden and has_tray):
        win.resize(460, 0)
        win.show()
    return app.exec()


def main():
    args = sys.argv[1:]
    if "--print-config" in args:
        print(graph_conf(load_settings()), end="")
        return 0
    if "-h" in args or "--help" in args:
        print("usage: wowspace [--hidden] [--print-config]\n  --hidden        start in the system tray\n"
              "  --print-config  print the PipeWire filter-chain config for the saved settings")
        return 0
    return run_gui("--hidden" in args)


if __name__ == "__main__":
    sys.exit(main())
