"""
SCREENSHOT TOOL — LAUNCHER (always running, no UI)

Registers Ctrl+Shift+P at the OS level via RegisterHotKey.
- If the main tool is running  → signals it to open the capture overlay
- If the main tool is closed   → launches it, then signals capture

This process is what goes in the Windows Startup folder.
It stays alive even when the user closes the tray tool.
"""

import ctypes
import ctypes.wintypes
import datetime
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

STARTUP_DELAY_S = 120

def _log(msg):
    log_path = os.path.join(os.environ["TEMP"], "screenshot_launcher.log")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()} — {msg}\n")

TOOL_SCRIPT = Path(__file__).parent / "screenshot_tool.py"

MUTEX_LAUNCHER = "ScreenshotTool_Launcher"
MUTEX_TOOL     = "ScreenshotTool_SingleInstance"
EVENT_CAPTURE  = "ScreenshotTool_CaptureEvent"

MOD_CTRL_SHIFT = 0x0002 | 0x0004   # MOD_CONTROL | MOD_SHIFT
VK_P           = 0x50
WM_HOTKEY      = 0x0312

user32   = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ── Tool exe builder (proper name + icon in Task Manager) ─────────────────────
def _embed_icon_in_exe(exe_path: str, ico_path: str):
    import struct
    try:
        import win32api, win32con
    except ImportError:
        return
    with open(ico_path, 'rb') as f:
        ico = f.read()
    _, ico_type, count = struct.unpack_from('<HHH', ico, 0)
    if ico_type != 1:
        return
    imgs = []
    for i in range(count):
        o = 6 + i * 16
        w, h, cc, _, pl, bpp, sz, off = struct.unpack_from('<BBBBHHII', ico, o)
        imgs.append((w, h, cc, pl, bpp, sz, ico[off:off + sz]))
    grp = struct.pack('<HHH', 0, 1, count)
    for idx, (w, h, cc, pl, bpp, sz, _) in enumerate(imgs):
        grp += struct.pack('<BBBBHHIH', w, h, cc, 0, pl, bpp, sz, idx + 1)
    hU = win32api.BeginUpdateResource(exe_path, False)
    for idx, (*_, data) in enumerate(imgs):
        win32api.UpdateResource(hU, win32con.RT_ICON, idx + 1, data)
    win32api.UpdateResource(hU, win32con.RT_GROUP_ICON, 1, grp)
    win32api.EndUpdateResource(hU, False)

def _patch_ver_info(raw: bytes, patches: dict) -> bytes:
    """Patch string values in a VS_VERSIONINFO binary resource."""
    import struct
    def rdwstr(b, o):
        e = o
        while e + 1 < len(b) and (b[e] or b[e + 1]):
            e += 2
        try:    s = b[o:e].decode('utf-16-le')
        except: s = ''
        return s, e + 2
    def rebuild(b, o):
        if o + 6 > len(b): return b''
        s = o
        wl, wvl, wt = struct.unpack_from('<HHH', b, o)
        if wl == 0: return b''
        o += 6
        key, o = rdwstr(b, o)
        o = (o + 3) & ~3
        vbytes = b[o: o + (wvl if wt == 0 else wvl * 2)]
        o += len(vbytes)
        o = (o + 3) & ~3
        ke = (key + '\x00').encode('utf-16-le')
        if wt == 1 and key in patches:
            ve = (patches[key] + '\x00').encode('utf-16-le')
            out = struct.pack('<HHH', 0, len(ve) // 2, 1) + ke
            while len(out) % 4: out += b'\x00'
            out += ve
            while len(out) % 4: out += b'\x00'
        else:
            kids, co = b'', o
            while co < s + wl:
                if co + 2 > len(b): break
                cl = struct.unpack_from('<H', b, co)[0]
                if cl == 0: break
                kids += rebuild(b, co)
                co = (co + cl + 3) & ~3
            out = struct.pack('<HHH', 0, wvl, wt) + ke
            while len(out) % 4: out += b'\x00'
            out += vbytes
            while len(out) % 4: out += b'\x00'
            out += kids
            while len(out) % 4: out += b'\x00'
        return struct.pack('<H', len(out)) + out[2:]
    return rebuild(raw, 0)

def _set_exe_description(exe_path: str, name: str):
    try:
        import win32api, win32con
    except ImportError:
        return
    h = win32api.LoadLibraryEx(exe_path, 0, win32con.LOAD_LIBRARY_AS_DATAFILE)
    try:
        res_names = win32api.EnumResourceNames(h, win32con.RT_VERSION)
        res_langs = win32api.EnumResourceLanguages(h, win32con.RT_VERSION, res_names[0])
        raw = bytes(win32api.LoadResource(h, win32con.RT_VERSION, res_names[0], res_langs[0]))
    finally:
        win32api.FreeLibrary(h)
    new_raw = _patch_ver_info(raw, {
        "FileDescription":  name,
        "InternalName":     name,
        "OriginalFilename": f"{name}.exe",
        "ProductName":      name,
    })
    hU = win32api.BeginUpdateResource(exe_path, False)
    win32api.UpdateResource(hU, win32con.RT_VERSION, res_names[0], new_raw)
    win32api.EndUpdateResource(hU, False)

def _ensure_tool_exe(tool_name: str, ico_path=None) -> Path:
    import shutil
    python_dir = Path(sys.executable).parent
    python_exe = python_dir / "pythonw.exe"
    tool_exe   = python_dir / f"{tool_name}.exe"
    ico        = Path(ico_path) if ico_path else None
    needs = (
        not tool_exe.exists()
        or python_exe.stat().st_mtime > tool_exe.stat().st_mtime
        or (ico and ico.exists() and ico.stat().st_mtime > tool_exe.stat().st_mtime)
    )
    if not needs and tool_exe.exists():
        try:
            import win32api
            vinfo = win32api.GetFileVersionInfo(str(tool_exe), r'\VarFileInfo\Translation')
            for _lang, _cp in vinfo:
                _desc = win32api.GetFileVersionInfo(str(tool_exe), f'\\StringFileInfo\\{_lang:04x}{_cp:04x}\\FileDescription')
                if _desc and 'Python' in _desc:
                    needs = True
                    break
        except Exception:
            pass
    if needs:
        shutil.copy2(str(python_exe), str(tool_exe))
        if ico and ico.exists():
            try:   _embed_icon_in_exe(str(tool_exe), str(ico))
            except Exception: pass
        try:   _set_exe_description(str(tool_exe), tool_name)
        except Exception: pass
    return tool_exe

# ── AppUserModelID — prevents Windows grouping this with other pythonw.exe apps ──
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ScreenshotTool.Launcher.1")
except Exception:
    pass

# ── Single-instance guard ─────────────────────────────────────────────────────
# use_last_error handle + explicit clear: windll.GetLastError() can read a
# stale 183 left by an earlier API call (it survives a SUCCESSFUL
# CreateMutexW), making a fresh launcher exit 0 as if a twin were running —
# which silently killed the hotkey (2026-06-10, same bug across all tools).
_k32_le = ctypes.WinDLL("kernel32", use_last_error=True)
ctypes.set_last_error(0)
_mutex = _k32_le.CreateMutexW(None, True, MUTEX_LAUNCHER)
if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
    ev = kernel32.OpenEventW(0x0002, False, EVENT_CAPTURE)
    if ev:
        kernel32.SetEvent(ev)
        kernel32.CloseHandle(ev)
    sys.exit(0)

def _get_pythonw() -> Path:
    venv = Path(__file__).parent / "venv" / "Scripts" / "pythonw.exe"
    if venv.exists():
        return venv
    system = Path(sys.executable).parent / "pythonw.exe"
    if system.exists():
        return system
    return Path(sys.executable)

PYTHONW = _get_pythonw()

# ── Register global hotkeys (retry — previous process may not have released yet) ─
for _attempt in range(20):
    if user32.RegisterHotKey(None, 1, MOD_CTRL_SHIFT, VK_P):
        _log(f"RegisterHotKey Ctrl+Shift+P succeeded on attempt {_attempt + 1}")
        break
    err = kernel32.GetLastError()
    _log(f"RegisterHotKey Ctrl+Shift+P attempt {_attempt + 1} failed, error={err}")
    time.sleep(0.3)
else:
    _log("RegisterHotKey Ctrl+Shift+P failed after all retries")

# ── Launch the main tool on startup ──────────────────────────────────────────
def tool_is_running():
    h = kernel32.OpenMutexW(0x00100000, False, MUTEX_TOOL)  # SYNCHRONIZE
    if h:
        kernel32.CloseHandle(h)
        return True
    return False

def signal_capture():
    ev = kernel32.OpenEventW(0x0002, False, EVENT_CAPTURE)  # EVENT_MODIFY_STATE
    if ev:
        kernel32.SetEvent(ev)
        kernel32.CloseHandle(ev)


def launch_tool_and_signal():
    subprocess.Popen(
        [str(PYTHONW), str(TOOL_SCRIPT)],
        creationflags=0x08000000    # CREATE_NO_WINDOW
    )
    # Wait for tool to initialise and create its event
    for _ in range(20):
        time.sleep(0.2)
        if tool_is_running():
            time.sleep(0.3)     # let event get created
            signal_capture()
            return

# Launch tool now if not already running (staggered — hotkey registers immediately)
_startup_timer = None
if not tool_is_running():
    if STARTUP_DELAY_S > 0:
        def _delayed_tool_start():
            if not tool_is_running():
                subprocess.Popen([str(PYTHONW), str(TOOL_SCRIPT)], creationflags=0x08000000)
        _startup_timer = threading.Timer(STARTUP_DELAY_S, _delayed_tool_start)
        _startup_timer.daemon = True
        _startup_timer.start()
    else:
        subprocess.Popen([str(PYTHONW), str(TOOL_SCRIPT)], creationflags=0x08000000)

# ── Windows message loop ──────────────────────────────────────────────────────
_log("message loop starting")
try:
    msg = ctypes.wintypes.MSG()
    while True:
        ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if ret == 0:
            _log("message loop exited normally (WM_QUIT received)")
            break
        if ret == -1:
            _log(f"GetMessageW returned -1 (error={kernel32.GetLastError()}) — exiting")
            break
        if msg.message == WM_HOTKEY:
            if msg.wParam == 1:   # Ctrl+Shift+P — capture
                if tool_is_running():
                    signal_capture()
                else:
                    # Cancel deferred startup and launch immediately.
                    if _startup_timer and _startup_timer.is_alive():
                        _startup_timer.cancel()
                    launch_tool_and_signal()
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
except Exception as e:
    _log(f"message loop exception: {e}")
    import traceback
    with open(os.path.join(os.environ["TEMP"], "screenshot_launcher.log"), "a", encoding="utf-8") as _f:
        _f.write(traceback.format_exc())
finally:
    _log("launcher exiting — unregistering hotkeys")
    user32.UnregisterHotKey(None, 1)
    user32.UnregisterHotKey(None, 2)
