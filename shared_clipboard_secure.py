import base64
import datetime
import functools
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

# --noconsole builds have no stdout or stderr. The servers need something to write to.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

PORT = 5000                    # HTTPS port, reachable from the phone
LOCAL_PORT = 5001              # HTTP port, reachable from this laptop only
HOTKEY = "<ctrl>+<alt>+v"      # change this line to use another shortcut
HOTKEY_LABEL = "Ctrl + Alt + V"
MAX_LEN = 100_000              # longest text that is synchronised
SYNC_MINUTES = 0               # clipboard sync switches itself off after this time (0 = never)
REMOTE_MINUTES = 0             # remote control switches itself off after this time (0 = never)


def alert(text):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "Lappy Control", 0x10)
    except Exception:
        print(text)


try:
    import pyperclip
    import pystray
    import qrcode
    import webview
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from flask import Flask, jsonify, request
    from PIL import Image, ImageDraw
    from pynput import keyboard, mouse as pointer
    from werkzeug.serving import WSGIRequestHandler, make_server
except ImportError as error:
    alert(
        f"Missing package: {error.name}\n\nRun this command, then start the app again:\n"
        "pip install flask qrcode[pil] pywebview pystray pynput pyperclip cryptography"
    )
    sys.exit(1)

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

kb = keyboard.Controller()
mouse_ctl = pointer.Controller()
input_lock = threading.Lock()  # one key or mouse action at a time

Key = keyboard.Key
# Single keys that the phone can press.
REMOTE_ACTIONS = {
    "vol_up": Key.media_volume_up,
    "vol_down": Key.media_volume_down,
    "mute": Key.media_volume_mute,
    "play_pause": Key.media_play_pause,
    "next_track": Key.media_next,
    "prev_track": Key.media_previous,
    "slide_next": Key.right,
    "slide_prev": Key.left,
    "slide_start": Key.f5,
    "slide_end": Key.esc,
    "enter": Key.enter,
    "backspace": Key.backspace,
    "tab": Key.tab,
    "esc": Key.esc,
    "up": Key.up,
    "down": Key.down,
    "left": Key.left,
    "right": Key.right,
    "delete": Key.delete,
    "home": Key.home,
    "end": Key.end,
    "page_up": Key.page_up,
    "page_down": Key.page_down,
    "win": Key.cmd,
}
# Shortcuts: a modifier key held while another key is pressed.
COMBOS = {
    "ctrl+c": ((Key.ctrl,), "c"),
    "ctrl+v": ((Key.ctrl,), "v"),
    "ctrl+x": ((Key.ctrl,), "x"),
    "ctrl+a": ((Key.ctrl,), "a"),
    "ctrl+z": ((Key.ctrl,), "z"),
    "alt+tab": ((Key.alt,), Key.tab),
    "alt+shift+tab": ((Key.alt, Key.shift), Key.tab),
    "ctrl+tab": ((Key.ctrl,), Key.tab),
    "ctrl+shift+tab": ((Key.ctrl, Key.shift), Key.tab),
    "win+tab": ((Key.cmd,), Key.tab),                 # Task View
    "win+d": ((Key.cmd,), "d"),                       # show the desktop
    "win+s": ((Key.cmd,), "s"),                       # search
    "win+n": ((Key.cmd,), "n"),                       # notifications
    "desktop_left": ((Key.ctrl, Key.cmd), Key.left),  # previous virtual desktop
    "desktop_right": ((Key.ctrl, Key.cmd), Key.right),
}

# ----------------------------------------------------------------- settings
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "SharedClipboard"
CONFIG_FILE = CONFIG_DIR / "config.json"
CERT_FILE = CONFIG_DIR / "cert.pem"
KEY_FILE = CONFIG_DIR / "key.pem"


def new_pin():
    return f"{secrets.randbelow(1_000_000):06d}"


def save_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg))


def load_config():
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except Exception:
        data = {}
    data.setdefault("pin", new_pin())
    data.setdefault("secret", secrets.token_hex(16))
    data["sync"] = False  # sync is always off after a restart
    data["remote"] = False  # remote control is always off after a restart
    data.setdefault("sessions", {})  # one entry for every signed-in device
    return data


cfg = load_config()
save_config()

# The shared text is kept in memory only.
shared = {"text": ""}
state = {"visible": True, "quitting": False, "last_clip": "", "sync_until": 0.0,
         "remote_until": 0.0, "left_down": False, "mouse_seen": 0.0}
lock = {"fails": 0, "rounds": 0, "until": 0.0}  # PIN lockout, shared by all phones
window = None
tray = None
hotkeys = None

# --------------------------------------------------------------------- page
PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f172a">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Lappy">
<title>Lappy Control</title>
<style>
  :root {
    --bg: #0f172a; --card: #1e293b; --line: #334155;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #6366f1; --good: #22c55e; --bad: #ef4444;
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 18px; min-height: 100vh; background: var(--bg); color: var(--text);
         font-family: "Segoe UI", system-ui, sans-serif; }
  .wrap { max-width: 620px; margin: 0 auto; }
  header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
  h1 { font-size: 20px; margin: 0; }
  .pill { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--bad); }
  .dot.on { background: var(--good); }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 14px; margin-bottom: 14px; }
  textarea, input[type=password] { width: 100%; background: var(--bg); color: var(--text);
    border: 1px solid var(--line); border-radius: 10px; padding: 12px; font-size: 16px;
    font-family: inherit; outline: none; }
  textarea { height: 220px; resize: vertical; }
  textarea:focus, input:focus { border-color: var(--accent); }
  .row { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
  button { flex: 1; min-width: 90px; padding: 12px; font-size: 15px; font-weight: 600; border: 0;
           border-radius: 10px; cursor: pointer; background: var(--line); color: var(--text); }
  button.primary { background: var(--accent); color: white; }
  button:active { transform: scale(0.97); }
  .meta { display: flex; justify-content: space-between; margin-top: 10px; font-size: 12px; color: var(--muted); }
  .hint { color: var(--muted); font-size: 13px; margin: 6px 0 0; }
  .bad { color: var(--bad); min-height: 18px; }
  .check { display: flex; align-items: center; gap: 10px; margin: 8px 0; font-size: 15px; }
  .check input { width: 20px; height: 20px; }
  .tabs { display: flex; gap: 6px; margin-bottom: 12px; }
  .tabs button { min-width: 0; padding: 11px 1px; font-size: 13px; }
  .pick { flex: 1; background: var(--bg); color: var(--text); border: 1px solid var(--line);
          border-radius: 8px; padding: 8px; font-size: 14px; }
  .tabs button.active { background: var(--accent); color: white; }
  .grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 8px; }
  .grid4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 8px; }
  .grid3 button, .grid4 button { min-width: 0; padding: 14px 4px; font-size: 14px; }
  .title { font-weight: 600; margin-top: 12px; }
  .title:first-child { margin-top: 0; }
  #pad { height: 280px; background: var(--bg); border: 1px dashed var(--line); border-radius: 14px;
         touch-action: none; user-select: none; -webkit-user-select: none; display: flex;
         align-items: center; justify-content: center; text-align: center; color: var(--muted);
         font-size: 13px; line-height: 1.7; margin-bottom: 4px; }
  #login { display: none; text-align: center; }
  #login p { color: var(--muted); }
  #loginError { color: var(--bad); min-height: 20px; font-size: 14px; }
  #desktop { text-align: center; }
  #desktop img { border-radius: 10px; background: white; padding: 6px; }
  #desktop p { margin: 10px 0 0; color: var(--muted); font-size: 13px; }
  #desktop code { color: var(--text); font-size: 14px; }
  .pin { color: var(--text); font-size: 24px; letter-spacing: 4px; }
  .device { display: flex; align-items: center; justify-content: space-between; gap: 8px;
            margin-top: 8px; font-size: 13px; text-align: left; }
  .device button { flex: none; min-width: 70px; padding: 8px 10px; font-size: 13px; }
  #pad.drag { border: 1px solid var(--accent); background: #1e2a4a; }
  /*LOCAL_STYLE*/
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Lappy Control</h1>
    <div class="pill"><span class="dot" id="dot"></span><span id="state">Connecting</span></div>
  </header>

  <div class="card" id="login">
    <p>Enter the PIN shown in the laptop window.</p>
    <input type="password" id="pin" inputmode="numeric" autocomplete="off" placeholder="PIN">
    <div id="loginError"></div>
    <div class="row"><button class="primary" onclick="login()">Unlock</button></div>
  </div>

  <div id="app">
    <div class="card">
      <label class="check"><input type="checkbox" id="remoteSwitch"> Allow the phone to control this laptop (media, mouse, keyboard)</label>
      <label class="check"><input type="checkbox" id="sync"> Sync with the Windows clipboard</label>
      <p class="hint" id="switchInfo"></p>
      <p class="hint bad" id="notice"></p>
    </div>

    <div class="tabs phone-only">
      <button class="active" id="btn-clip" onclick="showTab('clip')">Clipboard</button>
      <button id="btn-media" onclick="showTab('media')">Media</button>
      <button id="btn-mouse" onclick="showTab('mouse')">Mouse</button>
      <button id="btn-keys" onclick="showTab('keys')">Keyboard</button>
      <button id="btn-system" onclick="showTab('system')">System</button>
    </div>

    <div class="card" id="tab-clip">
      <textarea id="box" placeholder="Type or paste text here, then press Save."></textarea>
      <div class="meta"><span id="count">0 characters</span><span id="msg"></span></div>
      <div class="row">
        <button class="primary" onclick="save()">Save</button>
        <button onclick="copyText()">Copy</button>
        <button onclick="clearText()">Clear</button>
      </div>
    </div>

    <div class="card phone-only" id="tab-media" style="display:none">
      <div class="title">Volume</div>
      <div class="grid3">
        <button onclick="act('vol_down')">Volume -</button>
        <button onclick="act('mute')">Mute</button>
        <button onclick="act('vol_up')">Volume +</button>
      </div>
      <div class="title">Music and video</div>
      <div class="grid3">
        <button onclick="act('prev_track')">Previous</button>
        <button onclick="act('play_pause')">Play / Pause</button>
        <button onclick="act('next_track')">Next</button>
      </div>
      <div class="title">Laptop</div>
      <div class="row"><button onclick="lockLaptop(this)">Lock laptop</button></div>
      <div class="title">Slides</div>
      <div class="grid4">
        <button onclick="act('slide_prev')">Slide back</button>
        <button onclick="act('slide_start')">Start</button>
        <button onclick="act('slide_end')">End</button>
        <button onclick="act('slide_next')">Slide next</button>
      </div>
    </div>

    <div class="card phone-only" id="tab-mouse" style="display:none">
      <div id="pad">Touchpad<br>1 finger: move, tap = click<br>2 fingers: scroll, tap = right click<br>3 or 4 fingers: swipe or tap</div>
      <div class="row"><button id="dragBtn" onclick="toggleDrag()">Drag lock: off</button></div>
      <p class="hint">Drag (move windows, select text): tap, then touch again at once and move. Or switch Drag lock on, then move one finger.</p>
      <p class="hint">Three fingers: swipe left or right to switch, up for Task View, down for the desktop, tap for search.
      Four fingers: swipe left or right to change the desktop, up for Task View, down for the desktop, tap for notifications.</p>
      <div class="grid3">
        <button onclick="clickMouse('left')">Left click</button>
        <button onclick="clickMouse('double')">Double click</button>
        <button onclick="clickMouse('right')">Right click</button>
      </div>
      <div class="grid3">
        <button onclick="wheel(5)">Scroll up</button>
        <button onclick="clickMouse('middle')">Middle click</button>
        <button onclick="wheel(-5)">Scroll down</button>
      </div>
      <label class="check">Speed <input type="range" id="speed" min="0.5" max="4" step="0.1" value="1.8" style="flex:1"></label>
      <label class="check">3-finger swipe left/right
        <select id="three" style="flex:1; background: var(--bg); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 8px; font-size: 14px;">
          <option value="apps">Switch windows</option>
          <option value="tabs">Switch browser tabs</option>
        </select>
      </label>
    </div>

    <div class="card phone-only" id="tab-system" style="display:none">
      <div class="row" style="margin-top:0"><button onclick="loadSystem()">Refresh</button></div>
      <p class="hint">Remote control must be switched on for this tab.</p>

      <div class="title">Battery and power</div>
      <p class="hint" id="sysBattery">Loading...</p>
      <label class="check">Energy saver
        <select id="saver" class="pick">
          <option value="off">Off</option>
          <option value="20">Turns on at 20%</option>
          <option value="always">On whenever on battery</option>
        </select>
      </label>
      <label class="check">Power mode
        <select id="powerMode" class="pick">
          <option value="efficiency">Best power efficiency</option>
          <option value="balanced">Balanced</option>
          <option value="performance">Best performance</option>
        </select>
      </label>

      <div class="title">Volume</div>
      <label class="check"><input type="range" id="volRange" min="0" max="100" value="0" style="flex:1; width:auto"><span id="volText">...</span></label>
      <div class="row"><button id="muteBtn">Mute</button></div>

      <div class="title">Projection</div>
      <div class="grid4">
        <button onclick="project('internal')">PC screen only</button>
        <button onclick="project('clone')">Duplicate</button>
        <button onclick="project('extend')">Extend</button>
        <button onclick="projectOnly(this)">Second screen only</button>
      </div>

      <div class="title">Wi-Fi</div>
      <p class="hint" id="wifiNow">Loading...</p>
      <div id="wifiList"></div>
      <p class="hint">Switching to another network can disconnect this phone from the laptop, unless the phone joins the same network.</p>

      <div class="title">Bluetooth</div>
      <p class="hint" id="btState">Loading...</p>
      <div class="row">
        <button onclick="setBluetooth('On')">Turn on</button>
        <button onclick="bluetoothOff(this)">Turn off</button>
      </div>
      <div id="btList"></div>
      <p class="hint bad" id="btAdmin"></p>
      <div class="row">
        <button onclick="scanBluetooth()">Scan nearby</button>
        <button onclick="pairBluetooth()">Pair new device</button>
      </div>
      <div id="btFound"></div>
      <p class="hint">Pairing opens the Bluetooth page on the laptop. Finish it with the Mouse tab.</p>

      <div class="title">Mobile hotspot</div>
      <p class="hint" id="hotspotInfo">Loading...</p>
      <div class="row">
        <button onclick="hotspotStart(this)">Start hotspot</button>
        <button onclick="setHotspot('Off')">Stop hotspot</button>
      </div>
    </div>

    <div class="card phone-only" id="tab-keys" style="display:none">
      <textarea id="kbd" rows="3" style="height:90px" autocomplete="off" autocorrect="off"
        autocapitalize="off" spellcheck="false"></textarea>
      <p class="hint">Tap the box and type. Everything you type appears on the laptop at once.</p>
      <div class="grid4">
        <button onclick="act('enter')">Enter</button>
        <button onclick="act('backspace')">Backspace</button>
        <button onclick="act('tab')">Tab</button>
        <button onclick="act('esc')">Esc</button>
      </div>
      <div class="grid4">
        <button onclick="act('up')">Up</button>
        <button onclick="act('down')">Down</button>
        <button onclick="act('left')">Left</button>
        <button onclick="act('right')">Right</button>
      </div>
      <div class="grid4">
        <button onclick="act('delete')">Delete</button>
        <button onclick="act('home')">Home</button>
        <button onclick="act('end')">End</button>
        <button onclick="act('win')">Win</button>
      </div>
      <div class="grid4">
        <button onclick="act('ctrl+c')">Ctrl+C</button>
        <button onclick="act('ctrl+v')">Ctrl+V</button>
        <button onclick="act('ctrl+x')">Ctrl+X</button>
        <button onclick="act('ctrl+a')">Ctrl+A</button>
      </div>
      <div class="grid4">
        <button onclick="act('ctrl+z')">Ctrl+Z</button>
        <button onclick="act('alt+tab')">Alt+Tab</button>
        <button onclick="act('page_up')">Page up</button>
        <button onclick="act('page_down')">Page down</button>
      </div>
    </div>
  </div>

  __DESKTOP__
</div>

<script>
const box = document.getElementById("box");
const msg = document.getElementById("msg");
const count = document.getElementById("count");
const syncBox = document.getElementById("sync");
const remoteBox = document.getElementById("remoteSwitch");
const switchInfo = document.getElementById("switchInfo");
const noticeBox = document.getElementById("notice");
let lastSaved = "";
let noticeTimer = null;

function say(text) {
  msg.textContent = text;
  setTimeout(() => { msg.textContent = ""; }, 2000);
}
function notice(text) {
  noticeBox.textContent = text;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { noticeBox.textContent = ""; }, 3500);
}
function updateCount() { count.textContent = box.value.length + " characters"; }
function setOnline(on) {
  document.getElementById("dot").className = "dot" + (on ? " on" : "");
  document.getElementById("state").textContent = on ? "Connected" : "Offline";
}
function showLogin(show) {
  document.getElementById("login").style.display = show ? "block" : "none";
  document.getElementById("app").style.display = show ? "none" : "block";
}

async function post(path, body) {
  try {
    const reply = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
    if (!reply.ok) {
      let data = {};
      try { data = await reply.json(); } catch (e) {}
      notice(data.error || "Request failed");
    }
    return reply;
  } catch (e) {
    notice("Cannot reach the laptop.");
    return null;
  }
}

async function load() {
  try {
    const reply = await fetch("/get");
    setOnline(true);
    if (reply.status === 401) { showLogin(true); return; }
    showLogin(false);
    const data = await reply.json();
    if (document.activeElement !== box && data.text !== lastSaved) {
      box.value = data.text;
      lastSaved = data.text;
      updateCount();
    }
    if (document.activeElement !== syncBox) syncBox.checked = data.sync;
    if (document.activeElement !== remoteBox) remoteBox.checked = data.remote;
    switchInfo.textContent = "Remote control is " + (data.remote ? "on" : "off")
      + ". Clipboard sync is " + (data.sync ? "on" : "off") + ".";
  } catch (error) {
    setOnline(false);
  }
}

async function login() {
  const reply = await fetch("/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pin: document.getElementById("pin").value })
  });
  const data = await reply.json();
  if (data.ok) {
    document.getElementById("loginError").textContent = "";
    document.getElementById("pin").value = "";
    load();
  } else {
    document.getElementById("loginError").textContent = data.error;
  }
}

async function save() {
  await post("/set", { text: box.value });
  lastSaved = box.value;
  say("Saved");
}

function copyText() {
  box.focus();
  box.select();
  box.setSelectionRange(0, box.value.length);
  say(document.execCommand("copy") ? "Copied" : "Copy failed");
}

async function clearText() {
  box.value = "";
  updateCount();
  await save();
  say("Cleared");
}

// ---- Connected devices (shown in the laptop window only)
const deviceList = document.getElementById("deviceList");
let deviceKey = null;
let armedKey = null;

function armed(key, button, label) {
  if (armedKey === key) {
    armedKey = null;
    return true;
  }
  armedKey = key;
  button.textContent = "Press again to confirm";
  setTimeout(() => {
    if (armedKey === key) { armedKey = null; button.textContent = label; }
  }, 3000);
  return false;
}
async function eject(id, button) {
  if (!armed(id, button, "Eject")) return;
  await post("/eject", { id: id });
  location.reload();
}
async function ejectAll(button) {
  if (!armed("all", button, "Eject all devices")) return;
  await post("/eject", { all: true });
  location.reload();
}

async function loadDevices() {
  if (!deviceList) return;
  try {
    const reply = await fetch("/devices");
    if (!reply.ok) return;
    const data = await reply.json();
    if (data.pin !== document.getElementById("pinText").textContent) {
      location.reload();             // the PIN changed, so the QR code must be redrawn
      return;
    }
    const key = data.devices.map((d) => d.id).join(",");
    if (key !== deviceKey) {         // rebuild only when a device joined or left
      deviceKey = key;
      deviceList.textContent = "";
      if (!data.devices.length) deviceList.textContent = "No device is connected.";
      data.devices.forEach((d) => {
        const row = document.createElement("div");
        row.className = "device";
        const label = document.createElement("span");
        label.id = "dev-" + d.id;
        const button = document.createElement("button");
        button.textContent = "Eject";
        button.onclick = () => eject(d.id, button);
        row.appendChild(label);
        row.appendChild(button);
        deviceList.appendChild(row);
      });
    }
    data.devices.forEach((d) => {
      const label = document.getElementById("dev-" + d.id);
      if (label) label.textContent = d.label + " (" + d.ip + "), seen " + d.ago;
    });
  } catch (e) {}
}

function act(name) { return post("/remote", { action: name }); }
function clickMouse(kind) { return post("/mouse", { click: kind }); }
function wheel(n) { return post("/mouse", { scroll: n }); }
async function lockLaptop(button) {
  if (!armed("lock", button, "Lock laptop")) return;
  button.textContent = "Lock laptop";
  await post("/lock", {});
}

const TABS = ["clip", "media", "mouse", "keys", "system"];
function showTab(name) {
  TABS.forEach((t) => {
    document.getElementById("tab-" + t).style.display = t === name ? "block" : "none";
    document.getElementById("btn-" + t).className = t === name ? "active" : "";
  });
  if (name === "system") loadSystem();
}

// ---- Touchpad
const pad = document.getElementById("pad");
const dragBtn = document.getElementById("dragBtn");
let lastX = 0, lastY = 0, startX = 0, startY = 0, startT = 0, lastMid = 0, startMid = 0;
let moved = false, maxFingers = 0, busy = false, multi = false, swiped = false;
let accDx = 0, accDy = 0, accScroll = 0, gestureX = 0, gestureY = 0;
let dragLock = false, dragging = false, lastTapEnd = 0;
const holdQueue = [];

// Three and four finger gestures, like a Windows touchpad. Swipe left means "next".
function swipeAction(fingers, direction) {
  if (direction === "up") return "win+tab";        // Task View
  if (direction === "down") return "win+d";        // show the desktop
  const forward = direction === "left";
  if (fingers >= 4) return forward ? "desktop_right" : "desktop_left";
  if (document.getElementById("three").value === "tabs") {
    return forward ? "ctrl+tab" : "ctrl+shift+tab";
  }
  return forward ? "alt+tab" : "alt+shift+tab";
}

function centre(touches) {
  let x = 0, y = 0;
  for (const t of touches) { x += t.clientX; y += t.clientY; }
  return [x / touches.length, y / touches.length];
}

// Holding the left button down: the drag starts and ends through the same queue as the moves.
function setDragging(on) {
  if (dragging === on) return;
  dragging = on;
  holdQueue.push(on ? "down" : "up");
  pad.classList.toggle("drag", on);
  flush();
}
function toggleDrag() {
  dragLock = !dragLock;
  dragBtn.textContent = "Drag lock: " + (dragLock ? "on" : "off");
  dragBtn.className = dragLock ? "primary" : "";
  if (!dragLock) setDragging(false);
}

async function flush() {
  if (busy) return;
  const dx = Math.round(accDx);
  const dy = Math.round(accDy);
  const sc = Math.trunc(accScroll / 14);
  const hold = holdQueue.length ? holdQueue.shift() : "";
  if (!dx && !dy && !sc && !hold) return;
  accDx -= dx;
  accDy -= dy;
  accScroll -= sc * 14;
  busy = true;
  await post("/mouse", { dx: dx, dy: dy, scroll: sc, hold: hold });
  busy = false;
  if (Math.abs(accDx) >= 1 || Math.abs(accDy) >= 1 || Math.abs(accScroll) >= 14 || holdQueue.length) flush();
}

pad.addEventListener("touchstart", (e) => {
  e.preventDefault();
  const t = e.touches[0];
  lastX = t.clientX;
  lastY = t.clientY;
  if (e.touches.length === 1) {
    startX = t.clientX;
    startY = t.clientY;
    startT = Date.now();
    moved = false;
    maxFingers = 1;
    multi = false;
    swiped = false;
    // Drag: a touch right after a tap, or any touch while Drag lock is on
    if (dragLock || (lastTapEnd && Date.now() - lastTapEnd < 250)) {
      lastTapEnd = 0;
      setDragging(true);
    }
  } else {
    if (dragging && !dragLock) setDragging(false);   // a second finger ends the drag
    maxFingers = Math.max(maxFingers, e.touches.length);
    if (e.touches.length === 2) {
      lastMid = (e.touches[0].clientY + e.touches[1].clientY) / 2;
      startMid = lastMid;
    }
  }
  if (e.touches.length >= 3) {
    multi = true;
    [gestureX, gestureY] = centre(e.touches);
  }
}, { passive: false });

pad.addEventListener("touchmove", (e) => {
  e.preventDefault();
  if (multi) {                        // three or four fingers: a gesture, no pointer movement
    if (e.touches.length < 3) return;
    const [cx, cy] = centre(e.touches);
    const dx = cx - gestureX;
    const dy = cy - gestureY;
    if (Math.abs(dx) > 12 || Math.abs(dy) > 12) moved = true;
    if (swiped || (Math.abs(dx) < 50 && Math.abs(dy) < 50)) return;
    swiped = true;
    const direction = Math.abs(dx) > Math.abs(dy) ? (dx < 0 ? "left" : "right") : (dy < 0 ? "up" : "down");
    act(swipeAction(e.touches.length, direction));
    return;
  }
  if (e.touches.length === 2) {       // two fingers: scroll
    const mid = (e.touches[0].clientY + e.touches[1].clientY) / 2;
    accScroll += mid - lastMid;
    lastMid = mid;
    if (Math.abs(mid - startMid) > 8) moved = true;
  } else {                            // one finger: move the pointer
    const t = e.touches[0];
    const gain = parseFloat(document.getElementById("speed").value);
    accDx += (t.clientX - lastX) * gain;
    accDy += (t.clientY - lastY) * gain;
    lastX = t.clientX;
    lastY = t.clientY;
    if (Math.abs(t.clientX - startX) + Math.abs(t.clientY - startY) > 8) moved = true;
  }
  flush();
}, { passive: false });

pad.addEventListener("touchend", (e) => {
  e.preventDefault();
  if (e.touches.length === 0) {
    if (dragging) {
      setDragging(false);                           // let go of the button
    } else if (!moved && Date.now() - startT < 250) {
      if (maxFingers >= 4) act("win+n");            // notifications
      else if (maxFingers === 3) act("win+s");      // search
      else {
        clickMouse(maxFingers === 2 ? "right" : "left");
        if (maxFingers === 1) lastTapEnd = Date.now();
      }
    }
    maxFingers = 0;
  }
}, { passive: false });

pad.addEventListener("touchcancel", () => {
  if (dragging) setDragging(false);
  maxFingers = 0;
});

// ---- Keyboard: the phone keyboard types on the laptop
const kbd = document.getElementById("kbd");
const BASE = " ";            // a hidden starting character, so that Backspace works on an empty box
let prev = BASE;
let chain = Promise.resolve();

function resetKbd() {
  kbd.value = BASE;
  prev = BASE;
}
resetKbd();

kbd.addEventListener("input", () => {
  const cur = kbd.value;
  const limit = Math.min(prev.length, cur.length);
  let p = 0;
  while (p < limit && prev[p] === cur[p]) p++;
  const back = prev.length - p;     // characters that were deleted or replaced
  const add = cur.slice(p);         // characters that were added
  prev = cur;
  if (back > 0 || add) {
    chain = chain.then(() => post("/type", { back: back, text: add }));
  }
  if (cur.length > 300 || !cur.startsWith(BASE)) resetKbd();
});
function caretToEnd() {
  const n = kbd.value.length;
  kbd.setSelectionRange(n, n);
}
kbd.addEventListener("focus", () => setTimeout(caretToEnd, 0));
kbd.addEventListener("click", () => setTimeout(caretToEnd, 0));

// ---- System tab
const sysBattery = document.getElementById("sysBattery");
const saverSel = document.getElementById("saver");
const powerSel = document.getElementById("powerMode");
const volRange = document.getElementById("volRange");
const volText = document.getElementById("volText");
const muteBtn = document.getElementById("muteBtn");
const wifiNow = document.getElementById("wifiNow");
const wifiList = document.getElementById("wifiList");
const btState = document.getElementById("btState");
const btList = document.getElementById("btList");
const btAdmin = document.getElementById("btAdmin");
const btFound = document.getElementById("btFound");
const hotspotInfo = document.getElementById("hotspotInfo");

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
async function getJson(path) {
  try {
    const reply = await fetch(path);
    const data = await reply.json();
    if (!reply.ok) { notice(data.error || "Request failed"); return null; }
    return data;
  } catch (e) {
    notice("Cannot reach the laptop.");
    return null;
  }
}
async function postJson(path, body) {
  const reply = await post(path, body);
  if (!reply || !reply.ok) return null;
  try { return await reply.json(); } catch (e) { return null; }
}
function line(label, button) {          // a line of text with a button on the right
  const row = el("div", undefined, "device");
  row.appendChild(el("span", label));
  if (button) row.appendChild(button);
  return row;
}
function tapButton(text, key, handler, needConfirm) {
  const button = el("button", text);
  button.onclick = async () => {
    if (needConfirm && !armed(key, button, text)) return;
    button.textContent = text;
    await handler();
  };
  return button;
}

function loadSystem() {
  loadBattery(); loadPower(); loadVolume(); loadWifi(); loadBluetooth(); loadHotspot();
}

async function loadBattery() {
  const d = await getJson("/system/battery");
  if (!d) return;
  sysBattery.textContent = d.has_battery
    ? "Battery " + d.percent + "%, " + (d.plugged ? "plugged in" : "on battery") + (d.saver ? ", energy saver is on" : "")
    : "No battery found.";
  if (document.activeElement !== saverSel && d.saver_mode !== "custom") saverSel.value = d.saver_mode;
}
async function loadPower() {
  const d = await getJson("/system/power");
  if (d && document.activeElement !== powerSel) powerSel.value = d.mode;
}
saverSel.addEventListener("change", async () => {
  await postJson("/system/saver", { mode: saverSel.value });
  loadBattery();
});
powerSel.addEventListener("change", () => postJson("/system/power", { mode: powerSel.value }));

function showVolume(d) {
  volRange.value = d.level;
  volText.textContent = d.level + "%" + (d.muted ? " muted" : "");
  muteBtn.textContent = d.muted ? "Unmute" : "Mute";
}
async function loadVolume() {
  const d = await getJson("/system/volume");
  if (d) showVolume(d);
}
volRange.addEventListener("input", () => { volText.textContent = volRange.value + "%"; });
volRange.addEventListener("change", async () => {
  const d = await postJson("/system/volume", { level: parseInt(volRange.value, 10) });
  if (d) showVolume(d);
});
muteBtn.onclick = async () => {
  const d = await postJson("/system/volume", { mute: "toggle" });
  if (d) showVolume(d);
};

function project(mode) { return postJson("/system/project", { mode: mode }); }
function projectOnly(button) {
  if (!armed("second", button, "Second screen only")) return;
  button.textContent = "Second screen only";
  project("external");
}

async function loadWifi() {
  const d = await getJson("/system/wifi");
  if (!d) return;
  const current = d.current.ssid || "";
  wifiNow.textContent = current
    ? "Connected to " + current + (d.current.signal ? " (signal " + d.current.signal + ")" : "")
    : "Not connected to a Wi-Fi network.";
  wifiList.textContent = "";
  d.saved.forEach((n) => {
    if (n.name === current) { wifiList.appendChild(line(n.name + " (current)")); return; }
    const label = n.name + (n.in_range ? "" : " (not in range)");
    const button = tapButton("Switch", "wifi-" + n.name, async () => {
      notice("Switching to " + n.name + "...");
      await postJson("/system/wifi", { name: n.name });
      setTimeout(loadWifi, 5000);
    }, true);
    wifiList.appendChild(line(label, button));
  });
  if (!d.saved.length) wifiList.textContent = "No saved networks.";
}

async function loadBluetooth() {
  const d = await getJson("/system/bluetooth");
  if (!d) return;
  const words = { On: "on", Off: "off", none: "not found on this laptop" };
  btState.textContent = "Bluetooth is " + (words[d.radio] || "in an unknown state") + ".";
  btList.textContent = "";
  d.devices.forEach((dev) => {
    const status = dev.disabled ? "disconnected" : (dev.connected ? "connected" : "paired");
    const action = dev.disabled ? "connect" : "disconnect";
    const button = tapButton(dev.disabled ? "Connect" : "Disconnect", "bt-" + dev.id, async () => {
      await postJson("/system/bluetooth", { action: action, id: dev.id });
      loadBluetooth();
    }, !dev.disabled);
    btList.appendChild(line(dev.name + " (" + status + ")", button));
  });
  if (!d.devices.length) btList.textContent = "No paired devices found.";
  btAdmin.textContent = d.admin ? "" : "Connect and Disconnect need Lappy Control to run as administrator.";
}
async function setBluetooth(state) {
  await postJson("/system/bluetooth", { state: state });
  loadBluetooth();
}
function bluetoothOff(button) {
  if (!armed("btoff", button, "Turn off")) return;
  button.textContent = "Turn off";
  setBluetooth("Off");
}
async function scanBluetooth() {
  btFound.textContent = "Scanning, this takes about 15 seconds...";
  const d = await getJson("/system/bluetooth/scan");
  btFound.textContent = "";
  if (!d) return;
  if (!d.found.length) btFound.textContent = "No new devices found. Put the device in pairing mode and scan again.";
  d.found.forEach((name) => btFound.appendChild(line(name)));
}
function pairBluetooth() { return postJson("/system/bluetooth", { action: "settings" }); }

function showHotspot(d) {
  hotspotInfo.textContent = "Hotspot is " + d.state.toLowerCase()
    + (d.ssid ? ". Name: " + d.ssid : "") + (d.password ? ". Password: " + d.password : "") + ".";
}
async function loadHotspot() {
  const d = await getJson("/system/hotspot");
  if (d) showHotspot(d);
}
async function setHotspot(state) {
  hotspotInfo.textContent = "Please wait...";
  const d = await postJson("/system/hotspot", { state: state });
  if (d) showHotspot(d); else loadHotspot();
}
function hotspotStart(button) {
  if (!armed("hotspot", button, "Start hotspot")) return;
  button.textContent = "Start hotspot";
  setHotspot("On");
}

// ---- Switches
remoteBox.addEventListener("change", async () => {
  await post("/remoteswitch", { on: remoteBox.checked });
  load();
});
syncBox.addEventListener("change", async () => {
  await post("/sync", { on: syncBox.checked });
  load();
});

box.addEventListener("input", updateCount);
showTab("clip");
load();
setInterval(load, 2000);
loadDevices();
setInterval(loadDevices, 2000);
</script>
</body>
</html>
"""

DESKTOP = """
<div class="card" id="desktop">
  <img src="__QR__" alt="QR code" style="width:200px;height:200px">
  <p>Scan this code with the Lappy Control phone app.</p>
  <p>To use a phone browser instead, open:<br><code>__URL__</code><br>
  The browser shows a certificate warning. Choose Advanced, then Proceed.</p>
  <p>PIN for the browser:<br><span class="pin" id="pinText">__PIN__</span></p>
  <p>Show or hide this window: __HOTKEY__</p>
</div>

<div class="card" id="devices">
  <div class="title">Connected devices</div>
  <div id="deviceList" class="hint">Loading...</div>
  <div class="row"><button onclick="ejectAll(this)">Eject all devices</button></div>
  <p class="hint">Eject signs a device out and changes the PIN, so it cannot come back.
  Eject all also switches remote control and clipboard sync off.
  Scan the new QR code to connect your own phone again.</p>
</div>
"""

# ------------------------------------------------------------------ helpers


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def qr_data_uri(text):
    buffer = io.BytesIO()
    qrcode.make(text).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def ensure_certificate(ip):
    """Creates a self-signed certificate. It is renewed when the laptop's IP address changes."""
    if CERT_FILE.exists() and KEY_FILE.exists() and cfg.get("cert_ip") == ip:
        return
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Shared Clipboard")])
    addresses = {"127.0.0.1", ip}
    names = [x509.DNSName("localhost")] + [
        x509.IPAddress(ipaddress.ip_address(a)) for a in sorted(addresses)
    ]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cfg["cert_ip"] = ip
    save_config()


def cert_fingerprint():
    """SHA-256 fingerprint of the certificate. The phone app uses it to recognise this laptop."""
    cert = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
    return cert.fingerprint(hashes.SHA256()).hex()


def host_allowed(host):
    """Accepts only 'localhost' or an IP address as the site name.
    This blocks DNS rebinding, where a malicious website points its own name at this computer."""
    host = host.strip().lower()
    if host.startswith("["):
        name = host[1:host.find("]")]
    elif host.count(":") == 1:
        name = host.rsplit(":", 1)[0]
    else:
        name = host
    if name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def is_local():
    return request.remote_addr in ("127.0.0.1", "::1")


def device_label(agent):
    a = agent.lower()
    if "android" in a:
        return "Android phone (app)" if "; wv)" in a else "Android phone"
    if "iphone" in a or "ipad" in a:
        return "iPhone or iPad"
    if "windows" in a:
        return "Windows computer"
    if "mac os" in a or "macintosh" in a:
        return "Mac"
    return "Unknown device"


def add_session(ip, agent):
    """Creates a login for one device. Only a hash of the token is stored on disk."""
    token = secrets.token_urlsafe(24)
    now = time.time()
    sessions = cfg["sessions"]
    for key in [k for k, v in sessions.items() if now - v["last"] > 30 * 86400]:
        del sessions[key]
    while len(sessions) >= 20:
        del sessions[min(sessions, key=lambda k: sessions[k]["last"])]
    sessions[hashlib.sha256(token.encode()).hexdigest()] = {
        "ip": ip, "label": device_label(agent), "created": now, "last": now,
    }
    save_config()
    return token


def authorised():
    if is_local():
        return True
    token = request.cookies.get("sc_token", "")
    session = cfg["sessions"].get(hashlib.sha256(token.encode()).hexdigest()) if token else None
    if session is None:
        return False
    session["last"] = time.time()
    return True


def ago(seconds):
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    return f"{int(seconds // 3600)} h ago"


def eject_all():
    """Signs out every device, changes the PIN, and switches remote control and sync off."""
    cfg["sessions"] = {}
    cfg["remote"] = False
    cfg["sync"] = False
    cfg["pin"] = new_pin()
    save_config()
    release_mouse()


def sync_active():
    if cfg["sync"] and time.time() > state["sync_until"]:
        cfg["sync"] = False  # time is over
    return cfg["sync"]


def remote_active():
    if cfg["remote"] and time.time() > state["remote_until"]:
        cfg["remote"] = False  # time is over
    return cfg["remote"]


def deadline(minutes):
    return time.time() + minutes * 60 if minutes > 0 else float("inf")


def release_mouse():
    """Lets go of the left mouse button if a drag is still in progress."""
    if state["left_down"]:
        try:
            with input_lock:
                mouse_ctl.release(pointer.Button.left)
        except Exception:
            pass
        state["left_down"] = False


def set_remote(on):
    cfg["remote"] = bool(on)
    state["remote_until"] = deadline(REMOTE_MINUTES)
    if not on:
        release_mouse()


def set_sync(on):
    cfg["sync"] = bool(on)
    state["sync_until"] = deadline(SYNC_MINUTES)
    try:
        state["last_clip"] = pyperclip.paste()  # only new copies are shared
    except Exception:
        pass


# ----------------------------------------------------------------- guards


@app.before_request
def guard():
    if not host_allowed(request.host):
        return jsonify(error="bad host"), 400
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != request.host.lower():
            return jsonify(error="bad origin"), 403


@app.after_request
def add_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'"
    )
    return response


# ------------------------------------------------------------------- routes


@app.route("/")
def index():
    desktop = ""
    if is_local():
        ip = local_ip()
        url = f"https://{ip}:{PORT}"
        pair = f"sharedclip://pair?host={ip}&port={PORT}&pin={cfg['pin']}&fp={cert_fingerprint()}"
        desktop = (
            DESKTOP.replace("__QR__", qr_data_uri(pair))
            .replace("__URL__", url)
            .replace("__PIN__", cfg["pin"])
            .replace("__HOTKEY__", HOTKEY_LABEL)
        )
    page = PAGE.replace("__DESKTOP__", desktop)
    local_style = ".phone-only { display: none !important; }" if is_local() else ""
    return page.replace("/*LOCAL_STYLE*/", local_style)


@app.route("/login", methods=["POST"])
def login():
    wait = int(lock["until"] - time.time())
    if wait > 0:
        return jsonify(ok=False, error=f"Too many attempts. Wait {wait} seconds."), 429
    pin = str((request.get_json(silent=True) or {}).get("pin", "")).strip()
    if hmac.compare_digest(pin, cfg["pin"]):
        lock.update(fails=0, rounds=0, until=0.0)
        reply = jsonify(ok=True)
        token = add_session(request.remote_addr, request.headers.get("User-Agent", ""))
        reply.set_cookie("sc_token", token, max_age=30 * 86400,
                         httponly=True, samesite="Lax", secure=request.is_secure)
        return reply
    lock["fails"] += 1
    if lock["fails"] >= 5:
        # Each lockout is twice as long as the one before: 1, 2, 4 ... up to 15 minutes.
        lock["rounds"] += 1
        lock["fails"] = 0
        lock["until"] = time.time() + min(60 * 2 ** (lock["rounds"] - 1), 900)
    return jsonify(ok=False, error="Wrong PIN."), 403


@app.route("/get")
def get_text():
    if not authorised():
        return jsonify(error="locked"), 401
    return jsonify(text=shared["text"], sync=sync_active(), remote=remote_active())


@app.route("/set", methods=["POST"])
def set_text():
    if not authorised():
        return jsonify(error="locked"), 401
    text = str((request.get_json(silent=True) or {}).get("text", ""))[:MAX_LEN]
    shared["text"] = text
    if sync_active() and text and text != state["last_clip"]:
        try:
            pyperclip.copy(text)
            state["last_clip"] = text
        except Exception:
            pass
    return jsonify(ok=True)


def remote_gate():
    """Returns an error reply if remote control cannot be used now, otherwise None."""
    if not authorised():
        return jsonify(error="locked"), 401
    if not remote_active():
        return jsonify(error="Remote control is off. Switch it on at the top of this page."), 403
    return None


def tap(key):
    kb.press(key)
    kb.release(key)


def press_combo(modifiers, key):
    for modifier in modifiers:
        kb.press(modifier)
    try:
        tap(key)
    finally:
        for modifier in reversed(modifiers):
            kb.release(modifier)


@app.route("/remote", methods=["POST"])
def remote_route():
    blocked = remote_gate()
    if blocked:
        return blocked
    action = (request.get_json(silent=True) or {}).get("action")
    try:
        with input_lock:
            if action in REMOTE_ACTIONS:
                tap(REMOTE_ACTIONS[action])
            elif action in COMBOS:
                modifiers, key = COMBOS[action]
                press_combo(modifiers, key)
            else:
                return jsonify(error="Unknown action."), 400
    except Exception:
        return jsonify(error="The laptop could not press the key."), 500
    return jsonify(ok=True)


@app.route("/type", methods=["POST"])
def type_route():
    blocked = remote_gate()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    try:
        back = max(0, min(200, int(data.get("back", 0))))
    except (TypeError, ValueError):
        return jsonify(error="Bad request."), 400
    text = str(data.get("text", ""))[:500]
    try:
        with input_lock:
            for _ in range(back):
                tap(Key.backspace)
            if text:
                kb.type(text)
    except Exception:
        return jsonify(error="The laptop could not type."), 500
    return jsonify(ok=True)


@app.route("/mouse", methods=["POST"])
def mouse_route():
    blocked = remote_gate()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    try:
        dx = max(-600, min(600, int(data.get("dx", 0))))
        dy = max(-600, min(600, int(data.get("dy", 0))))
        scroll = max(-30, min(30, int(data.get("scroll", 0))))
    except (TypeError, ValueError):
        return jsonify(error="Bad request."), 400
    click = data.get("click")
    hold = data.get("hold")  # "down" presses the left button, "up" releases it (drag)
    try:
        with input_lock:
            if hold == "down" and not state["left_down"]:
                mouse_ctl.press(pointer.Button.left)
                state["left_down"] = True
            if dx or dy:
                mouse_ctl.move(dx, dy)
            if scroll:
                mouse_ctl.scroll(0, scroll)
            if click == "left":
                mouse_ctl.click(pointer.Button.left)
            elif click == "right":
                mouse_ctl.click(pointer.Button.right)
            elif click == "middle":
                mouse_ctl.click(pointer.Button.middle)
            elif click == "double":
                mouse_ctl.click(pointer.Button.left, 2)
            if hold == "up" and state["left_down"]:
                mouse_ctl.release(pointer.Button.left)
                state["left_down"] = False
            state["mouse_seen"] = time.time()
    except Exception:
        return jsonify(error="The laptop could not move the mouse."), 500
    return jsonify(ok=True)


@app.route("/lock", methods=["POST"])
def lock_route():
    blocked = remote_gate()
    if blocked:
        return blocked
    try:
        import ctypes
        if not ctypes.windll.user32.LockWorkStation():
            raise OSError("LockWorkStation failed")
    except Exception:
        return jsonify(error="The laptop could not lock."), 500
    return jsonify(ok=True)


@app.route("/remoteswitch", methods=["POST"])
def remote_switch_route():
    if not authorised():
        return jsonify(error="locked"), 401
    set_remote((request.get_json(silent=True) or {}).get("on", False))
    return jsonify(ok=True)


@app.route("/sync", methods=["POST"])
def sync_route():
    if not authorised():
        return jsonify(error="locked"), 401
    set_sync((request.get_json(silent=True) or {}).get("on", False))
    return jsonify(ok=True)


@app.route("/devices")
def devices_route():
    if not is_local():
        return jsonify(error="forbidden"), 403
    now = time.time()
    devices = [
        {"id": key, "label": s["label"], "ip": s["ip"], "ago": ago(now - s["last"])}
        for key, s in sorted(cfg["sessions"].items(), key=lambda kv: -kv[1]["last"])
    ]
    return jsonify(devices=devices, pin=cfg["pin"])


@app.route("/eject", methods=["POST"])
def eject_route():
    if not is_local():
        return jsonify(error="forbidden"), 403
    data = request.get_json(silent=True) or {}
    if data.get("all"):
        eject_all()
    else:
        if cfg["sessions"].pop(str(data.get("id", "")), None) is None:
            return jsonify(error="That device is already gone."), 404
        cfg["pin"] = new_pin()  # the ejected device cannot sign in again with the old PIN
        save_config()
    return jsonify(ok=True)


# ------------------------------------------------------------ system controls
# Battery, power, volume, projection, Wi-Fi, Bluetooth and hotspot. These work on Windows only.
# Functions that Python cannot reach directly use PowerShell, which is part of Windows.


class ActionFailed(Exception):
    """Something Windows could not do. The message is shown on the phone."""

    def __init__(self, message, status=500):
        super().__init__(message)
        self.status = status


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Lets PowerShell wait for Windows Runtime (WinRT) calls.
PS_ASYNC = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}
"""

# Reads the Bluetooth radio, or switches it when __SET__ is On or Off.
RADIO_PS = r"""
$null = [Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime]
$null = [Windows.Devices.Radios.RadioAccessStatus,Windows.System.Devices,ContentType=WindowsRuntime]
$null = [Windows.Devices.Radios.RadioState,Windows.System.Devices,ContentType=WindowsRuntime]
$null = Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus])
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
$bt = $radios | Where-Object { $_.Kind -eq 'Bluetooth' } | Select-Object -First 1
if (-not $bt) {
    $state = 'none'
} else {
    $want = '__SET__'
    if ($want) {
        $null = Await ($bt.SetStateAsync([Windows.Devices.Radios.RadioState]$want)) ([Windows.Devices.Radios.RadioAccessStatus])
    }
    $state = [string]$bt.State
}
"""

BT_STATUS_PS = PS_ASYNC + r"""
$state = 'unknown'
try {
""" + RADIO_PS.replace("__SET__", "") + r"""
} catch {
    $state = 'unknown'
}
$out = @()
$found = @(Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | Where-Object {
    $_.FriendlyName -and ($_.InstanceId -like 'BTHENUM\DEV_*' -or $_.InstanceId -like 'BTHLE\DEV_*')
})
foreach ($d in $found) {
    $connected = $false
    try {
        $connected = [bool](Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName '{83DA6326-97A6-4088-9453-A1923F573B29} 15' -ErrorAction Stop).Data
    } catch {}
    $out += [pscustomobject]@{
        name = [string]$d.FriendlyName
        id = [string]$d.InstanceId
        connected = $connected
        disabled = ([string]$d.Problem -eq 'CM_PROB_DISABLED')
    }
}
ConvertTo-Json -Compress -Depth 4 -InputObject @{ radio = $state; devices = $out }
"""

BT_SET_PS = PS_ASYNC + RADIO_PS + r"""
Write-Output $state
"""

BT_SCAN_PS = PS_ASYNC + r"""
$null = [Windows.Devices.Bluetooth.BluetoothDevice,Windows.Devices.Bluetooth,ContentType=WindowsRuntime]
$null = [Windows.Devices.Enumeration.DeviceInformation,Windows.Devices.Enumeration,ContentType=WindowsRuntime]
$null = [Windows.Devices.Enumeration.DeviceInformationCollection,Windows.Devices.Enumeration,ContentType=WindowsRuntime]
$selector = [Windows.Devices.Bluetooth.BluetoothDevice]::GetDeviceSelectorFromPairingState($false)
$found = Await ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($selector)) ([Windows.Devices.Enumeration.DeviceInformationCollection])
$names = @($found | Where-Object { $_.Name } | ForEach-Object { [string]$_.Name })
ConvertTo-Json -Compress -InputObject $names
"""

HOTSPOT_PS = PS_ASYNC + r"""
$null = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]
$null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
$null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
$conn = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
if (-not $conn) { throw 'There is no internet connection to share.' }
$manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($conn)
$want = '__SET__'
if ($want -eq 'On' -and [string]$manager.TetheringOperationalState -ne 'On') {
    $r = Await ($manager.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
    if ([string]$r.Status -ne 'Success') { throw ('The hotspot did not start: ' + [string]$r.Status) }
} elseif ($want -eq 'Off' -and [string]$manager.TetheringOperationalState -ne 'Off') {
    $r = Await ($manager.StopTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
    if ([string]$r.Status -ne 'Success') { throw ('The hotspot did not stop: ' + [string]$r.Status) }
}
$config = $manager.GetCurrentAccessPointConfiguration()
ConvertTo-Json -Compress -InputObject @{ state = [string]$manager.TetheringOperationalState; ssid = [string]$config.Ssid; password = [string]$config.Passphrase }
"""

VOLUME_PS = r"""
Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
  int f1(); int f2(); int f3(); int f4();
  int SetMasterVolumeLevelScalar(float fLevel, System.Guid pguidEventContext);
  int f5();
  int GetMasterVolumeLevelScalar(out float pfLevel);
  int f6(); int f7(); int f8(); int f9();
  int SetMute([MarshalAs(UnmanagedType.Bool)] bool bMute, System.Guid pguidEventContext);
  int GetMute(out bool pbMute);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
  int Activate(ref System.Guid id, int clsCtx, int activationParams, out IAudioEndpointVolume aev);
}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
  int f();
  int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint);
}
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject { }
public class Audio {
  static IAudioEndpointVolume Vol() {
    var enumerator = new MMDeviceEnumeratorComObject() as IMMDeviceEnumerator;
    IMMDevice dev = null;
    Marshal.ThrowExceptionForHR(enumerator.GetDefaultAudioEndpoint(0, 1, out dev));
    IAudioEndpointVolume epv = null;
    var epvid = typeof(IAudioEndpointVolume).GUID;
    Marshal.ThrowExceptionForHR(dev.Activate(ref epvid, 23, 0, out epv));
    return epv;
  }
  public static float Volume {
    get { float v = -1; Marshal.ThrowExceptionForHR(Vol().GetMasterVolumeLevelScalar(out v)); return v; }
    set { Marshal.ThrowExceptionForHR(Vol().SetMasterVolumeLevelScalar(value, System.Guid.Empty)); }
  }
  public static bool Mute {
    get { bool mute; Marshal.ThrowExceptionForHR(Vol().GetMute(out mute)); return mute; }
    set { Marshal.ThrowExceptionForHR(Vol().SetMute(value, System.Guid.Empty)); }
  }
}
'@
__COMMAND__
$ci = [System.Globalization.CultureInfo]::InvariantCulture
Write-Output ([string]::Format($ci, '{0}|{1}', [Audio]::Volume, [Audio]::Mute))
"""

BT_ID = re.compile(r"^(BTHENUM|BTHLE)\\DEV_[0-9A-Za-z&_.\\-]+$")
POWER_MODES = {"efficiency": "OVERLAY_SCHEME_MIN", "balanced": "OVERLAY_SCHEME_NONE",
               "performance": "OVERLAY_SCHEME_MAX"}
POWER_GUIDS = {
    "961cc777-2547-4f9d-8174-7d86181b8a7a": "efficiency",
    "00000000-0000-0000-0000-000000000000": "balanced",
    "3af9b8d9-7c97-431d-ad78-34a8bfea439f": "performance",
    "ded574b5-45a0-4f42-8737-46345c09c238": "performance",
}
PROJECT_MODES = {"internal": "/internal", "clone": "/clone", "extend": "/extend", "external": "/external"}


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def oem_encoding():
    try:
        import ctypes
        return "cp%d" % ctypes.windll.kernel32.GetOEMCP()
    except Exception:
        return "utf-8"


def run_ps(script, timeout=25):
    """Runs a PowerShell script (Windows PowerShell 5.1) and returns its text output."""
    script = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n$ErrorActionPreference = 'Stop'\n" + script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded],
            capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise ActionFailed("Windows took too long to answer.")
    except OSError:
        raise ActionFailed("PowerShell is not available on this computer.")
    if done.returncode != 0:
        error = done.stderr.decode("utf-8", errors="replace")
        line = next((x.strip() for x in error.splitlines() if x.strip()), "")
        raise ActionFailed(line[:160] or "Windows refused the request.")
    return done.stdout.decode("utf-8", errors="replace").strip()


def run_exe(args, timeout=15):
    try:
        done = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        raise ActionFailed("Windows did not answer.")
    return done.returncode, done.stdout.decode(oem_encoding(), errors="replace")


def read_json(text):
    try:
        return json.loads(text)
    except ValueError:
        raise ActionFailed("Windows gave an answer that could not be read.")


# ---- battery and power

def battery_status():
    import ctypes

    class PowerStatus(ctypes.Structure):
        _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("percent", ctypes.c_ubyte),
                    ("system", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]

    status = PowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        raise ActionFailed("Could not read the battery.")
    has_battery = status.flag != 128 and status.percent != 255
    return {"has_battery": has_battery, "percent": int(status.percent) if has_battery else None,
            "plugged": status.ac == 1, "saver": bool(status.system & 1)}


def saver_threshold():
    code, text = run_exe(["powercfg", "/query", "SCHEME_CURRENT", "SUB_ENERGYSAVER", "ESBATTTHRESHOLD"])
    values = re.findall(r"0x[0-9a-fA-F]{8}", text)
    if code != 0 or not values:
        return 0
    return int(values[-1], 16)  # the last value is the one used on battery


def saver_mode():
    level = saver_threshold()
    return {0: "off", 20: "20", 100: "always"}.get(level, "off" if level == 0 else "custom")


def set_saver(level):
    code, _ = run_exe(["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", "SUB_ENERGYSAVER",
                       "ESBATTTHRESHOLD", str(level)])
    if code != 0:
        raise ActionFailed("Windows did not accept the energy saver setting.")
    run_exe(["powercfg", "/setactive", "SCHEME_CURRENT"])


def power_mode_now(plugged):
    import winreg
    path = r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"
    name = "ActiveOverlayAcPowerScheme" if plugged else "ActiveOverlayDcPowerScheme"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            value = str(winreg.QueryValueEx(key, name)[0]).strip("{}").lower()
    except OSError:
        return "balanced"
    return POWER_GUIDS.get(value, "balanced")


def set_power_mode(mode):
    code, _ = run_exe(["powercfg", "/overlaysetactive", POWER_MODES[mode]])
    if code != 0:
        raise ActionFailed("This Windows version has no power mode switch.")


# ---- volume

def volume_read(command=""):
    out = run_ps(VOLUME_PS.replace("__COMMAND__", command), timeout=25)
    level, _, muted = out.partition("|")
    try:
        return {"level": int(round(float(level.replace(",", ".")) * 100)), "muted": muted.strip().lower() == "true"}
    except ValueError:
        raise ActionFailed("Could not read the volume.")


# ---- projection

def project(mode):
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "DisplaySwitch.exe")
    try:
        subprocess.Popen([exe, PROJECT_MODES[mode]], creationflags=NO_WINDOW)
    except OSError:
        raise ActionFailed("Could not change the projection.")


# ---- Wi-Fi: switch between saved networks

def wifi_saved():
    code, text = run_exe(["netsh", "wlan", "show", "profiles"])
    names = []
    for line in text.splitlines():
        found = re.match(r"^\s+[^:]+?\s*:\s*(.+)$", line)   # lines such as "    All User Profile : Name"
        if found and not found.group(1).startswith("<"):
            names.append(found.group(1).strip())
    return names


def wifi_status():
    code, text = run_exe(["netsh", "wlan", "show", "interfaces"])
    current = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "SSID":
            current["ssid"] = value.strip()
        elif sep and key.strip() == "Signal":
            current["signal"] = value.strip()
    code, text = run_exe(["netsh", "wlan", "show", "networks"])
    visible = {m.group(1).strip() for m in re.finditer(r"^SSID\s+\d+\s*:\s*(.*)$", text, re.M)}
    saved = [{"name": n, "in_range": n in visible} for n in wifi_saved()]
    return {"current": current, "saved": saved}


def wifi_connect(name):
    if name not in wifi_saved() or '"' in name:
        raise ActionFailed("That network is not saved on this laptop.", 400)
    try:
        done = subprocess.run('netsh wlan connect name="%s"' % name, capture_output=True, timeout=20,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        raise ActionFailed("Windows did not answer.")
    if done.returncode != 0:
        raise ActionFailed("Windows could not connect to that network.")


# ---- Bluetooth

def bluetooth_status():
    data = read_json(run_ps(BT_STATUS_PS, timeout=30))
    devices, seen = [], set()
    for d in data.get("devices") or []:
        if d["name"] not in seen:
            seen.add(d["name"])
            devices.append(d)
    return {"radio": data.get("radio", "unknown"), "devices": devices, "admin": is_admin()}


def bluetooth_radio(state):
    return run_ps(BT_SET_PS.replace("__SET__", state), timeout=30)


def bluetooth_device(action, device_id):
    if not BT_ID.match(device_id):
        raise ActionFailed("Unknown device.", 400)
    if not is_admin():
        raise ActionFailed("Connect and Disconnect need Lappy Control to run as administrator.", 403)
    command = "Disable-PnpDevice" if action == "disconnect" else "Enable-PnpDevice"
    run_ps("%s -InstanceId '%s' -Confirm:$false" % (command, device_id), timeout=30)


def bluetooth_scan():
    found = read_json(run_ps(BT_SCAN_PS, timeout=45))
    return sorted(set(found if isinstance(found, list) else [found]))


# ---- hotspot

def hotspot(state=""):
    return read_json(run_ps(HOTSPOT_PS.replace("__SET__", state), timeout=40))


# ---- routes

def gated(handler):
    """Remote control must be on. Errors become a message for the phone."""
    @functools.wraps(handler)
    def wrapper():
        blocked = remote_gate()
        if blocked:
            return blocked
        try:
            return jsonify(handler(request.get_json(silent=True) or {}))
        except ActionFailed as error:
            return jsonify(error=str(error)), error.status
        except Exception:
            return jsonify(error="That did not work on this laptop."), 500
    return wrapper


@app.route("/system/battery")
@gated
def system_battery(data):
    info = battery_status()
    info["saver_mode"] = saver_mode()
    return info


@app.route("/system/saver", methods=["POST"])
@gated
def system_saver(data):
    levels = {"off": 0, "20": 20, "always": 100}
    if data.get("mode") not in levels:
        raise ActionFailed("Unknown energy saver choice.", 400)
    set_saver(levels[data["mode"]])
    return {"ok": True}


@app.route("/system/power")
@gated
def system_power(data):
    return {"mode": power_mode_now(battery_status()["plugged"])}


@app.route("/system/power", methods=["POST"])
@gated
def system_power_set(data):
    if data.get("mode") not in POWER_MODES:
        raise ActionFailed("Unknown power mode.", 400)
    set_power_mode(data["mode"])
    return {"ok": True, "mode": data["mode"]}


@app.route("/system/volume")
@gated
def system_volume(data):
    return volume_read()


@app.route("/system/volume", methods=["POST"])
@gated
def system_volume_set(data):
    if "level" in data:
        try:
            level = max(0, min(100, int(data["level"])))
        except (TypeError, ValueError):
            raise ActionFailed("Bad volume.", 400)
        return volume_read("[Audio]::Volume = %.2f" % (level / 100.0))
    if data.get("mute") == "toggle":
        return volume_read("[Audio]::Mute = -not [Audio]::Mute")
    raise ActionFailed("Unknown volume action.", 400)


@app.route("/system/project", methods=["POST"])
@gated
def system_project(data):
    if data.get("mode") not in PROJECT_MODES:
        raise ActionFailed("Unknown projection.", 400)
    project(data["mode"])
    return {"ok": True}


@app.route("/system/wifi")
@gated
def system_wifi(data):
    return wifi_status()


@app.route("/system/wifi", methods=["POST"])
@gated
def system_wifi_set(data):
    wifi_connect(str(data.get("name", "")))
    return {"ok": True}


@app.route("/system/bluetooth")
@gated
def system_bluetooth(data):
    return bluetooth_status()


@app.route("/system/bluetooth", methods=["POST"])
@gated
def system_bluetooth_set(data):
    if data.get("state") in ("On", "Off"):
        return {"radio": bluetooth_radio(data["state"])}
    if data.get("action") in ("connect", "disconnect"):
        bluetooth_device(data["action"], str(data.get("id", "")))
        return {"ok": True}
    if data.get("action") == "settings":
        os.startfile("ms-settings:bluetooth")  # the laptop shows the pairing screen
        return {"ok": True}
    raise ActionFailed("Unknown Bluetooth action.", 400)


@app.route("/system/bluetooth/scan")
@gated
def system_bluetooth_scan(data):
    return {"found": bluetooth_scan()}


@app.route("/system/hotspot")
@gated
def system_hotspot(data):
    return hotspot()


@app.route("/system/hotspot", methods=["POST"])
@gated
def system_hotspot_set(data):
    if data.get("state") not in ("On", "Off"):
        raise ActionFailed("Unknown hotspot action.", 400)
    return hotspot(data["state"])



# --------------------------------------------------------- background jobs


def clipboard_loop():
    while True:
        time.sleep(0.7)
        if state["left_down"] and time.time() - state["mouse_seen"] > 10:
            release_mouse()
        try:
            current = pyperclip.paste()
        except Exception:
            continue
        if current == state["last_clip"]:
            continue
        state["last_clip"] = current
        if sync_active() and current and len(current) <= MAX_LEN:
            shared["text"] = current


def toggle_window():
    if state["visible"]:
        window.hide()
        state["visible"] = False
    else:
        window.show()
        state["visible"] = True
        try:
            window.on_top = True
            window.on_top = False
        except Exception:
            pass


def quit_app():
    state["quitting"] = True
    try:
        tray.stop()
        hotkeys.stop()
    except Exception:
        pass
    window.destroy()


def on_closing():
    # The X button hides the window. Use the tray menu to quit.
    if state["quitting"]:
        return True
    window.hide()
    state["visible"] = False
    return False


TRAY_ICON_B64 = (
    "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"
)


def make_icon():
    return Image.open(io.BytesIO(base64.b64decode(TRAY_ICON_B64))).convert("RGBA")


def port_is_free(address, port):
    s = socket.socket()
    try:
        s.bind((address, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_for_server():
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.1)


class PatientHandler(WSGIRequestHandler):
    timeout = 30  # closes idle connections, so they do not use threads forever


def start_servers():
    # The phone connects to the HTTPS server. The laptop window uses the loopback-only server.
    # The TLS handshake must not happen inside accept(). Browsers open idle
    # pre-connections, and a handshake inside accept() would freeze the whole server.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    secure = make_server("0.0.0.0", PORT, app, threaded=True, request_handler=PatientHandler)
    secure.socket = context.wrap_socket(secure.socket, server_side=True,
                                        do_handshake_on_connect=False)
    local = make_server("127.0.0.1", LOCAL_PORT, app, threaded=True,
                        request_handler=PatientHandler)
    threading.Thread(target=secure.serve_forever, daemon=True).start()
    threading.Thread(target=local.serve_forever, daemon=True).start()


def main():
    global window, tray, hotkeys

    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # correct mouse moves on scaled screens
    except Exception:
        pass

    if not (port_is_free("0.0.0.0", PORT) and port_is_free("127.0.0.1", LOCAL_PORT)):
        alert("Lappy Control is already running (look for its icon in the system tray), "
              f"or another program is using port {PORT} or {LOCAL_PORT}.")
        sys.exit(1)

    ensure_certificate(local_ip())

    try:
        state["last_clip"] = pyperclip.paste()
    except Exception:
        pass

    start_servers()
    threading.Thread(target=clipboard_loop, daemon=True).start()
    wait_for_server()

    # "--background" starts the app hidden in the tray (used by "Start with Windows").
    background = "--background" in sys.argv
    state["visible"] = not background
    window = webview.create_window("Lappy Control", f"http://127.0.0.1:{LOCAL_PORT}",
                                   width=520, height=860, hidden=background)
    window.events.closing += on_closing

    tray = pystray.Icon(
        "LappyControl",
        make_icon(),
        "Lappy Control",
        pystray.Menu(
            pystray.MenuItem(f"Show / hide ({HOTKEY_LABEL})",
                             lambda icon, item: toggle_window(), default=True),
            pystray.MenuItem("Sync with Windows clipboard",
                             lambda icon, item: set_sync(not sync_active()),
                             checked=lambda item: sync_active()),
            pystray.MenuItem("Allow phone remote control",
                             lambda icon, item: set_remote(not remote_active()),
                             checked=lambda item: remote_active()),
            pystray.MenuItem("Eject all devices", lambda icon, item: eject_all()),
            pystray.MenuItem("Quit", lambda icon, item: quit_app()),
        ),
    )
    tray.run_detached()

    hotkeys = keyboard.GlobalHotKeys({HOTKEY: toggle_window})
    hotkeys.start()

    webview.start()  # blocks until the window is destroyed
    os._exit(0)


if __name__ == "__main__":
    main()
