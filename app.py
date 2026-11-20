"""Minimal Dell PowerEdge fan controller (iDRAC7/8, iDRAC9 <= 3.30) (web UI over ipmitool).

Run:  pip install flask  &&  python app.py   ->  http://127.0.0.1:5000
Requires ipmitool on PATH (or set IPMITOOL env var to its full path) and
"IPMI over LAN" enabled in iDRAC (iDRAC Settings > Network > IPMI Settings).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

IPMITOOL = os.environ.get("IPMITOOL", "ipmitool")
PROFILES_FILE = Path(__file__).with_name("profiles.json")

DEFAULT_PROFILES = {
    "Silent": {"type": "static", "speed": 15},
    "Quiet": {"type": "static", "speed": 22},
    "Balanced": {"type": "static", "speed": 35},
    "Performance": {"type": "static", "speed": 60},
    "Full blast": {"type": "static", "speed": 100},
    "Custom curve": {
        "type": "curve",
        "points": [[30, 12], [45, 18], [55, 28], [65, 45], [72, 70], [78, 100]],
        "interval": 10,
        "hysteresis": 2,
        "failsafe_temp": 80,
    },
}

app = Flask(__name__)

# Connection info lives only in memory (password is never written to disk).
conn: dict = {"host": "", "user": "root", "password": ""}
state = {"mode": "unknown", "profile": None, "speed": None, "temps": {}, "log": []}
lock = threading.Lock()
curve_stop = threading.Event()
curve_thread: threading.Thread | None = None


def log(msg: str) -> None:
    state["log"] = ([f"{time.strftime('%H:%M:%S')} {msg}"] + state["log"])[:50]


def ipmi(*args: str, timeout: int = 20) -> str:
    if not conn["host"]:
        raise RuntimeError("No iDRAC host configured")
    cmd = [IPMITOOL, "-I", "lanplus", "-H", conn["host"], "-U", conn["user"],
           "-E", *args]  # -E: password read from IPMI_PASSWORD env, not argv
    env = {**os.environ, "IPMI_PASSWORD": conn["password"]}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise RuntimeError(f"ipmitool not found ('{IPMITOOL}'). Install it or set IPMITOOL.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("ipmitool timed out (check IP / IPMI over LAN)")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "ipmitool failed")
    return r.stdout


def set_manual() -> None:
    ipmi("raw", "0x30", "0x30", "0x01", "0x00")


def set_auto() -> None:
    ipmi("raw", "0x30", "0x30", "0x01", "0x01")
    state.update(mode="auto", speed=None)


def set_speed(pct: int) -> None:
    pct = max(0, min(100, int(pct)))
    set_manual()
    ipmi("raw", "0x30", "0x30", "0x02", "0xff", f"0x{pct:02x}")
    state.update(mode="manual", speed=pct)


def read_temps() -> dict:
    out = ipmi("sdr", "type", "temperature")
    temps = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 5:
            m = re.search(r"(-?\d+)\s*degrees", parts[4])
            if m:
                name = parts[0]
                # Dell reports two sensors named "Temp" (CPU1, CPU2); disambiguate by ID.
                if name in temps:
                    name = f"{name} ({parts[1]})"
                temps[name] = int(m.group(1))
    state["temps"] = temps
    return temps


def read_fans() -> dict:
    out = ipmi("sdr", "type", "fan")
    fans = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 5:
            m = re.search(r"(\d+)\s*RPM", parts[4])
            if m:
                fans[parts[0]] = int(m.group(1))
    return fans


def interpolate(points: list, temp: float) -> int:
    pts = sorted(points)
    if temp <= pts[0][0]:
        return pts[0][1]
    if temp >= pts[-1][0]:
        return pts[-1][1]
    for (t1, s1), (t2, s2) in zip(pts, pts[1:]):
        if t1 <= temp <= t2:
            return round(s1 + (s2 - s1) * (temp - t1) / max(t2 - t1, 1e-9))
    return 100


def curve_loop(name: str, prof: dict) -> None:
    last_temp = None
    while not curve_stop.is_set():
        try:
            with lock:
                temps = read_temps()
                # Use the hottest CPU-ish sensor; ignore inlet/exhaust for the curve.
                cpu = [v for k, v in temps.items() if "inlet" not in k.lower() and "exhaust" not in k.lower()]
                hottest = max(cpu or temps.values())
                if hottest >= prof.get("failsafe_temp", 80):
                    set_auto()
                    log(f"FAILSAFE: {hottest}°C >= {prof['failsafe_temp']}°C, returned to iDRAC auto")
                    state["profile"] = None
                    return
                if last_temp is None or abs(hottest - last_temp) >= prof.get("hysteresis", 2):
                    spd = interpolate(prof["points"], hottest)
                    if spd != state["speed"]:
                        set_speed(spd)
                        log(f"{name}: {hottest}°C -> {spd}%")
                    last_temp = hottest
        except Exception as e:  # any failure -> hand control back to iDRAC
            log(f"Curve error: {e}; reverting to auto")
            try:
                with lock:
                    set_auto()
            except Exception:
                pass
            state["profile"] = None
            return
        curve_stop.wait(max(3, int(prof.get("interval", 10))))


def stop_curve() -> None:
    global curve_thread
    curve_stop.set()
    if curve_thread and curve_thread.is_alive():
        curve_thread.join(timeout=30)
    curve_thread = None


def load_profiles() -> dict:
    if PROFILES_FILE.exists():
        try:
            return json.loads(PROFILES_FILE.read_text())
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_PROFILES))


def save_profiles(p: dict) -> None:
    PROFILES_FILE.write_text(json.dumps(p, indent=2))


# ---------------------------------------------------------------- routes
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


def ok(**kw):
    return jsonify(ok=True, **kw)


def err(e):
    return jsonify(ok=False, error=str(e)), 400


@app.route("/api/connect", methods=["POST"])
def api_connect():
    d = request.get_json(force=True)
    conn.update(host=d.get("host", "").strip(), user=d.get("user", "root").strip(),
                password=d.get("password", ""))
    try:
        with lock:
            temps, fans = read_temps(), read_fans()
        log(f"Connected to {conn['host']}")
        return ok(temps=temps, fans=fans)
    except Exception as e:
        return err(e)


@app.route("/api/status", methods=["GET"])
def api_status():
    try:
        with lock:
            temps, fans = read_temps(), read_fans()
        return ok(temps=temps, fans=fans, mode=state["mode"], profile=state["profile"],
                  speed=state["speed"], log=state["log"])
    except Exception as e:
        return jsonify(ok=False, error=str(e), mode=state["mode"], profile=state["profile"],
                       speed=state["speed"], log=state["log"])


@app.route("/api/profiles", methods=["GET"])
def api_profiles():
    return ok(profiles=load_profiles())


@app.route("/api/profiles", methods=["POST"])
def api_save_profile():
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    prof = d.get("profile")
    if not name or not isinstance(prof, dict):
        return err("name and profile required")
    if prof.get("type") == "curve":
        pts = prof.get("points") or []
        if len(pts) < 2 or any(not (0 <= s <= 100) for _, s in pts):
            return err("curve needs >=2 points with speed 0-100")
    p = load_profiles()
    p[name] = prof
    save_profiles(p)
    return ok(profiles=p)


@app.route("/api/profiles/<name>", methods=["DELETE"])
def api_delete_profile(name):
    p = load_profiles()
    p.pop(name, None)
    save_profiles(p)
    return ok(profiles=p)


@app.route("/api/apply", methods=["POST"])
def api_apply():
    global curve_thread
    d = request.get_json(force=True)
    name = d.get("name")
    prof = d.get("profile") or load_profiles().get(name)
    if not prof:
        return err("unknown profile")
    stop_curve()
    try:
        if prof["type"] == "static":
            with lock:
                set_speed(prof["speed"])
            log(f"Applied {name}: fixed {prof['speed']}%")
        elif prof["type"] == "curve":
            curve_stop.clear()
            curve_thread = threading.Thread(target=curve_loop, args=(name, prof), daemon=True)
            curve_thread.start()
            log(f"Started curve '{name}'")
        state["profile"] = name
        return ok()
    except Exception as e:
        return err(e)


@app.route("/api/auto", methods=["POST"])
def api_auto():
    stop_curve()
    try:
        with lock:
            set_auto()
        state["profile"] = None
        log("Returned to iDRAC automatic control")
        return ok()
    except Exception as e:
        return err(e)


@app.route("/api/pcie", methods=["POST"])
def api_pcie():
    """Toggle Dell's 'third-party PCIe card' extra cooling response."""
    enable = request.get_json(force=True).get("enable", True)
    try:
        with lock:
            ipmi("raw", "0x30", "0xce", "0x00", "0x16", "0x05", "0x00", "0x00", "0x00",
                 "0x05", "0x00", "0x00" if enable else "0x01", "0x00", "0x00")
        log(f"3rd-party PCIe cooling response {'enabled' if enable else 'disabled'}")
        return ok()
    except Exception as e:
        return err(e)


if __name__ == "__main__":
    host = os.environ.get("BIND", "127.0.0.1")
    app.run(host=host, port=int(os.environ.get("PORT", 5000)), threaded=True)
