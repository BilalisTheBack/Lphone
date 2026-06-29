"""
LPhone Agent — Geliştirilmiş Mobil Terminal & Dosya Yöneticisi
Özellikler:
  - Dokunmatik D-Pad navigasyonu
  - Ekstra tuş çubuğu (Esc, Alt, Ctrl, Fn tuşları)
  - Snippets / hızlı komut paneli
  - Terminal çıktı renklendirme (ANSI)
  - Canlı sistem dashboard (CPU/RAM/Tailscale)
  - Sürükle-bırak dosya yükleme
  - Syntax highlighting metin editörü
  - Klavye açıldığında otomatik terminal yeniden boyutlandırma
"""

import asyncio
import base64
import json
import os
import platform as _platform
import secrets as _sec
import struct
import subprocess
import shutil
import sys
import shlex
import time as _t
import threading as _thr
import zipfile
import tempfile
from datetime import datetime as _dt
from pathlib import Path

# Platform-specific imports (Linux/macOS only)
_IS_WINDOWS = _platform.system() == "Windows"
if not _IS_WINDOWS:
    import fcntl
    import pty
    import termios

import psutil
from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect,
    HTTPException, UploadFile, File, Form, Body
)
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(title="LPhone Agent")

# Active PTY sessions: session_id -> {fd, proc, task}
sessions: dict = {}

# ─── File-sharing token store ────────────────────────────────────────────────
_share_store: dict = {}   # token → {path, exp, name}

# ─── Task-scheduler store + background runner ────────────────────────────────
_jobs: dict      = {}
_jobs_lock       = _thr.Lock()
_notif_thresh    = {"cpu": 85.0, "ram": 90.0, "disk": 95.0}

def _next_run(schedule: str) -> float:
    s = (schedule or "").strip().lower()
    if s.startswith("every "):
        parts = s[6:].split()
        try:
            n    = float(parts[0])
            unit = (parts[1] if len(parts) > 1 else "m")[0]
            mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 60)
            return _t.time() + n * mult
        except Exception:
            pass
    return _t.time() + 3600

def _job_runner():
    while True:
        _t.sleep(15)
        with _jobs_lock:
            items = list(_jobs.values())
        for job in items:
            if not job.get("enabled"):
                continue
            if _t.time() >= job["next_run"]:
                try:
                    r   = subprocess.run(job["cmd"], shell=True, capture_output=True, text=True, timeout=60)
                    out = (r.stdout + r.stderr).strip()[:500]
                except Exception as exc:
                    out = str(exc)
                with _jobs_lock:
                    if job["id"] in _jobs:
                        _jobs[job["id"]]["last_run"] = _dt.now().strftime("%d.%m %H:%M")
                        _jobs[job["id"]]["output"]   = out
                        _jobs[job["id"]]["next_run"]  = _next_run(job["schedule"])

_thr.Thread(target=_job_runner, daemon=True).start()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Araç bulma — venv PATH kısıtlamasını aşmak için sistem dizinlerinde de ara
# ---------------------------------------------------------------------------
_SYSTEM_BIN_DIRS = ["/usr/bin", "/usr/local/bin", "/bin", "/usr/sbin",
                    "/snap/bin", "/opt/homebrew/bin"]

def _find_tool(name: str) -> str | None:
    """
    Aracı shutil.which ile arar; bulamazsa sistem dizinlerini direkt tarar.
    venv PATH kısıtlamasını bu sayede atlatırız.
    """
    found = shutil.which(name)
    if found:
        return found
    for d in _SYSTEM_BIN_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None

def _cmd_exists(name: str) -> bool:
    return _find_tool(name) is not None

def _system_env(extra: dict | None = None) -> dict:
    """
    venv PATH yerine sistem yollarını da içeren env oluşturur.
    Subprocess çağrılarında sistem araçlarının bulunmasını garantiler.
    """
    parts = os.environ.get("PATH", "").split(":")
    for d in _SYSTEM_BIN_DIRS:
        if d not in parts:
            parts.append(d)
    env = {**os.environ, "PATH": ":".join(p for p in parts if p)}
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Display tespiti — gerçek masaüstü önce, Xvfb son çare
# ---------------------------------------------------------------------------
_xvfb_proc = None

def _find_real_display() -> str | None:
    """
    X11 socket dosyalarına bakarak gerçek (masaüstü) display'i bul.
    Bu yöntem hiçbir araç gerektirmez; /tmp/.X11-unix/ varsa çalışır.
    :0, :1, :2 sırasıyla denenir.
    """
    unix_dir = "/tmp/.X11-unix"
    if os.path.isdir(unix_dir):
        for n in range(10):
            sock = os.path.join(unix_dir, f"X{n}")
            if os.path.exists(sock):
                return f":{n}"
    return None

def _ensure_display():
    """
    Gerçek masaüstü display'i önceliklendir.
    Bulunamazsa Xvfb ile sanal ekran başlat.
    venv içinde çalışırken bile masaüstünü yakalar.
    """
    global _xvfb_proc

    # 1. Gerçek bir X11 display var mı? (soket dosyası kontrolü)
    real = _find_real_display()
    if real:
        # Gerçek masaüstü bulundu — DISPLAY'i doğru değere zorla
        # (venv, DISPLAY'i yanlış miras almış olabilir)
        os.environ["DISPLAY"] = real
        return

    # 2. DISPLAY ayarlı ama socket yok (WSL, SSH, vs.) — olduğu gibi bırak
    if os.environ.get("DISPLAY"):
        return

    # 3. Hiç display yok — Xvfb ile sanal ekran başlat
    xvfb = _find_tool("Xvfb")
    if not xvfb:
        return  # Xvfb kurulu değil

    try:
        _xvfb_proc = subprocess.Popen(
            [xvfb, ":99", "-screen", "0", "1280x720x24", "-ac"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _t.sleep(0.8)
        os.environ["DISPLAY"] = ":99"
        env99 = _system_env({"DISPLAY": ":99"})
        # Sanal ekranda pencere yöneticisi + terminal başlat
        for wm in ["openbox", "fluxbox", "icewm"]:
            wm_bin = _find_tool(wm)
            if wm_bin:
                subprocess.Popen([wm_bin], env=env99,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _t.sleep(0.3)
                break
        xterm = _find_tool("xterm")
        if xterm:
            subprocess.Popen(
                [xterm, "-fa", "Monospace", "-fs", "11", "-bg", "#09090b", "-fg", "#e4e4e7"],
                env=env99, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass

_ensure_display()


def _get_monitors() -> list[dict]:
    """
    xrandr ile bağlı monitörleri listeler.
    Her monitör için {name, x, y, w, h, primary} döner.
    """
    xrandr = _find_tool("xrandr")
    if not xrandr:
        return []
    try:
        env = _system_env({"DISPLAY": os.environ.get("DISPLAY") or _find_real_display() or ":0"})
        r = subprocess.run([xrandr, "--query"], capture_output=True, text=True, timeout=3, env=env)
        monitors = []
        import re
        # Format: "HDMI-1 connected primary 1920x1080+0+0 ..."
        for line in r.stdout.splitlines():
            m = re.match(
                r"^(\S+) connected(?: primary)?\s+(\d+)x(\d+)\+(\d+)\+(\d+)",
                line,
            )
            if m:
                monitors.append({
                    "name": m.group(1),
                    "w": int(m.group(2)), "h": int(m.group(3)),
                    "x": int(m.group(4)), "y": int(m.group(5)),
                    "primary": "primary" in line,
                })
        return monitors
    except Exception:
        return []


def _capture_screen(monitor_idx: int = 0) -> bytes | None:
    """
    Ekran görüntüsünü PNG bayt olarak döndürür.
    monitor_idx=0 tüm ekran veya ilk monitör; 1,2,... belirli monitör.
    venv içinde çalışırken bile gerçek masaüstünü yakalar.
    """
    disp = os.environ.get("DISPLAY") or _find_real_display() or ":0"
    env  = _system_env({"DISPLAY": disp})
    tmp  = "/tmp/_lphone_cap.png"

    try:
        if os.path.exists(tmp):
            os.unlink(tmp)
    except Exception:
        pass

    # Monitör geometrisi (çoklu monitör için)
    monitors = _get_monitors()
    geo_args: list[str] = []   # scrot region args
    ffmpeg_geo: list[str] = [] # ffmpeg geometry
    if monitor_idx > 0 and monitors and monitor_idx <= len(monitors):
        m = monitors[monitor_idx - 1]
        geo_args = ["--autoselect",
                    str(m["x"]), str(m["y"]),
                    str(m["w"]), str(m["h"])]
        ffmpeg_geo = ["-video_size", f"{m['w']}x{m['h']}",
                      "-i", f"{disp}+{m['x']},{m['y']}"]
    elif monitors:
        # Tüm monitörler → scrot varsayılan
        m = monitors[0]
        ffmpeg_geo = ["-video_size", f"{m['w']}x{m['h']}", "-i", disp]
    else:
        ffmpeg_geo = ["-video_size", "1280x720", "-i", disp]

    # 1) scrot — en hızlı
    scrot = _find_tool("scrot")
    if scrot:
        try:
            cmd = [scrot, "-z"]
            if geo_args:
                cmd += geo_args
            cmd.append(tmp)
            r = subprocess.run(cmd, timeout=3, capture_output=True, env=env)
            if r.returncode == 0 and os.path.exists(tmp):
                data = open(tmp, "rb").read(); os.unlink(tmp); return data
        except Exception:
            pass

    # 2) ffmpeg x11grab
    ffmpeg = _find_tool("ffmpeg")
    if ffmpeg:
        try:
            r = subprocess.run(
                [ffmpeg, "-y", "-f", "x11grab"] + ffmpeg_geo +
                ["-vframes", "1", "-q:v", "2", tmp],
                timeout=5, capture_output=True, env=env)
            if r.returncode == 0 and os.path.exists(tmp):
                data = open(tmp, "rb").read(); os.unlink(tmp); return data
        except Exception:
            pass

    # 3) ImageMagick import
    imgimport = _find_tool("import")
    if imgimport:
        try:
            r = subprocess.run([imgimport, "-window", "root", tmp],
                               timeout=3, capture_output=True, env=env)
            if r.returncode == 0 and os.path.exists(tmp):
                data = open(tmp, "rb").read(); os.unlink(tmp); return data
        except Exception:
            pass

    # 4) gnome-screenshot
    gscr = _find_tool("gnome-screenshot")
    if gscr:
        try:
            r = subprocess.run([gscr, "-f", tmp], timeout=4, capture_output=True, env=env)
            if r.returncode == 0 and os.path.exists(tmp):
                data = open(tmp, "rb").read(); os.unlink(tmp); return data
        except Exception:
            pass

    # 5) spectacle (KDE)
    spec = _find_tool("spectacle")
    if spec:
        try:
            r = subprocess.run([spec, "-b", "-n", "-o", tmp],
                               timeout=4, capture_output=True, env=env)
            if r.returncode == 0 and os.path.exists(tmp):
                data = open(tmp, "rb").read(); os.unlink(tmp); return data
        except Exception:
            pass

    # 6) mss — Python kütüphanesi
    try:
        os.environ["DISPLAY"] = disp
        import mss, mss.tools  # type: ignore
        with mss.mss() as sct:
            idx = max(0, min(monitor_idx, len(sct.monitors) - 1))
            shot = sct.grab(sct.monitors[idx])
            mss.tools.to_png(shot.rgb, shot.size, output=tmp)
        if os.path.exists(tmp):
            data = open(tmp, "rb").read(); os.unlink(tmp); return data
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Clipboard helpers (xclip / xsel / xdotool fallback)
# ---------------------------------------------------------------------------
def _get_clipboard() -> str:
    disp = os.environ.get("DISPLAY") or _find_real_display() or ":0"
    env  = _system_env({"DISPLAY": disp})
    for tool, args in [
        ("xclip",   ["xclip", "-selection", "clipboard", "-o"]),
        ("xsel",    ["xsel",  "--clipboard", "--output"]),
    ]:
        bin_ = _find_tool(tool)
        if bin_:
            try:
                r = subprocess.run([bin_] + args[1:], capture_output=True, text=True,
                                   timeout=2, env=env)
                if r.returncode == 0:
                    return r.stdout
            except Exception:
                pass
    return ""


def _set_clipboard(text: str) -> bool:
    disp = os.environ.get("DISPLAY") or _find_real_display() or ":0"
    env  = _system_env({"DISPLAY": disp})
    for tool, args in [
        ("xclip", ["xclip", "-selection", "clipboard"]),
        ("xsel",  ["xsel",  "--clipboard", "--input"]),
    ]:
        bin_ = _find_tool(tool)
        if bin_:
            try:
                r = subprocess.run([bin_] + args[1:], input=text, capture_output=True,
                                   text=True, timeout=2, env=env)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
    return False


# ---------------------------------------------------------------------------
# Screen recording (ffmpeg continuous)
# ---------------------------------------------------------------------------
_recording_proc: subprocess.Popen | None = None
_recording_path: str = ""

def _start_recording(fps: int = 10) -> str | None:
    global _recording_proc, _recording_path
    if _recording_proc and _recording_proc.poll() is None:
        return _recording_path  # zaten kayıt yapılıyor
    ffmpeg = _find_tool("ffmpeg")
    if not ffmpeg:
        return None
    disp = os.environ.get("DISPLAY") or _find_real_display() or ":0"
    env  = _system_env({"DISPLAY": disp})
    monitors = _get_monitors()
    if monitors:
        m = monitors[0]
        geo = ["-video_size", f"{m['w']}x{m['h']}", "-i", disp]
    else:
        geo = ["-video_size", "1280x720", "-i", disp]
    out = f"/tmp/_lphone_rec_{int(_t.time())}.mp4"
    try:
        _recording_proc = subprocess.Popen(
            [ffmpeg, "-y", "-f", "x11grab", "-framerate", str(fps)] + geo +
            ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        _recording_path = out
        return out
    except Exception:
        return None

def _stop_recording() -> str | None:
    global _recording_proc
    if not _recording_proc:
        return None
    path = _recording_path
    try:
        _recording_proc.terminate()
        _recording_proc.wait(timeout=5)
    except Exception:
        try: _recording_proc.kill()
        except Exception: pass
    _recording_proc = None
    return path if os.path.exists(path) else None


# ---------------------------------------------------------------------------
# OCR — tesseract
# ---------------------------------------------------------------------------
def _ocr_screen(monitor_idx: int = 0) -> str:
    img = _capture_screen(monitor_idx)
    if not img:
        return ""
    tmp_png  = "/tmp/_lphone_ocr_in.png"
    tmp_base = "/tmp/_lphone_ocr_out"
    try:
        open(tmp_png, "wb").write(img)
        tess = _find_tool("tesseract")
        if not tess:
            return ""
        r = subprocess.run([tess, tmp_png, tmp_base, "-l", "tur+eng"],
                           capture_output=True, timeout=15)
        out_file = tmp_base + ".txt"
        if os.path.exists(out_file):
            text = open(out_file).read().strip()
            os.unlink(out_file)
            return text
    except Exception:
        pass
    finally:
        try: os.unlink(tmp_png)
        except Exception: pass
    return ""


# ---------------------------------------------------------------------------
# Wake-on-LAN
# ---------------------------------------------------------------------------
def _wol(mac: str) -> bool:
    """Sihirli WoL paketi gönderir. MAC: AA:BB:CC:DD:EE:FF veya AA-BB-CC-DD-EE-FF"""
    import socket
    mac_clean = mac.replace(":", "").replace("-", "").upper()
    if len(mac_clean) != 12:
        return False
    try:
        magic = bytes.fromhex("FF" * 6 + mac_clean * 16)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(magic, ("<broadcast>", 9))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# LAN cihaz tarama (ARP + socket ping)
# ---------------------------------------------------------------------------
def _lan_scan() -> list[dict]:
    """
    Yerel ağdaki aktif cihazları tarar.
    arp-scan kurulu değilse /proc/net/arp'tan okur.
    """
    results: list[dict] = []

    # 1) /proc/net/arp — kurulum gerektirmez
    try:
        with open("/proc/net/arp") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[2] != "0x0":
                    ip  = parts[0]
                    mac = parts[3]
                    if mac != "00:00:00:00:00:00":
                        results.append({"ip": ip, "mac": mac, "hostname": ""})
    except Exception:
        pass

    # 2) arp-scan (daha kapsamlı, root gerekebilir)
    arpscan = _find_tool("arp-scan")
    if arpscan and not results:
        try:
            r = subprocess.run([arpscan, "--localnet"],
                               capture_output=True, text=True, timeout=10)
            import re
            for line in r.stdout.splitlines():
                m = re.match(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f:]{17})\s*(.*)", line, re.I)
                if m:
                    results.append({"ip": m.group(1), "mac": m.group(2),
                                    "hostname": m.group(3).strip()})
        except Exception:
            pass

    # 3) Hostname çözümlemeyi dene (async olmadığı için hızlı timeout)
    import socket as _sock
    for dev in results:
        if not dev["hostname"]:
            try:
                dev["hostname"] = _sock.gethostbyaddr(dev["ip"])[0]
            except Exception:
                pass

    # Tekrarlananları çıkar (IP'ye göre)
    seen: set[str] = set()
    unique = []
    for d in results:
        if d["ip"] not in seen:
            seen.add(d["ip"])
            unique.append(d)
    return sorted(unique, key=lambda x: [int(p) for p in x["ip"].split(".")])


def _screen_status() -> dict:
    """Hangi ekran araçlarının mevcut ve çalışır durumda olduğunu döndürür."""
    disp = os.environ.get("DISPLAY", "")
    # Gerçek masaüstü socket'i var mı?
    real_disp = _find_real_display()
    tools = {
        "xvfb":    _cmd_exists("Xvfb"),
        "scrot":   _cmd_exists("scrot"),
        "ffmpeg":  _cmd_exists("ffmpeg"),
        "xdotool": _cmd_exists("xdotool"),
        "xterm":   _cmd_exists("xterm"),
        "import":  _cmd_exists("import"),
        "gnome-screenshot": _cmd_exists("gnome-screenshot"),
        "spectacle": _cmd_exists("spectacle"),
    }
    effective_disp = real_disp or disp
    capture_tools  = ["scrot", "ffmpeg", "import", "gnome-screenshot", "spectacle"]
    ready          = bool(effective_disp) and any(tools[t] for t in capture_tools)
    control_ready  = bool(effective_disp) and tools["xdotool"]
    missing        = [k for k, v in tools.items() if not v]
    return {
        "display":       effective_disp or None,
        "real_display":  real_disp,
        "tools":         tools,
        "capture_ready": ready,
        "control_ready": control_ready,
        "missing":       missing,
    }


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------
class RenameRequest(BaseModel):
    old_path: str
    new_name: str

class MkdirRequest(BaseModel):
    parent_path: str
    name: str

class NewFileRequest(BaseModel):
    parent_path: str
    name: str

class DeleteRequest(BaseModel):
    path: str

class WriteFileRequest(BaseModel):
    path: str
    content: str

class MoveRequest(BaseModel):
    src_path: str
    dest_dir: str

class CopyRequest(BaseModel):
    src_path: str
    dest_dir: str

class ExtractRequest(BaseModel):
    path: str


# ---------------------------------------------------------------------------
# Frontend HTML (single-file embed)
# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>LPhone Agent</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css"/>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<!-- CodeMirror 5 for syntax-highlighted editor -->
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/theme/dracula.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/python/python.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/javascript/javascript.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/shell/shell.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/yaml/yaml.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/markdown/markdown.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/xml/xml.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/htmlmixed/htmlmixed.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/css/css.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/edit/matchbrackets.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/edit/closebrackets.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/selection/active-line.min.js"></script>
<script>
tailwind.config = {
  theme: {
    extend: {
      colors: {
        background: '#09090b', panel: '#18181b', panelHover: '#27272a',
        border: '#27272a', borderBright: '#3f3f46', primary: '#3b82f6',
        textMain: '#e4e4e7', textMuted: '#a1a1aa', textDim: '#71717a'
      },
      fontFamily: { sans: ['Inter','sans-serif'], mono: ['JetBrains Mono','monospace'] }
    }
  }
}
</script>
<style>
/* ── Reset & base ─────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin:0; padding:0; height:100%; overflow:hidden; background:#09090b; color:#e4e4e7; }

/* ── Scrollbars ───────────────────────────────────────────────── */
.no-scrollbar::-webkit-scrollbar { display:none; }
.no-scrollbar { -ms-overflow-style:none; scrollbar-width:none; }
.scrollbar-thin::-webkit-scrollbar { width:5px; height:5px; }
.scrollbar-thin::-webkit-scrollbar-track { background:transparent; }
.scrollbar-thin::-webkit-scrollbar-thumb { background:#3f3f46; border-radius:10px; }

/* ── Tabs ─────────────────────────────────────────────────────── */
.tab-btn { transition:all .2s; border-top:2px solid transparent; }
.tab-btn.active { background:#18181b; border-top-color:#3b82f6; color:#e4e4e7; }
.tab-btn:not(.active) { opacity:.6; }
.tab-btn:hover:not(.active) { opacity:1; background:rgba(39,39,42,.5); }

/* ── File items ───────────────────────────────────────────────── */
.file-item { transition:all .15s; }
.file-item:hover { background:#27272a; }
.file-item:active { transform:scale(.98); }

/* ── Buttons ──────────────────────────────────────────────────── */
.btn-action { transition:all .15s; }
.btn-action:hover { background:#27272a; color:#fff; border-color:#3f3f46; }
.btn-action:active { transform:scale(.95); }

/* ── Key buttons ──────────────────────────────────────────────── */
.key-btn { transition:all .1s; user-select:none; }
.key-btn:active { background:rgba(59,130,246,.2); border-color:#3b82f6; color:#3b82f6; transform:scale(.95); }
.key-ctrl { border-color:rgba(239,68,68,.4); color:#fca5a5; }
.key-ctrl:active { background:rgba(239,68,68,.2); border-color:#ef4444; color:#ef4444; }
.key-alt  { border-color:rgba(234,179,8,.4);  color:#fde047; }
.key-alt:active  { background:rgba(234,179,8,.2); border-color:#eab308; color:#eab308; }
.key-esc  { border-color:rgba(168,85,247,.4); color:#d8b4fe; }
.key-esc:active  { background:rgba(168,85,247,.2); border-color:#a855f7; color:#a855f7; }
.key-tab  { border-color:rgba(139,92,246,.4);  color:#c4b5fd; }
.key-tab:active  { background:rgba(139,92,246,.2); border-color:#8b5cf6; color:#8b5cf6; }
.key-toggled { background:rgba(59,130,246,.15)!important; border-color:#3b82f6!important; color:#3b82f6!important; }

/* ── xterm ────────────────────────────────────────────────────── */
/* xterm-wrap is the SCROLL CONTAINER for horizontal overflow       */
.xterm-wrap { flex:1 1 auto; min-height:0; overflow-x:auto; overflow-y:hidden;
  position:relative; padding:4px 2px; }
.xterm-wrap .xterm { height:100%; padding:0 6px; min-width:max-content; }
.xterm-wrap .xterm-viewport { background-color:transparent!important;
  -webkit-overflow-scrolling:touch; overscroll-behavior-y:contain; }
.xterm-wrap .xterm-screen { outline:none; }

/* ── Favorites panel ────────────────────────────────────────── */
#favs-backdrop { position:fixed; inset:0; z-index:59; background:rgba(0,0,0,.55); display:none; }
#favs-backdrop.open { display:block; }
#favs-panel { position:fixed; bottom:0; left:0; right:0; z-index:60; background:#0f0f11;
  border-top:1px solid #27272a; border-radius:16px 16px 0 0;
  transform:translateY(100%); transition:transform .25s ease;
  max-height:72vh; display:flex; flex-direction:column; }
#favs-panel.open { transform:translateY(0); }
.fav-item { display:flex; align-items:center; gap:8px; padding:9px 12px;
  border-radius:10px; cursor:pointer; transition:background .15s; }
.fav-item:active { background:rgba(255,255,255,.07); }
/* ── Terminal horizontal scrollbar ───────────────────────────── */
.hscroll-track { height:4px; background:rgba(255,255,255,.06); position:relative;
  margin:0 4px 2px; border-radius:4px; flex-shrink:0; display:none; cursor:pointer; }
.hscroll-track.visible { display:block; }
.hscroll-thumb { position:absolute; top:0; height:100%; background:rgba(255,255,255,.25);
  border-radius:4px; min-width:28px; cursor:grab; transition:background .15s;
  touch-action:none; }
.hscroll-thumb:active, .hscroll-thumb.dragging { background:rgba(99,179,237,.7); cursor:grabbing; }

/* ── Dashboard strip ──────────────────────────────────────────── */
#dashboard-strip {
  display:flex; align-items:center; gap:12px; flex-wrap:nowrap;
  padding:3px 12px; background:#0f0f11; border-bottom:1px solid #27272a;
  font-size:10.5px; font-family:'JetBrains Mono',monospace; color:#71717a;
  overflow:hidden; white-space:nowrap; min-height:26px;
}
.dash-item { display:flex; align-items:center; gap:5px; }
.dash-bar { width:42px; height:5px; background:#27272a; border-radius:9px; overflow:hidden; }
.dash-bar-fill { height:100%; border-radius:9px; transition:width .5s; }

/* ── D-Pad overlay ────────────────────────────────────────────── */
#dpad-overlay {
  position:fixed; bottom:90px; right:14px; z-index:200;
  display:none; flex-direction:column; align-items:center; gap:2px;
  touch-action:none;
}
#dpad-overlay.visible { display:flex; }
.dpad-row { display:flex; gap:2px; }
.dpad-btn {
  width:44px; height:44px; border-radius:10px;
  background:rgba(24,24,27,.88); border:1px solid rgba(63,63,70,.7);
  color:#a1a1aa; font-size:18px; display:flex; align-items:center;
  justify-content:center; cursor:pointer; backdrop-filter:blur(8px);
  user-select:none; transition:background .1s;
}
.dpad-btn:active { background:rgba(59,130,246,.35); color:#93c5fd; }
.dpad-center { background:rgba(59,130,246,.15); border-color:rgba(59,130,246,.4); }

/* ── Snippets panel ───────────────────────────────────────────── */
#snippets-panel {
  position:fixed; bottom:0; left:0; right:0; z-index:150;
  background:#111113; border-top:1px solid #27272a;
  transform:translateY(100%); transition:transform .3s cubic-bezier(.16,1,.3,1);
  max-height:55vh; display:flex; flex-direction:column;
}
#snippets-panel.open { transform:translateY(0); }

/* ── Extra key bar ────────────────────────────────────────────── */
#extra-key-bar {
  overflow:hidden; max-height:0; transition:max-height .25s ease;
  background:#0c0c0e; border-top:1px solid #1f1f22;
}
#extra-key-bar.open { max-height:52px; }

/* ── Context menu ─────────────────────────────────────────────── */
#ctx-menu { opacity:0; transform:scale(.95); pointer-events:none; transition:all .1s ease-out; }
#ctx-menu.visible { opacity:1; transform:scale(1); pointer-events:auto; }

/* ── Toasts ───────────────────────────────────────────────────── */
.toast-enter { transform:translateY(100%); opacity:0; }
.toast-enter-active { transform:translateY(0); opacity:1; transition:all .3s cubic-bezier(.16,1,.3,1); }
.toast-exit-active  { transform:translateY(100%); opacity:0; transition:all .2s ease-in; }

/* ── Modal ────────────────────────────────────────────────────── */
.cm-editor-wrap .CodeMirror { height:100%; font-family:'JetBrains Mono',monospace; font-size:13px; line-height:1.5; }

/* ── Drag zone ────────────────────────────────────────────────── */
.drag-over { outline:2px dashed #3b82f6!important; background:rgba(59,130,246,.05)!important; }

/* ── Image lightbox ───────────────────────────────────────────── */
#img-lightbox { position:fixed; inset:0; z-index:200; background:rgba(0,0,0,.92);
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  opacity:0; pointer-events:none; transition:opacity .18s ease; }
#img-lightbox.visible { opacity:1; pointer-events:auto; }
#img-lightbox img { max-width:calc(100vw - 32px); max-height:calc(100vh - 80px);
  object-fit:contain; border-radius:8px; box-shadow:0 8px 64px rgba(0,0,0,.8); }

/* ── Text preview ─────────────────────────────────────────────── */
#txt-preview { position:fixed; inset:0; z-index:200; background:rgba(0,0,0,.85);
  display:flex; align-items:center; justify-content:center;
  opacity:0; pointer-events:none; transition:opacity .18s ease; }
#txt-preview.visible { opacity:1; pointer-events:auto; }
#txt-preview-card { background:#18181b; border:1px solid #3f3f46; border-radius:16px;
  width:min(92vw,760px); max-height:85vh; display:flex; flex-direction:column;
  box-shadow:0 16px 64px rgba(0,0,0,.7); overflow:hidden; }
#txt-preview pre { margin:0; padding:14px 16px; font-family:'JetBrains Mono',monospace;
  font-size:12px; line-height:1.6; color:#e4e4e7; white-space:pre-wrap;
  word-break:break-all; overflow-y:auto; flex:1; }

/* ── Mobile ───────────────────────────────────────────────────── */
@media (max-width:767px) { .mobile-hidden { display:none!important; } }

/* ── Swipe hint ───────────────────────────────────────────────── */
.swipe-container { overflow:hidden; position:relative; }

/* ── Tool slide-up panels ─────────────────────────────────────── */
.tool-backdrop { position:fixed; inset:0; z-index:59; background:rgba(0,0,0,.55);
  display:none; }
.tool-backdrop.open { display:block; }
.tool-panel { position:fixed; bottom:0; left:0; right:0; z-index:60; background:#0f0f11;
  border-top:1px solid #27272a; border-radius:16px 16px 0 0;
  transform:translateY(100%); transition:transform .25s ease;
  max-height:85vh; display:flex; flex-direction:column; }
.tool-panel.open { transform:translateY(0); }
.tool-panel-hdr { display:flex; align-items:center; justify-content:space-between;
  padding:14px 16px 10px; border-bottom:1px solid #27272a; flex-shrink:0; }
.tool-panel-body { flex:1; overflow-y:auto; padding:12px 14px 24px; }

/* ── Tools grid ───────────────────────────────────────────────── */
.tools-grid { display:flex; flex-direction:column; gap:0; padding:0 0 24px; }
.tools-section { padding:16px 14px 0; }
.tools-section-label {
  font-size:10px; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
  color:#52525b; margin-bottom:8px; padding-left:2px; }
.tools-row { display:grid; grid-template-columns:repeat(2,1fr); gap:9px; }
.tool-card {
  background:#131316; border:1px solid #222227; border-radius:14px;
  padding:14px 14px 13px; display:flex; flex-direction:row; align-items:center; gap:12px;
  cursor:pointer; transition:background .13s, border-color .13s;
  min-height:64px; text-align:left; }
.tool-card:active { background:#1e1e24; border-color:#3b82f680; transform:scale(.98); }
@media(hover:hover){.tool-card:hover{background:#1c1c22;border-color:#3f3f46;}}
.tool-card-icon { width:40px; height:40px; border-radius:12px; display:flex;
  align-items:center; justify-content:center; flex-shrink:0; }
.tool-card-text { display:flex; flex-direction:column; gap:2px; min-width:0; }
.tool-card-label { font-size:11.5px; font-weight:600; color:#e4e4e7; line-height:1.2; }
.tool-card-desc { font-size:10px; color:#52525b; line-height:1.3; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }

/* ── Process table ────────────────────────────────────────────── */
.proc-table { width:100%; border-collapse:collapse; font-size:11.5px; font-family:
  'JetBrains Mono',monospace; }
.proc-table th { text-align:left; padding:5px 6px; color:#71717a; font-weight:600;
  font-size:10px; border-bottom:1px solid #27272a; position:sticky; top:0;
  background:#0f0f11; }
.proc-table td { padding:5px 6px; color:#d4d4d8; border-bottom:1px solid #1a1a1f; 
  white-space:nowrap; }
.proc-table tr:hover td { background:rgba(255,255,255,.03); }

/* ── Autocomplete bar ─────────────────────────────────────────── */
.ac-bar { display:flex; gap:5px; overflow-x:auto; padding:4px 8px; flex-shrink:0;
  background:#111113; border-top:1px solid #1e1e23; scrollbar-width:none; }
.ac-bar::-webkit-scrollbar { display:none; }
.ac-chip { padding:3px 10px; border-radius:6px; background:#1e1e26; border:1px solid #2e2e3a;
  color:#c4c4cf; font-size:11px; font-family:'JetBrains Mono',monospace; white-space:nowrap;
  cursor:pointer; flex-shrink:0; transition:background .12s; }
.ac-chip:hover { background:#2a2a38; }

/* ── Live screen overlay ──────────────────────────────────────── */
#screen-modal { position:fixed; inset:0; background:#000; z-index:200;
  display:none; flex-direction:column; }
#screen-modal.open { display:flex; }
#screen-img { max-width:100%; max-height:100%; object-fit:contain; flex:1;
  cursor:crosshair; touch-action:none; }

/* ── Share modal ──────────────────────────────────────────────── */
#share-modal { position:fixed; inset:0; z-index:150; background:rgba(0,0,0,.7);
  backdrop-filter:blur(4px); display:none; align-items:center; justify-content:center; }
#share-modal.open { display:flex; }
#share-box { background:#141417; border:1px solid #27272a; border-radius:18px;
  padding:22px; width:min(90vw,340px); }

/* ── Notification dot ─────────────────────────────────────────── */
.notif-badge { position:absolute; top:-3px; right:-3px; width:8px; height:8px;
  border-radius:50%; background:#ef4444; border:2px solid #0f0f11; display:none; }
.notif-badge.on { display:block; }

/* ── Device panel ────────────────────────────────────────────── */
#dev-backdrop { position:fixed; inset:0; z-index:69; background:rgba(0,0,0,.6);
  backdrop-filter:blur(3px); display:none; }
#dev-backdrop.open { display:block; }
#dev-panel { position:fixed; top:0; left:0; bottom:0; z-index:70;
  width:min(88vw,340px); background:#0d0d10;
  border-right:1px solid #27272a;
  transform:translateX(-100%); transition:transform .28s cubic-bezier(.16,1,.3,1);
  display:flex; flex-direction:column; }
#dev-panel.open { transform:translateX(0); }
.dev-card { display:flex; align-items:center; gap:10px; padding:10px 14px;
  border-radius:10px; cursor:pointer; transition:background .15s; user-select:none; }
.dev-card:hover { background:rgba(255,255,255,.05); }
.dev-card.active { background:rgba(59,130,246,.12); border:1px solid rgba(59,130,246,.3); }
.dev-dot { width:10px; height:10px; border-radius:50%; flex-shrink:0; }
.dev-status { width:7px; height:7px; border-radius:50%; flex-shrink:0; }
.dev-status.online  { background:#22c55e; box-shadow:0 0 6px #22c55e88; }
.dev-status.offline { background:#52525b; }
.dev-status.pinging { background:#f59e0b; animation:blink .8s infinite; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }
#dev-switcher-btn { display:flex; align-items:center; gap:6px; padding:4px 10px;
  border-radius:7px; border:1px solid #3f3f46; background:transparent;
  color:#a1a1aa; font-size:12px; cursor:pointer; transition:all .15s; font-family:inherit; }
#dev-switcher-btn:hover { background:#27272a; color:#fff; border-color:#52525b; }
.dev-ssh-badge { font-size:9px; padding:1px 5px; border-radius:4px;
  background:rgba(99,102,241,.2); color:#a5b4fc; font-family:'JetBrains Mono',monospace; }
/* ── Platform terminal theme ──────────────────────────────────── */
.term-theme-windows .xterm-viewport { background:#0c0c0c!important; }
.term-theme-macos   .xterm-viewport { background:#1e1e2e!important; }
.term-theme-arch    .xterm-viewport { background:#050510!important; }
.term-theme-fedora  .xterm-viewport { background:#06060f!important; }
</style>
</head>
<body class="antialiased text-sm">

<!-- ═══════════════════════════════════════════════════════════════
     DEVICE PANEL
══════════════════════════════════════════════════════════════════ -->
<div id="dev-backdrop" onclick="Devices.close()"></div>
<div id="dev-panel">
  <!-- Header -->
  <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 16px 10px;border-bottom:1px solid #1f1f22;flex-shrink:0">
    <div style="display:flex;align-items:center;gap:8px">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2.5" stroke-linecap="round"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/><line x1="12" y1="12" x2="12" y2="16"/><line x1="10" y1="14" x2="14" y2="14"/></svg>
      <span style="font-size:13px;font-weight:700;color:#e4e4e7">Cihazlar</span>
    </div>
    <div style="display:flex;gap:6px">
      <button onclick="Devices.showAdd()" title="Yeni cihaz ekle"
        style="padding:5px 10px;font-size:11px;background:rgba(59,130,246,.15);color:#93c5fd;border:1px solid rgba(59,130,246,.3);border-radius:7px;cursor:pointer;font-family:inherit;font-weight:600">+ Ekle</button>
      <button onclick="Devices.close()"
        style="padding:5px 8px;font-size:14px;background:transparent;color:#52525b;border:none;cursor:pointer">✕</button>
    </div>
  </div>
  <!-- Device list -->
  <div id="dev-list-wrap" style="flex:1;overflow-y:auto;padding:10px 10px 0">
    <div id="dev-list" style="display:flex;flex-direction:column;gap:2px"></div>
    <!-- Tailscale peers auto-discovered -->
    <div id="dev-ts-section" style="margin-top:8px;padding-top:8px;border-top:1px solid #1f1f22"></div>
  </div>
  <!-- Footer info -->
  <div style="padding:10px 14px;border-top:1px solid #1f1f22;font-size:10px;color:#3f3f46;flex-shrink:0">
    SSH bağlantıları için sunucuda <code style="color:#52525b;background:#18181b;padding:1px 4px;border-radius:3px">pip install paramiko</code> gereklidir.
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════════
     APP SHELL
══════════════════════════════════════════════════════════════════ -->
<div id="app" class="flex flex-col h-full w-full overflow-hidden">

  <!-- Desktop header -->
  <header class="hidden md:flex h-12 border-b border-border bg-panel items-center px-4 justify-between shrink-0 z-10 gap-3">
    <div class="flex items-center gap-3 shrink-0">
      <div class="w-7 h-7 rounded-lg bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center text-white shadow-[0_0_14px_rgba(59,130,246,.3)]">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
      </div>
      <span class="font-bold text-[14px] tracking-tight">LPhone Agent</span>
    </div>
    <!-- Device switcher -->
    <button id="dev-switcher-btn" onclick="Devices.open()" title="Cihaz yönetimi">
      <span id="dev-active-dot" class="dev-dot" style="background:#3b82f6"></span>
      <span id="dev-active-name" class="font-medium" style="color:#e4e4e7">Bu Cihaz</span>
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
    </button>
    <div class="flex items-center gap-2 ml-auto shrink-0">
      <button onclick="addTerminal()" class="btn-action flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-border bg-background text-textMuted text-xs font-semibold">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> Yeni Terminal
      </button>
      <button onclick="addFileManager()" class="btn-action flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-border bg-background text-textMuted text-xs font-semibold">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> Yeni Tarayıcı
      </button>
    </div>
  </header>

  <!-- Main panels -->
  <main class="flex-1 flex flex-col md:flex-row overflow-hidden relative w-full">

    <!-- ── TERMINAL SECTION ─────────────────────────────────────── -->
    <section id="sec-terminals" class="flex-1 flex flex-col border-r border-border min-w-0">
      <!-- Dashboard strip -->
      <div id="dashboard-strip">
        <div class="dash-item">
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#60a5fa" stroke-width="2.5"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
          <span id="dash-cpu">CPU —</span>
          <div class="dash-bar"><div id="dash-cpu-bar" class="dash-bar-fill bg-blue-500" style="width:0%"></div></div>
        </div>
        <div class="dash-item">
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#a3e635" stroke-width="2.5"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z"/><path d="M12 6v6l4 2"/></svg>
          <span id="dash-ram">RAM —</span>
          <div class="dash-bar"><div id="dash-ram-bar" class="dash-bar-fill bg-lime-500" style="width:0%"></div></div>
        </div>
        <div class="dash-item" title="Disk">
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2.5"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
          <span id="dash-disk">Disk —</span>
          <div class="dash-bar"><div id="dash-disk-bar" class="dash-bar-fill bg-amber-500" style="width:0%"></div></div>
        </div>
        <div class="w-px h-3 bg-zinc-700 mx-1"></div>
        <div class="dash-item" id="dash-tailscale">
          <span class="w-1.5 h-1.5 rounded-full bg-zinc-600" id="dash-ts-dot"></span>
          <span id="dash-ts-label">Tailscale —</span>
        </div>
      </div>

      <!-- Tab bar -->
      <div class="flex h-10 bg-background border-b border-border items-end px-2 gap-1 overflow-x-auto no-scrollbar shrink-0" id="terminal-tabs-container"></div>

      <!-- Terminal views container -->
      <div id="terminal-views-container" class="flex-1 relative bg-[#0a0a0c]"></div>
    </section>

    <!-- ── FILE MANAGER SECTION ─────────────────────────────────── -->
    <section id="sec-files" class="flex-1 flex flex-col min-w-0 mobile-hidden bg-background">
      <div class="flex h-10 bg-background border-b border-border items-end px-2 gap-1 overflow-x-auto no-scrollbar shrink-0" id="file-tabs-container"></div>
      <div id="file-views-container" class="flex-1 relative"></div>
    </section>

    <!-- ── SETTINGS SECTION ─────────────────────────────────────── -->
    <section id="sec-settings" class="flex-1 flex flex-col bg-panel p-5 mobile-hidden hidden">
      <h2 class="text-lg font-bold mb-5 text-white tracking-tight">Sistem Ayarları</h2>
      <div class="space-y-3 max-w-md">
        <div class="bg-background border border-border rounded-xl p-4 flex items-center justify-between">
          <div><div class="font-medium text-white text-sm mb-0.5">Yeni Terminal</div><div class="text-xs text-textMuted">İzole pty bağlantısı açar.</div></div>
          <button onclick="addTerminal(); switchMobileTab('terminals')" class="p-2 bg-primary/10 text-primary rounded-lg hover:bg-primary/20">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          </button>
        </div>
        <div class="bg-background border border-border rounded-xl p-4 flex items-center justify-between">
          <div><div class="font-medium text-white text-sm mb-0.5">Yeni Dosya Tarayıcı</div><div class="text-xs text-textMuted">Bağımsız dosya yöneticisi.</div></div>
          <button onclick="addFileManager(); switchMobileTab('files')" class="p-2 bg-emerald-500/10 text-emerald-500 rounded-lg hover:bg-emerald-500/20">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          </button>
        </div>
        <!-- D-Pad start setting -->
        <div class="bg-background border border-border rounded-xl p-4 flex items-center justify-between">
          <div>
            <div class="font-medium text-white text-sm mb-0.5">D-Pad Başlangıçta Açık</div>
            <div class="text-xs text-textMuted">Terminal sekmesine geçildiğinde yön tuşları görünsün.</div>
          </div>
          <button id="dpad-setting-btn" onclick="AppSettings.toggleDpad()"
                  class="w-11 h-6 rounded-full bg-zinc-700 transition-colors relative flex items-center px-0.5 shrink-0">
            <span id="dpad-setting-knob" class="w-5 h-5 rounded-full bg-white shadow transition-transform block"></span>
          </button>
        </div>
        <div class="bg-background border border-border rounded-xl p-4">
          <div class="font-medium text-white text-sm mb-3">Canlı Sistem Bilgisi</div>
          <div id="settings-stats" class="grid grid-cols-2 gap-2 text-xs text-textMuted font-mono"></div>
        </div>
      </div>
    </section>

    <!-- ── TOOLS SECTION ─────────────────────────────────────────── -->
    <section id="sec-tools" class="flex-1 flex flex-col bg-background mobile-hidden overflow-y-auto">

      <!-- sticky header -->
      <div style="position:sticky;top:0;z-index:10;background:rgba(9,9,11,.95);backdrop-filter:blur(12px);border-bottom:1px solid #1e1e23;padding:12px 14px 10px;flex-shrink:0;display:flex;align-items:center;gap:10px;">
        <div style="flex:1;">
          <div style="font-size:15px;font-weight:700;color:#fff;line-height:1;">Araçlar</div>
          <div style="font-size:10px;color:#52525b;margin-top:2px;">Tüm araçlara buradan ulaşın</div>
        </div>
        <button onclick="Notifs.requestPermission()" id="notif-btn"
          style="display:flex;align-items:center;gap:6px;padding:6px 11px;border-radius:9px;border:1px solid #27272a;background:#111113;color:#71717a;font-size:11px;font-weight:600;cursor:pointer;font-family:inherit;">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
          Bildirim
        </button>
      </div>

      <div class="tools-grid">

        <!-- ── EKRAN & KONTROL ─────────────────────────── -->
        <div class="tools-section">
          <div class="tools-section-label">🖥 Ekran &amp; Kontrol</div>
          <div class="tools-row">
            <div class="tool-card" onclick="ScreenViewer.openLive(true)">
              <div class="tool-card-icon" style="background:rgba(239,68,68,.15)">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2" stroke-linecap="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2"/></svg>
              </div>
              <div class="tool-card-text">
                <div class="tool-card-label">Canlı Ekran</div>
                <div class="tool-card-desc">İzle + fare &amp; klavye</div>
              </div>
            </div>
            <div class="tool-card" onclick="ScreenViewer.screenshot()">
              <div class="tool-card-icon" style="background:rgba(236,72,153,.15)">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#ec4899" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
              </div>
              <div class="tool-card-text">
                <div class="tool-card-label">Ekran Görüntüsü</div>
                <div class="tool-card-desc">Anlık yakalama</div>
              </div>
            </div>
            <div class="tool-card" id="tc-rec-card" onclick="ScreenViewer.openLive(false); setTimeout(()=>ScreenViewer.toggleRecording(),600)">
              <div class="tool-card-icon" style="background:rgba(239,68,68,.12)">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3" fill="#ef4444"/></svg>
              </div>
              <div class="tool-card-text">
                <div class="tool-card-label">Ekran Kaydı</div>
                <div class="tool-card-desc">ffmpeg MP4 kayıt</div>
              </div>
            </div>
            <div class="tool-card" onclick="ScreenViewer.runOCR()">
              <div class="tool-card-icon" style="background:rgba(168,85,247,.15)">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#a855f7" stroke-width="2" stroke-linecap="round"><path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><rect x="7" y="7" width="10" height="10"/></svg>
              </div>
              <div class="tool-card-text">
                <div class="tool-card-label">OCR</div>
                <div class="tool-card-desc">Ekrandan metin oku</div>
              </div>
            </div>
            <div class="tool-card" onclick="ScreenViewer.openClipboard()">
              <div class="tool-card-icon" style="background:rgba(20,184,166,.15)">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#14b8a6" stroke-width="2" stroke-linecap="round"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/></svg>
              </div>
              <div class="tool-card-text">
                <div class="tool-card-label">Pano Senkronu</div>
                <div class="tool-card-desc">Kopyala / yapıştır</div>
              </div>
            </div>
          </div>
        </div>

        <!-- ── SİSTEM ────────────────────────────────────── -->
        <div class="tools-section" style="margin-top:18px;">
          <div class="tools-section-label">⚙️ Sistem</div>
          <div class="tools-row">
            <div class="tool-card" onclick="ProcessMgr.open()">
              <div class="tool-card-icon" style="background:rgba(59,130,246,.15)">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#60a5fa" stroke-width="2" stroke-linecap="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
              </div>
              <div class="tool-card-text">
                <div class="tool-card-label">İşlem Yöneticisi</div>
                <div class="tool-card-desc">CPU/RAM · kill</div>
              </div>
            </div>
            <div class="tool-card" onclick="PkgMgr.open()">
              <div class="tool-card-icon" style="background:rgba(16,185,129,.15)">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2" stroke-linecap="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>
              </div>
              <div class="tool-card-text">
                <div class="tool-card-label">Paket Yöneticisi</div>
                <div class="tool-card-desc">apt · pip · npm</div>
              </div>
            </div>
            <div class="tool-card" onclick="Scheduler.open()">
              <div class="tool-card-icon" style="background:rgba(245,158,11,.15)">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
              </div>
              <div class="tool-card-text">
                <div class="tool-card-label">Zamanlayıcı</div>
                <div class="tool-card-desc">Periyodik komutlar</div>
              </div>
            </div>
            <div class="tool-card" onclick="Notifs.openSettings()">
              <div class="tool-card-icon" style="background:rgba(251,146,60,.15)">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fb923c" stroke-width="2" stroke-linecap="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
              </div>
              <div class="tool-card-text">
                <div class="tool-card-label">Bildirim Eşikleri</div>
                <div class="tool-card-desc">CPU / RAM uyarıları</div>
              </div>
            </div>
            <div class="tool-card" onclick="Plugins.open()">
              <div class="tool-card-icon" style="background:rgba(99,102,241,.15)">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#818cf8" stroke-width="2" stroke-linecap="round"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>
              </div>
              <div class="tool-card-text">
                <div class="tool-card-label">Modüller</div>
                <div class="tool-card-desc">Eklenti yönetimi</div>
              </div>
            </div>
          </div>
        </div>

        <!-- ── AĞ ────────────────────────────────────────── -->
        <div class="tools-section" style="margin-top:18px;">
          <div class="tools-section-label">🌐 Ağ</div>
          <div class="tools-row">
            <div class="tool-card" onclick="LANScan.open()">
              <div class="tool-card-icon" style="background:rgba(34,197,94,.15)">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="2" stroke-linecap="round"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
              </div>
              <div class="tool-card-text">
                <div class="tool-card-label">Ağ Tarama</div>
                <div class="tool-card-desc">LAN cihazlarını keşfet</div>
              </div>
            </div>
            <div class="tool-card" onclick="WoL.open()">
              <div class="tool-card-icon" style="background:rgba(251,146,60,.15)">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fb923c" stroke-width="2" stroke-linecap="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>
              </div>
              <div class="tool-card-text">
                <div class="tool-card-label">Wake-on-LAN</div>
                <div class="tool-card-desc">Cihazı uyandır</div>
              </div>
            </div>
          </div>
        </div>

        <!-- ── DOSYA ──────────────────────────────────────── -->
        <div class="tools-section" style="margin-top:18px;">
          <div class="tools-section-label">📁 Dosya</div>
          <div class="tools-row">
            <div class="tool-card" onclick="FileSync.open()">
              <div class="tool-card-icon" style="background:rgba(139,92,246,.15)">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#a78bfa" stroke-width="2" stroke-linecap="round"><polyline points="1 4 1 10 7 10"/><polyline points="23 20 23 14 17 14"/><path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15"/></svg>
              </div>
              <div class="tool-card-text">
                <div class="tool-card-label">Dosya Senkronu</div>
                <div class="tool-card-desc">rsync ile eşitle</div>
              </div>
            </div>
          </div>
        </div>

      </div><!-- /tools-grid -->
    </section>

  </main><!-- /main -->

  <!-- ═══════════════════════════════════════════════════════
       TOOL PANELS (slide-up)
  ════════════════════════════════════════════════════════ -->

  <!-- Process Manager Panel -->
  <div class="tool-backdrop" id="proc-bd" onclick="ProcessMgr.close()"></div>
  <div class="tool-panel" id="proc-panel">
    <div class="tool-panel-hdr">
      <span class="font-bold text-white text-sm">⚡ İşlem Yöneticisi</span>
      <div class="flex items-center gap-2">
        <button onclick="ProcessMgr.refresh()" class="px-2.5 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded-lg text-zinc-300 font-medium">Yenile</button>
        <button onclick="ProcessMgr.toggleAutoRefresh()" id="proc-auto-btn"
          class="px-2.5 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 rounded-lg text-zinc-300 font-medium">Oto: Kapalı</button>
        <button onclick="ProcessMgr.close()" class="text-zinc-500 hover:text-white text-xl leading-none px-1">×</button>
      </div>
    </div>
    <div class="tool-panel-body p-0" id="proc-body" style="padding:0"></div>
  </div>

  <!-- Package Manager Panel -->
  <div class="tool-backdrop" id="pkg-bd" onclick="PkgMgr.close()"></div>
  <div class="tool-panel" id="pkg-panel">
    <div class="tool-panel-hdr">
      <span class="font-bold text-white text-sm">📦 Paket Yöneticisi</span>
      <button onclick="PkgMgr.close()" class="text-zinc-500 hover:text-white text-xl leading-none px-1">×</button>
    </div>
    <div class="tool-panel-body">
      <div class="flex gap-2 mb-3">
        <select id="pkg-mgr-sel" class="flex-1 bg-zinc-800 border border-zinc-700 text-white rounded-lg px-2 py-1.5 text-sm">
          <option value="apt">sudo apt</option>
          <option value="pip">pip</option>
          <option value="npm">npm</option>
        </select>
        <input id="pkg-search-inp" type="search" placeholder="Paket ara..." autocomplete="off"
          class="flex-1 bg-zinc-800 border border-zinc-700 text-white rounded-lg px-3 py-1.5 text-sm outline-none placeholder:text-zinc-600"
          oninput="PkgMgr.onInput(this.value)"
          onkeydown="if(event.key==='Enter') PkgMgr.search()">
        <button onclick="PkgMgr.search()" class="px-3 py-1.5 bg-primary rounded-lg text-white text-sm font-semibold">Ara</button>
      </div>
      <div id="pkg-results" class="space-y-1.5 mb-3"></div>
      <div class="border-t border-border pt-3 mt-3">
        <div class="text-xs font-semibold text-zinc-500 mb-2">HIZLI KOMUT</div>
        <div class="flex gap-2">
          <input id="pkg-cmd-inp" type="text" placeholder="örn: apt install htop"
            class="flex-1 bg-zinc-800 border border-zinc-700 text-white rounded-lg px-3 py-1.5 text-sm outline-none placeholder:text-zinc-600 font-mono"
            onkeydown="if(event.key==='Enter') PkgMgr.runCmd()">
          <button onclick="PkgMgr.runCmd()" class="px-3 py-1.5 bg-emerald-600 rounded-lg text-white text-sm font-semibold">Çalıştır</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Task Scheduler Panel -->
  <div class="tool-backdrop" id="sched-bd" onclick="Scheduler.close()"></div>
  <div class="tool-panel" id="sched-panel">
    <div class="tool-panel-hdr">
      <span class="font-bold text-white text-sm">⏰ Görev Zamanlayıcı</span>
      <div class="flex items-center gap-2">
        <button onclick="Scheduler.showAdd()" class="px-2.5 py-1 text-xs bg-primary/90 rounded-lg text-white font-medium">+ Ekle</button>
        <button onclick="Scheduler.close()" class="text-zinc-500 hover:text-white text-xl leading-none px-1">×</button>
      </div>
    </div>
    <div class="tool-panel-body">
      <!-- Add form (hidden by default) -->
      <div id="sched-add-form" style="display:none" class="bg-zinc-900 border border-zinc-700 rounded-xl p-3 mb-3 flex flex-col gap-2">
        <input id="sched-inp-name" type="text" placeholder="Görev adı" autocomplete="off"
          class="bg-zinc-800 border border-zinc-700 text-white rounded-lg px-3 py-1.5 text-sm outline-none placeholder:text-zinc-600">
        <input id="sched-inp-cmd"  type="text" placeholder="Komut (örn: df -h)" autocomplete="off"
          class="bg-zinc-800 border border-zinc-700 text-white rounded-lg px-3 py-1.5 text-sm outline-none placeholder:text-zinc-600 font-mono">
        <input id="sched-inp-sched" type="text" placeholder="Zamanlama: every 5m | every 1h | every 2d" autocomplete="off"
          class="bg-zinc-800 border border-zinc-700 text-white rounded-lg px-3 py-1.5 text-sm outline-none placeholder:text-zinc-600">
        <div class="flex gap-2 mt-1">
          <button onclick="Scheduler.cancelAdd()" class="flex-1 py-1.5 text-sm bg-zinc-800 rounded-lg text-zinc-400 font-semibold">İptal</button>
          <button onclick="Scheduler.saveAdd()" class="flex-1 py-1.5 text-sm bg-primary rounded-lg text-white font-semibold">Kaydet</button>
        </div>
      </div>
      <div id="sched-list" class="space-y-2"></div>
      <div id="sched-empty" class="text-center py-10 text-zinc-600 text-sm">Henüz görev yok. + Ekle ile başlayın.</div>
    </div>
  </div>

  <!-- File Sync Panel -->
  <div class="tool-backdrop" id="sync-bd" onclick="FileSync.close()"></div>
  <div class="tool-panel" id="sync-panel">
    <div class="tool-panel-hdr">
      <span class="font-bold text-white text-sm">🔄 Dosya Senkronizasyonu</span>
      <button onclick="FileSync.close()" class="text-zinc-500 hover:text-white text-xl leading-none px-1">×</button>
    </div>
    <div class="tool-panel-body flex flex-col gap-3">
      <div>
        <label class="text-xs text-zinc-500 font-semibold mb-1 block">KAYNAK</label>
        <input id="sync-src" type="text" placeholder="/home/user/myfiles/ veya user@host:/path/"
          class="w-full bg-zinc-800 border border-zinc-700 text-white rounded-lg px-3 py-2 text-sm outline-none placeholder:text-zinc-600 font-mono">
      </div>
      <div>
        <label class="text-xs text-zinc-500 font-semibold mb-1 block">HEDEF</label>
        <input id="sync-dst" type="text" placeholder="/backup/ veya user@host:/backup/"
          class="w-full bg-zinc-800 border border-zinc-700 text-white rounded-lg px-3 py-2 text-sm outline-none placeholder:text-zinc-600 font-mono">
      </div>
      <div class="flex gap-2">
        <button onclick="FileSync.run(true)" class="flex-1 py-2 text-sm bg-zinc-800 border border-zinc-700 rounded-lg text-zinc-300 font-semibold">Simüle Et</button>
        <button onclick="FileSync.run(false)" class="flex-1 py-2 text-sm bg-primary rounded-lg text-white font-semibold">Senkronize Et</button>
      </div>
      <pre id="sync-output" class="bg-zinc-900 rounded-xl p-3 text-xs text-zinc-300 font-mono whitespace-pre-wrap max-h-[240px] overflow-y-auto hidden"></pre>
    </div>
  </div>

  <!-- Notification Settings Panel -->
  <div class="tool-backdrop" id="notif-bd" onclick="Notifs.closeSettings()"></div>
  <div class="tool-panel" id="notif-panel">
    <div class="tool-panel-hdr">
      <span class="font-bold text-white text-sm">🔔 Bildirim Eşikleri</span>
      <button onclick="Notifs.closeSettings()" class="text-zinc-500 hover:text-white text-xl leading-none px-1">×</button>
    </div>
    <div class="tool-panel-body flex flex-col gap-4">
      <p class="text-xs text-zinc-500">Belirtilen değeri aşan CPU/RAM/Disk kullanımında bildirim alırsınız.</p>
      <div>
        <label class="text-xs text-zinc-500 font-semibold mb-1 block">CPU EŞIĞI (%)</label>
        <input id="thresh-cpu" type="number" min="1" max="100" value="85"
          class="w-full bg-zinc-800 border border-zinc-700 text-white rounded-lg px-3 py-2 text-sm outline-none">
      </div>
      <div>
        <label class="text-xs text-zinc-500 font-semibold mb-1 block">RAM EŞIĞI (%)</label>
        <input id="thresh-ram" type="number" min="1" max="100" value="90"
          class="w-full bg-zinc-800 border border-zinc-700 text-white rounded-lg px-3 py-2 text-sm outline-none">
      </div>
      <div>
        <label class="text-xs text-zinc-500 font-semibold mb-1 block">DİSK EŞIĞI (%)</label>
        <input id="thresh-disk" type="number" min="1" max="100" value="95"
          class="w-full bg-zinc-800 border border-zinc-700 text-white rounded-lg px-3 py-2 text-sm outline-none">
      </div>
      <button onclick="Notifs.saveSettings()" class="py-2.5 bg-primary rounded-xl text-white font-semibold text-sm">Kaydet</button>
    </div>
  </div>

  <!-- Plugins Panel -->
  <div class="tool-backdrop" id="plugins-bd" onclick="Plugins.close()"></div>
  <div class="tool-panel" id="plugins-panel">
    <div class="tool-panel-hdr">
      <span class="font-bold text-white text-sm">⭐ Modüller</span>
      <button onclick="Plugins.close()" class="text-zinc-500 hover:text-white text-xl leading-none px-1">×</button>
    </div>
    <div class="tool-panel-body" id="plugins-list"></div>
  </div>

  <!-- Live Screen Full-Screen Modal -->
  <div id="screen-modal">
    <!-- Top bar -->
    <div class="flex items-center px-3 py-2 bg-zinc-900/95 shrink-0 gap-2 flex-wrap border-b border-zinc-800">
      <span id="screen-modal-title" class="text-xs text-zinc-400 font-mono flex-1 min-w-0 truncate">Canlı Ekran</span>
      <span id="screen-fps" class="text-xs text-zinc-600 font-mono"></span>
      <!-- Monitor picker -->
      <select id="screen-monitor-sel" onchange="ScreenViewer.setMonitor(+this.value)"
        class="bg-zinc-800 border border-zinc-700 text-zinc-300 text-xs rounded-lg px-2 py-1 outline-none">
        <option value="0">🖥 Tüm Ekran</option>
      </select>
      <!-- Performance mode -->
      <select id="screen-perf-sel" onchange="ScreenViewer.setPerf(this.value)"
        class="bg-zinc-800 border border-zinc-700 text-zinc-300 text-xs rounded-lg px-2 py-1 outline-none">
        <option value="balanced">⚖ Dengeli</option>
        <option value="fast">⚡ Düşük Gecikme</option>
        <option value="quality">🎨 Kalite</option>
        <option value="saver">🔋 Veri Tasarrufu</option>
      </select>
      <!-- Record button -->
      <button id="screen-rec-btn" onclick="ScreenViewer.toggleRecording()"
        class="px-2.5 py-1 text-xs rounded-lg bg-zinc-800 text-zinc-400 font-medium flex items-center gap-1">
        <span id="screen-rec-dot" class="w-2 h-2 rounded-full bg-zinc-600 inline-block"></span>Kayıt
      </button>
      <!-- Clipboard button -->
      <button onclick="ScreenViewer.openClipboard()"
        class="px-2.5 py-1 text-xs rounded-lg bg-zinc-800 text-zinc-400 font-medium">📋</button>
      <!-- OCR button -->
      <button onclick="ScreenViewer.runOCR()"
        class="px-2.5 py-1 text-xs rounded-lg bg-zinc-800 text-zinc-400 font-medium" title="Ekrandan metin oku (OCR)">🔍</button>
      <!-- Control toggle -->
      <button id="screen-ctrl-btn" onclick="ScreenViewer.toggleControl()"
        class="px-2.5 py-1 text-xs rounded-lg bg-zinc-800 text-zinc-400 font-medium">Kontrol: Kapalı</button>
      <button onclick="ScreenViewer.close()" class="text-zinc-500 hover:text-white text-xl leading-none px-1.5">×</button>
    </div>
    <!-- Input bar (visible when control on) -->
    <div id="screen-ctrl-bar" class="hidden px-3 py-2 bg-zinc-900 border-b border-zinc-700 flex items-center gap-2 shrink-0 flex-wrap">
      <input id="screen-type-inp" type="text" placeholder="Metin yaz (Enter gönderir)"
        class="flex-1 bg-zinc-800 border border-zinc-700 text-white rounded-lg px-2 py-1 text-xs outline-none min-w-0"
        onkeydown="if(event.key==='Enter'){ScreenViewer.typeText(this.value);this.value=''}">
      <button onclick="ScreenViewer.sendKey('Return')" class="px-2 py-1 text-xs bg-zinc-800 rounded text-zinc-300">↵</button>
      <button onclick="ScreenViewer.sendKey('Tab')"    class="px-2 py-1 text-xs bg-zinc-800 rounded text-zinc-300">⇥</button>
      <button onclick="ScreenViewer.sendKey('Escape')" class="px-2 py-1 text-xs bg-zinc-800 rounded text-zinc-300">Esc</button>
      <button onclick="ScreenViewer.sendKey('ctrl+c')" class="px-2 py-1 text-xs bg-zinc-800 rounded text-zinc-300">^C</button>
      <button onclick="ScreenViewer.sendKey('super')"  class="px-2 py-1 text-xs bg-zinc-800 rounded text-zinc-300">⊞</button>
      <button onclick="ScreenViewer.sendKey('alt+F4')" class="px-2 py-1 text-xs bg-zinc-800 rounded text-red-400">Alt+F4</button>
    </div>
    <!-- Image area -->
    <div class="flex-1 overflow-hidden flex items-center justify-center bg-black relative">
      <img id="screen-img" src="" alt="" draggable="false">
      <div id="screen-err" style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;pointer-events:none;gap:10px;padding:20px;"></div>
    </div>
  </div>

  <!-- Clipboard panel (slide-up) -->
  <div id="clip-panel" style="position:fixed;inset:0;z-index:210;background:rgba(0,0,0,.7);display:none;align-items:flex-end;justify-content:center;">
    <div style="background:#141417;border:1px solid #27272a;border-radius:18px 18px 0 0;padding:20px;width:100%;max-width:560px;max-height:70vh;display:flex;flex-direction:column;gap:12px;">
      <div style="display:flex;align-items:center;justify-content:space-between;">
        <span style="font-weight:700;color:#fff;font-size:15px;">📋 Pano Senkronu</span>
        <button onclick="ScreenViewer.closeClipboard()" style="background:none;border:none;color:#71717a;font-size:20px;cursor:pointer;padding:0 4px;">×</button>
      </div>
      <textarea id="clip-text" rows="6" placeholder="Panodaki metin burada gösterilir…"
        style="background:#09090b;border:1px solid #27272a;border-radius:10px;color:#e4e4e7;padding:10px;font-family:'JetBrains Mono',monospace;font-size:12px;resize:vertical;outline:none;width:100%;box-sizing:border-box;"></textarea>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button onclick="ScreenViewer.clipboardRead()" style="flex:1;min-width:120px;padding:8px;background:#27272a;color:#a1a1aa;border:none;border-radius:8px;font-size:12px;cursor:pointer;font-family:inherit;">⬇ Sunucudan Oku</button>
        <button onclick="ScreenViewer.clipboardWrite()" style="flex:1;min-width:120px;padding:8px;background:#2563eb;color:#fff;border:none;border-radius:8px;font-size:12px;cursor:pointer;font-family:inherit;">⬆ Sunucuya Gönder</button>
        <button onclick="navigator.clipboard?.readText().then(t=>{document.getElementById('clip-text').value=t}).catch(()=>{})"
          style="flex:1;min-width:120px;padding:8px;background:#16a34a;color:#fff;border:none;border-radius:8px;font-size:12px;cursor:pointer;font-family:inherit;">📱 Telefon Panosundan</button>
      </div>
    </div>
  </div>

  <!-- OCR result panel -->
  <div id="ocr-panel" style="position:fixed;inset:0;z-index:210;background:rgba(0,0,0,.7);display:none;align-items:flex-end;justify-content:center;">
    <div style="background:#141417;border:1px solid #27272a;border-radius:18px 18px 0 0;padding:20px;width:100%;max-width:560px;max-height:80vh;display:flex;flex-direction:column;gap:12px;">
      <div style="display:flex;align-items:center;justify-content:space-between;">
        <span style="font-weight:700;color:#fff;font-size:15px;">🔍 OCR — Ekrandan Metin</span>
        <button onclick="document.getElementById('ocr-panel').style.display='none'" style="background:none;border:none;color:#71717a;font-size:20px;cursor:pointer;">×</button>
      </div>
      <div id="ocr-status" style="color:#a1a1aa;font-size:12px;">Tesseract ile ekran taranıyor…</div>
      <textarea id="ocr-text" rows="10" readonly placeholder="OCR sonucu burada çıkar…"
        style="background:#09090b;border:1px solid #27272a;border-radius:10px;color:#e4e4e7;padding:10px;font-family:'JetBrains Mono',monospace;font-size:12px;resize:vertical;outline:none;width:100%;box-sizing:border-box;flex:1;"></textarea>
      <div style="display:flex;gap:8px;">
        <button onclick="navigator.clipboard?.writeText(document.getElementById('ocr-text').value).then(()=>showToast('Kopyalandı'))"
          style="flex:1;padding:8px;background:#27272a;color:#a1a1aa;border:none;border-radius:8px;font-size:12px;cursor:pointer;font-family:inherit;">📋 Kopyala</button>
        <button onclick="ScreenViewer.runOCR()"
          style="flex:1;padding:8px;background:#7c3aed;color:#fff;border:none;border-radius:8px;font-size:12px;cursor:pointer;font-family:inherit;">🔄 Yenile</button>
      </div>
    </div>
  </div>

  <!-- Share Link Modal -->
  <div id="share-modal" onclick="ShareLink.close()">
    <div id="share-box" onclick="event.stopPropagation()">
      <div class="font-bold text-white text-base mb-1">📤 Paylaşım Linki</div>
      <div id="share-filename" class="text-xs text-zinc-500 mb-3 font-mono truncate"></div>
      <div class="mb-3">
        <label class="text-xs text-zinc-500 font-semibold mb-1 block">SÜRE</label>
        <select id="share-hours" class="w-full bg-zinc-800 border border-zinc-700 text-white rounded-lg px-2 py-1.5 text-sm">
          <option value="1">1 Saat</option>
          <option value="6">6 Saat</option>
          <option value="24">24 Saat</option>
          <option value="72">3 Gün</option>
          <option value="168">1 Hafta</option>
        </select>
      </div>
      <div id="share-link-box" class="hidden mb-3">
        <label class="text-xs text-zinc-500 font-semibold mb-1 block">LINK</label>
        <div class="flex gap-2">
          <input id="share-link-inp" type="text" readonly
            class="flex-1 bg-zinc-900 border border-zinc-700 text-white rounded-lg px-3 py-1.5 text-xs font-mono outline-none min-w-0">
          <button onclick="ShareLink.copy()" class="px-3 py-1.5 bg-zinc-700 hover:bg-zinc-600 rounded-lg text-white text-xs font-semibold">Kopyala</button>
        </div>
      </div>
      <div class="flex gap-2">
        <button onclick="ShareLink.close()" class="flex-1 py-2 text-sm bg-zinc-800 rounded-xl text-zinc-400 font-semibold">İptal</button>
        <button onclick="ShareLink.generate()" class="flex-1 py-2 text-sm bg-primary rounded-xl text-white font-semibold">Link Oluştur</button>
      </div>
    </div>
  </div>

  <!-- ── MOBILE BOTTOM NAV ──────────────────────────────────────── -->
  <nav class="md:hidden shrink-0 z-20" style="
      background:#0d0d10;
      border-top:1px solid #1e1e23;
      display:flex; align-items:stretch;
      height:60px;
      padding-bottom:env(safe-area-inset-bottom,0px);
      box-shadow:0 -1px 0 #27272a,0 -8px 24px rgba(0,0,0,.35);
  ">
    <!-- Terminal -->
    <button id="nav-terminals" onclick="switchMobileTab('terminals')"
      style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
             border:none;background:none;cursor:pointer;transition:all .15s;position:relative;color:#60a5fa;">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
      <span style="font-size:9.5px;font-weight:600;letter-spacing:.01em;">Terminal</span>
      <span class="nav-indicator" style="position:absolute;bottom:0;left:50%;transform:translateX(-50%);width:28px;height:2.5px;border-radius:2px;background:#3b82f6;"></span>
    </button>
    <!-- Dosyalar -->
    <button id="nav-files" onclick="switchMobileTab('files')"
      style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
             border:none;background:none;cursor:pointer;transition:all .15s;position:relative;color:#52525b;">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
      <span style="font-size:9.5px;font-weight:600;letter-spacing:.01em;">Dosyalar</span>
    </button>
    <!-- Cihazlar -->
    <button onclick="Devices.open()"
      style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
             border:none;background:none;cursor:pointer;transition:all .15s;position:relative;color:#52525b;">
      <div style="position:relative;display:inline-block;">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/></svg>
        <span id="mob-dev-dot" style="position:absolute;top:-1px;right:-1px;width:8px;height:8px;border-radius:50%;background:#3b82f6;border:2px solid #0d0d10;box-shadow:0 0 6px #3b82f6aa;"></span>
      </div>
      <span id="mob-device-label" style="font-size:9.5px;font-weight:600;letter-spacing:.01em;">Cihazlar</span>
    </button>
    <!-- Araçlar -->
    <button id="nav-tools" onclick="switchMobileTab('tools')"
      style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
             border:none;background:none;cursor:pointer;transition:all .15s;position:relative;color:#52525b;">
      <div style="position:relative;">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>
        <span id="notif-nav-badge" class="notif-badge"></span>
      </div>
      <span style="font-size:9.5px;font-weight:600;letter-spacing:.01em;">Araçlar</span>
    </button>
    <!-- Ayarlar -->
    <button id="nav-settings" onclick="switchMobileTab('settings')"
      style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
             border:none;background:none;cursor:pointer;transition:all .15s;position:relative;color:#52525b;">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      <span style="font-size:9.5px;font-weight:600;letter-spacing:.01em;">Ayarlar</span>
    </button>
  </nav>

</div><!-- /app -->

<!-- ═══════════════════════════════════════════════════════════════
     FAVORITES PANEL
══════════════════════════════════════════════════════════════════ -->
<div id="favs-backdrop" onclick="Favs.close()"></div>
<div id="favs-panel">
  <div style="padding:14px 16px 10px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #27272a;flex-shrink:0;">
    <span style="font-size:14px;font-weight:700;color:white;">★ Favori Komutlar</span>
    <div style="display:flex;gap:8px;align-items:center;">
      <button onclick="Favs.showAdd()" style="font-size:11px;padding:5px 12px;border:1px solid rgba(59,130,246,.5);border-radius:7px;color:#60a5fa;background:rgba(59,130,246,.12);font-weight:600;">+ Ekle</button>
      <button onclick="Favs.close()" style="width:28px;height:28px;border-radius:50%;background:rgba(255,255,255,.06);color:#a1a1aa;font-size:18px;line-height:1;display:flex;align-items:center;justify-content:center;">×</button>
    </div>
  </div>
  <div style="padding:10px 12px;overflow-y:auto;flex:1;">
    <div id="favs-add-form" style="display:none;padding:10px;background:#18181b;border-radius:10px;margin-bottom:10px;flex-direction:column;gap:7px;">
      <input id="favs-inp-name" type="text" placeholder="Kısa ad (ör: git durum)"
        style="width:100%;box-sizing:border-box;background:#0a0a0c;border:1px solid #3f3f46;border-radius:7px;padding:7px 10px;font-size:12px;color:white;outline:none;"
        onfocus="this.style.borderColor='#3b82f6'" onblur="this.style.borderColor='#3f3f46'">
      <input id="favs-inp-cmd" type="text" placeholder="Komut (ör: git status)"
        style="width:100%;box-sizing:border-box;background:#0a0a0c;border:1px solid #3f3f46;border-radius:7px;padding:7px 10px;font-size:12px;color:white;outline:none;font-family:'JetBrains Mono',monospace;"
        onfocus="this.style.borderColor='#3b82f6'" onblur="this.style.borderColor='#3f3f46'">
      <div style="display:flex;gap:7px;justify-content:flex-end;">
        <button onclick="Favs.cancelAdd()" style="font-size:12px;padding:5px 12px;border:1px solid #3f3f46;border-radius:7px;color:#71717a;">İptal</button>
        <button onclick="Favs.saveAdd()" style="font-size:12px;padding:5px 14px;border:1px solid rgba(34,197,94,.4);border-radius:7px;color:#4ade80;background:rgba(34,197,94,.1);font-weight:600;">Kaydet</button>
      </div>
    </div>
    <div id="favs-list"></div>
    <div id="favs-empty" style="text-align:center;padding:36px 0;color:#52525b;font-size:12px;line-height:1.8;">
      Henüz favori yok.<br><span style="color:#3f3f46;">+ Ekle butonuna basın.</span>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════════
     D-PAD OVERLAY
══════════════════════════════════════════════════════════════════ -->
<div id="dpad-overlay">
  <div class="dpad-row">
    <div class="dpad-btn" ontouchstart="dpadPress('\x1b[A')" onmousedown="dpadPress('\x1b[A')" title="Yukarı">▲</div>
  </div>
  <div class="dpad-row">
    <div class="dpad-btn" ontouchstart="dpadPress('\x1b[D')" onmousedown="dpadPress('\x1b[D')" title="Sol">◀</div>
    <div class="dpad-btn dpad-center" ontouchstart="dpadPress('\r')" onmousedown="dpadPress('\r')" title="Enter">↵</div>
    <div class="dpad-btn" ontouchstart="dpadPress('\x1b[C')" onmousedown="dpadPress('\x1b[C')" title="Sağ">▶</div>
  </div>
  <div class="dpad-row">
    <div class="dpad-btn" ontouchstart="dpadPress('\x1b[B')" onmousedown="dpadPress('\x1b[B')" title="Aşağı">▼</div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════════
     SNIPPETS PANEL (slide-up sheet)
══════════════════════════════════════════════════════════════════ -->
<div id="snippets-panel">
  <div class="flex items-center justify-between px-4 py-2.5 border-b border-border shrink-0">
    <span class="font-bold text-sm text-white">⚡ Hızlı Komutlar</span>
    <button onclick="closeSnippets()" class="text-zinc-400 hover:text-white">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  </div>
  <div class="flex-1 overflow-y-auto p-3 scrollbar-thin" id="snippets-body"></div>
</div>
<div id="snippets-backdrop" class="fixed inset-0 bg-black/50 z-[140] hidden" onclick="closeSnippets()"></div>

<!-- ═══════════════════════════════════════════════════════════════
     CONTEXT MENU
══════════════════════════════════════════════════════════════════ -->
<div id="ctx-menu" class="fixed bg-panel border border-borderBright rounded-xl p-1.5 min-w-[200px] shadow-2xl z-50">
  <div id="ctx-items" class="flex flex-col"></div>
</div>

<!-- ═══════════════════════════════════════════════════════════════
     GENERIC MODAL
══════════════════════════════════════════════════════════════════ -->
<div id="modal-overlay" class="fixed inset-0 bg-black/70 backdrop-blur-sm z-[100] items-center justify-center hidden opacity-0 transition-opacity duration-200">
  <div class="bg-panel border border-borderBright rounded-2xl p-6 w-[90%] max-w-[360px] shadow-2xl transform scale-95 transition-transform duration-200" id="modal-card">
    <h3 id="modal-title" class="text-base font-bold text-white mb-2 tracking-tight">Başlık</h3>
    <div id="modal-body" class="mb-5"></div>
    <div class="flex items-center justify-end gap-3" id="modal-footer">
      <button class="px-4 py-2 rounded-lg text-sm font-semibold bg-background border border-border text-textMuted hover:text-white transition-colors" onclick="closeModal()">İptal</button>
      <button id="modal-confirm" class="px-4 py-2 rounded-lg text-sm font-semibold bg-primary text-white hover:bg-blue-600 transition-colors">Onayla</button>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════════
     IMAGE LIGHTBOX
══════════════════════════════════════════════════════════════════ -->
<div id="img-lightbox">
  <div class="flex items-center justify-between w-full px-4 py-2 shrink-0">
    <span id="img-lightbox-name" class="text-zinc-300 text-sm font-mono truncate max-w-[80%]"></span>
    <div class="flex items-center gap-2">
      <a id="img-lightbox-dl" class="px-3 py-1.5 rounded-lg border border-zinc-700 text-zinc-400 hover:text-white text-xs font-semibold transition-colors" title="İndir">⬇ İndir</a>
      <button onclick="closeImgLightbox()" class="p-1.5 rounded-lg border border-zinc-700 text-zinc-400 hover:text-white transition-colors" title="Kapat">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
  </div>
  <div class="flex-1 flex items-center justify-center px-4 pb-4 overflow-hidden" onclick="closeImgLightbox()">
    <img id="img-lightbox-img" src="" alt="" onclick="event.stopPropagation()">
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════════
     TEXT PREVIEW OVERLAY
══════════════════════════════════════════════════════════════════ -->
<div id="txt-preview" onclick="closeTxtPreview()">
  <div id="txt-preview-card" onclick="event.stopPropagation()">
    <div class="flex items-center justify-between px-4 py-3 border-b border-zinc-800 shrink-0">
      <div>
        <span id="txt-preview-name" class="text-white text-sm font-semibold font-mono"></span>
        <span id="txt-preview-size" class="ml-2 text-zinc-500 text-xs"></span>
      </div>
      <div class="flex items-center gap-2">
        <button id="txt-preview-edit-btn" class="px-3 py-1 rounded-lg bg-primary/10 border border-primary/30 text-primary text-xs font-semibold hover:bg-primary hover:text-white transition-colors">Editörde Aç</button>
        <button onclick="closeTxtPreview()" class="p-1.5 rounded-lg border border-zinc-700 text-zinc-400 hover:text-white transition-colors">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
    </div>
    <pre id="txt-preview-content" class="scrollbar-thin"></pre>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════════
     TEXT EDITOR MODAL (full-screen)
══════════════════════════════════════════════════════════════════ -->
<div id="editor-overlay" class="fixed inset-0 bg-[#09090b] z-[110] flex flex-col hidden">
  <div class="flex items-center justify-between px-4 h-12 border-b border-border bg-panel shrink-0">
    <div class="flex items-center gap-3">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#60a5fa" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
      <span id="editor-filename" class="font-mono text-xs text-zinc-200 font-semibold truncate max-w-[200px]"></span>
      <span id="editor-modified" class="text-[10px] text-amber-400 hidden">● değiştirildi</span>
    </div>
    <div class="flex items-center gap-2">
      <button id="editor-save-btn" onclick="editorSave()" class="px-3 py-1 rounded-md bg-primary text-white text-xs font-semibold hover:bg-blue-600 transition-colors">Kaydet</button>
      <button onclick="editorClose()" class="px-3 py-1 rounded-md bg-background border border-border text-textMuted text-xs font-semibold hover:text-white transition-colors">Kapat</button>
    </div>
  </div>
  <div id="editor-cm-wrap" class="flex-1 overflow-hidden cm-editor-wrap"></div>
  <div class="flex items-center gap-4 px-4 py-1.5 border-t border-border bg-panel text-[10px] font-mono text-zinc-500 shrink-0">
    <span id="editor-status">Hazır</span>
    <span id="editor-lang"></span>
    <span id="editor-cursor-pos">Satır 1, Sütun 1</span>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════════
     TOAST
══════════════════════════════════════════════════════════════════ -->
<div id="toast-container" class="fixed bottom-16 md:bottom-6 right-1/2 translate-x-1/2 md:translate-x-0 md:right-6 z-[300] flex flex-col gap-2 pointer-events-none items-center md:items-end"></div>

<!-- ═══════════════════════════════════════════════════════════════
     JAVASCRIPT
══════════════════════════════════════════════════════════════════ -->
<script>
'use strict';

/* ── SVG icon set ────────────────────────────────────────────────── */
const SVGS = {
  close:    '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  plus:     '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
  terminal: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
  folder:   '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
  more:     '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg>',
  download: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
  trash:    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>',
  edit:     '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>',
  refresh:  '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>',
  upload:   '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>',
  list:     '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>',
  grid:     '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>',
  file:     '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
  zip:      '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>',
  img:      '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>',
  dpad:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="8 12 12 8 16 12"/><polyline points="8 12 12 16 16 12"/></svg>',
  cut:      '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="6" cy="20" r="2"/><circle cx="6" cy="4" r="2"/><line x1="6" y1="6" x2="6" y2="18"/><line x1="6" y1="10" x2="20" y2="3"/><line x1="6" y1="14" x2="20" y2="21"/></svg>',
  copy:     '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  paste:    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/></svg>',
  move:     '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="5 9 2 12 5 15"/><polyline points="9 5 12 2 15 5"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="12" y1="2" x2="12" y2="22"/></svg>',
  eye:      '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
  extract:  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/><rect x="9" y="2" width="6" height="4" rx="1"/></svg>',
};

/* ── File icon helper ────────────────────────────────────────────── */
function getFileIconExt(name, isDir) {
  if (isDir) return { cls: 'text-blue-400 bg-blue-500/15', svg: SVGS.folder };
  const ext = name.split('.').pop().toLowerCase();
  const map = {
    py: { cls:'text-yellow-400 bg-yellow-400/15', svg:SVGS.file },
    js: { cls:'text-yellow-400 bg-yellow-400/15', svg:SVGS.file },
    ts: { cls:'text-yellow-400 bg-yellow-400/15', svg:SVGS.file },
    sh: { cls:'text-emerald-400 bg-emerald-500/15', svg:SVGS.terminal },
    bash: { cls:'text-emerald-400 bg-emerald-500/15', svg:SVGS.terminal },
    zip: { cls:'text-purple-400 bg-purple-500/15', svg:SVGS.zip },
    tar: { cls:'text-purple-400 bg-purple-500/15', svg:SVGS.zip },
    gz:  { cls:'text-purple-400 bg-purple-500/15', svg:SVGS.zip },
    txt: { cls:'text-zinc-400 bg-zinc-500/15', svg:SVGS.file },
    md:  { cls:'text-zinc-400 bg-zinc-500/15', svg:SVGS.file },
    log: { cls:'text-zinc-400 bg-zinc-500/15', svg:SVGS.file },
    png: { cls:'text-rose-400 bg-rose-500/15', svg:SVGS.img },
    jpg: { cls:'text-rose-400 bg-rose-500/15', svg:SVGS.img },
    jpeg:{ cls:'text-rose-400 bg-rose-500/15', svg:SVGS.img },
    json:{ cls:'text-slate-400 bg-slate-500/15', svg:SVGS.file },
    yaml:{ cls:'text-slate-400 bg-slate-500/15', svg:SVGS.file },
    toml:{ cls:'text-slate-400 bg-slate-500/15', svg:SVGS.file },
    html:{ cls:'text-orange-400 bg-orange-500/15', svg:SVGS.file },
    css: { cls:'text-sky-400 bg-sky-500/15', svg:SVGS.file },
    env: { cls:'text-rose-400 bg-rose-500/15', svg:SVGS.file },
    conf:{ cls:'text-zinc-400 bg-zinc-500/15', svg:SVGS.file },
    sql: { cls:'text-teal-400 bg-teal-500/15', svg:SVGS.file },
  };
  return map[ext] || { cls:'text-zinc-400 bg-zinc-500/15', svg:SVGS.file };
}

/* ── App state ───────────────────────────────────────────────────── */
const state = {
  terminals:    [],
  fileManagers: [],
  activeTermId: null,
  activeFileId: null,
  ctrlActive:   false,
  altActive:    false,
  clipboard:    { item: null, action: null },  // action: 'cut' | 'copy'
};

/* ── Clipboard helpers ───────────────────────────────────────────── */
function clipboardSet(file, action) {
  state.clipboard = { item: file, action };
  updatePasteButtons();
  showToast(action === 'cut'
    ? `✂ Kesildi: ${file.name}`
    : `📋 Kopyalandı: ${file.name}`);
}

function updatePasteButtons() {
  const hasCb = !!state.clipboard.item;
  state.fileManagers.forEach(fm => {
    const btn = document.getElementById(`${fm.id}-paste-btn`);
    if (btn) btn.classList.toggle('hidden', !hasCb);
  });
}

/* ═══════════════════════════════════════════════════════════════════
   MODULE: Preview (Image lightbox + Text preview)
══════════════════════════════════════════════════════════════════ */
const IMAGE_EXTS = new Set(['png','jpg','jpeg','gif','webp','svg','bmp','ico','avif','tiff']);
const TEXT_EXTS  = new Set([
  'txt','md','log','json','yaml','yml','toml','ini','conf','cfg','env','gitignore',
  'py','js','ts','jsx','tsx','sh','bash','zsh','fish','html','css','xml','csv',
  'rs','go','java','c','cpp','h','hpp','rb','php','lua','r','sql','tf','dockerfile',
]);

function getPreviewType(name) {
  const ext = name.split('.').pop().toLowerCase();
  if (IMAGE_EXTS.has(ext)) return 'image';
  if (TEXT_EXTS.has(ext))  return 'text';
  return null;
}

function openImagePreview(path, name) {
  const lb  = document.getElementById('img-lightbox');
  const img = document.getElementById('img-lightbox-img');
  const nm  = document.getElementById('img-lightbox-name');
  const dl  = document.getElementById('img-lightbox-dl');
  nm.textContent  = name;
  img.src         = '/api/download?path=' + encodeURIComponent(path);
  dl.href         = '/api/download?path=' + encodeURIComponent(path);
  dl.download     = name;
  lb.classList.add('visible');
}

function closeImgLightbox() {
  const lb  = document.getElementById('img-lightbox');
  const img = document.getElementById('img-lightbox-img');
  lb.classList.remove('visible');
  setTimeout(() => { img.src = ''; }, 200);
}

async function openTextPreview(path, name, size) {
  const overlay = document.getElementById('txt-preview');
  const pre     = document.getElementById('txt-preview-content');
  const nm      = document.getElementById('txt-preview-name');
  const sz      = document.getElementById('txt-preview-size');
  const editBtn = document.getElementById('txt-preview-edit-btn');

  nm.textContent  = name;
  sz.textContent  = size ? formatSize(size) : '';
  pre.textContent = 'Yükleniyor…';
  editBtn.onclick = () => { closeTxtPreview(); Editor.open(path); };
  overlay.classList.add('visible');

  try {
    const data = await DevFS.readFile(path);
    if (data.success) {
      pre.textContent = data.binary ? '[İkili dosya — önizleme desteklenmiyor]' : data.content;
    } else {
      pre.textContent = '⚠ ' + (data.error || 'Okunamadı');
    }
  } catch(e) {
    pre.textContent = '⚠ Bağlantı hatası';
  }
}

function closeTxtPreview() {
  document.getElementById('txt-preview').classList.remove('visible');
  document.getElementById('txt-preview-content').textContent = '';
}

function formatSize(bytes) {
  if (bytes < 1024)        return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeImgLightbox(); closeTxtPreview(); }
});

async function extractArchive(file, fmId) {
  const fm = getFileManager(fmId);
  showToast('Çıkarılıyor…');
  const res  = await fetch('/api/extract', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: file.path }),
  });
  const data = await res.json();
  if (data.success) {
    showToast(`✅ Çıkarıldı → ${data.dest_dir.split('/').pop()}/`);
    fm.fetchDir();
  } else {
    showToast(data.error, 'error');
  }
}

async function clipboardPaste(fmId) {
  const fm  = getFileManager(fmId);
  const cb  = state.clipboard;
  if (!fm || !cb.item) return;

  const endpoint = cb.action === 'cut' ? '/api/move' : '/api/copy';
  const res  = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ src_path: cb.item.path, dest_dir: fm.path }),
  });
  const data = await res.json();
  if (data.success) {
    showToast(cb.action === 'cut' ? 'Taşındı' : 'Kopyalandı');
    if (cb.action === 'cut') {
      state.clipboard = { item: null, action: null };
      updatePasteButtons();
      state.fileManagers.forEach(f => f.fetchDir());
    } else {
      fm.fetchDir();
    }
  } else {
    showToast(data.error, 'error');
  }
}

/* ── Utility: ID generator ───────────────────────────────────────── */
function genId() { return Math.random().toString(36).slice(2, 9); }

/* ── Utility: Toast ──────────────────────────────────────────────── */
function showToast(msg, type = 'success') {
  const c = document.getElementById('toast-container');
  const t = document.createElement('div');
  const isErr = type === 'error';
  const isWarn = type === 'warn';
  const palette = isErr
    ? 'bg-red-500/10 border-red-500/30 text-red-400'
    : isWarn
      ? 'bg-amber-500/10 border-amber-500/30 text-amber-400'
      : 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400';
  const icon = isErr
    ? '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>'
    : isWarn
      ? '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>'
      : '<polyline points="20 6 9 17 4 12"/>';
  t.className = `toast-enter flex items-center gap-2.5 px-4 py-2.5 rounded-xl border shadow-xl backdrop-blur-md font-medium text-[12px] ${palette}`;
  t.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">${icon}</svg><span>${msg}</span>`;
  c.appendChild(t);
  requestAnimationFrame(() => { t.classList.remove('toast-enter'); t.classList.add('toast-enter-active'); });
  setTimeout(() => {
    t.classList.remove('toast-enter-active');
    t.classList.add('toast-exit-active');
    setTimeout(() => t.remove(), 220);
  }, 2600);
}

/* ── Layout switcher (mobile) ────────────────────────────────────── */
function switchMobileTab(tab) {
  const NAV_IDS = ['nav-terminals','nav-files','nav-tools','nav-settings'];
  const ACTIVE_COLOR  = '#60a5fa';  /* blue-400 */
  const INACTIVE_COLOR= '#52525b';  /* zinc-600 */

  // update nav button colours + bottom indicator
  NAV_IDS.forEach(id => {
    const btn = document.getElementById(id);
    if (!btn) return;
    const isActive = id === 'nav-' + tab;
    btn.style.color = isActive ? ACTIVE_COLOR : INACTIVE_COLOR;
    let ind = btn.querySelector('.nav-indicator');
    if (isActive) {
      if (!ind) {
        ind = document.createElement('span');
        ind.className = 'nav-indicator';
        ind.style.cssText = 'position:absolute;bottom:0;left:50%;transform:translateX(-50%);width:28px;height:2.5px;border-radius:2px;background:#3b82f6;';
        btn.appendChild(ind);
      }
    } else {
      if (ind) ind.remove();
    }
  });

  const t  = document.getElementById('sec-terminals');
  const f  = document.getElementById('sec-files');
  const s  = document.getElementById('sec-settings');
  const to = document.getElementById('sec-tools');
  if (window.innerWidth >= 768) return;

  [t, f, to].forEach(el => el.classList.add('mobile-hidden'));
  s.classList.add('mobile-hidden', 'hidden');

  if (tab === 'terminals') {
    t.classList.remove('mobile-hidden');
    requestAnimationFrame(() => { const at = getTerminal(state.activeTermId); if (at) { try { at.fitAddon.fit(); at.sendResize(); } catch(e){} } });
    setDpadVisible(AppSettings.dpadDefault());
  } else {
    setDpadVisible(false);
  }
  if (tab === 'files')    f.classList.remove('mobile-hidden');
  if (tab === 'settings') { s.classList.remove('mobile-hidden', 'hidden'); refreshSettingsStats(); }
  if (tab === 'tools')    to.classList.remove('mobile-hidden');
}

/* ═══════════════════════════════════════════════════════════════════
   MODULE: Dashboard (CPU / RAM / Tailscale polling)
══════════════════════════════════════════════════════════════════ */
const Dashboard = (() => {
  let interval = null;

  function fmtBytes(mb) {
    return mb >= 1024 ? (mb/1024).toFixed(1)+'GB' : mb+'MB';
  }

  async function refresh() {
    try {
      const [sysRes, tsRes] = await Promise.all([
        fetch('/api/system_stats'),
        fetch('/api/tailscale_status'),
      ]);
      const sys = await sysRes.json();
      const ts  = await tsRes.json();

      // CPU
      const cpu = sys.cpu_percent.toFixed(0);
      document.getElementById('dash-cpu').textContent = `CPU ${cpu}%`;
      document.getElementById('dash-cpu-bar').style.width = cpu + '%';
      document.getElementById('dash-cpu-bar').className =
        'dash-bar-fill transition-all ' + (cpu > 80 ? 'bg-red-500' : cpu > 50 ? 'bg-amber-500' : 'bg-blue-500');

      // RAM
      const ram = sys.ram_percent.toFixed(0);
      document.getElementById('dash-ram').textContent =
        `RAM ${fmtBytes(sys.ram_used_mb)}/${fmtBytes(sys.ram_total_mb)}`;
      document.getElementById('dash-ram-bar').style.width = ram + '%';
      document.getElementById('dash-ram-bar').className =
        'dash-bar-fill transition-all ' + (ram > 80 ? 'bg-red-500' : ram > 60 ? 'bg-amber-500' : 'bg-lime-500');

      // Disk
      const disk = sys.disk_percent.toFixed(0);
      document.getElementById('dash-disk').textContent =
        `Disk ${sys.disk_used_gb}/${sys.disk_total_gb}GB`;
      document.getElementById('dash-disk-bar').style.width = disk + '%';
      document.getElementById('dash-disk-bar').className =
        'dash-bar-fill transition-all ' + (disk > 85 ? 'bg-red-500' : disk > 65 ? 'bg-amber-500' : 'bg-amber-500');

      // Notification threshold check
      Notifs.check(sys);

      // Tailscale
      const dot   = document.getElementById('dash-ts-dot');
      const label = document.getElementById('dash-ts-label');
      if (ts.connected) {
        dot.className   = 'w-1.5 h-1.5 rounded-full bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,.7)]';
        label.textContent = `TS ${ts.ip}`;
        label.className   = 'text-emerald-400';
      } else {
        dot.className   = 'w-1.5 h-1.5 rounded-full bg-zinc-600';
        label.textContent = `TS ${ts.backend_state}`;
        label.className   = 'text-zinc-500';
      }

      // Settings panel live stats
      const ss = document.getElementById('settings-stats');
      if (ss) ss.innerHTML = `
        <div>CPU:</div><div class="text-zinc-200">${sys.cpu_percent.toFixed(1)}%</div>
        <div>RAM:</div><div class="text-zinc-200">${fmtBytes(sys.ram_used_mb)} / ${fmtBytes(sys.ram_total_mb)} (${sys.ram_percent.toFixed(0)}%)</div>
        <div>Disk:</div><div class="text-zinc-200">${sys.disk_used_gb}GB / ${sys.disk_total_gb}GB (${sys.disk_percent.toFixed(0)}%)</div>
        <div>TS IP:</div><div class="text-zinc-200">${ts.ip}</div>
        <div>TS Durum:</div><div class="${ts.connected ? 'text-emerald-400':'text-zinc-500'}">${ts.backend_state}</div>
        <div>Peer:</div><div class="text-zinc-200">${ts.peer_count}</div>
      `;
    } catch (_) { /* silently ignore network errors */ }
  }

  function start() {
    refresh();
    interval = setInterval(refresh, 4000);
  }

  return { start };
})();

function refreshSettingsStats() { /* triggers next dashboard refresh cycle */ }

/* ═══════════════════════════════════════════════════════════════════
   MODULE: D-Pad
══════════════════════════════════════════════════════════════════ */
function setDpadVisible(v) {
  document.getElementById('dpad-overlay').classList.toggle('visible', v);
}

function dpadPress(seq) {
  const t = getTerminal(state.activeTermId);
  if (t) t.sendKey(seq);
}

// Make D-Pad draggable
(function initDpadDrag() {
  const el = document.getElementById('dpad-overlay');
  let sx = 0, sy = 0, ox = 0, oy = 0, dragging = false;

  function onStart(e) {
    const src = e.target.closest('.dpad-btn');
    if (src) return;   // don't drag when pressing a button
    dragging = true;
    const pt = e.touches ? e.touches[0] : e;
    sx = pt.clientX; sy = pt.clientY;
    const rect = el.getBoundingClientRect();
    ox = rect.left; oy = rect.top;
    e.preventDefault();
  }
  function onMove(e) {
    if (!dragging) return;
    const pt = e.touches ? e.touches[0] : e;
    const dx = pt.clientX - sx; const dy = pt.clientY - sy;
    const newX = Math.max(0, Math.min(window.innerWidth  - el.offsetWidth,  ox + dx));
    const newY = Math.max(0, Math.min(window.innerHeight - el.offsetHeight, oy + dy));
    el.style.right = 'auto'; el.style.bottom = 'auto';
    el.style.left  = newX + 'px'; el.style.top = newY + 'px';
    e.preventDefault();
  }
  function onEnd() { dragging = false; }

  el.addEventListener('touchstart', onStart, { passive: false });
  el.addEventListener('touchmove',  onMove,  { passive: false });
  el.addEventListener('touchend',   onEnd);
  el.addEventListener('mousedown',  onStart);
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup',   onEnd);
})();

/* ═══════════════════════════════════════════════════════════════════
   MODULE: App Settings (D-Pad default, etc.)
══════════════════════════════════════════════════════════════════ */
const AppSettings = (() => {
  const K_DPAD = 'lphone_dpad_open';
  function dpadDefault() { return localStorage.getItem(K_DPAD) === '1'; }
  function toggleDpad() {
    const next = !dpadDefault();
    localStorage.setItem(K_DPAD, next ? '1' : '0');
    _applyDpadBtn();
  }
  function _applyDpadBtn() {
    const on  = dpadDefault();
    const btn  = document.getElementById('dpad-setting-btn');
    const knob = document.getElementById('dpad-setting-knob');
    if (!btn) return;
    btn.className  = `w-11 h-6 rounded-full transition-colors relative flex items-center px-0.5 shrink-0 ${on ? 'bg-blue-600' : 'bg-zinc-700'}`;
    knob.style.transform = on ? 'translateX(20px)' : 'translateX(0)';
  }
  function init() { _applyDpadBtn(); }
  return { dpadDefault, toggleDpad, init };
})();

/* ═══════════════════════════════════════════════════════════════════
   MODULE: Favorites (★ komutlar)
══════════════════════════════════════════════════════════════════ */
const Favs = (() => {
  const LS = 'lphone_favs';
  function load() { try { return JSON.parse(localStorage.getItem(LS)) || []; } catch(_) { return []; } }
  function save(l) { localStorage.setItem(LS, JSON.stringify(l)); }
  function _esc(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

  function render() {
    const list  = load();
    const el    = document.getElementById('favs-list');
    const empty = document.getElementById('favs-empty');
    if (!el) return;
    if (list.length === 0) { el.innerHTML = ''; empty.style.display = 'block'; return; }
    empty.style.display = 'none';
    el.innerHTML = list.map((f, i) => `
      <div class="fav-item" onclick="Favs.run(${i})">
        <div style="flex:1;min-width:0;">
          <div style="font-size:13px;font-weight:600;color:#e4e4e7;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${_esc(f.name)}</div>
          <div style="font-size:11px;color:#71717a;font-family:'JetBrains Mono',monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${_esc(f.cmd)}</div>
        </div>
        <button onclick="event.stopPropagation();Favs.del(${i})"
          style="padding:4px 9px;color:#52525b;font-size:18px;flex-shrink:0;line-height:1;border-radius:6px;"
          onmouseenter="this.style.color='#ef4444'" onmouseleave="this.style.color='#52525b'">×</button>
      </div>
    `).join('');
  }

  function open() {
    render();
    document.getElementById('favs-panel').classList.add('open');
    document.getElementById('favs-backdrop').classList.add('open');
  }
  function close() {
    document.getElementById('favs-panel').classList.remove('open');
    document.getElementById('favs-backdrop').classList.remove('open');
    cancelAdd();
  }
  function showAdd() {
    const f = document.getElementById('favs-add-form');
    f.style.display = 'flex';
    setTimeout(() => document.getElementById('favs-inp-cmd').focus(), 50);
  }
  function cancelAdd() {
    const f = document.getElementById('favs-add-form');
    if (f) f.style.display = 'none';
    const n = document.getElementById('favs-inp-name');
    const c = document.getElementById('favs-inp-cmd');
    if (n) n.value = ''; if (c) c.value = '';
  }
  function saveAdd() {
    const name = (document.getElementById('favs-inp-name')?.value || '').trim();
    const cmd  = (document.getElementById('favs-inp-cmd')?.value  || '').trim();
    if (!cmd) {
      const ci = document.getElementById('favs-inp-cmd');
      if (ci) { ci.style.borderColor = '#ef4444'; ci.focus(); }
      return;
    }
    const l = load(); l.push({ name: name || cmd, cmd }); save(l);
    cancelAdd(); render();
    showToast('★ Favori eklendi');
  }
  function run(i) {
    const f = load()[i]; if (!f) return;
    const t = getTerminal(state.activeTermId);
    if (t) t.sendCmd(f.cmd);
    close();
    if (window.innerWidth < 768) switchMobileTab('terminals');
  }
  function del(i) {
    const l = load(); l.splice(i, 1); save(l); render();
  }
  return { open, close, showAdd, cancelAdd, saveAdd, run, del };
})();

/* ═══════════════════════════════════════════════════════════════════
   MODULE: Snippets panel
══════════════════════════════════════════════════════════════════ */
const SNIPPETS = [
  { cat:'Dosya & Dizin', items:[
    { label:'ls -la',     cmd:'ls -la' },
    { label:'pwd',        cmd:'pwd' },
    { label:'df -h',      cmd:'df -h' },
    { label:'du -sh *',   cmd:'du -sh *' },
    { label:'find . -name "*.py"', cmd:'find . -name "*.py"' },
  ]},
  { cat:'Git', items:[
    { label:'git status', cmd:'git status' },
    { label:'git log',    cmd:'git log --oneline -15' },
    { label:'git diff',   cmd:'git diff' },
    { label:'git pull',   cmd:'git pull' },
    { label:'git push',   cmd:'git push' },
    { label:'git add -A', cmd:'git add -A' },
    { label:'git commit', cmd:'git commit -m ""' },
  ]},
  { cat:'Süreç & Sistem', items:[
    { label:'ps aux',     cmd:'ps aux --sort=-%cpu | head -20' },
    { label:'top',        cmd:'top' },
    { label:'htop',       cmd:'htop' },
    { label:'free -h',    cmd:'free -h' },
    { label:'uname -a',   cmd:'uname -a' },
    { label:'uptime',     cmd:'uptime' },
    { label:'who',        cmd:'who' },
  ]},
  { cat:'Ağ', items:[
    { label:'ip a',           cmd:'ip a' },
    { label:'netstat',        cmd:'netstat -tulpn 2>/dev/null | head -20' },
    { label:'ping 8.8.8.8',   cmd:'ping -c 4 8.8.8.8' },
    { label:'curl ifconfig.me', cmd:'curl ifconfig.me' },
    { label:'ss -tulpn',      cmd:'ss -tulpn' },
    { label:'tailscale status', cmd:'tailscale status' },
  ]},
  { cat:'Python', items:[
    { label:'python3 --version', cmd:'python3 --version' },
    { label:'pip list',    cmd:'pip list' },
    { label:'pip install', cmd:'pip install ' },
    { label:'venv create', cmd:'python3 -m venv .venv && source .venv/bin/activate' },
    { label:'pytest',      cmd:'pytest -v' },
  ]},
  { cat:'Docker', items:[
    { label:'docker ps',     cmd:'docker ps' },
    { label:'docker images', cmd:'docker images' },
    { label:'docker logs',   cmd:'docker logs ' },
    { label:'docker compose up', cmd:'docker compose up -d' },
    { label:'docker compose down', cmd:'docker compose down' },
  ]},
];

function buildSnippetsUI() {
  const body = document.getElementById('snippets-body');
  body.innerHTML = '';
  SNIPPETS.forEach(({ cat, items }) => {
    const sec = document.createElement('div');
    sec.className = 'mb-4';
    sec.innerHTML = `<div class="text-[9px] font-bold text-zinc-500 uppercase tracking-widest mb-2 px-1">${cat}</div>`;
    const grid = document.createElement('div');
    grid.className = 'grid grid-cols-2 gap-1.5';
    items.forEach(({ label, cmd }) => {
      const btn = document.createElement('button');
      btn.className = 'text-left px-3 py-2 bg-background border border-border rounded-lg text-[11px] font-mono text-zinc-300 hover:text-white hover:border-borderBright active:scale-95 transition-all truncate';
      btn.textContent = label;
      btn.onclick = () => { runSnippet(cmd); closeSnippets(); };
      grid.appendChild(btn);
    });
    sec.appendChild(grid);
    body.appendChild(sec);
  });
}

function runSnippet(cmd) {
  const t = getTerminal(state.activeTermId);
  if (t) t.sendCmd(cmd);
}

function toggleSnippets() {
  const panel = document.getElementById('snippets-panel');
  const backdrop = document.getElementById('snippets-backdrop');
  if (panel.classList.contains('open')) {
    closeSnippets();
  } else {
    panel.classList.add('open');
    backdrop.classList.remove('hidden');
    panel.style.zIndex = 150;
  }
}

function closeSnippets() {
  document.getElementById('snippets-panel').classList.remove('open');
  document.getElementById('snippets-backdrop').classList.add('hidden');
}

/* ═══════════════════════════════════════════════════════════════════
   MODULE: Text Editor (CodeMirror 5)
══════════════════════════════════════════════════════════════════ */
const Editor = (() => {
  let cm = null;
  let currentPath = null;
  let modified = false;

  const EXT_MODES = {
    py:'python', js:'javascript', ts:'javascript', jsx:'javascript', tsx:'javascript',
    sh:'shell', bash:'shell', zsh:'shell',
    yaml:'yaml', yml:'yaml', json:'javascript',
    md:'markdown', html:'htmlmixed', htm:'htmlmixed',
    css:'css', xml:'xml', toml:'toml',
  };

  function getModeForPath(path) {
    const ext = path.split('.').pop().toLowerCase();
    return EXT_MODES[ext] || null;
  }

  function init() {
    if (cm) return;
    const wrap = document.getElementById('editor-cm-wrap');
    const textarea = document.createElement('textarea');
    wrap.appendChild(textarea);
    cm = CodeMirror.fromTextArea(textarea, {
      theme:          'dracula',
      lineNumbers:    true,
      matchBrackets:  true,
      autoCloseBrackets: true,
      styleActiveLine: true,
      indentUnit:     4,
      tabSize:        4,
      indentWithTabs: false,
      lineWrapping:   false,
      extraKeys: {
        'Ctrl-S': () => save(),
        'Cmd-S':  () => save(),
        Tab: (cm) => {
          if (cm.somethingSelected()) cm.indentSelection('add');
          else cm.replaceSelection('    ', 'end');
        }
      }
    });
    cm.on('change', () => {
      if (!modified) {
        modified = true;
        document.getElementById('editor-modified').classList.remove('hidden');
      }
    });
    cm.on('cursorActivity', () => {
      const cur = cm.getCursor();
      document.getElementById('editor-cursor-pos').textContent =
        `Satır ${cur.line + 1}, Sütun ${cur.ch + 1}`;
    });
  }

  async function open(path) {
    try {
      // DevFS üzerinden oku — aktif cihaza (local/SSH) göre doğru API seçilir
      const data = await DevFS.readFile(path);
      if (!data.success) { showToast(data.error || 'Okunamadı', 'error'); return; }
      if (data.binary)   { showToast('İkili dosya editörde açılamaz', 'error'); return; }

      init();
      currentPath = path;
      modified    = false;
      document.getElementById('editor-modified').classList.add('hidden');
      document.getElementById('editor-filename').textContent = path.split('/').pop();
      document.getElementById('editor-overlay').classList.remove('hidden');

      const name = path.split('/').pop();
      const mode = getModeForPath(name);
      cm.setValue(data.content);
      cm.setOption('mode', mode || 'text');
      document.getElementById('editor-lang').textContent = mode || 'plain text';
      // Uzak cihazda açılıyorsa bilgi göster
      try {
        const devId = Devices.activeId();
        if (devId !== 'local') {
          const dev = Devices.getDevById(devId);
          document.getElementById('editor-status').textContent = dev ? `${dev.name} (SSH)` : 'SSH';
        } else {
          document.getElementById('editor-status').textContent = 'Hazır';
        }
      } catch(_) { document.getElementById('editor-status').textContent = 'Hazır'; }

      setTimeout(() => { cm.refresh(); cm.focus(); }, 50);
    } catch (e) { showToast('Dosya okunamadı: ' + e.message, 'error'); }
  }

  async function save() {
    if (!currentPath || !cm) return;
    document.getElementById('editor-status').textContent = 'Kaydediliyor...';
    try {
      // DevFS üzerinden yaz — aktif cihaza göre doğru API
      const data = await DevFS.writeFile(currentPath, cm.getValue());
      if (data.success) {
        modified = false;
        document.getElementById('editor-modified').classList.add('hidden');
        document.getElementById('editor-status').textContent = 'Kaydedildi ✓';
        showToast('Kaydedildi');
      } else {
        showToast(data.error, 'error');
        document.getElementById('editor-status').textContent = 'Hata!';
      }
    } catch(e) { showToast('Kayıt hatası: ' + e.message, 'error'); }
  }

  function close() {
    if (modified) {
      if (!confirm('Kaydedilmemiş değişiklikler var. Çıkmak istiyor musunuz?')) return;
    }
    document.getElementById('editor-overlay').classList.add('hidden');
    currentPath = null; modified = false;
    document.getElementById('editor-modified').classList.add('hidden');
  }

  return { open, close, save };
})();

function editorSave()  { Editor.save(); }
function editorClose() { Editor.close(); }

/* ═══════════════════════════════════════════════════════════════════
   MODULE: Terminal
══════════════════════════════════════════════════════════════════ */
class TerminalInstance {
  constructor() {
    this.id = 'term_' + genId();
    this.ws = null;
    this.connected = false;
    this.extraBarOpen = false;
    this.renderDOM();
    this.initXterm();
    this.connect();
    this.initKeyboardResize();
    this.initHorizontalScroll();
  }

  /* Build the DOM for this terminal tab + view */
  renderDOM() {
    // Tab
    this.tabEl = document.createElement('div');
    this.tabEl.className = 'tab-btn flex items-center gap-1.5 px-3 h-full rounded-t-lg text-[12px] font-semibold cursor-pointer shrink-0';
    this.tabEl.innerHTML = `
      <span class="text-primary">${SVGS.terminal}</span>
      <span class="max-w-[80px] truncate" id="${this.id}-title">Terminal</span>
      <button class="ml-1 p-0.5 rounded hover:bg-zinc-700/50 text-zinc-500 hover:text-white transition-colors"
              onclick="event.stopPropagation(); closeTerminal('${this.id}')">${SVGS.close}</button>
    `;
    this.tabEl.onclick = () => activateTerminal(this.id);
    document.getElementById('terminal-tabs-container').appendChild(this.tabEl);

    // View
    this.viewEl = document.createElement('div');
    this.viewEl.className = 'absolute inset-0 flex hidden';
    this.viewEl.innerHTML = `
      <!-- Main terminal column -->
      <div class="flex-1 flex flex-col relative overflow-hidden">

        <!-- Status bar -->
        <div class="h-8 border-b border-zinc-800/70 bg-[#0c0c0e] flex justify-between items-center px-3 shrink-0">
          <div class="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wider">
            <span class="w-1.5 h-1.5 rounded-full bg-red-500" id="${this.id}-dot"></span>
            <span class="text-zinc-400" id="${this.id}-status">Bağlanıyor...</span>
          </div>
          <div class="flex items-center gap-1">
            <!-- Snippets toggle (mobile) -->
            <button class="md:hidden p-1 rounded text-zinc-500 hover:text-amber-400 hover:bg-zinc-800 transition-colors"
                    onclick="toggleSnippets()" title="Snippets">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
            </button>
            <!-- Favorites toggle (mobile) -->
            <button class="md:hidden p-1 rounded text-zinc-500 hover:text-amber-400 hover:bg-zinc-800 transition-colors"
                    onclick="Favs.open()" title="Favori Komutlar">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
            </button>
            <!-- D-Pad toggle (mobile) -->
            <button class="md:hidden p-1 rounded text-zinc-500 hover:text-blue-400 hover:bg-zinc-800 transition-colors"
                    id="${this.id}-dpad-btn" onclick="toggleDpad('${this.id}')" title="D-Pad">${SVGS.dpad}</button>
            <!-- Extra key bar toggle -->
            <button class="p-1 rounded text-zinc-500 hover:text-purple-400 hover:bg-zinc-800 transition-colors"
                    id="${this.id}-extra-btn" onclick="getTerminal('${this.id}').toggleExtraBar()" title="Ekstra Tuşlar">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M8 4v16"/><path d="M16 4v16"/><path d="M2 12h20"/><path d="M2 8h6"/><path d="M2 16h6"/><path d="M16 8h6"/><path d="M16 16h6"/></svg>
            </button>
            <button class="p-1 rounded text-zinc-500 hover:text-zinc-100 hover:bg-zinc-800 transition-colors"
                    onclick="getTerminal('${this.id}').connect()" title="Yeniden Bağlan">${SVGS.refresh}</button>
            <button class="p-1 rounded text-zinc-500 hover:text-zinc-100 hover:bg-zinc-800 transition-colors"
                    onclick="getTerminal('${this.id}').term.clear()" title="Temizle">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M3 6h18M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
            </button>
          </div>
        </div>

        <!-- Extra key bar (collapsible: Esc, Alt, Ctrl, Fn keys) -->
        <div id="${this.id}-extra-bar" class="extra-key-bar shrink-0 border-b border-zinc-800/50 bg-[#0c0c0e]"
             style="overflow:hidden; max-height:0; transition:max-height .25s ease;">
          <div class="flex gap-1.5 px-2 py-1.5 overflow-x-auto no-scrollbar">
            <button class="key-btn key-esc bg-panel border border-border rounded px-2.5 py-1 text-[10.5px] font-mono shrink-0 font-semibold"
                    onclick="getTerminal('${this.id}').sendKey('\\x1b')">Esc</button>
            <button class="key-btn key-ctrl bg-panel border border-border rounded px-2.5 py-1 text-[10.5px] font-mono shrink-0 font-semibold"
                    id="${this.id}-ctrl-btn" onclick="getTerminal('${this.id}').toggleCtrl()">Ctrl</button>
            <button class="key-btn key-alt bg-panel border border-border rounded px-2.5 py-1 text-[10.5px] font-mono shrink-0 font-semibold"
                    id="${this.id}-alt-btn" onclick="getTerminal('${this.id}').toggleAlt()">Alt</button>
            <div class="w-px h-5 self-center bg-zinc-700 mx-0.5 shrink-0"></div>
            <button class="key-btn bg-panel border border-border rounded px-2.5 py-1 text-[10.5px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x1b[15~')">F5</button>
            <button class="key-btn bg-panel border border-border rounded px-2.5 py-1 text-[10.5px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x1b[17~')">F6</button>
            <button class="key-btn bg-panel border border-border rounded px-2.5 py-1 text-[10.5px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x1b[18~')">F7</button>
            <button class="key-btn bg-panel border border-border rounded px-2.5 py-1 text-[10.5px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x1b[19~')">F8</button>
            <div class="w-px h-5 self-center bg-zinc-700 mx-0.5 shrink-0"></div>
            <button class="key-btn bg-panel border border-border rounded px-2.5 py-1 text-[10.5px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x1b[H')">Home</button>
            <button class="key-btn bg-panel border border-border rounded px-2.5 py-1 text-[10.5px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x1b[F')">End</button>
            <button class="key-btn bg-panel border border-border rounded px-2.5 py-1 text-[10.5px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x1b[5~')">PgUp</button>
            <button class="key-btn bg-panel border border-border rounded px-2.5 py-1 text-[10.5px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x1b[6~')">PgDn</button>
            <button class="key-btn bg-panel border border-border rounded px-2.5 py-1 text-[10.5px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x7f')">Del</button>
            <button class="key-btn bg-panel border border-border rounded px-2.5 py-1 text-[10.5px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x1b[2~')">Ins</button>
          </div>
        </div>

        <!-- xterm.js output -->
        <div id="${this.id}-out" class="xterm-wrap bg-[#0a0a0c]"></div>

        <!-- Horizontal scroll track (shown only when content overflows) -->
        <div id="${this.id}-hscroll" class="hscroll-track">
          <div id="${this.id}-hscroll-thumb" class="hscroll-thumb" style="width:40%;left:0%"></div>
        </div>

        <!-- Autocomplete suggestion bar -->
        <div id="${this.id}-ac-bar" class="ac-bar hidden"></div>

        <!-- Primary quick-key bar -->
        <div class="shrink-0 border-t border-border bg-[#0c0c0e] px-2 py-1.5">
          <div class="flex gap-1.5 overflow-x-auto no-scrollbar">
            <button class="key-btn key-ctrl bg-panel border border-border rounded px-2.5 py-1.5 text-[11px] font-mono shrink-0 font-semibold"
                    onclick="getTerminal('${this.id}').sendKey('\\x03')">Ctrl+C</button>
            <button class="key-btn bg-primary/15 border border-primary/40 text-primary rounded px-2.5 py-1.5 text-[11px] font-mono shrink-0 font-semibold"
                    onclick="getTerminal('${this.id}').sendKey('\\r')">↵ Enter</button>
            <button class="key-btn key-tab bg-panel border border-border rounded px-2.5 py-1.5 text-[11px] font-mono shrink-0 font-semibold"
                    onclick="getTerminal('${this.id}').sendKey('\\t')">Tab ⇥</button>
            <button class="key-btn bg-panel border border-border text-zinc-400 rounded px-2.5 py-1.5 text-[11px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x1b[A')">↑</button>
            <button class="key-btn bg-panel border border-border text-zinc-400 rounded px-2.5 py-1.5 text-[11px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x1b[B')">↓</button>
            <button class="key-btn bg-panel border border-border text-zinc-400 rounded px-2.5 py-1.5 text-[11px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x04')">Ctrl+D</button>
            <button class="key-btn bg-panel border border-border text-zinc-400 rounded px-2.5 py-1.5 text-[11px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x1a')">Ctrl+Z</button>
            <button class="key-btn bg-panel border border-border text-zinc-400 rounded px-2.5 py-1.5 text-[11px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('\\x0c')">Ctrl+L</button>
            <div class="w-px h-5 self-center bg-zinc-700 mx-0.5 shrink-0"></div>
            <button class="key-btn bg-panel border border-border text-zinc-400 rounded px-2.5 py-1.5 text-[11px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('/')">/</button>
            <button class="key-btn bg-panel border border-border text-zinc-400 rounded px-2.5 py-1.5 text-[11px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('~')">~</button>
            <button class="key-btn bg-panel border border-border text-zinc-400 rounded px-2.5 py-1.5 text-[11px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('|')">|</button>
            <button class="key-btn bg-panel border border-border text-zinc-400 rounded px-2.5 py-1.5 text-[11px] font-mono shrink-0"
                    onclick="getTerminal('${this.id}').sendKey('>')">></button>
          </div>
        </div>
      </div><!-- /main col -->

      <!-- Desktop snippets sidebar -->
      <div class="hidden md:flex flex-col w-[190px] border-l border-border bg-panel overflow-y-auto scrollbar-thin shrink-0">
        <div class="text-[9px] font-bold text-textMuted uppercase tracking-widest px-3 pt-3 pb-1.5">Sistem</div>
        <div class="flex flex-col gap-1 px-2 mb-3">
          ${['ls -la','pwd','df -h','free -h','uname -a','uptime'].map(c =>
            `<button class="text-left px-2.5 py-1.5 bg-background border border-border rounded-md text-[10.5px] font-mono text-zinc-400 hover:text-white hover:border-borderBright transition-colors" onclick="getTerminal('${this.id}').sendCmd('${c.replace(/'/g,"\\'")}')">📁 ${c}</button>`
          ).join('')}
        </div>
        <div class="text-[9px] font-bold text-textMuted uppercase tracking-widest px-3 pb-1.5">Ağ & Git</div>
        <div class="flex flex-col gap-1 px-2 mb-3">
          ${['tailscale status','ip a','ping -c 3 8.8.8.8','git status','git log --oneline -10','git pull'].map(c =>
            `<button class="text-left px-2.5 py-1.5 bg-background border border-border rounded-md text-[10.5px] font-mono text-zinc-400 hover:text-white hover:border-borderBright transition-colors" onclick="getTerminal('${this.id}').sendCmd('${c.replace(/'/g,"\\'")}')">▸ ${c}</button>`
          ).join('')}
        </div>
        <div class="text-[9px] font-bold text-textMuted uppercase tracking-widest px-3 pb-1.5">Süreçler</div>
        <div class="flex flex-col gap-1 px-2">
          ${['ps aux --sort=-%cpu | head -15','ps aux --sort=-%mem | head -15','journalctl -n 30 --no-pager','kill -9 '].map(c =>
            `<button class="text-left px-2.5 py-1.5 bg-background border border-border rounded-md text-[10.5px] font-mono text-zinc-400 hover:text-white hover:border-borderBright transition-colors" onclick="getTerminal('${this.id}').sendCmd('${c.replace(/'/g,"\\'")}')">▸ ${c}</button>`
          ).join('')}
        </div>
      </div>
    `;
    document.getElementById('terminal-views-container').appendChild(this.viewEl);
  }

  initXterm() {
    this.term = new Terminal({
      cursorBlink: true,
      cursorStyle: 'bar',
      fontFamily: "'JetBrains Mono', monospace",
      fontSize: 13,
      lineHeight: 1.26,
      scrollback: 10000,
      convertEol: true,
      scrollSensitivity: 5,
      fastScrollSensitivity: 15,
      theme: {
        background:        '#0a0a0c',
        foreground:        '#e4e4e7',
        cursor:            '#3b82f6',
        cursorAccent:      '#0a0a0c',
        selectionBackground: 'rgba(59,130,246,.35)',
        black:       '#18181b', red:         '#f87171',
        green:       '#a3e635', yellow:      '#facc15',
        blue:        '#60a5fa', magenta:     '#c084fc',
        cyan:        '#22d3ee', white:       '#e4e4e7',
        brightBlack: '#52525b', brightRed:   '#fca5a5',
        brightGreen: '#bef264', brightYellow:'#fde047',
        brightBlue:  '#93c5fd', brightMagenta:'#d8b4fe',
        brightCyan:  '#67e8f9', brightWhite: '#fafafa',
      }
    });
    this.fitAddon = new FitAddon.FitAddon();
    this.term.loadAddon(this.fitAddon);
    this.term.open(document.getElementById(`${this.id}-out`));
    try { this.fitAddon.fit(); this._enforceMinCols(); } catch(e) {}

    // Forward every keystroke straight to the pty (Ctrl/Alt modifier logic)
    this.term.onData(data => {
      if (!this.ws || !this.connected) return;
      let send = data;
      if (state.ctrlActive && data.length === 1) {
        send = String.fromCharCode(data.charCodeAt(0) & 0x1f);
        this.deactivateCtrl();
      } else if (state.altActive && data.length === 1) {
        send = '\x1b' + data;
        this.deactivateAlt();
      }
      this.ws.send(JSON.stringify({ type:'pty_in', sessionId:this.id, data: send }));
    });

    this._ro = new ResizeObserver(() => {
      try { this.fitAddon.fit(); this._enforceMinCols(); } catch(e) {}
    });
    this._ro.observe(document.getElementById(`${this.id}-out`));
  }

  /* Enforce minimum column count so wide output (neofetch etc.) is scrollable */
  _enforceMinCols() {
    const MIN_COLS = 80;
    if (this.term && this.term.cols < MIN_COLS) {
      this.term.resize(MIN_COLS, this.term.rows);
      this.sendResize();
    }
  }

  /* Visual Viewport API: shrink terminal when software keyboard opens */
  initKeyboardResize() {
    if (!window.visualViewport) return;
    const vvp = window.visualViewport;
    const onResize = () => {
      const heightDiff = window.innerHeight - vvp.height;
      const outEl = document.getElementById(`${this.id}-out`);
      if (!outEl) return;
      if (heightDiff > 100) {
        outEl.style.marginBottom = heightDiff + 'px';
      } else {
        outEl.style.marginBottom = '0px';
      }
      try { this.fitAddon.fit(); this._enforceMinCols(); } catch(e) {}
    };
    vvp.addEventListener('resize', onResize);
    this._vvpResize = onResize;
  }

  /* Touch scroll: horizontal on xterm-wrap, fast vertical on xterm-viewport */
  initHorizontalScroll() {
    setTimeout(() => {
      const out   = document.getElementById(`${this.id}-out`);
      const track = document.getElementById(`${this.id}-hscroll`);
      const thumb = document.getElementById(`${this.id}-hscroll-thumb`);
      if (!out || !track || !thumb) return;

      const getVp = () => out.querySelector('.xterm-viewport');

      /* ── Scrollbar thumb position ─────────────────────────── */
      const updateBar = () => {
        const vis = out.clientWidth, total = out.scrollWidth;
        if (total <= vis + 2) { track.classList.remove('visible'); return; }
        track.classList.add('visible');
        const thumbW = Math.max(28, (vis / total) * track.clientWidth);
        const left   = (out.scrollLeft / (total - vis)) * (track.clientWidth - thumbW);
        thumb.style.width = thumbW + 'px';
        thumb.style.left  = left   + 'px';
      };

      out.addEventListener('scroll', updateBar, { passive: true });
      this.term.onRender(updateBar);
      this.term.onResize(() => setTimeout(updateBar, 50));

      /* ── Thumb drag (touch) ────────────────────────────────── */
      let td = { x: 0, sl: 0 };
      thumb.addEventListener('touchstart', e => {
        td = { x: e.touches[0].clientX, sl: out.scrollLeft };
        thumb.classList.add('dragging'); e.preventDefault();
      }, { passive: false });
      thumb.addEventListener('touchmove', e => {
        const dx    = e.touches[0].clientX - td.x;
        const ratio = (out.scrollWidth - out.clientWidth) / (track.clientWidth - thumb.offsetWidth);
        out.scrollLeft = Math.max(0, td.sl + dx * ratio);
        updateBar(); e.preventDefault();
      }, { passive: false });
      thumb.addEventListener('touchend', () => thumb.classList.remove('dragging'));

      /* ── Thumb drag (mouse) ────────────────────────────────── */
      thumb.addEventListener('mousedown', e => {
        let md = { x: e.clientX, sl: out.scrollLeft };
        thumb.classList.add('dragging');
        const mv = ev => {
          const ratio = (out.scrollWidth - out.clientWidth) / (track.clientWidth - thumb.offsetWidth);
          out.scrollLeft = Math.max(0, md.sl + (ev.clientX - md.x) * ratio);
          updateBar();
        };
        const up = () => {
          thumb.classList.remove('dragging');
          document.removeEventListener('mousemove', mv);
          document.removeEventListener('mouseup',   up);
        };
        document.addEventListener('mousemove', mv);
        document.addEventListener('mouseup',   up);
        e.preventDefault();
      });

      /* ── Unified touch handler on terminal area ────────────
         • Horizontal-dominant swipe  → scroll xterm-wrap (horizontal)
         • Vertical-dominant swipe    → fast momentum scroll on viewport
         Decision is made after 6 px of movement.                    */
      let t = { x:0, y:0, sl:0, st:0, lastY:0, lastT:0, vy:0,
                axis: null, rafId: null };

      out.addEventListener('touchstart', ev => {
        cancelAnimationFrame(t.rafId);
        const vp = getVp();
        t = { x: ev.touches[0].clientX, y: ev.touches[0].clientY,
              sl: out.scrollLeft,
              st: vp ? vp.scrollTop : 0,
              lastY: ev.touches[0].clientY, lastT: Date.now(),
              vy: 0, axis: null, rafId: null };
      }, { passive: true });

      out.addEventListener('touchmove', ev => {
        const vp = getVp();
        const dx = ev.touches[0].clientX - t.x;
        const dy = ev.touches[0].clientY - t.y;

        if (t.axis === null) {
          if (Math.abs(dx) < 6 && Math.abs(dy) < 6) return;
          t.axis = Math.abs(dx) >= Math.abs(dy) ? 'h' : 'v';
        }

        if (t.axis === 'h') {
          out.scrollLeft = Math.max(0, t.sl - dx);
          updateBar();
          ev.preventDefault();
        } else if (t.axis === 'v' && vp) {
          // Fast vertical: 3× multiplier so it doesn't feel sluggish
          vp.scrollTop = Math.max(0, t.st - dy * 3);
          // Track velocity for momentum
          const now = Date.now();
          t.vy = (ev.touches[0].clientY - t.lastY) / Math.max(1, now - t.lastT);
          t.lastY = ev.touches[0].clientY; t.lastT = now;
          ev.preventDefault();
        }
      }, { passive: false });

      out.addEventListener('touchend', () => {
        if (t.axis !== 'v') return;
        const vp = getVp(); if (!vp) return;
        let vel = -t.vy * 16 * 3; // px/frame with multiplier
        const decay = 0.88;
        const animate = () => {
          if (Math.abs(vel) < 1) return;
          vp.scrollTop = Math.max(0, vp.scrollTop + vel);
          vel *= decay;
          t.rafId = requestAnimationFrame(animate);
        };
        t.rafId = requestAnimationFrame(animate);
      });

      setTimeout(updateBar, 300);
    }, 350);
  }

  /* Extra key bar toggle */
  toggleExtraBar() {
    this.extraBarOpen = !this.extraBarOpen;
    const bar = document.getElementById(`${this.id}-extra-bar`);
    bar.style.maxHeight = this.extraBarOpen ? '52px' : '0';
    const btn = document.getElementById(`${this.id}-extra-btn`);
    btn.classList.toggle('text-purple-400', this.extraBarOpen);
    try { this.fitAddon.fit(); this._enforceMinCols(); } catch(e) {}
  }

  /* Ctrl toggle (sticky modifier) */
  toggleCtrl() {
    state.ctrlActive = !state.ctrlActive;
    document.getElementById(`${this.id}-ctrl-btn`).classList.toggle('key-toggled', state.ctrlActive);
    if (this.term) this.term.focus();
  }
  deactivateCtrl() {
    state.ctrlActive = false;
    document.getElementById(`${this.id}-ctrl-btn`)?.classList.remove('key-toggled');
  }

  /* Alt toggle */
  toggleAlt() {
    state.altActive = !state.altActive;
    document.getElementById(`${this.id}-alt-btn`).classList.toggle('key-toggled', state.altActive);
    if (this.term) this.term.focus();
  }
  deactivateAlt() {
    state.altActive = false;
    document.getElementById(`${this.id}-alt-btn`)?.classList.remove('key-toggled');
  }

  sendResize() {
    if (this.ws && this.connected && this.term) {
      this.ws.send(JSON.stringify({ type:'resize', sessionId:this.id, cols:this.term.cols, rows:this.term.rows }));
    }
  }

  connect() {
    clearTimeout(this._reconnTimer);
    if (this.ws) { try { this.ws.close(); } catch(_) {} this.ws = null; }
    this.setStatus(false, 'Bağlanıyor...');
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    let ws;
    try { ws = new WebSocket(proto + location.host + '/ws'); }
    catch(e) { this._scheduleReconnect(); return; }
    this.ws = ws;

    ws.onopen = () => {
      this._reconnDelay = 1000;   // reset backoff on success
      try { this.fitAddon.fit(); this._enforceMinCols(); } catch(_) {}
      if (this._sessionAlive) {
        // Re-attach to the existing PTY — don't create a new bash
        ws.send(JSON.stringify({ type:'attach_session', sessionId:this.id }));
      } else {
        ws.send(JSON.stringify({
          type:'create_session', sessionId:this.id,
          command: PkgMgr.getShell(),
          cols:this.term.cols, rows:this.term.rows
        }));
      }
      setTimeout(() => this.sendResize(), 150);
    };

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'pty_out') {
        const bin = atob(msg.data);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        this.term.write(bytes);
      } else if (msg.type === 'create_session_ok' || msg.type === 'attach_session_ok') {
        this._sessionAlive = true;
        this.setStatus(true, 'Aktif');
        this.term.focus();
      } else if (msg.type === 'session_dead') {
        // Server says PTY died while we were away — open fresh shell
        this._sessionAlive = false;
        this.term.writeln('\r\n\x1b[33m[Oturum sonlandı, yeni terminal açılıyor...]\x1b[0m\r\n');
        ws.send(JSON.stringify({
          type:'create_session', sessionId:this.id,
          command: PkgMgr.getShell(),
          cols:this.term.cols, rows:this.term.rows
        }));
      }
    };

    ws.onclose = () => {
      clearInterval(this._pingInterval);
      if (this.ws !== ws) return;   // stale close from old socket
      this.connected = false;
      this._scheduleReconnect();
    };
    ws.onerror = () => {};   // onclose fires after onerror; handled there
    // Keepalive ping every 20s to survive NAT/idle timeouts
    this._pingInterval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'ping'}));
    }, 20000);
  }

  _scheduleReconnect() {
    const delay = this._reconnDelay || 1000;
    this._reconnDelay = Math.min((this._reconnDelay || 1000) * 2, 30000);
    const secs = Math.round(delay / 1000);
    this.setStatus(false, `Yeniden bağlanıyor (${secs}s)...`);
    this._reconnTimer = setTimeout(() => this.connect(), delay);
  }

  setStatus(isConnected, text) {
    this.connected = isConnected;
    const dot = document.getElementById(`${this.id}-dot`);
    const txt = document.getElementById(`${this.id}-status`);
    if (isConnected) {
      dot.className = 'w-1.5 h-1.5 rounded-full bg-emerald-500 shadow-[0_0_7px_rgba(16,185,129,.6)]';
      txt.textContent = text; txt.className = 'text-emerald-400';
    } else {
      dot.className = 'w-1.5 h-1.5 rounded-full bg-red-500 shadow-[0_0_7px_rgba(239,68,68,.6)]';
      txt.textContent = text; txt.className = 'text-red-400';
    }
  }

  sendKey(key) {
    if (this.ws && this.connected) {
      this.ws.send(JSON.stringify({ type:'pty_in', sessionId:this.id, data: key }));
    }
    if (this.term) this.term.focus();
  }

  sendCmd(cmd) {
    if (this.ws && this.connected && cmd) {
      this.ws.send(JSON.stringify({ type:'pty_in', sessionId:this.id, data: cmd + '\r' }));
    }
    if (this.term) this.term.focus();
  }

  destroy() {
    clearTimeout(this._reconnTimer);
    clearInterval(this._pingInterval);
    if (window.visualViewport && this._vvpResize) {
      window.visualViewport.removeEventListener('resize', this._vvpResize);
    }
    if (this.ws)  { try { this.ws.close(); } catch(_) {} }
    if (this._ro) this._ro.disconnect();
    if (this.term) this.term.dispose();
    this.tabEl.remove();
    this.viewEl.remove();
  }
}

/* ── Terminal helpers ────────────────────────────────────────────── */
function addTerminal() {
  const t = new TerminalInstance();
  state.terminals.push(t);
  activateTerminal(t.id);
  Autocomplete.hookTerminal(t);
}

function getTerminal(id) { return state.terminals.find(t => t.id === id); }

function activateTerminal(id) {
  state.activeTermId = id;
  state.terminals.forEach(t => {
    const active = t.id === id;
    t.tabEl.classList.toggle('active', active);
    t.viewEl.classList.toggle('hidden', !active);
    if (active) {
      requestAnimationFrame(() => {
        try { t.fitAddon.fit(); t.sendResize(); t.term.focus(); } catch(e) {}
      });
    }
  });
}

function closeTerminal(id) {
  const t = getTerminal(id);
  if (!t) return;
  t.destroy();
  state.terminals = state.terminals.filter(x => x.id !== id);
  if (state.activeTermId === id) {
    if (state.terminals.length > 0) activateTerminal(state.terminals[state.terminals.length - 1].id);
    else addTerminal();
  }
}

function toggleDpad(termId) {
  const overlay = document.getElementById('dpad-overlay');
  const visible = overlay.classList.toggle('visible');
  const btn = document.getElementById(termId + '-dpad-btn');
  if (btn) btn.classList.toggle('text-blue-400', visible);
}

/* ═══════════════════════════════════════════════════════════════════
   MODULE: File Manager
══════════════════════════════════════════════════════════════════ */
class FileManagerInstance {
  constructor() {
    this.id          = 'file_' + genId();
    this.path        = '.';
    this.viewMode    = 'list';
    this.items       = [];
    this.searchQuery = '';
    this.renderDOM();
    this.fetchDir();
    this.initSwipe();
  }

  renderDOM() {
    // Tab
    this.tabEl = document.createElement('div');
    this.tabEl.className = 'tab-btn flex items-center gap-1.5 px-3 h-full rounded-t-lg text-[12px] font-semibold cursor-pointer shrink-0';
    this.tabEl.innerHTML = `
      <span class="text-emerald-400">${SVGS.folder}</span>
      <span class="max-w-[80px] truncate" id="${this.id}-title">/</span>
      <span id="${this.id}-dev-badge" style="display:none;font-size:9px;padding:1px 5px;border-radius:4px;color:#fff;font-weight:600;margin-left:2px;align-items:center"></span>
      <button class="ml-1 p-0.5 rounded hover:bg-zinc-700/50 text-zinc-500 hover:text-white transition-colors"
              onclick="event.stopPropagation(); closeFileManager('${this.id}')">${SVGS.close}</button>
    `;
    this.tabEl.onclick = () => activateFileManager(this.id);
    document.getElementById('file-tabs-container').appendChild(this.tabEl);

    // View
    this.viewEl = document.createElement('div');
    this.viewEl.className = 'absolute inset-0 flex flex-col hidden';
    this.viewEl.innerHTML = `
      <!-- Toolbar -->
      <div class="h-11 border-b border-border bg-panel flex items-center justify-between px-3 shrink-0 gap-2">
        <div id="${this.id}-bc" class="flex items-center gap-0.5 overflow-x-auto no-scrollbar font-mono text-[11px] text-zinc-400 whitespace-nowrap flex-1"></div>
        <div class="flex items-center gap-1 shrink-0">
          <button class="p-1.5 rounded-md hover:bg-zinc-800 text-zinc-400 hover:text-zinc-100 transition-colors"
                  onclick="getFileManager('${this.id}').fetchDir()" title="Yenile">${SVGS.refresh}</button>
          <div class="w-px h-4 bg-border mx-0.5"></div>
          <button class="p-1.5 rounded-md hover:bg-zinc-800 text-zinc-400 hover:text-zinc-100 transition-colors"
                  onclick="openModal('mkdir','${this.id}')" title="Yeni Klasör">${SVGS.folder}</button>
          <button class="p-1.5 rounded-md hover:bg-zinc-800 text-zinc-400 hover:text-zinc-100 transition-colors"
                  onclick="openModal('newfile','${this.id}')" title="Yeni Dosya">${SVGS.plus}</button>
          <label class="p-1.5 rounded-md hover:bg-zinc-800 text-zinc-400 hover:text-emerald-400 transition-colors cursor-pointer"
                 title="Dosya Yükle">
            ${SVGS.upload}
            <input type="file" multiple class="hidden" id="${this.id}-upload-input">
          </label>
          <button class="p-1.5 rounded-md hover:bg-zinc-800 text-amber-400 hover:text-amber-300 transition-colors hidden"
                  id="${this.id}-paste-btn" onclick="clipboardPaste('${this.id}')" title="Yapıştır">${SVGS.paste}</button>
          <div class="w-px h-4 bg-border mx-0.5"></div>
          <button class="p-1.5 rounded-md hover:bg-zinc-800 text-zinc-400 hover:text-primary transition-colors"
                  id="${this.id}-search-btn" onclick="getFileManager('${this.id}').toggleSearch()" title="Ara">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          </button>
          <div class="w-px h-4 bg-border mx-0.5"></div>
          <button class="p-1.5 rounded-md hover:bg-zinc-800 text-zinc-400 hover:text-primary transition-colors"
                  id="${this.id}-v-list" onclick="getFileManager('${this.id}').setView('list')">${SVGS.list}</button>
          <button class="p-1.5 rounded-md hover:bg-zinc-800 text-zinc-400 hover:text-primary transition-colors"
                  id="${this.id}-v-grid" onclick="getFileManager('${this.id}').setView('grid')">${SVGS.grid}</button>
        </div>
      </div>

      <!-- Search bar (hidden by default) -->
      <div id="${this.id}-search-bar" class="border-b border-border bg-panel px-3 py-2 flex items-center gap-2 shrink-0" style="display:none;">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#71717a" stroke-width="2.5" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input id="${this.id}-search-inp" type="search" placeholder="Dosya veya klasör ara..."
          class="flex-1 bg-transparent text-[12.5px] text-white outline-none placeholder:text-zinc-600 min-w-0"
          oninput="getFileManager('${this.id}').onSearch(this.value)">
        <button onclick="getFileManager('${this.id}').closeSearch()"
                class="text-zinc-500 hover:text-white text-lg leading-none px-1">×</button>
      </div>

      <!-- Drag-drop upload overlay -->
      <div id="${this.id}-drop-overlay" class="absolute inset-0 z-30 hidden flex-col items-center justify-center
           bg-black/70 backdrop-blur-sm border-2 border-dashed border-primary rounded-lg m-2">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="1.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        <p class="text-primary font-semibold mt-3 text-sm">Dosyaları buraya bırakın</p>
        <p class="text-zinc-400 text-xs mt-1">${this.path}</p>
      </div>

      <!-- File list -->
      <div id="${this.id}-content" class="flex-1 overflow-y-auto p-2 scrollbar-thin bg-background"></div>
    `;
    document.getElementById('file-views-container').appendChild(this.viewEl);

    // Upload via input
    setTimeout(() => {
      const inp = document.getElementById(`${this.id}-upload-input`);
      if (inp) inp.onchange = (e) => this.uploadFiles(e.target.files);

      // Drag-drop
      this.viewEl.addEventListener('dragover', (e) => {
        e.preventDefault();
        document.getElementById(`${this.id}-drop-overlay`).classList.remove('hidden');
        document.getElementById(`${this.id}-drop-overlay`).classList.add('flex');
      });
      this.viewEl.addEventListener('dragleave', (e) => {
        if (!this.viewEl.contains(e.relatedTarget)) this.hideDrop();
      });
      this.viewEl.addEventListener('drop', (e) => {
        e.preventDefault();
        this.hideDrop();
        if (e.dataTransfer.files.length) this.uploadFiles(e.dataTransfer.files);
      });
    }, 0);

    this.setView(this.viewMode);
  }

  hideDrop() {
    const o = document.getElementById(`${this.id}-drop-overlay`);
    if (o) { o.classList.add('hidden'); o.classList.remove('flex'); }
  }

  toggleSearch() {
    const bar = document.getElementById(`${this.id}-search-bar`);
    const btn = document.getElementById(`${this.id}-search-btn`);
    if (!bar) return;
    const showing = bar.style.display !== 'none';
    if (showing) {
      this.closeSearch();
    } else {
      bar.style.display = 'flex';
      btn?.classList.add('text-primary', 'bg-primary/10');
      setTimeout(() => document.getElementById(`${this.id}-search-inp`)?.focus(), 60);
    }
  }

  closeSearch() {
    const bar = document.getElementById(`${this.id}-search-bar`);
    const btn = document.getElementById(`${this.id}-search-btn`);
    const inp = document.getElementById(`${this.id}-search-inp`);
    if (bar) bar.style.display = 'none';
    btn?.classList.remove('text-primary', 'bg-primary/10');
    if (inp) inp.value = '';
    this.searchQuery = '';
    this.renderItems(this.items);
  }

  onSearch(q) {
    this.searchQuery = q.toLowerCase();
    const filtered = this.searchQuery
      ? this.items.filter(f => f.name.toLowerCase().includes(this.searchQuery))
      : this.items;
    this.renderItems(filtered);
  }

  async uploadFiles(files) {
    let ok = 0, fail = 0;
    for (const file of files) {
      const fd = new FormData();
      fd.append('path', this.path);
      fd.append('file', file);
      try {
        const res  = await fetch('/api/upload', { method:'POST', body:fd });
        const data = await res.json();
        if (data.success) ok++;
        else { fail++; showToast(`${file.name}: ${data.error}`, 'error'); }
      } catch(e) { fail++; }
    }
    if (ok)   showToast(`${ok} dosya yüklendi`);
    if (fail) showToast(`${fail} dosya başarısız`, 'error');
    this.fetchDir();
  }

  /* Swipe left/right to switch tabs (mobile) */
  initSwipe() {
    let sx = 0, sy = 0;
    this.viewEl.addEventListener('touchstart', (e) => {
      sx = e.touches[0].clientX; sy = e.touches[0].clientY;
    }, { passive: true });
    this.viewEl.addEventListener('touchend', (e) => {
      const dx = e.changedTouches[0].clientX - sx;
      const dy = e.changedTouches[0].clientY - sy;
      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) {
        const fms = state.fileManagers;
        const idx = fms.findIndex(f => f.id === this.id);
        if (dx < 0 && idx < fms.length - 1) activateFileManager(fms[idx + 1].id);
        if (dx > 0 && idx > 0)              activateFileManager(fms[idx - 1].id);
      }
    }, { passive: true });
  }

  setView(mode) {
    this.viewMode = mode;
    ['list','grid'].forEach(m => {
      const btn = document.getElementById(`${this.id}-v-${m}`);
      if (btn) { btn.classList.toggle('text-primary', m===mode); btn.classList.toggle('bg-primary/10', m===mode); }
    });
    if (this.items) this.renderItems(this.items);
  }

  async fetchDir() {
    try {
      const data = await DevFS.ls(this.path);
      if (data.success) {
        this.path        = data.current_path;
        this.items       = data.items;
        this.searchQuery = '';
        const si = document.getElementById(`${this.id}-search-inp`);
        if (si) si.value = '';
        this.renderBreadcrumb();
        this.renderItems(this.items);
        document.getElementById(`${this.id}-title`).textContent = this.path.split('/').pop() || '/';
        // Aktif cihaz adını tab badge'ine yaz
        try {
          const devId = Devices.activeId();
          const badge = document.getElementById(`${this.id}-dev-badge`);
          if (badge) {
            if (devId === 'local') { badge.style.display = 'none'; }
            else {
              const dev = Devices.getDevById(devId);
              if (dev) {
                badge.style.display = 'inline-flex';
                badge.style.background = dev.color;
                badge.textContent = dev.name;
              }
            }
          }
        } catch(_) {}
      } else showToast('Dizin okunamadı: ' + data.error, 'error');
    } catch(e) { showToast('Bağlantı hatası: ' + e.message, 'error'); }
  }

  renderBreadcrumb() {
    const bc = document.getElementById(`${this.id}-bc`);
    bc.innerHTML = '';
    const parts = this.path.replace(/\/+/g, '/').split('/').filter(Boolean);
    const paths  = ['/'];
    parts.forEach((p, i) => paths.push('/' + parts.slice(0, i+1).join('/')));
    ['/', ...parts].forEach((label, i) => {
      if (i > 0) bc.insertAdjacentHTML('beforeend', '<span class="text-zinc-600 mx-0.5">/</span>');
      const btn = document.createElement('button');
      btn.className = `hover:text-primary transition-colors px-0.5 rounded ${i===paths.length-1 ? 'text-zinc-200' : ''}`;
      btn.textContent = label;
      btn.onclick = () => { this.path = paths[i]; this.fetchDir(); };
      bc.appendChild(btn);
    });
    bc.scrollLeft = bc.scrollWidth;
  }

  renderItems(items) {
    const container = document.getElementById(`${this.id}-content`);
    container.innerHTML = '';
    container.className = `flex-1 overflow-y-auto p-2 scrollbar-thin bg-background ${
      this.viewMode === 'grid'
        ? 'grid grid-cols-[repeat(auto-fill,minmax(96px,1fr))] gap-2'
        : 'flex flex-col gap-0.5'
    }`;

    if (items.length === 0) {
      container.innerHTML = '<div class="flex flex-col items-center justify-center h-full text-zinc-600 text-xs italic gap-2 mt-8">' +
        '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>Klasör boş</div>';
      return;
    }

    items.forEach(file => {
      const icon = getFileIconExt(file.name, file.is_dir);
      const el   = document.createElement('div');
      const fileJson = JSON.stringify(file).replace(/"/g,'&quot;');

      if (this.viewMode === 'list') {
        el.className = 'file-item flex items-center justify-between px-2 py-1.5 rounded-lg cursor-pointer group';
        el.innerHTML = `
          <div class="flex items-center gap-2.5 overflow-hidden">
            <div class="w-7 h-7 rounded-md flex items-center justify-center shrink-0 ${icon.cls}">${icon.svg}</div>
            <span class="text-[12.5px] font-medium truncate ${file.is_dir ? 'text-zinc-200' : 'text-zinc-400'}">${file.name}</span>
          </div>
          <div class="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
            ${!file.is_dir ? `
              <a class="p-1 rounded hover:bg-zinc-700 text-zinc-500 hover:text-emerald-400 transition-colors"
                 href="/api/download?path=${encodeURIComponent(file.path)}" onclick="event.stopPropagation()" title="İndir">${SVGS.download}</a>
              <button class="p-1 rounded hover:bg-zinc-700 text-zinc-500 hover:text-blue-400 transition-colors"
                      onclick="event.stopPropagation(); Editor.open('${file.path.replace(/'/g,"\\'")}');" title="Düzenle">${SVGS.edit}</button>
            ` : ''}
            <button class="p-1 rounded hover:bg-zinc-700 text-zinc-500 hover:text-white transition-colors"
                    onclick="event.stopPropagation(); openCtx(event,${fileJson},'${this.id}')">${SVGS.more}</button>
          </div>
        `;
      } else {
        el.className = 'file-item flex flex-col items-center p-2.5 rounded-xl border border-transparent hover:border-border cursor-pointer relative group';
        el.innerHTML = `
          <button class="absolute top-1 right-1 p-0.5 rounded bg-zinc-800/80 text-zinc-400 hover:text-white opacity-0 group-hover:opacity-100 z-10 border border-border transition-opacity"
                  onclick="event.stopPropagation(); openCtx(event,${fileJson},'${this.id}')">${SVGS.more}</button>
          ${!file.is_dir ? `
            <a class="absolute top-1 left-1 p-0.5 rounded bg-zinc-800/80 text-emerald-400 opacity-0 group-hover:opacity-100 z-10 border border-border hover:text-emerald-300 transition-opacity"
               href="/api/download?path=${encodeURIComponent(file.path)}" onclick="event.stopPropagation()" title="İndir">${SVGS.download}</a>
          ` : ''}
          <div class="w-9 h-9 rounded-lg flex items-center justify-center mb-1.5 mt-0.5 ${icon.cls}">${icon.svg}</div>
          <span class="text-[10.5px] font-medium text-center w-full truncate ${file.is_dir ? 'text-zinc-200' : 'text-zinc-400'}">${file.name}</span>
        `;
      }

      if (file.is_dir) {
        el.onclick = () => { this.path = file.path; this.fetchDir(); };
      } else {
        const pt = getPreviewType(file.name);
        if (pt === 'image') {
          el.onclick = () => openImagePreview(file.path, file.name);
        } else if (pt === 'text') {
          el.onclick = () => openTextPreview(file.path, file.name, file.size);
        }
      }
      container.appendChild(el);
    });
  }

  destroy() { this.tabEl.remove(); this.viewEl.remove(); }
}

/* ── File manager helpers ────────────────────────────────────────── */
function addFileManager() {
  const fm = new FileManagerInstance();
  state.fileManagers.push(fm);
  activateFileManager(fm.id);
}

function getFileManager(id) { return state.fileManagers.find(f => f.id === id); }

function activateFileManager(id) {
  state.activeFileId = id;
  state.fileManagers.forEach(f => {
    const active = f.id === id;
    f.tabEl.classList.toggle('active', active);
    f.viewEl.classList.toggle('hidden', !active);
  });
}

function closeFileManager(id) {
  const f = getFileManager(id);
  if (!f) return;
  f.destroy();
  state.fileManagers = state.fileManagers.filter(x => x.id !== id);
  if (state.activeFileId === id) {
    if (state.fileManagers.length > 0) activateFileManager(state.fileManagers[state.fileManagers.length-1].id);
    else addFileManager();
  }
}

/* ═══════════════════════════════════════════════════════════════════
   MODULE: Context menu
══════════════════════════════════════════════════════════════════ */
const ctxMenu = document.getElementById('ctx-menu');

function openCtx(e, file, fmId) {
  e.preventDefault();
  const itemsEl = document.getElementById('ctx-items');
  itemsEl.innerHTML = '';

  const addItem = (svg, label, cls, onClick) => {
    const b = document.createElement('button');
    b.className = `flex items-center gap-2.5 w-full px-3 py-2 text-[12.5px] font-medium rounded-lg transition-colors ${cls || 'text-zinc-300 hover:bg-zinc-800 hover:text-white'}`;
    b.innerHTML = `${svg} ${label}`;
    b.onclick = (ev) => { ev.stopPropagation(); closeCtx(); onClick(); };
    itemsEl.appendChild(b);
  };

  if (!file.is_dir) {
    const pt = getPreviewType(file.name);
    if (pt) {
      const label = pt === 'image' ? 'Resmi Görüntüle' : 'Önizle';
      addItem(SVGS.eye, label, 'text-sky-400 hover:bg-sky-500/10 hover:text-sky-300',
        () => pt === 'image' ? openImagePreview(file.path, file.name)
                             : openTextPreview(file.path, file.name, file.size));
    }
    addItem(SVGS.download, 'İndir', '', () => location.href = '/api/download?path=' + encodeURIComponent(file.path));
    addItem(SVGS.edit, 'Editörde Aç', '', () => Editor.open(file.path));
  }
  addItem(SVGS.zip, 'ZIP İndir', '', () => location.href = '/api/download_zip?path=' + encodeURIComponent(file.path));
  const archiveExts = ['zip','tar','gz','bz2','xz','tgz','tbz2','7z','rar'];
  if (!file.is_dir && archiveExts.includes(file.name.split('.').pop().toLowerCase())) {
    addItem(SVGS.extract, 'Çıkar (Extract)', 'text-emerald-400 hover:bg-emerald-500/10 hover:text-emerald-300', () => extractArchive(file, fmId));
  }
  itemsEl.insertAdjacentHTML('beforeend', '<div class="h-px bg-border my-1"></div>');
  addItem(SVGS.cut,  'Kes',           'text-amber-400 hover:bg-amber-500/10 hover:text-amber-300', () => clipboardSet(file, 'cut'));
  addItem(SVGS.copy, 'Kopyala',       '', () => clipboardSet(file, 'copy'));
  addItem(SVGS.move, 'Taşı…',         '', () => openModal('move', fmId, file));
  itemsEl.insertAdjacentHTML('beforeend', '<div class="h-px bg-border my-1"></div>');
  addItem(SVGS.edit, 'Yeniden Adlandır', '', () => openModal('rename', fmId, file));
  itemsEl.insertAdjacentHTML('beforeend', '<div class="h-px bg-border my-1"></div>');
  if (!file.is_dir) {
    itemsEl.insertAdjacentHTML('beforeend', '<div class="h-px bg-border my-1"></div>');
    addItem('<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg>',
      'Paylaşım Linki', 'text-sky-400 hover:bg-sky-500/10 hover:text-sky-300', () => ShareLink.open(file.path, file.name));
  }
  itemsEl.insertAdjacentHTML('beforeend', '<div class="h-px bg-border my-1"></div>');
  addItem(SVGS.trash, 'Sil', 'text-red-400 hover:bg-red-500/10 hover:text-red-300', () => openModal('delete', fmId, file));

  ctxMenu.classList.add('visible');
  let x = e.clientX, y = e.clientY;
  if (x + 210 > window.innerWidth)  x = window.innerWidth  - 215;
  if (y + 200  > window.innerHeight) y = window.innerHeight - 210;
  ctxMenu.style.left = x + 'px'; ctxMenu.style.top = y + 'px';
}

function closeCtx() { ctxMenu.classList.remove('visible'); }
document.addEventListener('click', closeCtx);
window.addEventListener('resize',  closeCtx);

/* ═══════════════════════════════════════════════════════════════════
   MODULE: Modals
══════════════════════════════════════════════════════════════════ */
const modalOverlay = document.getElementById('modal-overlay');
const modalCard    = document.getElementById('modal-card');

function openModal(action, fmId, file = null) {
  const titleEl   = document.getElementById('modal-title');
  const bodyEl    = document.getElementById('modal-body');
  const btnConfirm= document.getElementById('modal-confirm');
  const fm        = getFileManager(fmId);

  modalOverlay.classList.remove('hidden');
  requestAnimationFrame(() => {
    modalOverlay.classList.remove('opacity-0');
    modalCard.classList.remove('scale-95');
  });

  btnConfirm.className = 'px-4 py-2 rounded-lg text-sm font-semibold transition-colors bg-primary text-white hover:bg-blue-600';

  if (action === 'rename') {
    titleEl.textContent = 'Yeniden Adlandır';
    bodyEl.innerHTML = `<input type="text" id="modal-inp" class="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-white focus:border-primary focus:outline-none" value="${file.name}">`;
    btnConfirm.onclick = async () => {
      const val = document.getElementById('modal-inp').value.trim();
      if (!val) return;
      const d = await DevFS.rename(file.path, val);
      if (d.success) { showToast('Yeniden adlandırıldı'); fm.fetchDir(); } else showToast(d.error, 'error');
      closeModal();
    };
  } else if (action === 'delete') {
    titleEl.textContent = 'Kalıcı Olarak Sil';
    const isDir = file.is_dir;
    bodyEl.innerHTML = `
      <p class="text-zinc-400 text-sm">
        <strong class="text-white">${file.name}</strong>
        ${isDir ? '<span class="text-amber-400"> (klasör + tüm içeriği)</span>' : ''}
        kalıcı olarak silinecek. Bu işlem geri alınamaz.
      </p>`;
    btnConfirm.className = 'px-4 py-2 rounded-lg text-sm font-semibold transition-colors bg-red-500/10 border border-red-500/30 text-red-500 hover:bg-red-500 hover:text-white';
    btnConfirm.textContent = 'Sil';
    btnConfirm.onclick = async () => {
      const d = await DevFS.del(file.path);
      if (d.success) { showToast('Silindi'); fm.fetchDir(); } else showToast(d.error, 'error');
      closeModal();
    };
  } else if (action === 'mkdir') {
    titleEl.textContent = 'Yeni Klasör';
    bodyEl.innerHTML = `<input type="text" id="modal-inp" class="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-white focus:border-primary focus:outline-none" placeholder="Klasör adı">`;
    btnConfirm.onclick = async () => {
      const val = document.getElementById('modal-inp').value.trim();
      if (!val) return;
      const d = await DevFS.mkdir(fm.path, val);
      if (d.success) { showToast('Oluşturuldu'); fm.fetchDir(); } else showToast(d.error, 'error');
      closeModal();
    };
  } else if (action === 'newfile') {
    titleEl.textContent = 'Yeni Dosya';
    bodyEl.innerHTML = `<input type="text" id="modal-inp" class="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-white focus:border-primary focus:outline-none" placeholder="Dosya adı (örn: app.py)">`;
    btnConfirm.onclick = async () => {
      const val = document.getElementById('modal-inp').value.trim();
      if (!val) return;
      const d = await DevFS.newfile(fm.path, val);
      if (d.success) { showToast('Oluşturuldu'); fm.fetchDir(); } else showToast(d.error, 'error');
      closeModal();
    };
  } else if (action === 'move') {
    titleEl.textContent = `Taşı: ${file.name}`;
    bodyEl.innerHTML = `
      <p class="text-zinc-500 text-xs mb-2">Hedef klasör yolunu girin:</p>
      <input type="text" id="modal-inp"
             class="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-white font-mono focus:border-primary focus:outline-none"
             value="${fm.path}" placeholder="/hedef/klasör">
      <p class="text-zinc-600 text-[11px] mt-1.5">Mevcut konum: <span class="text-zinc-400">${fm.path}</span></p>`;
    btnConfirm.textContent = 'Taşı';
    btnConfirm.onclick = async () => {
      const dest = document.getElementById('modal-inp').value.trim();
      if (!dest) return;
      const r = await fetch('/api/move', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({src_path:file.path, dest_dir:dest})});
      const d = await r.json();
      if (d.success) { showToast('Taşındı'); state.fileManagers.forEach(f => f.fetchDir()); } else showToast(d.error, 'error');
      closeModal();
    };
  }

  setTimeout(() => {
    const inp = document.getElementById('modal-inp');
    if (inp) { inp.focus(); inp.select(); }
  }, 80);
  document.getElementById('modal-inp')?.addEventListener('keydown', e => { if (e.key==='Enter') btnConfirm.click(); });
}

function closeModal() {
  modalOverlay.classList.add('opacity-0');
  modalCard.classList.add('scale-95');
  setTimeout(() => modalOverlay.classList.add('hidden'), 200);
}

/* ═══════════════════════════════════════════════════════════════════
   MODULE: Process Manager
══════════════════════════════════════════════════════════════════ */
const ProcessMgr = (() => {
  let _timer = null;

  function open() {
    document.getElementById('proc-bd').classList.add('open');
    document.getElementById('proc-panel').classList.add('open');
    refresh();
  }
  function close() {
    document.getElementById('proc-bd').classList.remove('open');
    document.getElementById('proc-panel').classList.remove('open');
    stopAutoRefresh();
  }
  async function refresh() {
    const body = document.getElementById('proc-body');
    body.innerHTML = '<div class="p-4 text-center text-zinc-600 text-xs">Yükleniyor…</div>';
    try {
      const d = await fetch('/api/processes').then(r => r.json());
      if (!d.ok) { body.innerHTML = `<div class="p-4 text-red-400 text-xs">${d.error}</div>`; return; }
      const rows = d.processes.map(p => `
        <tr>
          <td><span class="px-1.5 py-0.5 rounded text-[10px] ${
            p.status==='running'?'bg-emerald-900/50 text-emerald-400':'bg-zinc-800 text-zinc-500'
          }">${p.status}</span></td>
          <td class="font-semibold text-zinc-200">${p.name}</td>
          <td>${p.pid}</td>
          <td>${p.user}</td>
          <td class="${p.cpu>70?'text-red-400':p.cpu>30?'text-amber-400':'text-zinc-400'}">${p.cpu}%</td>
          <td class="${p.mem>70?'text-red-400':p.mem>20?'text-amber-400':'text-zinc-400'}">${p.mem.toFixed(1)}%</td>
          <td class="max-w-[120px]"><span class="text-zinc-600 truncate block" style="max-width:120px" title="${p.cmd.replace(/"/g,'&quot;')}">${p.cmd}</span></td>
          <td>
            <button onclick="ProcessMgr.kill(${p.pid},15)"
              class="px-1.5 py-0.5 text-[10px] rounded bg-amber-900/40 text-amber-400 hover:bg-amber-900/70 mr-1">TERM</button>
            <button onclick="ProcessMgr.kill(${p.pid},9)"
              class="px-1.5 py-0.5 text-[10px] rounded bg-red-900/40 text-red-400 hover:bg-red-900/70">KILL</button>
          </td>
        </tr>`).join('');
      body.innerHTML = `<div style="overflow-x:auto"><table class="proc-table">
        <thead><tr><th>Durum</th><th>Ad</th><th>PID</th><th>Kullanıcı</th><th>CPU</th><th>RAM</th><th>Komut</th><th>İşlem</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
    } catch(e) { body.innerHTML = `<div class="p-4 text-red-400 text-xs">${e.message}</div>`; }
  }
  async function kill(pid, sig) {
    const d = await fetch('/api/kill', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({pid, signal: sig})}).then(r => r.json());
    if (d.ok) { showToast(`PID ${pid} → SIG${sig}`); setTimeout(refresh, 800); }
    else showToast(d.error || 'Hata', 'error');
  }
  function toggleAutoRefresh() {
    const btn = document.getElementById('proc-auto-btn');
    if (_timer) { stopAutoRefresh(); btn.textContent = 'Oto: Kapalı'; }
    else { _timer = setInterval(refresh, 3000); btn.textContent = 'Oto: Açık'; }
  }
  function stopAutoRefresh() {
    if (_timer) { clearInterval(_timer); _timer = null; }
    const btn = document.getElementById('proc-auto-btn');
    if (btn) btn.textContent = 'Oto: Kapalı';
  }
  return { open, close, refresh, kill, toggleAutoRefresh };
})();


/* ═══════════════════════════════════════════════════════════════════
   MODULE: Package Manager
══════════════════════════════════════════════════════════════════ */
const PkgMgr = (() => {
  // OS bilgisi yüklenince doldurulur
  let _managers = [];   // [{id, label, search, install}, ...]
  let _shell    = '/bin/bash';

  function _getMgr() {
    const sel = document.getElementById('pkg-mgr-sel');
    return _managers.find(m => m.id === sel.value) || _managers[0] || {id:'pip',label:'pip',install:'pip install',search:null};
  }

  function applyOsInfo(info) {
    _managers = info.managers || [];
    _shell    = info.shell    || '/bin/bash';
    const sel = document.getElementById('pkg-mgr-sel');
    sel.innerHTML = _managers.map(m => `<option value="${m.id}">${m.label}</option>`).join('');
    // Araç kartı açıklamasını güncelle
    const desc = document.querySelector('.tool-card[onclick="PkgMgr.open()"] .tool-card-desc');
    if (desc) desc.textContent = _managers.map(m=>m.label).slice(0,3).join(' · ');
    // Sistem bilgisini dashboard'a yaz
    const strip = document.getElementById('dashboard-strip');
    if (strip && info.distro) {
      const ex = document.getElementById('dash-os-item');
      if (!ex) {
        const el = document.createElement('div');
        el.id = 'dash-os-item';
        el.className = 'dash-item';
        el.innerHTML = `<span style="color:#a1a1aa">🖥</span><span style="color:#e4e4e7;font-weight:600">${info.distro}${info.version?' '+info.version:''}</span>`;
        strip.prepend(el);
      }
    }
  }

  function open() {
    document.getElementById('pkg-bd').classList.add('open');
    document.getElementById('pkg-panel').classList.add('open');
  }
  function close() {
    document.getElementById('pkg-bd').classList.remove('open');
    document.getElementById('pkg-panel').classList.remove('open');
  }
  function onInput(val) {
    clearTimeout(PkgMgr._t);
    PkgMgr._t = setTimeout(() => { if (val.trim()) search(); }, 600);
  }
  async function search() {
    const q   = document.getElementById('pkg-search-inp').value.trim();
    const mgr = _getMgr();
    if (!q) return;
    const el  = document.getElementById('pkg-results');
    if (!mgr.search) {
      el.innerHTML = `<div class="text-xs text-zinc-500 py-2">${mgr.label} araması desteklenmiyor — komutu elle yazın.</div>`;
      document.getElementById('pkg-cmd-inp').value = `${mgr.install} ${q}`;
      return;
    }
    el.innerHTML = '<div class="text-xs text-zinc-600 py-2">Aranıyor…</div>';
    const d = await fetch(`/api/pkgs/search?q=${encodeURIComponent(q)}&mgr=${mgr.id}`).then(r=>r.json());
    if (!d.ok) { el.innerHTML = `<div class="text-xs text-red-400 py-2">${d.error}</div>`; return; }
    if (!d.results.length) { el.innerHTML = '<div class="text-xs text-zinc-600 py-2">Sonuç bulunamadı</div>'; return; }
    el.innerHTML = d.results.map(line => {
      const [name, ...rest] = line.split(/\s+[-–:]\s*/);
      const desc = rest.join(' ') || '';
      return `<div class="flex items-start gap-2 py-1.5 border-b border-zinc-800">
        <button onclick="PkgMgr.fill('${name.trim().replace(/'/g,'\\\'')}')"
          class="shrink-0 px-2 py-0.5 text-[10px] bg-primary/20 text-primary rounded font-mono mt-0.5">+</button>
        <div>
          <div class="text-xs font-semibold text-zinc-200 font-mono">${name.trim()}</div>
          ${desc ? `<div class="text-[10px] text-zinc-500">${desc.slice(0,120)}</div>` : ''}
        </div>
      </div>`;
    }).join('');
  }
  function fill(pkg) {
    const mgr = _getMgr();
    document.getElementById('pkg-cmd-inp').value = `${mgr.install} ${pkg}`;
  }
  function runCmd() {
    const cmd = document.getElementById('pkg-cmd-inp').value.trim();
    if (!cmd) return;
    const t = getTerminal(state.activeTermId);
    if (t) { t.sendCmd(cmd); close(); switchMobileTab('terminals'); }
    else showToast('Önce bir terminal açın', 'error');
  }
  return { open, close, search, onInput, fill, runCmd, applyOsInfo, getShell: () => _shell };
})();


/* ═══════════════════════════════════════════════════════════════════
   MODULE: Task Scheduler
══════════════════════════════════════════════════════════════════ */
const Scheduler = (() => {
  function open() {
    document.getElementById('sched-bd').classList.add('open');
    document.getElementById('sched-panel').classList.add('open');
    refresh();
  }
  function close() {
    document.getElementById('sched-bd').classList.remove('open');
    document.getElementById('sched-panel').classList.remove('open');
    cancelAdd();
  }
  async function refresh() {
    const d = await fetch('/api/jobs').then(r => r.json());
    const list = document.getElementById('sched-list');
    const empty = document.getElementById('sched-empty');
    if (!d.jobs.length) { list.innerHTML = ''; empty.style.display = 'block'; return; }
    empty.style.display = 'none';
    list.innerHTML = d.jobs.map(j => `
      <div class="bg-zinc-900 border border-zinc-700 rounded-xl p-3">
        <div class="flex items-start gap-2">
          <div class="flex-1 min-w-0">
            <div class="text-sm font-semibold text-zinc-200">${j.name}</div>
            <div class="text-xs font-mono text-zinc-500 mt-0.5 truncate">${j.cmd}</div>
            <div class="flex items-center gap-2 mt-1">
              <span class="text-[10px] text-zinc-600">${j.schedule}</span>
              <span class="text-[10px] text-zinc-600">Son: ${j.last_run}</span>
            </div>
          </div>
          <div class="flex items-center gap-1.5 shrink-0">
            <button onclick="Scheduler.runNow('${j.id}')" class="px-2 py-1 text-[10px] bg-zinc-800 hover:bg-zinc-700 rounded text-zinc-300">▶</button>
            <button onclick="Scheduler.toggle('${j.id}')"
              class="px-2 py-1 text-[10px] rounded font-medium ${j.enabled ? 'bg-emerald-900/40 text-emerald-400' : 'bg-zinc-800 text-zinc-500'}">${j.enabled ? 'Açık' : 'Kapalı'}</button>
            <button onclick="Scheduler.del('${j.id}')" class="px-1.5 py-1 text-[10px] bg-red-900/30 text-red-400 rounded">×</button>
          </div>
        </div>
        ${j.output ? `<pre class="mt-2 text-[10px] text-zinc-500 font-mono bg-zinc-950 rounded p-2 overflow-x-auto whitespace-pre-wrap">${j.output.slice(0,300)}</pre>` : ''}
      </div>`).join('');
  }
  function showAdd() {
    document.getElementById('sched-add-form').style.display = 'flex';
    setTimeout(() => document.getElementById('sched-inp-name').focus(), 50);
  }
  function cancelAdd() {
    document.getElementById('sched-add-form').style.display = 'none';
    ['sched-inp-name','sched-inp-cmd','sched-inp-sched'].forEach(id => {
      const el = document.getElementById(id); if(el) el.value = '';
    });
  }
  async function saveAdd() {
    const name     = document.getElementById('sched-inp-name').value.trim();
    const cmd      = document.getElementById('sched-inp-cmd').value.trim();
    const schedule = document.getElementById('sched-inp-sched').value.trim() || 'every 1h';
    if (!cmd) { document.getElementById('sched-inp-cmd').style.borderColor = '#ef4444'; return; }
    await fetch('/api/jobs', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name: name||cmd, cmd, schedule})});
    cancelAdd(); await refresh(); showToast('Görev eklendi');
  }
  async function toggle(jid) {
    await fetch(`/api/jobs/${jid}/toggle`, {method:'PATCH'});
    refresh();
  }
  async function del(jid) {
    await fetch(`/api/jobs/${jid}`, {method:'DELETE'});
    showToast('Görev silindi'); refresh();
  }
  async function runNow(jid) {
    showToast('Çalıştırılıyor…');
    const d = await fetch(`/api/jobs/${jid}/run_now`, {method:'POST'}).then(r => r.json());
    if (d.ok) { showToast('Görev tamamlandı'); refresh(); }
    else showToast(d.error || 'Hata', 'error');
  }
  return { open, close, refresh, showAdd, cancelAdd, saveAdd, toggle, del, runNow };
})();


/* ═══════════════════════════════════════════════════════════════════
   MODULE: File Sync
══════════════════════════════════════════════════════════════════ */
const FileSync = (() => {
  function open() {
    document.getElementById('sync-bd').classList.add('open');
    document.getElementById('sync-panel').classList.add('open');
  }
  function close() {
    document.getElementById('sync-bd').classList.remove('open');
    document.getElementById('sync-panel').classList.remove('open');
  }
  async function run(dry) {
    const src = document.getElementById('sync-src').value.trim();
    const dst = document.getElementById('sync-dst').value.trim();
    if (!src || !dst) { showToast('Kaynak ve hedef gerekli', 'error'); return; }
    const out = document.getElementById('sync-output');
    out.classList.remove('hidden');
    out.textContent = dry ? 'Simüle ediliyor…' : 'Senkronize ediliyor…';
    const d = await fetch('/api/sync', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({src, dst, dry})}).then(r => r.json());
    out.textContent = d.output || d.error || '(çıktı yok)';
    if (d.ok) showToast(dry ? 'Simülasyon tamamlandı' : 'Senkronizasyon tamamlandı');
    else showToast(d.error || 'Hata', 'error');
  }
  return { open, close, run };
})();


/* ═══════════════════════════════════════════════════════════════════
   MODULE: Screen Viewer (screenshot + live stream + remote control)
══════════════════════════════════════════════════════════════════ */
const ScreenViewer = (() => {
  let _ws = null, _ctrl = false, _scaleX = 1, _scaleY = 1, _realW = 1920, _realH = 1080;
  let _frames = 0, _fpsTimer = null;
  let _recActive = false;
  // Performans presetleri: fps değerleri
  const PERF = { fast: 15, balanced: 5, quality: 3, saver: 1 };
  let _curFps = 5, _curMonitor = 0, _curPerf = 'balanced';

  function close() {
    document.getElementById('screen-modal').classList.remove('open');
    if (_ws) { try { _ws.close(); } catch(_){} _ws = null; }
    if (_fpsTimer) { clearInterval(_fpsTimer); _fpsTimer = null; }
  }

  function _showErr(msg) {
    const el = document.getElementById('screen-err');
    const lines = msg.split('\n');
    el.innerHTML = lines.map((l,i) => {
      if (i===0) return `<div style="color:#f87171;font-weight:600;font-size:13px;font-family:'JetBrains Mono',monospace;text-align:center">${l}</div>`;
      if (l.startsWith('sudo apt')) return `<code style="display:block;background:#1a1a2e;color:#86efac;font-family:'JetBrains Mono',monospace;font-size:12px;padding:8px 14px;border-radius:8px;border:1px solid #166534;white-space:pre">${l}</code>`;
      return l ? `<div style="color:#a1a1aa;font-size:11px;text-align:center">${l}</div>` : '';
    }).join('');
  }

  // Monitör listesini dropdown'a doldur
  async function _loadMonitors() {
    try {
      const d = await fetch('/api/monitors').then(r => r.json());
      const sel = document.getElementById('screen-monitor-sel');
      sel.innerHTML = '<option value="0">🖥 Tüm Ekran</option>';
      (d.monitors || []).forEach((m, i) => {
        const opt = document.createElement('option');
        opt.value = i + 1;
        opt.textContent = `${m.primary ? '★ ' : ''}${m.name} (${m.w}×${m.h})`;
        sel.appendChild(opt);
      });
    } catch(_) {}
  }

  function setMonitor(idx) {
    _curMonitor = idx;
    if (_ws) _restartStream();  // monitör değişince stream'i yeniden başlat
  }

  function setPerf(mode) {
    _curPerf = mode;
    _curFps  = PERF[mode] || 5;
    if (_ws) _restartStream();
  }

  async function screenshot() {
    showToast('Ekran alınıyor…');
    document.getElementById('screen-modal-title').textContent = 'Ekran Görüntüsü';
    document.getElementById('screen-err').innerHTML = '';
    document.getElementById('screen-modal').classList.add('open');
    _ctrl = false;
    document.getElementById('screen-ctrl-btn').textContent = 'Kontrol: Kapalı';
    document.getElementById('screen-ctrl-bar').classList.add('hidden');
    await _loadMonitors();
    const d = await fetch(`/api/screenshot?monitor=${_curMonitor}`).then(r => r.json());
    if (!d.ok) { _showErr(d.error); return; }
    const img = document.getElementById('screen-img');
    img.src = 'data:image/png;base64,' + d.data;
    img.onload = () => { _realW = img.naturalWidth; _realH = img.naturalHeight; };
  }

  async function openLive(withControl = false) {
    const img   = document.getElementById('screen-img');
    const errEl = document.getElementById('screen-err');
    document.getElementById('screen-modal-title').textContent = 'Canlı Ekran & Kontrol';
    errEl.innerHTML = '';
    img.src = '';
    document.getElementById('screen-modal').classList.add('open');

    _ctrl = withControl;
    document.getElementById('screen-ctrl-btn').textContent = `Kontrol: ${_ctrl ? 'Açık ✓' : 'Kapalı'}`;
    document.getElementById('screen-ctrl-bar').classList.toggle('hidden', !_ctrl);

    errEl.innerHTML = '<div style="color:#a1a1aa;font-size:12px">Durum kontrol ediliyor…</div>';
    let st;
    try { st = await fetch('/api/screen_status').then(r => r.json()); }
    catch(e) { _showErr('Sunucuya bağlanılamadı'); return; }

    if (!st.capture_ready) {
      const cmd = 'sudo apt install -y scrot xdotool xclip tesseract-ocr tesseract-ocr-tur';
      errEl.innerHTML = `
        <div style="background:#111;border:1px solid #3f3f46;border-radius:12px;padding:20px 22px;max-width:400px;display:flex;flex-direction:column;gap:12px;">
          <div style="color:#f87171;font-weight:700;font-size:14px;">⚠️ Ekran araçları kurulu değil</div>
          <div style="color:#a1a1aa;font-size:12px;line-height:1.8;">
            DISPLAY: <code style="color:#fde047">${st.display || st.real_display || 'yok'}</code><br>
            ${Object.entries(st.tools).map(([k,v])=>`<span style="color:${v?'#86efac':'#f87171'}">${v?'✓':'✗'} ${k}</span>`).join('  ')}
          </div>
          <code style="display:block;background:#0d1117;color:#86efac;font-family:'JetBrains Mono',monospace;font-size:11px;padding:10px;border-radius:8px;border:1px solid #166534;white-space:pre-wrap;user-select:all">${cmd}</code>
          <button onclick="navigator.clipboard.writeText('${cmd}').then(()=>showToast('Kopyalandı'))"
            style="padding:8px;background:#166534;color:#86efac;border:none;border-radius:8px;font-size:12px;cursor:pointer;">📋 Komutu Kopyala</button>
        </div>`;
      return;
    }

    if (_ctrl && !st.control_ready) {
      showToast('xdotool kurulu değil — sadece izleme modu', 'error');
      _ctrl = false;
      document.getElementById('screen-ctrl-btn').textContent = 'Kontrol: Kapalı';
      document.getElementById('screen-ctrl-bar').classList.add('hidden');
    }

    errEl.innerHTML = '';
    await _loadMonitors();
    _startStream();
    _setupImgEvents(img);
  }

  function _startStream() {
    if (_ws) { try { _ws.close(); } catch(_){} _ws = null; }
    const img   = document.getElementById('screen-img');
    const errEl = document.getElementById('screen-err');
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    _ws = new WebSocket(`${proto}://${location.host}/ws/screen`);
    _ws.onopen = () => {
      // İlk mesaj olarak config gönder
      _ws.send(JSON.stringify({ fps: _curFps, monitor: _curMonitor }));
    };
    _ws.onmessage = ev => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'frame') {
          img.src = 'data:image/png;base64,' + msg.data;
          img.onload = () => { _realW = img.naturalWidth; _realH = img.naturalHeight; _updateScale(); };
          errEl.innerHTML = '';
          _frames++;
        } else if (msg.type === 'error') {
          _showErr(msg.msg);
        }
      } catch(_) {}
    };
    _ws.onclose = () => {
      if (_fpsTimer) { clearInterval(_fpsTimer); _fpsTimer = null; }
    };
    _frames = 0;
    if (_fpsTimer) clearInterval(_fpsTimer);
    _fpsTimer = setInterval(() => {
      document.getElementById('screen-fps').textContent = `${_frames} fps`;
      _frames = 0;
    }, 1000);
  }

  function _restartStream() {
    _startStream();
  }

  function _updateScale() {
    const img = document.getElementById('screen-img');
    const r = img.getBoundingClientRect();
    if (r.width && r.height && _realW && _realH) {
      _scaleX = _realW / r.width;
      _scaleY = _realH / r.height;
    }
  }

  function _inp(body) {
    fetch('/api/input', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
      .then(r=>r.json()).then(d=>{ if(!d.ok && d.error) showToast(d.error,'error'); }).catch(()=>{});
  }

  function _setupImgEvents(img) {
    img.onmousemove = ev => {
      if (!_ctrl) return;
      _updateScale();
      const r = img.getBoundingClientRect();
      _inp({action:'move', x:Math.round((ev.clientX-r.left)*_scaleX), y:Math.round((ev.clientY-r.top)*_scaleY)});
    };
    img.onclick = ev => {
      if (!_ctrl) return;
      _updateScale();
      const r = img.getBoundingClientRect();
      _inp({action:'click', x:Math.round((ev.clientX-r.left)*_scaleX), y:Math.round((ev.clientY-r.top)*_scaleY), button:1});
    };
    img.ondblclick = ev => {
      if (!_ctrl) return;
      const r = img.getBoundingClientRect();
      _inp({action:'dblclick', x:Math.round((ev.clientX-r.left)*_scaleX), y:Math.round((ev.clientY-r.top)*_scaleY), button:1});
    };
    img.oncontextmenu = ev => {
      if (!_ctrl) return; ev.preventDefault();
      const r = img.getBoundingClientRect();
      _inp({action:'click', x:Math.round((ev.clientX-r.left)*_scaleX), y:Math.round((ev.clientY-r.top)*_scaleY), button:3});
    };
    img.onwheel = ev => {
      if (!_ctrl) return; ev.preventDefault();
      _inp({action:'scroll', dir: ev.deltaY < 0 ? 'up' : 'down'});
    };
    // Touch (mobil)
    let _lastTap = 0, _touchMoveTimer = null;
    img.ontouchstart = ev => { if (!_ctrl) return; ev.preventDefault(); };
    img.ontouchmove  = ev => {
      if (!_ctrl) return; ev.preventDefault();
      const t = ev.touches[0];
      const r = img.getBoundingClientRect();
      if (_touchMoveTimer) clearTimeout(_touchMoveTimer);
      _touchMoveTimer = setTimeout(() => {
        _inp({action:'move', x:Math.round((t.clientX-r.left)*_scaleX), y:Math.round((t.clientY-r.top)*_scaleY)});
      }, 30);
    };
    img.ontouchend = ev => {
      if (!_ctrl) return; ev.preventDefault();
      const t = ev.changedTouches[0];
      const r = img.getBoundingClientRect();
      const x = Math.round((t.clientX-r.left)*_scaleX);
      const y = Math.round((t.clientY-r.top)*_scaleY);
      const now = Date.now();
      if (now - _lastTap < 300) { _inp({action:'dblclick', x, y, button:1}); _lastTap=0; return; }
      _lastTap = now;
      _inp({action:'move', x, y});
      setTimeout(() => { if (Date.now() - _lastTap >= 290) _inp({action:'click', x, y, button:1}); }, 310);
    };
  }

  function toggleControl() {
    _ctrl = !_ctrl;
    document.getElementById('screen-ctrl-btn').textContent = `Kontrol: ${_ctrl ? 'Açık ✓' : 'Kapalı'}`;
    document.getElementById('screen-ctrl-bar').classList.toggle('hidden', !_ctrl);
  }

  function sendKey(key) {
    fetch('/api/input', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'key', key})});
  }

  function typeText(text) {
    if (!text) return;
    fetch('/api/input', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'type', text})});
  }

  // ── Recording ────────────────────────────────────────────────────────────
  async function toggleRecording() {
    const btn = document.getElementById('screen-rec-btn');
    const dot = document.getElementById('screen-rec-dot');
    if (_recActive) {
      const d = await fetch('/api/recording/stop', {method:'POST'}).then(r=>r.json());
      _recActive = false;
      dot.style.background = '#52525b';
      btn.style.color = '';
      if (d.ok) {
        const mb = (d.size / 1048576).toFixed(1);
        showToast(`Kayıt durduruldu (${mb} MB)`);
        // İndir
        setTimeout(() => {
          const a = document.createElement('a');
          a.href = '/api/recording/download';
          a.download = 'kayit.mp4';
          a.click();
        }, 800);
      } else {
        showToast(d.error || 'Kayıt durdurulamadı', 'error');
      }
    } else {
      const fps = _curPerf === 'fast' ? 20 : _curPerf === 'quality' ? 8 : _curPerf === 'saver' ? 3 : 10;
      const d = await fetch('/api/recording/start', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({fps})
      }).then(r=>r.json());
      if (d.ok) {
        _recActive = true;
        dot.style.background = '#ef4444';
        btn.style.color = '#ef4444';
        showToast('Kayıt başladı 🔴');
      } else {
        showToast(d.error || 'Kayıt başlatılamadı', 'error');
      }
    }
  }

  // ── Clipboard ────────────────────────────────────────────────────────────
  function openClipboard() {
    document.getElementById('clip-panel').style.display = 'flex';
    clipboardRead();
  }
  function closeClipboard() {
    document.getElementById('clip-panel').style.display = 'none';
  }
  async function clipboardRead() {
    const d = await fetch('/api/clipboard').then(r=>r.json());
    document.getElementById('clip-text').value = d.text || '';
    showToast('Pano okundu');
  }
  async function clipboardWrite() {
    const text = document.getElementById('clip-text').value;
    const d = await fetch('/api/clipboard', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text})
    }).then(r=>r.json());
    if (d.ok) showToast('Panoya gönderildi ✓');
    else showToast(d.error || 'Hata', 'error');
  }

  // ── OCR ──────────────────────────────────────────────────────────────────
  async function runOCR() {
    const panel = document.getElementById('ocr-panel');
    const status = document.getElementById('ocr-status');
    const txt    = document.getElementById('ocr-text');
    panel.style.display = 'flex';
    status.textContent = 'Tesseract ile ekran taranıyor…';
    txt.value = '';
    showToast('OCR çalışıyor…');
    const d = await fetch('/api/ocr', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({monitor: _curMonitor})
    }).then(r=>r.json());
    if (d.ok) {
      txt.value = d.text;
      status.textContent = `${d.text.length} karakter tanındı`;
      showToast('OCR tamamlandı ✓');
    } else {
      status.textContent = d.error || 'Tanıma başarısız';
      showToast(d.error || 'OCR hatası', 'error');
    }
  }

  return {
    close, screenshot, openLive, toggleControl, sendKey, typeText,
    toggleRecording, openClipboard, closeClipboard, clipboardRead, clipboardWrite,
    runOCR, setMonitor, setPerf,
  };
})();


/* ═══════════════════════════════════════════════════════════════════
   MODULE: Wake-on-LAN
══════════════════════════════════════════════════════════════════ */
const WoL = (() => {
  let _panel = null;
  function _getPanel() {
    if (_panel) return _panel;
    _panel = document.createElement('div');
    _panel.style.cssText = 'position:fixed;inset:0;z-index:210;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;';
    _panel.innerHTML = `
      <div style="background:#141417;border:1px solid #27272a;border-radius:18px;padding:24px;width:min(90vw,360px);display:flex;flex-direction:column;gap:14px;">
        <div style="display:flex;align-items:center;justify-content:space-between;">
          <span style="font-weight:700;color:#fff;font-size:15px;">⚡ Wake-on-LAN</span>
          <button id="wol-close-btn" style="background:none;border:none;color:#71717a;font-size:20px;cursor:pointer;">×</button>
        </div>
        <div style="color:#a1a1aa;font-size:12px;line-height:1.5;">Uyandırmak istediğiniz cihazın MAC adresini girin. Cihaz aynı yerel ağda olmalı.</div>
        <input id="wol-mac-inp" type="text" placeholder="AA:BB:CC:DD:EE:FF"
          style="background:#09090b;border:1px solid #27272a;border-radius:8px;color:#e4e4e7;padding:10px;font-family:'JetBrains Mono',monospace;font-size:13px;outline:none;width:100%;box-sizing:border-box;">
        <div style="color:#71717a;font-size:11px;">LAN taramasından bulunan cihazların MAC'ini kullanabilirsiniz.</div>
        <button id="wol-send-btn"
          style="padding:10px;background:#2563eb;color:#fff;border:none;border-radius:8px;font-size:13px;cursor:pointer;font-weight:600;">
          ⚡ Sihirli Paket Gönder
        </button>
        <div id="wol-result" style="font-size:12px;text-align:center;min-height:16px;"></div>
      </div>`;
    document.body.appendChild(_panel);
    _panel.querySelector('#wol-close-btn').onclick = close;
    _panel.onclick = e => { if (e.target === _panel) close(); };
    _panel.querySelector('#wol-send-btn').onclick = send;
    return _panel;
  }
  function open() {
    _getPanel().style.display = 'flex';
    _getPanel().querySelector('#wol-result').textContent = '';
  }
  function close() { _getPanel().style.display = 'none'; }
  async function send() {
    const mac = _getPanel().querySelector('#wol-mac-inp').value.trim();
    const res = _getPanel().querySelector('#wol-result');
    if (!mac) { res.style.color='#f87171'; res.textContent='MAC adresi girin'; return; }
    res.style.color='#a1a1aa'; res.textContent='Gönderiliyor…';
    const d = await fetch('/api/wol', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({mac})
    }).then(r=>r.json());
    if (d.ok) {
      res.style.color='#86efac'; res.textContent='✓ Sihirli paket gönderildi!';
      showToast('Wake-on-LAN paketi gönderildi ✓');
    } else {
      res.style.color='#f87171'; res.textContent=d.error||'Hata';
    }
  }
  return { open, close };
})();


/* ═══════════════════════════════════════════════════════════════════
   MODULE: LAN Scan
══════════════════════════════════════════════════════════════════ */
const LANScan = (() => {
  let _panel = null;
  function _getPanel() {
    if (_panel) return _panel;
    _panel = document.createElement('div');
    _panel.style.cssText = 'position:fixed;inset:0;z-index:210;background:rgba(0,0,0,.7);display:none;align-items:flex-end;justify-content:center;';
    _panel.innerHTML = `
      <div style="background:#141417;border:1px solid #27272a;border-radius:18px 18px 0 0;padding:20px;width:100%;max-width:560px;max-height:80vh;display:flex;flex-direction:column;gap:12px;">
        <div style="display:flex;align-items:center;justify-content:space-between;">
          <span style="font-weight:700;color:#fff;font-size:15px;">🔍 Yerel Ağ Tarama</span>
          <button id="lan-close-btn" style="background:none;border:none;color:#71717a;font-size:20px;cursor:pointer;">×</button>
        </div>
        <div style="color:#a1a1aa;font-size:12px;">/proc/net/arp + arp-scan ile yerel ağdaki aktif cihazları listeler.</div>
        <button id="lan-scan-btn" style="padding:9px;background:#16a34a;color:#fff;border:none;border-radius:8px;font-size:12px;cursor:pointer;font-weight:600;">🔍 Tara</button>
        <div id="lan-list" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:6px;min-height:80px;"></div>
      </div>`;
    document.body.appendChild(_panel);
    _panel.querySelector('#lan-close-btn').onclick = close;
    _panel.onclick = e => { if (e.target === _panel) close(); };
    _panel.querySelector('#lan-scan-btn').onclick = scan;
    return _panel;
  }
  function open() { _getPanel().style.display = 'flex'; }
  function close() { _getPanel().style.display = 'none'; }
  async function scan() {
    const btn  = _getPanel().querySelector('#lan-scan-btn');
    const list = _getPanel().querySelector('#lan-list');
    btn.textContent = '⏳ Taranıyor…'; btn.disabled = true;
    list.innerHTML = '<div style="color:#71717a;font-size:12px;">Tarama yapılıyor…</div>';
    try {
      const d = await fetch('/api/lan_scan').then(r => r.json());
      if (!d.ok || !d.devices.length) {
        list.innerHTML = '<div style="color:#a1a1aa;font-size:12px;">Cihaz bulunamadı. arp-scan kurarak daha iyi sonuç alabilirsiniz.</div>';
      } else {
        list.innerHTML = d.devices.map(dev => `
          <div style="background:#1c1c1f;border:1px solid #27272a;border-radius:10px;padding:10px 12px;display:flex;align-items:center;gap:10px;">
            <div style="flex:1;min-width:0;">
              <div style="color:#e4e4e7;font-size:12px;font-family:'JetBrains Mono',monospace;">${dev.ip}</div>
              <div style="color:#71717a;font-size:11px;">${dev.mac} ${dev.hostname ? '· ' + dev.hostname : ''}</div>
            </div>
            <button onclick="WoL.open();document.getElementById('wol-mac-inp')?document.getElementById('wol-mac-inp').value='${dev.mac}':null"
              style="padding:5px 10px;background:#27272a;color:#a1a1aa;border:none;border-radius:6px;font-size:11px;cursor:pointer;" title="WoL ile uyandır">⚡ WoL</button>
            <button onclick="Devices.addFromLan('${dev.ip}','${dev.hostname||dev.ip}')"
              style="padding:5px 10px;background:#2563eb;color:#fff;border:none;border-radius:6px;font-size:11px;cursor:pointer;" title="Cihaz listesine ekle">+ Ekle</button>
          </div>`).join('');
      }
    } catch(e) {
      list.innerHTML = `<div style="color:#f87171;font-size:12px;">Hata: ${e.message}</div>`;
    }
    btn.textContent = '🔄 Yeniden Tara'; btn.disabled = false;
  }
  return { open, close, scan };
})();


/* ═══════════════════════════════════════════════════════════════════
   MODULE: Share Link
══════════════════════════════════════════════════════════════════ */
const ShareLink = (() => {
  let _path = '', _name = '';
  function open(path, name) {
    _path = path; _name = name;
    document.getElementById('share-filename').textContent = name;
    document.getElementById('share-link-box').classList.add('hidden');
    document.getElementById('share-modal').classList.add('open');
  }
  function close() {
    document.getElementById('share-modal').classList.remove('open');
  }
  async function generate() {
    const hours = parseInt(document.getElementById('share-hours').value);
    const d = await fetch('/api/share', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: _path, hours})}).then(r => r.json());
    if (!d.ok) { showToast(d.error || 'Hata', 'error'); return; }
    const url = `${location.origin}/api/shared/${d.token}`;
    document.getElementById('share-link-inp').value = url;
    document.getElementById('share-link-box').classList.remove('hidden');
    showToast(`Link oluşturuldu (${hours} saat)`);
  }
  function copy() {
    const inp = document.getElementById('share-link-inp');
    navigator.clipboard?.writeText(inp.value).then(() => showToast('Kopyalandı!'));
  }
  return { open, close, generate, copy };
})();


/* ═══════════════════════════════════════════════════════════════════
   MODULE: System Notifications
══════════════════════════════════════════════════════════════════ */
const Notifs = (() => {
  let _thresh = {cpu: 85, ram: 90, disk: 95};
  let _granted = false;
  let _alerted  = {cpu: false, ram: false, disk: false};

  function requestPermission() {
    if (!('Notification' in window)) { showToast('Bu tarayıcı bildirimleri desteklemiyor', 'error'); return; }
    Notification.requestPermission().then(perm => {
      _granted = perm === 'granted';
      showToast(_granted ? '✅ Bildirimler açık' : 'Bildirim izni verilmedi', _granted ? undefined : 'error');
      document.getElementById('notif-nav-badge').classList.toggle('on', !_granted);
    });
  }
  function openSettings() {
    document.getElementById('thresh-cpu').value  = _thresh.cpu;
    document.getElementById('thresh-ram').value  = _thresh.ram;
    document.getElementById('thresh-disk').value = _thresh.disk;
    document.getElementById('notif-bd').classList.add('open');
    document.getElementById('notif-panel').classList.add('open');
  }
  function closeSettings() {
    document.getElementById('notif-bd').classList.remove('open');
    document.getElementById('notif-panel').classList.remove('open');
  }
  async function saveSettings() {
    const cpu  = parseInt(document.getElementById('thresh-cpu').value);
    const ram  = parseInt(document.getElementById('thresh-ram').value);
    const disk = parseInt(document.getElementById('thresh-disk').value);
    _thresh = {cpu, ram, disk};
    await fetch('/api/notif_thresh', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({cpu, ram, disk})});
    closeSettings(); showToast('Eşikler kaydedildi');
    _alerted = {cpu: false, ram: false, disk: false};
  }
  function _notify(title, body) {
    if (!_granted) return;
    try { new Notification(title, {body, icon: '/favicon.ico'}); } catch(_) {}
  }
  function check(stats) {
    if (!_granted) return;
    if (stats.cpu_percent >= _thresh.cpu && !_alerted.cpu) {
      _notify('⚠️ Yüksek CPU', `CPU kullanımı: ${stats.cpu_percent}% (eşik: ${_thresh.cpu}%)`);
      _alerted.cpu = true;
    } else if (stats.cpu_percent < _thresh.cpu - 5) {
      _alerted.cpu = false;
    }
    if (stats.ram_percent >= _thresh.ram && !_alerted.ram) {
      _notify('⚠️ Yüksek RAM', `RAM kullanımı: ${stats.ram_percent}% (eşik: ${_thresh.ram}%)`);
      _alerted.ram = true;
    } else if (stats.ram_percent < _thresh.ram - 5) {
      _alerted.ram = false;
    }
    if (stats.disk_percent >= _thresh.disk && !_alerted.disk) {
      _notify('⚠️ Yüksek Disk', `Disk kullanımı: ${stats.disk_percent}% (eşik: ${_thresh.disk}%)`);
      _alerted.disk = true;
    } else if (stats.disk_percent < _thresh.disk - 5) {
      _alerted.disk = false;
    }
  }
  // auto-grant if already permitted
  if ('Notification' in window && Notification.permission === 'granted') _granted = true;
  return { requestPermission, openSettings, closeSettings, saveSettings, check };
})();


/* ═══════════════════════════════════════════════════════════════════
   MODULE: Plugins (module/feature toggles)
══════════════════════════════════════════════════════════════════ */
const Plugins = (() => {
  const MODS = [
    {id:'autocomplete', name:'Otomatik Tamamlama', desc:'Terminal yazarken komut önerileri gösterir.', default:true},
    {id:'notif_monitor', name:'Sistem Bildirimleri', desc:'Eşik aşıldığında uyarı gönderir.', default:true},
    {id:'hscroll', name:'Yatay Kaydırma Çubuğu', desc:'Terminal altında yatay kaydırma barı.', default:true},
    {id:'dashboard', name:'Dashboard Şeridi', desc:'CPU/RAM/Disk anlık gösterimi.', default:true},
    {id:'dpad', name:'D-Pad Yön Tuşları', desc:'Dokunmatik yön kontrolü.', default:false},
  ];
  function _get(id) { const v = localStorage.getItem('plugin_' + id); return v === null ? MODS.find(m=>m.id===id)?.default : v==='1'; }
  function _set(id, val) { localStorage.setItem('plugin_' + id, val ? '1' : '0'); }

  function open() {
    document.getElementById('plugins-bd').classList.add('open');
    document.getElementById('plugins-panel').classList.add('open');
    render();
  }
  function close() {
    document.getElementById('plugins-bd').classList.remove('open');
    document.getElementById('plugins-panel').classList.remove('open');
  }
  function render() {
    const el = document.getElementById('plugins-list');
    el.innerHTML = MODS.map(m => {
      const on = _get(m.id);
      return `<div class="flex items-start gap-3 py-3 border-b border-zinc-800 last:border-0">
        <div class="flex-1 min-w-0">
          <div class="text-sm font-semibold text-zinc-200">${m.name}</div>
          <div class="text-xs text-zinc-500 mt-0.5">${m.desc}</div>
        </div>
        <button onclick="Plugins.toggle('${m.id}')" id="plug-${m.id}-btn"
          class="w-11 h-6 rounded-full transition-colors relative flex items-center px-0.5 shrink-0 ${on ? 'bg-blue-600' : 'bg-zinc-700'}">
          <span id="plug-${m.id}-knob" class="w-5 h-5 rounded-full bg-white shadow block transition-transform"
            style="transform:${on ? 'translateX(20px)' : 'translateX(0)'}"></span>
        </button>
      </div>`;
    }).join('');
  }
  function toggle(id) {
    const val = !_get(id);
    _set(id, val);
    const btn  = document.getElementById(`plug-${id}-btn`);
    const knob = document.getElementById(`plug-${id}-knob`);
    if (btn)  btn.className  = btn.className.replace(val ? 'bg-zinc-700' : 'bg-blue-600', val ? 'bg-blue-600' : 'bg-zinc-700');
    if (knob) knob.style.transform = val ? 'translateX(20px)' : 'translateX(0)';
    showToast(`${MODS.find(m=>m.id===id)?.name}: ${val ? 'Açık' : 'Kapalı'}`);
  }
  function isOn(id) { return _get(id); }
  return { open, close, toggle, render, isOn };
})();


/* ═══════════════════════════════════════════════════════════════════
   MODULE: Autocomplete (command suggestions bar)
══════════════════════════════════════════════════════════════════ */
const Autocomplete = (() => {
  const _hist = [];  // command history
  let _timer = null;

  function _addHistory(cmd) {
    const c = cmd.trim();
    if (!c) return;
    const idx = _hist.indexOf(c);
    if (idx !== -1) _hist.splice(idx, 1);
    _hist.unshift(c);
    if (_hist.length > 100) _hist.pop();
  }

  function hookTerminal(t) {
    // Track submitted commands
    const origSendCmd = t.sendCmd.bind(t);
    t.sendCmd = function(cmd) { _addHistory(cmd); origSendCmd(cmd); };

    // Also watch raw Enter presses in the buffer
    let _buf = '';
    t.term.onData(data => {
      if (!Plugins.isOn('autocomplete')) return;
      if (data === '\r' || data === '\n') {
        if (_buf.trim()) _addHistory(_buf);
        _buf = '';
        hideFor(t.id);
      } else if (data === '\x7f') {
        _buf = _buf.slice(0, -1);
        schedFetch(t.id, _buf);
      } else if (data.length === 1 && data.charCodeAt(0) >= 32) {
        _buf += data;
        schedFetch(t.id, _buf);
      }
    });
  }

  function schedFetch(termId, q) {
    clearTimeout(_timer);
    const word = q.trim().split(/\s+/).pop();
    if (!word || word.length < 2) { hideFor(termId); return; }
    _timer = setTimeout(() => _fetchAndShow(termId, q, word), 350);
  }

  async function _fetchAndShow(termId, line, word) {
    const bar = document.getElementById(`${termId}-ac-bar`);
    if (!bar) return;
    // history matches
    const histMatches = _hist.filter(h => h.includes(line.trim()) && h !== line.trim()).slice(0, 4);
    // remote completions
    let cmds = [];
    try {
      const d = await fetch(`/api/complete?q=${encodeURIComponent(word)}`).then(r => r.json());
      cmds = [...(d.commands || []), ...(d.files || [])].slice(0, 6);
    } catch(_) {}
    const all = [...new Set([...histMatches, ...cmds])].slice(0, 8);
    if (!all.length) { bar.classList.add('hidden'); return; }
    bar.innerHTML = all.map(s => {
      const isHistItem = histMatches.includes(s);
      return `<button class="ac-chip ${isHistItem ? 'border-zinc-600' : ''}" onclick="Autocomplete.pick('${termId}', ${JSON.stringify(s)})">${s.replace(/</g,'&lt;').slice(0,40)}</button>`;
    }).join('');
    bar.classList.remove('hidden');
  }

  function hideFor(termId) {
    const bar = document.getElementById(`${termId}-ac-bar`);
    if (bar) bar.classList.add('hidden');
  }

  function pick(termId, val) {
    const t = getTerminal(termId); if (!t) return;
    // Send Ctrl-U to clear line then type the picked value
    if (t.ws && t.ws.readyState === 1) {
      t.ws.send(JSON.stringify({type:'input', data: '\x15' + val}));
    }
    hideFor(termId);
  }

  return { hookTerminal, hideFor, pick };
})();

/* ═══════════════════════════════════════════════════════════════════
   MODULE: DevFS — aktif cihaza göre dosya sistemi soyutlama katmanı
   Tüm dosya yöneticisi işlemleri buradan geçer.
══════════════════════════════════════════════════════════════════ */
const DevFS = (() => {
  function _devId() {
    try { return Devices.activeId(); } catch(_) { return 'local'; }
  }
  const J = {method:'POST', headers:{'Content-Type':'application/json'}};

  async function ls(path) {
    const d = _devId();
    if (d === 'local') return fetch('/api/files?path=' + encodeURIComponent(path)).then(r=>r.json());
    return fetch(`/api/sftp/${d}/ls?path=` + encodeURIComponent(path)).then(r=>r.json());
  }

  async function readFile(path) {
    const d = _devId();
    if (d === 'local') return fetch('/api/read_file?path=' + encodeURIComponent(path)).then(r=>r.json());
    return fetch(`/api/sftp/${d}/read?path=` + encodeURIComponent(path)).then(r=>r.json());
  }

  async function writeFile(path, content) {
    const d = _devId();
    if (d === 'local') return fetch('/api/write_file', {...J, body:JSON.stringify({path,content})}).then(r=>r.json());
    return fetch(`/api/sftp/${d}/write`, {...J, body:JSON.stringify({path,content})}).then(r=>r.json());
  }

  async function rename(old_path, new_name) {
    const d = _devId();
    if (d === 'local') return fetch('/api/rename', {...J, body:JSON.stringify({old_path,new_name})}).then(r=>r.json());
    return fetch(`/api/sftp/${d}/rename`, {...J, body:JSON.stringify({old_path,new_name})}).then(r=>r.json());
  }

  async function del(path) {
    const d = _devId();
    if (d === 'local') return fetch('/api/delete', {...J, body:JSON.stringify({path})}).then(r=>r.json());
    return fetch(`/api/sftp/${d}/delete`, {...J, body:JSON.stringify({path})}).then(r=>r.json());
  }

  async function mkdir(parent_path, name) {
    const d = _devId();
    if (d === 'local') return fetch('/api/mkdir', {...J, body:JSON.stringify({parent_path,name})}).then(r=>r.json());
    return fetch(`/api/sftp/${d}/mkdir`, {...J, body:JSON.stringify({parent_path,name})}).then(r=>r.json());
  }

  async function newfile(parent_path, name) {
    const d = _devId();
    if (d === 'local') return fetch('/api/newfile', {...J, body:JSON.stringify({parent_path,name})}).then(r=>r.json());
    return fetch(`/api/sftp/${d}/newfile`, {...J, body:JSON.stringify({parent_path,name})}).then(r=>r.json());
  }

  return { ls, readFile, writeFile, rename, del, mkdir, newfile };
})();

/* ═══════════════════════════════════════════════════════════════════
   MODULE: Devices — çok cihaz yönetimi ve SSH terminal
══════════════════════════════════════════════════════════════════ */
const Devices = (() => {
  const COLORS = ['#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6','#06b6d4','#ec4899'];
  let _active = 'local';
  let _devs   = [];
  let _pingTimers = {};

  function open() {
    document.getElementById('dev-backdrop').classList.add('open');
    document.getElementById('dev-panel').classList.add('open');
    refresh();
  }
  function close() {
    document.getElementById('dev-backdrop').classList.remove('open');
    document.getElementById('dev-panel').classList.remove('open');
  }

  async function refresh() {
    const d = await fetch('/api/devices').then(r=>r.json());
    _devs = d.devices || [];
    render();
    _devs.forEach(dev => _pingDevice(dev));
    _loadTailscalePeers();
  }

  function render() {
    const list = document.getElementById('dev-list');
    const sysInfo = window._sysInfo || {};
    const localName = (sysInfo.distro || 'Bu Cihaz') + (sysInfo.version ? ' ' + sysInfo.version : '');

    // Local device card
    const localActive = _active === 'local';
    list.innerHTML = `
      <div class="dev-card ${localActive?'active':''}" onclick="Devices.setActive('local')" style="margin-bottom:4px">
        <div class="dev-dot" style="background:#3b82f6"></div>
        <div style="flex:1;min-width:0">
          <div style="font-size:13px;font-weight:600;color:#e4e4e7">${localName}</div>
          <div style="font-size:10px;color:#52525b;margin-top:1px">localhost · ${sysInfo.shell || '/bin/bash'}</div>
        </div>
        <div class="dev-status online"></div>
        ${localActive ? '<span style="font-size:9px;color:#3b82f6;font-weight:700">AKTİF</span>' : ''}
      </div>` +
      _devs.map(dev => {
        const isActive = _active === dev.id;
        const statusId = `dev-st-${dev.id}`;
        return `
        <div class="dev-card ${isActive?'active':''}" onclick="Devices.setActive('${dev.id}')" style="margin-bottom:4px">
          <div class="dev-dot" style="background:${dev.color}"></div>
          <div style="flex:1;min-width:0">
            <div style="font-size:13px;font-weight:600;color:#e4e4e7;display:flex;align-items:center;gap:6px">
              ${dev.name}
              <span class="dev-ssh-badge">SSH</span>
            </div>
            <div style="font-size:10px;color:#52525b;margin-top:1px">${dev.user}@${dev.host}:${dev.port}</div>
            ${dev.note ? `<div style="font-size:10px;color:#71717a">${dev.note}</div>` : ''}
          </div>
          <div style="display:flex;align-items:center;gap:6px">
            <div id="${statusId}" class="dev-status pinging" title="Erişilebilirlik kontrol ediliyor…"></div>
            <button onclick="event.stopPropagation();Devices.openSSH('${dev.id}')" title="SSH Terminal aç"
              style="padding:3px 7px;font-size:10px;background:rgba(99,102,241,.15);color:#a5b4fc;border:1px solid rgba(99,102,241,.3);border-radius:5px;cursor:pointer">SSH</button>
            <button onclick="event.stopPropagation();Devices.del('${dev.id}')" title="Sil"
              style="padding:3px 6px;font-size:11px;background:rgba(239,68,68,.1);color:#f87171;border:1px solid rgba(239,68,68,.2);border-radius:5px;cursor:pointer">×</button>
          </div>
        </div>`;
      }).join('');
  }

  async function _pingDevice(dev) {
    const el = document.getElementById(`dev-st-${dev.id}`);
    if (!el) return;
    el.className = 'dev-status pinging';
    const d = await fetch(`/api/devices/${dev.id}/ping`).then(r=>r.json()).catch(()=>({reachable:false}));
    if (el) el.className = `dev-status ${d.reachable ? 'online' : 'offline'}`;
    el && (el.title = d.reachable ? 'Erişilebilir' : (d.error || 'Erişilemiyor'));
  }

  function setActive(id) {
    _active = id;
    if (id === 'local') {
      document.getElementById('dev-active-dot').style.background = '#3b82f6';
      const sys = window._sysInfo || {};
      document.getElementById('dev-active-name').textContent = sys.distro || 'Bu Cihaz';
    } else {
      const dev = _devs.find(d => d.id === id);
      if (dev) {
        document.getElementById('dev-active-dot').style.background = dev.color;
        document.getElementById('dev-active-name').textContent = dev.name;
        openSSH(id);
      }
    }
    // Tüm açık dosya yöneticilerini uzak cihaz klasörüne bağla
    try {
      state.fileManagers.forEach(fm => {
        fm.path = id === 'local' ? '.' : '/';
        fm.fetchDir();
      });
    } catch(_) {}
    render();
    close();
  }

  async function _loadTailscalePeers() {
    const section = document.getElementById('dev-ts-section');
    if (!section) return;
    section.innerHTML = '<div style="font-size:10px;color:#52525b;padding:4px 4px 2px">Tailscale taranıyor…</div>';
    const d = await fetch('/api/tailscale/peers').then(r=>r.json()).catch(()=>({ok:false}));
    if (!d.ok || !d.peers.length) {
      section.innerHTML = `<div style="font-size:10px;color:#3f3f46;padding:4px;font-style:italic">
        ${d.error || 'Tailscale ağında cihaz bulunamadı'}</div>`;
      return;
    }
    section.innerHTML = `<div style="font-size:10px;color:#52525b;font-weight:600;padding:6px 4px 4px;text-transform:uppercase;letter-spacing:.05em">
        🌐 Tailscale Ağı</div>` +
      d.peers.map(p => {
        const online  = p.online;
        const already = _devs.find(dv => dv.host === p.ip || dv.host === p.dns_name);
        return `<div class="dev-card" style="margin-bottom:3px;opacity:${online?1:.5}">
          <div class="dev-dot" style="background:${online?'#6366f1':'#3f3f46'}"></div>
          <div style="flex:1;min-width:0">
            <div style="font-size:12px;font-weight:600;color:#c4c4cc">${p.hostname}</div>
            <div style="font-size:10px;color:#52525b">${p.ip} · ${p.os||'?'}</div>
          </div>
          <div style="display:flex;align-items:center;gap:5px">
            <div class="dev-status ${online?'online':'offline'}"></div>
            ${already
              ? `<span style="font-size:9px;color:#52525b">Ekli</span>`
              : `<button onclick="Devices._addFromTs('${p.hostname}','${p.ip}','${p.dns_name||p.ip}')"
                   style="padding:3px 7px;font-size:10px;background:rgba(99,102,241,.15);color:#a5b4fc;border:1px solid rgba(99,102,241,.3);border-radius:5px;cursor:pointer">Ekle</button>`
            }
          </div>
        </div>`;
      }).join('');
  }

  async function _addFromTs(name, ip, host) {
    await fetch('/api/devices', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name, host, user:'root', port:22, color:'#6366f1', note:'Tailscale'})});
    showToast(`${name} eklendi`);
    refresh();
  }

  function openSSH(devId) {
    const dev = _devs.find(d => d.id === devId);
    if (!dev) return;
    close();
    // Yeni terminal sekme açıp SSH WS'e bağla
    const id = 'ssh-' + devId + '-' + Date.now();
    const tab = _createTab(id, `SSH: ${dev.name}`, dev.color);
    const view = _createView(id);
    document.getElementById('terminal-tabs-container').appendChild(tab);
    document.getElementById('terminal-views-container').appendChild(view);
    const term = new Terminal({
      theme: { background:'#050810', foreground:'#e4e4e7', cursor:'#3b82f6',
               selectionBackground:'rgba(59,130,246,.3)' },
      fontFamily:"'JetBrains Mono',monospace", fontSize:13, lineHeight:1.4,
      scrollback:5000, cursorBlink:true,
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(view.querySelector('.ssh-term-host'));
    setTimeout(() => { try { fit.fit(); } catch(_){} }, 60);
    term.writeln(`\x1b[36mSSH bağlantısı kuruluyor: ${dev.user}@${dev.host}:${dev.port}\x1b[0m`);
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/ssh/${devId}`);
    ws.onmessage = e => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'pty_out') {
        const bin = atob(msg.data);
        const bytes = new Uint8Array(bin.length);
        for (let i=0; i<bin.length; i++) bytes[i] = bin.charCodeAt(i);
        term.write(bytes);
      } else if (msg.type === 'error') {
        term.writeln(`\r\n\x1b[31m${msg.msg}\x1b[0m`);
      }
    };
    ws.onclose = () => term.writeln('\r\n\x1b[33m[SSH bağlantısı kapandı]\x1b[0m');
    term.onData(d => { if (ws.readyState===WebSocket.OPEN) ws.send(JSON.stringify({type:'pty_in',data:d})); });
    term.onResize(({cols,rows}) => {
      if (ws.readyState===WebSocket.OPEN) ws.send(JSON.stringify({type:'resize',cols,rows}));
    });
    state.activeTermId = id;
    _activateTab(id);
    if (window.innerWidth < 768) switchMobileTab('terminals');
  }

  function _createTab(id, label, color) {
    const el = document.createElement('div');
    el.id = `tab-${id}`;
    el.className = 'tab-btn flex items-center gap-1.5 px-3 h-full text-xs shrink-0 cursor-pointer text-textMuted font-mono active';
    el.style.borderTopColor = color;
    el.innerHTML = `<span style="width:6px;height:6px;border-radius:50%;background:${color};flex-shrink:0"></span>
      <span>${label}</span>
      <button class="ml-1 opacity-50 hover:opacity-100" onclick="event.stopPropagation();_closeSshTab('${id}')">×</button>`;
    el.onclick = () => _activateTab(id);
    return el;
  }

  function _createView(id) {
    const el = document.createElement('div');
    el.id = `view-${id}`;
    el.className = 'absolute inset-0 flex flex-col';
    el.innerHTML = `<div class="ssh-term-host" style="flex:1;min-height:0;padding:4px 6px"></div>`;
    return el;
  }

  function _activateTab(id) {
    document.querySelectorAll('#terminal-tabs-container .tab-btn').forEach(t => t.classList.toggle('active', t.id===`tab-${id}`));
    document.querySelectorAll('#terminal-views-container > div').forEach(v => v.style.display = v.id===`view-${id}`?'flex':'none');
    state.activeTermId = id;
  }

  async function showAdd() {
    const colors = COLORS;
    const html = `
      <div id="dev-add-form" style="padding:16px;display:flex;flex-direction:column;gap:10px;">
        <div style="font-size:13px;font-weight:700;color:#e4e4e7;margin-bottom:4px">➕ Yeni Cihaz Ekle</div>
        <input id="da-name"  type="text" placeholder="Cihaz adı (örn: Pi 4 Home)" style="background:#18181b;border:1px solid #3f3f46;color:#fff;border-radius:8px;padding:8px 10px;font-size:12px;outline:none;font-family:inherit">
        <input id="da-host"  type="text" placeholder="IP / hostname (örn: 192.168.1.5)" style="background:#18181b;border:1px solid #3f3f46;color:#fff;border-radius:8px;padding:8px 10px;font-size:12px;outline:none;font-family:inherit">
        <div style="display:flex;gap:8px">
          <input id="da-user" type="text" placeholder="Kullanıcı" value="root" style="flex:1;background:#18181b;border:1px solid #3f3f46;color:#fff;border-radius:8px;padding:8px 10px;font-size:12px;outline:none;font-family:inherit">
          <input id="da-port" type="number" placeholder="Port" value="22" style="width:70px;background:#18181b;border:1px solid #3f3f46;color:#fff;border-radius:8px;padding:8px 10px;font-size:12px;outline:none;font-family:inherit">
        </div>
        <input id="da-note"  type="text" placeholder="Not (opsiyonel)" style="background:#18181b;border:1px solid #3f3f46;color:#fff;border-radius:8px;padding:8px 10px;font-size:12px;outline:none;font-family:inherit">
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          ${colors.map((c,i)=>`<div onclick="document.getElementById('da-color').value='${c}';document.querySelectorAll('.da-col').forEach(e=>e.style.outline='none');this.style.outline='2px solid #fff'" class="da-col" style="width:22px;height:22px;border-radius:50%;background:${c};cursor:pointer;${i===0?'outline:2px solid #fff':''}"></div>`).join('')}
          <input id="da-color" type="hidden" value="${colors[0]}">
        </div>
        <div style="display:flex;gap:8px;margin-top:4px">
          <button onclick="Devices._saveAdd()" style="flex:1;padding:8px;background:#3b82f6;color:#fff;border:none;border-radius:8px;font-size:13px;cursor:pointer;font-weight:600;font-family:inherit">Kaydet</button>
          <button onclick="Devices._cancelAdd()" style="padding:8px 14px;background:#27272a;color:#a1a1aa;border:none;border-radius:8px;font-size:13px;cursor:pointer;font-family:inherit">İptal</button>
        </div>
      </div>`;
    const panel = document.getElementById('dev-list-wrap');
    const existing = document.getElementById('dev-add-form');
    if (existing) { existing.remove(); }
    else { panel.insertAdjacentHTML('afterbegin', html); }
  }

  async function _saveAdd() {
    const name  = document.getElementById('da-name')?.value.trim();
    const host  = document.getElementById('da-host')?.value.trim();
    const user  = document.getElementById('da-user')?.value.trim() || 'root';
    const port  = document.getElementById('da-port')?.value.trim() || '22';
    const note  = document.getElementById('da-note')?.value.trim();
    const color = document.getElementById('da-color')?.value || '#3b82f6';
    if (!host) { showToast('IP/hostname gerekli', 'error'); return; }
    await fetch('/api/devices', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name: name||host, host, user, port:parseInt(port), note, color})});
    showToast(`${name||host} eklendi`);
    _cancelAdd();
    refresh();
  }

  function _cancelAdd() {
    document.getElementById('dev-add-form')?.remove();
  }

  async function del(id) {
    if (!confirm('Bu cihazı silmek istiyor musunuz?')) return;
    await fetch(`/api/devices/${id}`, {method:'DELETE'});
    if (_active === id) setActive('local');
    refresh();
  }

  function applyOsTheme(info) {
    const id = (info.distro_id || '').toLowerCase();
    const cls = id.includes('arch') ? 'term-theme-arch'
              : id.includes('fedora')||id.includes('rhel') ? 'term-theme-fedora'
              : info.system === 'Windows' ? 'term-theme-windows'
              : info.system === 'Darwin'  ? 'term-theme-macos'
              : '';
    if (cls) document.body.classList.add(cls);
  }

  function activeId()  { return _active; }
  function getDevById(id) { return _devs.find(d => d.id === id) || null; }

  function addFromLan(ip, hostname) {
    // LAN taramasından gelen cihazı hızlıca Ekle formuna doldur
    open();
    setTimeout(() => {
      showAdd();
      const hEl = document.getElementById('dev-add-host');
      const nEl = document.getElementById('dev-add-name');
      if (hEl) hEl.value = ip;
      if (nEl) nEl.value = hostname || ip;
    }, 200);
  }
  return { open, close, refresh, setActive, openSSH, showAdd, _saveAdd, _cancelAdd, del, applyOsTheme, activeId, getDevById, _addFromTs, addFromLan };
})();

function _closeSshTab(id) {
  document.getElementById(`tab-${id}`)?.remove();
  document.getElementById(`view-${id}`)?.remove();
}

buildSnippetsUI();
AppSettings.init();
Dashboard.start();

// ── OS tespiti — paket yöneticisi, terminal teması ve cihaz başlığını ayarla ──
(async () => {
  try {
    const info = await fetch('/api/sysinfo').then(r => r.json());
    window._sysInfo = info;
    PkgMgr.applyOsInfo(info);
    Devices.applyOsTheme(info);
    // Aktif cihaz düğmesini güncelle
    const nameEl = document.getElementById('dev-active-name');
    if (nameEl && info.distro) nameEl.textContent = info.distro;
    // Mobil başlık etiketi
    const mob = document.getElementById('mob-device-label');
    if (mob && info.distro) mob.textContent = info.distro;
  } catch(e) {
    // Sunucu henüz hazır değil — varsayılan değerler kalır
  }
})();

addTerminal();
addFileManager();
switchMobileTab('terminals');
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes — Serve frontend
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    return HTML


# ---------------------------------------------------------------------------
# Routes — File system
# ---------------------------------------------------------------------------
@app.get("/api/files")
def list_files(path: str = "."):
    try:
        abs_path = os.path.abspath(path)
        entries  = os.listdir(abs_path)
        items    = []
        for entry in entries:
            full = os.path.join(abs_path, entry)
            try:
                items.append({
                    "name":   entry,
                    "is_dir": os.path.isdir(full),
                    "path":   full,
                    "size":   os.path.getsize(full) if os.path.isfile(full) else 0,
                })
            except OSError:
                continue
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {"success": True, "current_path": abs_path, "items": items}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/download")
def download_file(path: str):
    abs_path = os.path.abspath(path)
    if os.path.isfile(abs_path):
        return FileResponse(abs_path, media_type="application/octet-stream",
                            filename=os.path.basename(abs_path))
    raise HTTPException(status_code=404, detail="Dosya bulunamadı")


@app.get("/api/download_zip")
def download_zip(path: str):
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="Bulunamadı")
    base_name = os.path.basename(abs_path.rstrip("/"))
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.isdir(abs_path):
            for root, _, files in os.walk(abs_path):
                for fname in files:
                    fp = os.path.join(root, fname)
                    zf.write(fp, os.path.relpath(fp, os.path.dirname(abs_path)))
        else:
            zf.write(abs_path, base_name)
    return FileResponse(tmp.name, media_type="application/zip",
                        filename=base_name + ".zip")


@app.post("/api/upload")
async def upload_file(path: str = Form(...), file: UploadFile = File(...)):
    try:
        dest_dir = os.path.abspath(path)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, file.filename)
        content = await file.read()
        with open(dest, "wb") as f:
            f.write(content)
        return {"success": True, "path": dest, "size": len(content)}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/read_file")
def read_file(path: str):
    try:
        abs_path = os.path.abspath(path)
        if not os.path.isfile(abs_path):
            return {"success": False, "error": "Dosya bulunamadı"}
        size = os.path.getsize(abs_path)
        if size > 2 * 1024 * 1024:   # 2 MB limit
            return {"success": False, "error": "Dosya çok büyük (>2MB)"}
        with open(abs_path, "r", errors="replace") as f:
            content = f.read()
        return {
            "success": True, "content": content,
            "path": abs_path, "name": os.path.basename(abs_path),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/write_file")
def write_file(req: WriteFileRequest):
    try:
        abs_path = os.path.abspath(req.path)
        with open(abs_path, "w") as f:
            f.write(req.content)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/rename")
def rename_item(req: RenameRequest):
    try:
        old = os.path.abspath(req.old_path)
        new = os.path.join(os.path.dirname(old), req.new_name)
        if os.path.exists(new):
            return {"success": False, "error": "Bu isimde bir öğe zaten var"}
        os.rename(old, new)
        return {"success": True, "new_path": new}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/mkdir")
def make_dir(req: MkdirRequest):
    try:
        new_dir = os.path.join(os.path.abspath(req.parent_path), req.name)
        os.makedirs(new_dir, exist_ok=False)
        return {"success": True, "path": new_dir}
    except FileExistsError:
        return {"success": False, "error": "Bu isimde klasör zaten var"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/newfile")
def new_file(req: NewFileRequest):
    try:
        dest = os.path.join(os.path.abspath(req.parent_path), req.name)
        if os.path.exists(dest):
            return {"success": False, "error": "Bu isimde dosya zaten var"}
        Path(dest).touch()
        return {"success": True, "path": dest}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/delete")
def delete_item(req: DeleteRequest):
    try:
        abs_path = os.path.abspath(req.path)
        if not os.path.exists(abs_path):
            return {"success": False, "error": "Bulunamadı"}
        if os.path.isdir(abs_path):
            shutil.rmtree(abs_path)
        else:
            os.remove(abs_path)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/move")
def move_item(req: MoveRequest):
    """Move a file or directory into dest_dir, preserving the original name."""
    try:
        src  = os.path.abspath(req.src_path)
        dest = os.path.abspath(req.dest_dir)
        if not os.path.exists(src):
            return {"success": False, "error": "Kaynak bulunamadı"}
        if not os.path.isdir(dest):
            return {"success": False, "error": "Hedef klasör mevcut değil"}
        target = os.path.join(dest, os.path.basename(src))
        if os.path.exists(target):
            return {"success": False, "error": f"Hedefte '{os.path.basename(src)}' zaten var"}
        shutil.move(src, target)
        return {"success": True, "new_path": target}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/copy")
def copy_item(req: CopyRequest):
    """Copy a file or directory into dest_dir, preserving the original name."""
    try:
        src  = os.path.abspath(req.src_path)
        dest = os.path.abspath(req.dest_dir)
        if not os.path.exists(src):
            return {"success": False, "error": "Kaynak bulunamadı"}
        if not os.path.isdir(dest):
            return {"success": False, "error": "Hedef klasör mevcut değil"}
        base   = os.path.basename(src)
        target = os.path.join(dest, base)
        # Auto-number on conflict: file.txt → file_copy1.txt
        if os.path.exists(target):
            name, ext = os.path.splitext(base)
            counter = 1
            while os.path.exists(target):
                target = os.path.join(dest, f"{name}_copy{counter}{ext}")
                counter += 1
        if os.path.isdir(src):
            shutil.copytree(src, target)
        else:
            shutil.copy2(src, target)
        return {"success": True, "new_path": target}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/extract")
def extract_archive(req: ExtractRequest):
    """Extract a zip/tar/gz/bz2/xz archive next to itself."""
    import zipfile, tarfile
    try:
        src = os.path.abspath(req.path)
        if not os.path.isfile(src):
            return {"success": False, "error": "Dosya bulunamadı"}

        name = os.path.basename(src)
        # Strip all known archive suffixes to get a clean folder name
        for suffix in ('.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2',
                       '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar'):
            if name.lower().endswith(suffix):
                stem = name[: len(name) - len(suffix)]
                break
        else:
            stem = os.path.splitext(name)[0]

        dest = os.path.join(os.path.dirname(src), stem)
        # Avoid overwriting an existing directory
        if os.path.exists(dest):
            counter = 1
            while os.path.exists(f"{dest}_{counter}"):
                counter += 1
            dest = f"{dest}_{counter}"

        if zipfile.is_zipfile(src):
            with zipfile.ZipFile(src, 'r') as zf:
                zf.extractall(dest)
        elif tarfile.is_tarfile(src):
            with tarfile.open(src, 'r:*') as tf:
                tf.extractall(dest)
        else:
            # Fallback: shutil.unpack_archive handles many formats
            shutil.unpack_archive(src, dest)

        return {"success": True, "dest_dir": dest}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Routes — System monitoring
# ---------------------------------------------------------------------------
@app.get("/api/system_stats")
def system_stats():
    """Return CPU, RAM, and disk usage."""
    cpu = psutil.cpu_percent(interval=0.15)
    mem = psutil.virtual_memory()
    disk= psutil.disk_usage("/")
    return {
        "cpu_percent":    round(cpu, 1),
        "ram_used_mb":    mem.used  // (1024 * 1024),
        "ram_total_mb":   mem.total // (1024 * 1024),
        "ram_percent":    round(mem.percent, 1),
        "disk_used_gb":   disk.used  // (1024 ** 3),
        "disk_total_gb":  disk.total // (1024 ** 3),
        "disk_percent":   round(disk.percent, 1),
    }


@app.get("/api/tailscale/peers")
def api_tailscale_peers():
    """Tailscale ağındaki tüm peer'ları döner."""
    try:
        result = subprocess.run(["tailscale", "status", "--json"],
                                capture_output=True, text=True, timeout=4)
        data = json.loads(result.stdout)
        peers = []
        for key, peer in data.get("Peer", {}).items():
            ips = peer.get("TailscaleIPs", [])
            peers.append({
                "key":       key,
                "hostname":  peer.get("HostName", "?"),
                "dns_name":  peer.get("DNSName", "").rstrip("."),
                "ip":        ips[0] if ips else "",
                "os":        peer.get("OS", ""),
                "online":    peer.get("Online", False),
                "active":    peer.get("Active", False),
            })
        self_node = data.get("Self", {})
        self_ips  = self_node.get("TailscaleIPs", [])
        return {"ok": True, "peers": peers,
                "self": {"ip":       self_ips[0] if self_ips else "",
                         "hostname": self_node.get("HostName", "")}}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"ok": False, "peers": [], "error": "Tailscale kurulu değil veya çalışmıyor"}
    except Exception as e:
        return {"ok": False, "peers": [], "error": str(e)}


# ─── SFTP bağlantı havuzu ─────────────────────────────────────────────────
_sftp_pool: dict = {}   # dev_id → (SSHClient, SFTPClient)

def _get_sftp(dev_id: str):
    import paramiko, stat as stat_mod
    if dev_id in _sftp_pool:
        ssh, sftp = _sftp_pool[dev_id]
        try:
            transport = ssh.get_transport()
            if transport and transport.is_active():
                return sftp
        except Exception:
            pass
        del _sftp_pool[dev_id]
    devs = _load_devices()
    dev  = next((d for d in devs if d["id"] == dev_id), None)
    if not dev:
        raise RuntimeError("Cihaz bulunamadı")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=dev["host"], port=dev["port"],
                username=dev["user"], timeout=10,
                look_for_keys=True, allow_agent=True)
    sftp = ssh.open_sftp()
    _sftp_pool[dev_id] = (ssh, sftp)
    return sftp

def _sftp_rm_recursive(sftp, path):
    import stat as stat_mod
    try:
        sftp.remove(path)
    except IOError:
        for item in sftp.listdir_attr(path):
            ip = path.rstrip("/") + "/" + item.filename
            if stat_mod.S_ISDIR(item.st_mode or 0):
                _sftp_rm_recursive(sftp, ip)
            else:
                sftp.remove(ip)
        sftp.rmdir(path)

@app.get("/api/sftp/{dev_id}/ls")
async def sftp_ls(dev_id: str, path: str = "/"):
    try:
        loop = asyncio.get_event_loop()
        sftp = await loop.run_in_executor(None, _get_sftp, dev_id)
        import stat as stat_mod
        attrs = await loop.run_in_executor(None, sftp.listdir_attr, path)
        items = []
        for a in sorted(attrs, key=lambda x: (not stat_mod.S_ISDIR(x.st_mode or 0), x.filename.lower())):
            is_dir = stat_mod.S_ISDIR(a.st_mode or 0)
            p = path.rstrip("/") + "/" + a.filename
            items.append({"name": a.filename, "path": p, "is_dir": is_dir,
                          "size": a.st_size or 0, "modified": a.st_mtime or 0})
        return {"success": True, "current_path": path, "items": items}
    except Exception as e:
        _sftp_pool.pop(dev_id, None)
        return {"success": False, "error": str(e)}

@app.get("/api/sftp/{dev_id}/read")
async def sftp_read(dev_id: str, path: str = "/"):
    try:
        loop = asyncio.get_event_loop()
        sftp = await loop.run_in_executor(None, _get_sftp, dev_id)
        def _r():
            with sftp.open(path, "rb") as f: return f.read()
        raw = await loop.run_in_executor(None, _r)
        try:    return {"success": True, "content": raw.decode("utf-8")}
        except: return {"success": True, "content": base64.b64encode(raw).decode(), "binary": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/sftp/{dev_id}/write")
async def sftp_write(dev_id: str, data: dict = Body(...)):
    try:
        path    = data["path"];  content = data.get("content", "")
        loop    = asyncio.get_event_loop()
        sftp    = await loop.run_in_executor(None, _get_sftp, dev_id)
        def _w():
            with sftp.open(path, "w") as f: f.write(content)
        await loop.run_in_executor(None, _w)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/sftp/{dev_id}/mkdir")
async def sftp_mkdir(dev_id: str, data: dict = Body(...)):
    try:
        parent = data.get("parent_path", "/");  name = data["name"]
        path   = parent.rstrip("/") + "/" + name
        loop   = asyncio.get_event_loop()
        sftp   = await loop.run_in_executor(None, _get_sftp, dev_id)
        await loop.run_in_executor(None, sftp.mkdir, path)
        return {"success": True, "path": path}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/sftp/{dev_id}/newfile")
async def sftp_newfile(dev_id: str, data: dict = Body(...)):
    try:
        parent = data.get("parent_path", "/");  name = data["name"]
        path   = parent.rstrip("/") + "/" + name
        loop   = asyncio.get_event_loop()
        sftp   = await loop.run_in_executor(None, _get_sftp, dev_id)
        def _t():
            with sftp.open(path, "w") as f: f.write("")
        await loop.run_in_executor(None, _t)
        return {"success": True, "path": path}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/sftp/{dev_id}/rename")
async def sftp_rename(dev_id: str, data: dict = Body(...)):
    try:
        old_path = data["old_path"];  new_name = data["new_name"]
        parent   = "/".join(old_path.rstrip("/").split("/")[:-1]) or "/"
        new_path = parent.rstrip("/") + "/" + new_name
        loop     = asyncio.get_event_loop()
        sftp     = await loop.run_in_executor(None, _get_sftp, dev_id)
        await loop.run_in_executor(None, sftp.rename, old_path, new_path)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/sftp/{dev_id}/delete")
async def sftp_delete(dev_id: str, data: dict = Body(...)):
    try:
        path = data["path"]
        loop = asyncio.get_event_loop()
        sftp = await loop.run_in_executor(None, _get_sftp, dev_id)
        await loop.run_in_executor(None, _sftp_rm_recursive, sftp, path)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/tailscale_status")
def tailscale_status():
    """Return Tailscale connection status and IP."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=3
        )
        data       = json.loads(result.stdout)
        self_node  = data.get("Self", {})
        ts_ips     = self_node.get("TailscaleIPs", [])
        return {
            "connected":    data.get("BackendState") == "Running",
            "ip":           ts_ips[0] if ts_ips else "N/A",
            "hostname":     self_node.get("HostName", "N/A"),
            "backend_state":data.get("BackendState", "Unknown"),
            "peer_count":   len(data.get("Peer", {})),
        }
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"connected": False, "ip": "N/A", "hostname": "N/A",
                "backend_state": "Yüklü Değil", "peer_count": 0}
    except (json.JSONDecodeError, KeyError):
        return {"connected": False, "ip": "N/A", "hostname": "N/A",
                "backend_state": "Hata", "peer_count": 0}


# ---------------------------------------------------------------------------
# Routes — File sharing
# ---------------------------------------------------------------------------
@app.post("/api/share")
async def api_share(data: dict = Body(...)):
    path = data.get("path", "")
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "Dosya bulunamadı"}
    hours = max(1, min(int(data.get("hours", 1)), 168))
    tok   = _sec.token_urlsafe(12)
    _share_store[tok] = {"path": path, "exp": _t.time() + hours * 3600, "name": os.path.basename(path)}
    return {"ok": True, "token": tok, "hours": hours}

@app.get("/api/shared/{tok}")
async def api_shared_dl(tok: str):
    e = _share_store.get(tok)
    if not e or _t.time() > e["exp"]:
        raise HTTPException(404, "Link süresi dolmuş veya bulunamadı")
    return FileResponse(e["path"], filename=e["name"])


# ---------------------------------------------------------------------------
# Routes — Screenshot / Live screen / Remote input
# ---------------------------------------------------------------------------
def _detect_os() -> dict:
    """İşletim sistemi, dağıtım ve mevcut paket yöneticilerini tespit eder."""
    import platform
    system = platform.system()  # 'Windows', 'Linux', 'Darwin'

    if system == "Windows":
        # Windows: cmd veya powershell, winget/choco
        mgrs = []
        if shutil.which("winget"):  mgrs.append({"id":"winget", "label":"winget", "search":"winget search",    "install":"winget install"})
        if shutil.which("choco"):   mgrs.append({"id":"choco",  "label":"choco",  "search":"choco search",     "install":"choco install -y"})
        if shutil.which("scoop"):   mgrs.append({"id":"scoop",  "label":"scoop",  "search":"scoop search",     "install":"scoop install"})
        if shutil.which("pip"):     mgrs.append({"id":"pip",    "label":"pip",    "search":None,               "install":"pip install"})
        if shutil.which("npm"):     mgrs.append({"id":"npm",    "label":"npm",    "search":"npm search",       "install":"npm install -g"})
        shell = "powershell.exe" if shutil.which("powershell.exe") else "cmd.exe"
        distro = "Windows"
        ver = platform.version()
    elif system == "Darwin":
        mgrs = []
        if shutil.which("brew"):    mgrs.append({"id":"brew",   "label":"brew",   "search":"brew search",      "install":"brew install"})
        if shutil.which("port"):    mgrs.append({"id":"port",   "label":"port",   "search":"port search",      "install":"sudo port install"})
        if shutil.which("pip3"):    mgrs.append({"id":"pip",    "label":"pip",    "search":None,               "install":"pip3 install"})
        if shutil.which("npm"):     mgrs.append({"id":"npm",    "label":"npm",    "search":"npm search",       "install":"npm install -g"})
        shell = os.environ.get("SHELL", "/bin/zsh")
        distro = "macOS"
        ver = platform.mac_ver()[0]
    else:
        # Linux — dağıtımı tespit et
        distro, ver, distro_id = "Linux", "", ""
        try:
            import distro as _d
            distro    = _d.name()
            ver       = _d.version()
            distro_id = _d.id().lower()
        except Exception:
            pass
        if not distro_id:
            try:
                with open("/etc/os-release") as f:
                    info = dict(l.strip().split("=",1) for l in f if "=" in l)
                distro_id = info.get("ID","").strip('"').lower()
                distro    = info.get("PRETTY_NAME", info.get("NAME","Linux")).strip('"')
                ver       = info.get("VERSION_ID","").strip('"')
            except Exception:
                pass

        mgrs = []
        # Dağıtıma özgü ana paket yöneticisi
        if distro_id in ("fedora","rhel","centos","rocky","almalinux"):
            if shutil.which("dnf"):
                mgrs.append({"id":"dnf",    "label":"dnf",    "search":"dnf search",        "install":"sudo dnf install -y"})
            if shutil.which("rpm"):
                mgrs.append({"id":"rpm",    "label":"rpm",    "search":None,                "install":"sudo rpm -i"})
        elif distro_id in ("arch","manjaro","garuda","endeavouros"):
            if shutil.which("pacman"):
                mgrs.append({"id":"pacman", "label":"pacman", "search":"pacman -Ss",         "install":"sudo pacman -S --noconfirm"})
            if shutil.which("yay"):
                mgrs.append({"id":"yay",    "label":"yay",    "search":"yay -Ss",            "install":"yay -S --noconfirm"})
            if shutil.which("paru"):
                mgrs.append({"id":"paru",   "label":"paru",   "search":"paru -Ss",           "install":"paru -S --noconfirm"})
        elif distro_id in ("opensuse","opensuse-leap","opensuse-tumbleweed","sles"):
            if shutil.which("zypper"):
                mgrs.append({"id":"zypper", "label":"zypper", "search":"zypper search",      "install":"sudo zypper install -y"})
        elif distro_id in ("alpine",):
            if shutil.which("apk"):
                mgrs.append({"id":"apk",    "label":"apk",    "search":"apk search",         "install":"apk add"})
        elif distro_id in ("void",):
            if shutil.which("xbps-install"):
                mgrs.append({"id":"xbps",   "label":"xbps",   "search":"xbps-query -Rs",     "install":"sudo xbps-install -y"})
        elif distro_id in ("gentoo",):
            if shutil.which("emerge"):
                mgrs.append({"id":"emerge", "label":"emerge", "search":"emerge --search",    "install":"sudo emerge"})
        # Debian/Ubuntu fallback (ve diğer apt tabanlılar)
        if not mgrs or distro_id in ("debian","ubuntu","linuxmint","pop","kali","raspbian","parrot"):
            if shutil.which("apt"):
                mgrs.append({"id":"apt",    "label":"sudo apt","search":"apt-cache search",  "install":"sudo apt install -y"})
            elif shutil.which("apt-get"):
                mgrs.append({"id":"apt",    "label":"sudo apt-get","search":None,            "install":"sudo apt-get install -y"})
        # Ek evrensel yöneticiler
        if shutil.which("snap"):    mgrs.append({"id":"snap",   "label":"snap",  "search":"snap find",          "install":"sudo snap install"})
        if shutil.which("flatpak"): mgrs.append({"id":"flatpak","label":"flatpak","search":"flatpak search",    "install":"flatpak install -y"})
        if shutil.which("pip3") or shutil.which("pip"):
            mgrs.append({"id":"pip", "label":"pip", "search":None, "install":"pip install"})
        if shutil.which("npm"):
            mgrs.append({"id":"npm", "label":"npm", "search":"npm search", "install":"npm install -g"})
        if shutil.which("cargo"):
            mgrs.append({"id":"cargo","label":"cargo","search":"cargo search","install":"cargo install"})
        shell = os.environ.get("SHELL", "/bin/bash")
        if not shutil.which(shell.split("/")[-1]):
            for sh in ["/bin/bash","/bin/sh","/bin/zsh"]:
                if shutil.which(sh): shell = sh; break

    return {
        "system":  system,
        "distro":  distro,
        "version": ver,
        "shell":   shell,
        "managers": mgrs if mgrs else [
            {"id":"pip","label":"pip","search":None,"install":"pip install"},
            {"id":"npm","label":"npm","search":"npm search","install":"npm install -g"},
        ],
    }

_OS_INFO: dict = {}

# ---------------------------------------------------------------------------
# Device store — SSH ve yerel cihaz yönetimi
# ---------------------------------------------------------------------------
_DEVICES_FILE = Path.home() / ".lphone_devices.json"

def _load_devices() -> list:
    try:
        return json.loads(_DEVICES_FILE.read_text())
    except Exception:
        return []

def _save_devices(devs: list):
    _DEVICES_FILE.write_text(json.dumps(devs, indent=2))


@app.get("/api/devices")
async def api_devices_list():
    devs = _load_devices()
    return {"ok": True, "devices": devs}

@app.post("/api/devices")
async def api_device_add(data: dict = Body(...)):
    devs = _load_devices()
    dev = {
        "id":      _sec.token_hex(6),
        "name":    data.get("name", "Cihaz"),
        "host":    data.get("host", ""),
        "port":    int(data.get("port", 22)),
        "user":    data.get("user", "root"),
        "color":   data.get("color", "#3b82f6"),
        "note":    data.get("note", ""),
        "type":    data.get("type", "ssh"),   # "ssh" | "url"
        "url":     data.get("url", ""),       # for type=url
    }
    devs.append(dev)
    _save_devices(devs)
    return {"ok": True, "device": dev}

@app.delete("/api/devices/{dev_id}")
async def api_device_delete(dev_id: str):
    devs = [d for d in _load_devices() if d["id"] != dev_id]
    _save_devices(devs)
    return {"ok": True}

@app.patch("/api/devices/{dev_id}")
async def api_device_update(dev_id: str, data: dict = Body(...)):
    devs = _load_devices()
    for d in devs:
        if d["id"] == dev_id:
            d.update({k: v for k, v in data.items() if k != "id"})
    _save_devices(devs)
    return {"ok": True}

@app.get("/api/devices/{dev_id}/ping")
async def api_device_ping(dev_id: str):
    """Cihazın erişilebilir olup olmadığını TCP ile kontrol eder."""
    import socket
    devs = _load_devices()
    dev  = next((d for d in devs if d["id"] == dev_id), None)
    if not dev:
        return {"ok": False, "error": "Cihaz bulunamadı"}
    try:
        sock = socket.create_connection((dev["host"], dev["port"]), timeout=3)
        sock.close()
        return {"ok": True, "reachable": True}
    except Exception as e:
        return {"ok": True, "reachable": False, "error": str(e)}


# ---------------------------------------------------------------------------
# WebSocket — SSH PTY tunnel (paramiko gerektirir)
# ---------------------------------------------------------------------------
@app.websocket("/ws/ssh/{dev_id}")
async def ws_ssh(ws: WebSocket, dev_id: str):
    await ws.accept()
    try:
        import paramiko  # type: ignore
    except ImportError:
        await ws.send_json({"type": "error",
                            "msg": "paramiko kurulu değil — pip install paramiko"})
        await ws.close()
        return

    devs = _load_devices()
    dev  = next((d for d in devs if d["id"] == dev_id), None)
    if not dev:
        await ws.send_json({"type": "error", "msg": "Cihaz bulunamadı"})
        await ws.close()
        return

    loop   = asyncio.get_event_loop()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        await loop.run_in_executor(None, lambda: client.connect(
            hostname=dev["host"], port=dev["port"], username=dev["user"],
            timeout=10, look_for_keys=True, allow_agent=True,
        ))
    except Exception as e:
        await ws.send_json({"type": "error", "msg": f"SSH bağlantısı başarısız: {e}"})
        await ws.close(); return

    chan = await loop.run_in_executor(None, lambda: (
        lambda ch: (ch.get_pty(term="xterm-256color", width=220, height=50), ch)[1]
    )(client.invoke_shell()))

    async def _read_ssh():
        try:
            while True:
                if chan.recv_ready():
                    data = await loop.run_in_executor(None, chan.recv, 4096)
                    if not data: break
                    await ws.send_json({"type": "pty_out",
                                        "data": base64.b64encode(data).decode()})
                else:
                    await asyncio.sleep(0.02)
        except Exception:
            pass

    read_task = asyncio.create_task(_read_ssh())
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "pty_in":
                await loop.run_in_executor(None, chan.send, msg["data"].encode())
            elif msg.get("type") == "resize":
                await loop.run_in_executor(None,
                    lambda: chan.resize_pty(width=msg.get("cols",80), height=msg.get("rows",24)))
    except Exception:
        pass
    finally:
        read_task.cancel()
        try: chan.close()
        except: pass
        client.close()


@app.on_event("startup")
async def _startup():
    global _OS_INFO
    _OS_INFO = _detect_os()

@app.get("/api/sysinfo")
async def api_sysinfo():
    if not _OS_INFO:
        return _detect_os()
    return _OS_INFO

@app.get("/api/screen_status")
async def api_screen_status():
    return _screen_status()

@app.get("/api/screenshot")
async def api_screenshot():
    data = _capture_screen()
    if data:
        return {"ok": True, "data": base64.b64encode(data).decode()}
    disp = os.environ.get("DISPLAY") or _find_real_display() or "bulunamadı"
    return {"ok": False, "error": (
        f"Ekran yakalama başarısız — DISPLAY={disp}\n\n"
        "Çözüm: sudo apt install -y scrot xdotool\n"
        "(Masaüstünde çalıştırıyorsanız scrot yeterli; Xvfb gerekmez)"
    )}

@app.post("/api/input")
async def api_input(data: dict = Body(...)):
    action = data.get("action", "")
    # venv PATH bypass + gerçek masaüstü display kullan
    disp_val = os.environ.get("DISPLAY") or _find_real_display() or ":0"
    env = _system_env({"DISPLAY": disp_val})
    xdotool_bin = _find_tool("xdotool") or "xdotool"
    def _xdo(*args, t=2):
        r = subprocess.run([xdotool_bin] + list(args), timeout=t,
                           capture_output=True, env=env)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="replace").strip() or f"xdotool exit {r.returncode}")
    try:
        if action == "move":
            _xdo("mousemove", str(data["x"]), str(data["y"]))
        elif action == "click":
            _xdo("click", str(data.get("button", 1)))
        elif action == "dblclick":
            _xdo("click", "--repeat", "2", str(data.get("button", 1)))
        elif action == "scroll":
            _xdo("click", "4" if data.get("dir", "up") == "up" else "5")
        elif action == "key":
            _xdo("key", "--", data.get("key", ""))
        elif action == "type":
            _xdo("type", "--delay", "20", "--", data.get("text", ""), t=5)
        else:
            return {"ok": False, "error": f"Bilinmeyen eylem: {action}"}
        return {"ok": True}
    except FileNotFoundError:
        return {"ok": False, "error": "xdotool kurulu değil — sudo apt install xdotool"}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Routes — Command completion
# ---------------------------------------------------------------------------
@app.get("/api/complete")
async def api_complete(q: str = ""):
    try:
        # Command names
        r1 = subprocess.run(["bash", "-c", f"compgen -c -- {shlex.quote(q)} 2>/dev/null"],
                            capture_output=True, text=True, timeout=2)
        cmds  = [x for x in r1.stdout.splitlines() if x.startswith(q)][:20]
        # File paths
        r2 = subprocess.run(["bash", "-c", f"compgen -f -- {shlex.quote(q)} 2>/dev/null"],
                            capture_output=True, text=True, timeout=2)
        files = [x for x in r2.stdout.splitlines() if x.startswith(q)][:10]
        return {"ok": True, "commands": cmds, "files": files}
    except Exception as e:
        return {"ok": True, "commands": [], "files": [], "error": str(e)}


# ---------------------------------------------------------------------------
# Routes — Process manager
# ---------------------------------------------------------------------------
@app.get("/api/processes")
async def api_processes():
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'username', 'cpu_percent',
                                   'memory_percent', 'status', 'cmdline']):
        try:
            cmd = " ".join(p.info.get("cmdline") or []) or p.info["name"]
            procs.append({
                "pid":    p.info["pid"],
                "name":   p.info["name"],
                "user":   (p.info.get("username") or "?")[:12],
                "cpu":    round(p.info.get("cpu_percent") or 0, 1),
                "mem":    round(p.info.get("memory_percent") or 0, 1),
                "status": p.info.get("status", "?"),
                "cmd":    cmd[:80],
            })
        except Exception:
            pass
    procs.sort(key=lambda x: x["cpu"], reverse=True)
    return {"ok": True, "processes": procs[:60]}

@app.post("/api/kill")
async def api_kill(data: dict = Body(...)):
    import signal as _sig
    pid     = int(data.get("pid", 0))
    sig_num = int(data.get("signal", 15))
    try:
        os.kill(pid, sig_num)
        return {"ok": True}
    except ProcessLookupError:
        return {"ok": False, "error": "Süreç bulunamadı"}
    except PermissionError:
        return {"ok": False, "error": "İzin reddedildi"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Routes — Package manager (search + stream install via PTY)
# ---------------------------------------------------------------------------
@app.get("/api/pkgs/search")
async def api_pkg_search(q: str = "", mgr: str = "apt"):
    try:
        cmd_map = {
            "apt":     ["apt-cache", "search", "--", q],
            "dnf":     ["dnf", "search", "--", q],
            "pacman":  ["pacman", "-Ss", q],
            "yay":     ["yay", "-Ss", "--aur", q],
            "paru":    ["paru", "-Ss", "--aur", q],
            "zypper":  ["zypper", "search", "--", q],
            "apk":     ["apk", "search", q],
            "xbps":    ["xbps-query", "-Rs", q],
            "emerge":  ["emerge", "--search", q],
            "brew":    ["brew", "search", q],
            "winget":  ["winget", "search", q],
            "choco":   ["choco", "search", q],
            "scoop":   ["scoop", "search", q],
            "snap":    ["snap", "find", q],
            "flatpak": ["flatpak", "search", q],
            "cargo":   ["cargo", "search", "--limit", "20", q],
            "npm":     ["npm", "search", "--no-color", q],
        }
        if mgr == "pip":
            r = subprocess.run(
                [sys.executable, "-m", "pip", "index", "versions", q],
                capture_output=True, text=True, timeout=15)
            lines = [l for l in (r.stdout or r.stderr).splitlines() if l.strip()][:25]
        elif mgr in cmd_map:
            r = subprocess.run(cmd_map[mgr], capture_output=True, text=True, timeout=15)
            lines = [l for l in (r.stdout or r.stderr).splitlines() if l.strip()][:25]
        else:
            return {"ok": False, "error": f"Bilinmeyen paket yöneticisi: {mgr}"}
        return {"ok": True, "results": lines}
    except FileNotFoundError:
        return {"ok": False, "error": f"{mgr} bu sistemde kurulu değil"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Routes — Task scheduler
# ---------------------------------------------------------------------------
@app.get("/api/jobs")
async def api_jobs_list():
    with _jobs_lock:
        return {"ok": True, "jobs": list(_jobs.values())}

@app.post("/api/jobs")
async def api_job_create(data: dict = Body(...)):
    jid  = _sec.token_hex(4)
    job  = {
        "id":       jid,
        "name":     data.get("name", "İsimsiz"),
        "cmd":      data.get("cmd", ""),
        "schedule": data.get("schedule", "every 1h"),
        "enabled":  True,
        "next_run": _next_run(data.get("schedule", "every 1h")),
        "last_run": "—",
        "output":   "",
    }
    with _jobs_lock:
        _jobs[jid] = job
    return {"ok": True, "job": job}

@app.delete("/api/jobs/{jid}")
async def api_job_delete(jid: str):
    with _jobs_lock:
        _jobs.pop(jid, None)
    return {"ok": True}

@app.patch("/api/jobs/{jid}/toggle")
async def api_job_toggle(jid: str):
    with _jobs_lock:
        if jid not in _jobs:
            return {"ok": False}
        _jobs[jid]["enabled"] = not _jobs[jid]["enabled"]
        return {"ok": True, "enabled": _jobs[jid]["enabled"]}

@app.post("/api/jobs/{jid}/run_now")
async def api_job_run_now(jid: str):
    with _jobs_lock:
        job = _jobs.get(jid)
    if not job:
        return {"ok": False}
    try:
        r   = subprocess.run(job["cmd"], shell=True, capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr).strip()[:500]
    except Exception as e:
        out = str(e)
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid]["last_run"] = _dt.now().strftime("%d.%m %H:%M")
            _jobs[jid]["output"]   = out
            _jobs[jid]["next_run"] = _next_run(job["schedule"])
    return {"ok": True, "output": out}


# ---------------------------------------------------------------------------
# Routes — File sync (rsync)
# ---------------------------------------------------------------------------
@app.post("/api/sync")
async def api_sync(data: dict = Body(...)):
    src = data.get("src", "").strip()
    dst = data.get("dst", "").strip()
    if not src or not dst:
        return {"ok": False, "error": "Kaynak ve hedef gerekli"}
    dry = data.get("dry", False)
    flags = ["-avz", "--progress"]
    if dry:
        flags.append("--dry-run")
    try:
        r = subprocess.run(["rsync"] + flags + [src, dst],
                           capture_output=True, text=True, timeout=120)
        return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()[:3000]}
    except FileNotFoundError:
        return {"ok": False, "error": "rsync kurulu değil"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Zaman aşımı (120s)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Routes — Notification thresholds
# ---------------------------------------------------------------------------
@app.get("/api/notif_thresh")
async def api_notif_thresh_get():
    return {"ok": True, "thresholds": _notif_thresh}

@app.post("/api/notif_thresh")
async def api_notif_thresh_set(data: dict = Body(...)):
    for k in ("cpu", "ram", "disk"):
        if k in data:
            _notif_thresh[k] = float(data[k])
    return {"ok": True, "thresholds": _notif_thresh}


# ---------------------------------------------------------------------------
# Routes — Monitors
# ---------------------------------------------------------------------------
@app.get("/api/monitors")
async def api_monitors():
    return {"ok": True, "monitors": _get_monitors()}


# ---------------------------------------------------------------------------
# Routes — Clipboard
# ---------------------------------------------------------------------------
@app.get("/api/clipboard")
async def api_clipboard_get():
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _get_clipboard)
    return {"ok": True, "text": text}

@app.post("/api/clipboard")
async def api_clipboard_set(data: dict = Body(...)):
    text = data.get("text", "")
    loop = asyncio.get_event_loop()
    ok   = await loop.run_in_executor(None, _set_clipboard, text)
    if ok:
        return {"ok": True}
    return {"ok": False, "error": "xclip veya xsel kurulu değil — sudo apt install xclip"}


# ---------------------------------------------------------------------------
# Routes — Screen recording
# ---------------------------------------------------------------------------
@app.post("/api/recording/start")
async def api_recording_start(data: dict = Body(default={})):
    fps  = int(data.get("fps", 10))
    path = _start_recording(fps)
    if path:
        return {"ok": True, "path": path}
    return {"ok": False, "error": "ffmpeg kurulu değil veya kayıt başlatılamadı"}

@app.post("/api/recording/stop")
async def api_recording_stop():
    path = _stop_recording()
    if path:
        size = os.path.getsize(path) if os.path.exists(path) else 0
        return {"ok": True, "path": path, "size": size}
    return {"ok": False, "error": "Aktif kayıt yok"}

@app.get("/api/recording/download")
async def api_recording_download():
    if not _recording_path or not os.path.exists(_recording_path):
        raise HTTPException(404, "Kayıt dosyası bulunamadı")
    fname = os.path.basename(_recording_path)
    return FileResponse(_recording_path, media_type="video/mp4",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

@app.get("/api/recording/status")
async def api_recording_status():
    active = bool(_recording_proc and _recording_proc.poll() is None)
    size   = 0
    if _recording_path and os.path.exists(_recording_path):
        try: size = os.path.getsize(_recording_path)
        except Exception: pass
    return {"ok": True, "active": active, "path": _recording_path, "size": size}


# ---------------------------------------------------------------------------
# Routes — OCR
# ---------------------------------------------------------------------------
@app.post("/api/ocr")
async def api_ocr(data: dict = Body(default={})):
    monitor = int(data.get("monitor", 0))
    loop    = asyncio.get_event_loop()
    text    = await loop.run_in_executor(None, _ocr_screen, monitor)
    if text:
        return {"ok": True, "text": text}
    if not _find_tool("tesseract"):
        return {"ok": False, "error": "tesseract kurulu değil — sudo apt install tesseract-ocr tesseract-ocr-tur"}
    return {"ok": False, "error": "Ekranda tanınabilir metin bulunamadı"}


# ---------------------------------------------------------------------------
# Routes — Wake-on-LAN
# ---------------------------------------------------------------------------
@app.post("/api/wol")
async def api_wol(data: dict = Body(...)):
    mac = data.get("mac", "")
    if not mac:
        return {"ok": False, "error": "MAC adresi gerekli"}
    ok = _wol(mac)
    return {"ok": ok, "error": None if ok else "Geçersiz MAC adresi"}


# ---------------------------------------------------------------------------
# Routes — LAN scan
# ---------------------------------------------------------------------------
@app.get("/api/lan_scan")
async def api_lan_scan():
    loop    = asyncio.get_event_loop()
    devices = await loop.run_in_executor(None, _lan_scan)
    return {"ok": True, "devices": devices}


# ---------------------------------------------------------------------------
# WebSocket — Live screen stream (screenshot polling)
# ---------------------------------------------------------------------------
@app.websocket("/ws/screen")
async def ws_screen(ws: WebSocket):
    await ws.accept()
    # İlk mesaj: config (fps, monitor, quality)
    fps     = 2
    monitor = 0
    try:
        cfg_raw = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
        fps     = max(1, min(30, int(cfg_raw.get("fps",     fps))))
        monitor = max(0, int(cfg_raw.get("monitor", monitor)))
    except Exception:
        pass
    interval = 1.0 / fps

    loop = asyncio.get_event_loop()
    fail_count = 0
    try:
        while True:
            data = await loop.run_in_executor(None, _capture_screen, monitor)
            if data:
                fail_count = 0
                frame = base64.b64encode(data).decode()
                await ws.send_json({"type": "frame", "data": frame})
                await asyncio.sleep(interval)
            else:
                fail_count += 1
                disp = os.environ.get("DISPLAY") or _find_real_display() or "bulunamadı"
                await ws.send_json({
                    "type": "error",
                    "msg": (
                        f"Ekran yakalanamıyor — DISPLAY={disp}\n\n"
                        "Çözüm: sudo apt install -y scrot xdotool\n"
                        "(venv içinde çalışıyorsanız uygulama masaüstünü "
                        "otomatik bulur; yeniden başlatmaya gerek yok)"
                    ),
                })
                await asyncio.sleep(4)
    except (WebSocketDisconnect, Exception):
        pass


# ---------------------------------------------------------------------------
# WebSocket — PTY terminal sessions
# ---------------------------------------------------------------------------
# Sessions persist across WebSocket disconnects so terminals survive network
# blips, Tailscale re-connects, browser backgrounding, etc.
# session structure: {fd, proc, task, ws, buf}
#   ws  = currently attached WebSocket (None when client is disconnected)
#   buf = bytearray of last 64 KB of PTY output for replay on re-attach

async def _stream_pty(session_id: str):
    """Reads PTY output, buffers it, and forwards to the attached WS (if any)."""
    sess     = sessions[session_id]
    master_fd = sess["fd"]
    loop     = asyncio.get_event_loop()
    try:
        while True:
            buf = await loop.run_in_executor(None, os.read, master_fd, 4096)
            if not buf:
                break
            # Append to replay buffer, keep last 64 KB
            sess["buf"] += buf
            if len(sess["buf"]) > 65536:
                sess["buf"] = sess["buf"][-65536:]
            # Forward to currently attached WS
            ws = sess.get("ws")
            if ws:
                try:
                    await ws.send_json({
                        "type":      "pty_out",
                        "sessionId": session_id,
                        "data":      base64.b64encode(buf).decode("ascii"),
                    })
                except Exception:
                    sess["ws"] = None   # detach silently on send error
    except Exception:
        pass
    finally:
        # PTY process exited — remove the session entirely
        sessions.pop(session_id, None)


@app.websocket("/ws")
async def ws_router(websocket: WebSocket):
    await websocket.accept()
    attached: set = set()   # session IDs attached to THIS connection

    try:
        while True:
            msg    = await websocket.receive_json()
            m_type = msg.get("type")
            s_id   = msg.get("sessionId")

            if m_type == "create_session":
                if s_id in sessions:
                    # Session already alive (e.g. quick reconnect race) — re-attach
                    sess = sessions[s_id]
                    sess["ws"] = websocket
                    attached.add(s_id)
                    replay = bytes(sess["buf"])
                    if replay:
                        await websocket.send_json({
                            "type": "pty_out", "sessionId": s_id,
                            "data": base64.b64encode(replay).decode("ascii"),
                        })
                    await websocket.send_json({"type": "create_session_ok", "sessionId": s_id, "ok": True})
                else:
                    cols = int(msg.get("cols") or 80)
                    rows = int(msg.get("rows") or 24)
                    env  = {**os.environ, "TERM": "xterm-256color"}
                    cmd  = msg.get("command") or ("/bin/bash" if not _IS_WINDOWS else "cmd.exe")

                    if _IS_WINDOWS:
                        # Windows: PTY yok — subprocess pipe ile çalıştır
                        import msvcrt
                        proc = subprocess.Popen(
                            cmd, shell=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env,
                            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                        )
                        sessions[s_id] = {"fd": None, "proc": proc, "ws": websocket,
                                          "buf": bytearray(), "win": True}
                        task = asyncio.create_task(_stream_pty(s_id))
                        sessions[s_id]["task"] = task
                    else:
                        m_fd, s_fd = pty.openpty()
                        try:
                            fcntl.ioctl(m_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                        except Exception:
                            pass
                        proc = subprocess.Popen(
                            [cmd],
                            stdin=s_fd, stdout=s_fd, stderr=s_fd,
                            preexec_fn=os.setsid, env=env,
                        )
                        os.close(s_fd)
                        sessions[s_id] = {"fd": m_fd, "proc": proc, "ws": websocket, "buf": bytearray()}
                        task = asyncio.create_task(_stream_pty(s_id))
                        sessions[s_id]["task"] = task

                    attached.add(s_id)
                    await websocket.send_json({"type": "create_session_ok", "sessionId": s_id, "ok": True})

            elif m_type == "attach_session":
                # Client reconnected — re-attach to existing session
                if s_id in sessions:
                    sess = sessions[s_id]
                    sess["ws"] = websocket
                    attached.add(s_id)
                    # Replay buffered output so client catches up
                    replay = bytes(sess["buf"])
                    if replay:
                        await websocket.send_json({
                            "type": "pty_out", "sessionId": s_id,
                            "data": base64.b64encode(replay).decode("ascii"),
                        })
                    await websocket.send_json({"type": "attach_session_ok", "sessionId": s_id, "ok": True})
                else:
                    # Session died while disconnected
                    await websocket.send_json({"type": "session_dead", "sessionId": s_id})

            elif m_type == "resize":
                if s_id in sessions and not sessions[s_id].get("win"):
                    cols = int(msg.get("cols") or 80)
                    rows = int(msg.get("rows") or 24)
                    try:
                        fcntl.ioctl(sessions[s_id]["fd"], termios.TIOCSWINSZ,
                                    struct.pack("HHHH", rows, cols, 0, 0))
                    except Exception:
                        pass

            elif m_type == "ping":
                await websocket.send_json({"type": "pong"})

            elif m_type == "pty_in":
                if s_id in sessions:
                    data = msg.get("data", "")
                    os.write(sessions[s_id]["fd"], data.encode("utf-8"))

    except WebSocketDisconnect:
        pass
    finally:
        # Detach WS from sessions — PTY processes keep running
        for s_id in attached:
            if s_id in sessions and sessions[s_id].get("ws") is websocket:
                sessions[s_id]["ws"] = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
