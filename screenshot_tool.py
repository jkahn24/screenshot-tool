"""
SCREENSHOT TOOL
Ctrl+Shift+P   -> drag-select a region to capture
Ctrl+V         -> paste the captured image (standard clipboard)
Ctrl+Shift+V   -> type the file path of the last capture into any input
Escape         -> cancel in-progress capture
System tray    -> right-click for options / exit
"""

import os
import sys
import time
import queue
import threading
import datetime
import ctypes
from pathlib import Path

from PIL import Image, ImageGrab
import pystray
import tkinter as tk

APP_USER_MODEL_ID = "ScreenshotTool.Launcher.1"

# ── Config ────────────────────────────────────────────────────────────────────
SAVE_DIR      = Path(os.environ.get("SCREENSHOT_TOOL_DIR")
                     or Path.home() / "Downloads" / "Screenshots")
OVERLAY_ALPHA = 0.40
OVERLAY_COLOR = "#111111"
BORDER_COLOR  = "#FFFFFF"
BORDER_WIDTH  = 2
MIN_SIZE      = 10

# ── State ─────────────────────────────────────────────────────────────────────
last_path: str  = ""
capturing       = False
_capture_queue  = queue.Queue()   # main thread watches this to run captures
_path_timer     = [None]          # timer that clears last_path after 30s

PATH_TTL          = 30  # seconds before screenshot path is forgotten

def _request_capture():
    if not capturing:
        _capture_queue.put(1)

def _set_last_path(path: str):
    global last_path
    last_path = path
    if _path_timer[0]:
        _path_timer[0].cancel()
    t = threading.Timer(PATH_TTL, _clear_last_path)
    t.daemon = True
    t.start()
    _path_timer[0] = t

def _clear_last_path():
    global last_path
    last_path = ""
    _path_timer[0] = None

# ── DPI awareness — must be set before any window is created ──────────────────
# Without this, Windows virtualises GetSystemMetrics into logical pixels and
# then DPI-scales the window back up, causing the frozen screenshot to appear
# zoomed in on high-DPI or multi-monitor setups.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # Per-monitor DPI aware v1
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()    # Fallback: system DPI aware
    except Exception:
        pass

# ── Windows API ───────────────────────────────────────────────────────────────
kernel32 = ctypes.windll.kernel32

_click_wav_path = None   # path to WAV file on disk

_SHUTTER_WAV = Path(__file__).parent / "sounds" / "shutter_hard_click.wav"

def _get_sound_path():
    """Return the shutter WAV path, falling back to a generated synthetic click."""
    if _SHUTTER_WAV.exists():
        return str(_SHUTTER_WAV)
    # Fallback: generate synthetic click
    global _click_wav_path
    if _click_wav_path and Path(_click_wav_path).exists():
        return _click_wav_path
    import wave, struct, math, random
    sr = 44100
    samples = []
    rng = random.Random(42)
    for _ in range(int(sr * 0.003)):
        samples.append(rng.randint(-32767, 32767))
    for i in range(int(sr * 0.08)):
        t = i / sr
        env = math.exp(-t * 40)
        v   = env * (0.75 * math.sin(2 * math.pi * 600  * t) +
                     0.25 * math.sin(2 * math.pi * 1200 * t))
        samples.append(max(-32767, min(32767, int(v * 32767))))
    path = str(Path(os.environ["TEMP"]) / "ss_click_v3.wav")
    with wave.open(path, "w") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(sr)
        f.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    _click_wav_path = path
    return path

def beep():
    """Play camera-click via PowerShell — fresh audio session, bypasses Volume Mixer."""
    import subprocess
    try:
        path = _get_sound_path()
        _log(f"beep: launching powershell for {path}")
        subprocess.Popen(
            ["powershell", "-WindowStyle", "Hidden", "-NoProfile", "-Command",
             f"(New-Object Media.SoundPlayer '{path}').PlaySync()"],
            creationflags=0x08000000   # CREATE_NO_WINDOW
        )
        _log("beep: powershell launched")
    except Exception as e:
        _log(f"beep: powershell failed ({e}) — trying Beep fallback")
        try:
            kernel32.Beep(1800, 120)
            _log("beep: Beep fallback done")
        except Exception as e2:
            _log(f"beep: Beep fallback also failed ({e2})")

def set_clipboard_image(file_path: str):
    """Put the saved PNG on the clipboard via PowerShell (avoids ctypes pointer issues)."""
    import subprocess
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        f'[System.Windows.Forms.Clipboard]::SetImage([System.Drawing.Bitmap]::new("{file_path}"))'
    )
    try:
        subprocess.run(
            ["powershell", "-WindowStyle", "Hidden", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", ps],
            creationflags=0x08000000,
            timeout=15,  # a hung PowerShell must not freeze the tool
        )
    except subprocess.TimeoutExpired:
        _log("set_clipboard_image: powershell timed out after 15s")
    except Exception as e:
        _log(f"set_clipboard_image failed: {e}")

_TERMINAL_EXES = {
    "windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    "bash.exe", "wsl.exe", "wt.exe", "mintty.exe", "alacritty.exe",
    "conhost.exe", "hyper.exe", "terminus.exe",
    "cursor.exe", "code.exe",   # Electron editors with integrated terminals
}

def _foreground_exe() -> str:
    """Return the lowercased exe name of the current foreground window, or ''."""
    u32  = ctypes.windll.user32
    k32  = ctypes.windll.kernel32
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = ctypes.c_ulong(0)
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFO
    if not h:
        return ""
    try:
        buf  = ctypes.create_unicode_buffer(260)
        size = ctypes.c_ulong(260)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
    finally:
        k32.CloseHandle(h)
    return ""

def _active_window_is_terminal() -> bool:
    return _foreground_exe() in _TERMINAL_EXES

def _paste_image_from_clipboard():
    """Re-send Ctrl+V so the image (already on clipboard as CF_BITMAP) gets pasted."""
    u32     = ctypes.windll.user32
    VK_CTRL = 0x11
    VK_V    = 0x56
    UP      = 0x0002
    u32.keybd_event(VK_CTRL, 0, 0,  0)
    u32.keybd_event(VK_V,    0, 0,  0)
    u32.keybd_event(VK_V,    0, UP, 0)
    u32.keybd_event(VK_CTRL, 0, UP, 0)

def type_path(text: str):
    """Set clipboard text and paste via Ctrl+V — fully native, no subprocess."""
    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32

    # Explicit 64-bit-safe types (default c_int would truncate handles on 64-bit Windows)
    k32.GlobalAlloc.restype    = ctypes.c_void_p
    k32.GlobalAlloc.argtypes   = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalLock.restype     = ctypes.c_void_p
    k32.GlobalLock.argtypes    = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes  = [ctypes.c_void_p]
    u32.SetClipboardData.restype  = ctypes.c_void_p
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    data = (text + '\0').encode('utf-16-le')
    h    = k32.GlobalAlloc(0x0042, len(data))   # GHND = GMEM_MOVEABLE|GMEM_ZEROINIT
    ptr  = k32.GlobalLock(h)
    ctypes.memmove(ptr, data, len(data))
    k32.GlobalUnlock(h)
    u32.OpenClipboard(None)
    u32.EmptyClipboard()
    u32.SetClipboardData(13, h)                  # CF_UNICODETEXT = 13
    u32.CloseClipboard()

    # Inject Ctrl+V directly via Win32 — no subprocess, no pynput Controller
    # Release Shift first: caller may still have Shift held (Ctrl+Shift+V trigger),
    # which would turn Ctrl+V into Ctrl+Shift+V and cause a second paste loop.
    VK_SHIFT = 0x10
    VK_CTRL  = 0x11
    VK_V     = 0x56
    UP       = 0x02
    u32.keybd_event(VK_SHIFT, 0, UP, 0)
    u32.keybd_event(VK_CTRL, 0, 0,  0)
    u32.keybd_event(VK_V,    0, 0,  0)
    u32.keybd_event(VK_V,    0, UP, 0)
    u32.keybd_event(VK_CTRL, 0, UP, 0)

# ── Error log helper ─────────────────────────────────────────────────────────
def _log(msg):
    import traceback
    log_path = os.path.join(os.environ["TEMP"], "screenshot_tool.log")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()} — {msg}\n")
        if msg == "exception":
            f.write(traceback.format_exc())

# ── Overlay capture UI ────────────────────────────────────────────────────────
def do_capture():
    global capturing, last_path

    if capturing:
        return
    capturing = True
    try:
        _run_capture()
    except Exception:
        _log("exception")
    finally:
        capturing = False   # always reset so next Ctrl+Shift+P works

def _run_capture():
    global last_path

    from PIL import ImageTk, ImageFilter
    from PIL import ImageDraw as _IDraw

    SAVE_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. Freeze all monitors at physical resolution ─────────────────────────
    full_img       = ImageGrab.grab(all_screens=True)
    phys_w, phys_h = full_img.size
    _log(f"grabbed {phys_w}x{phys_h}")

    # ── 2. Build overlay window spanning all monitors ──────────────────────────
    _u32   = ctypes.windll.user32
    virt_x = _u32.GetSystemMetrics(76)
    virt_y = _u32.GetSystemMetrics(77)
    sw     = _u32.GetSystemMetrics(78)
    sh     = _u32.GetSystemMetrics(79)

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="black")
    root.geometry(f"{sw}x{sh}+{virt_x}+{virt_y}")
    root.update()
    _log(f"virtual desktop {virt_x},{virt_y} {sw}x{sh}")

    scale_x = phys_w / sw
    scale_y = phys_h / sh
    _log(f"scale {scale_x:.3f} x {scale_y:.3f}")

    display_img = full_img.resize((sw, sh), Image.LANCZOS) if (phys_w != sw or phys_h != sh) else full_img

    # ── 3. Frosted-glass overlay ───────────────────────────────────────────────
    # Downsample → blur → upsample (fast; full-res Gaussian is slow at 3K+ px)
    _bw = max(1, sw // 3)
    _bh = max(1, sh // 3)
    frosted = (display_img
               .resize((_bw, _bh), Image.BOX)
               .filter(ImageFilter.GaussianBlur(radius=1))
               .resize((sw, sh), Image.BILINEAR))

    dim    = Image.new("RGBA", (sw, sh), (0, 0, 0, 54))
    dimmed = Image.alpha_composite(frosted.convert("RGBA"), dim).convert("RGB")
    dimmed_tk = ImageTk.PhotoImage(dimmed)

    # Pre-render both a dark and light grid — redraw() picks based on the actual crop region
    GRID    = 46
    _GRID_A = 13   # 5% opacity (13/255)
    def _make_grid(r, g, b):
        ol = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        d  = _IDraw.Draw(ol)
        for _x in range(GRID, sw, GRID):
            d.line([(_x, 0), (_x, sh)], fill=(r, g, b, _GRID_A), width=1)
        for _y in range(GRID, sh, GRID):
            d.line([(0, _y), (sw, _y)], fill=(r, g, b, _GRID_A), width=1)
        return Image.alpha_composite(display_img.convert("RGBA"), ol).convert("RGB")
    display_grid_dark  = _make_grid(0, 0, 0)        # dark lines for bright backgrounds
    display_grid_light = _make_grid(255, 255, 255)   # light lines for dark backgrounds

    canvas = tk.Canvas(root, highlightthickness=0, bd=0, cursor="crosshair", bg="black")
    canvas.pack(fill="both", expand=True)
    canvas.create_image(0, 0, anchor="nw", image=dimmed_tk)
    canvas._dim = dimmed_tk

    # ── 4. Selection canvas items (pre-created, updated in-place) ─────────────
    HS   = 5
    FONT = ("Segoe UI", 11, "bold")
    PAD  = 6
    _sel_img  = canvas.create_image(0, 0, anchor="nw", state="hidden")
    # WYSIWYG backdrop preview (Recording + backdrop on). Sits above the dim
    # layer and the plain crop, below the white border / handles / pill.
    _bd_preview = canvas.create_image(0, 0, anchor="nw", state="hidden")
    _sel_rect = canvas.create_rectangle(0, 0, 1, 1, outline=BORDER_COLOR,
                                        width=BORDER_WIDTH, state="hidden")
    _corners  = [canvas.create_rectangle(0, 0, 1, 1, fill=BORDER_COLOR,
                                         outline="", state="hidden") for _ in range(4)]
    _pill_bg  = canvas.create_rectangle(0, 0, 1, 1, fill="#1C1C1E",
                                        outline=BORDER_COLOR, width=1, state="hidden")
    _pill_txt = canvas.create_text(0, 0, text="", fill="white",
                                   font=FONT, state="hidden")

    # ── 5. WhisperWin-style floating HUD bar ───────────────────────────────────
    HUD_W  = 620
    HUD_H  = 54
    HUD_R  = 14
    HUD_X  = (sw - HUD_W) // 2
    HUD_Y  = 36
    HUD_CY = HUD_Y + HUD_H // 2
    hud_pos = [HUD_X, HUD_Y]   # mutable — updated as cursor moves between monitors

    # Glass background: crop frosted screenshot at HUD pos, extra blur, dark tint
    _hx1, _hy1 = max(0, HUD_X), max(0, HUD_Y)
    _hx2, _hy2 = min(sw, HUD_X + HUD_W), min(sh, HUD_Y + HUD_H)
    _hud_crop  = (frosted.crop((_hx1, _hy1, _hx2, _hy2))
                  .filter(ImageFilter.GaussianBlur(radius=8)))
    _hud_base  = Image.new("RGBA", (HUD_W, HUD_H), (0, 0, 0, 0))
    _hud_base.paste(_hud_crop.convert("RGBA"), (0, 0))
    _hud_ov    = Image.new("RGBA", (HUD_W, HUD_H), (0, 0, 0, 0))
    _hd        = _IDraw.Draw(_hud_ov)
    _hd.rounded_rectangle([0, 0, HUD_W - 1, HUD_H - 1], radius=HUD_R,
                           fill=(12, 12, 18, 218))
    _hd.rounded_rectangle([0, 0, HUD_W - 1, HUD_H - 1], radius=HUD_R,
                           outline=(85, 85, 100, 200), width=1)
    _hud_img   = Image.alpha_composite(_hud_base, _hud_ov)
    _hud_photo = ImageTk.PhotoImage(_hud_img)
    canvas.create_image(HUD_X, HUD_Y, anchor="nw", image=_hud_photo, tags="hud")
    canvas._hud = _hud_photo

    _dx = HUD_X + 22
    canvas.create_oval(_dx - 5, HUD_CY - 5, _dx + 5, HUD_CY + 5,
                       fill="#FF453A", outline="", tags="hud")

    _mode_id = canvas.create_text(HUD_X + 36, HUD_CY, text="Single", anchor="w",
                                   fill="white", font=("Segoe UI", 12), tags="hud")

    canvas.create_text(HUD_X + 152, HUD_CY, text="S",
                       fill="#4A4A4A", font=("Segoe UI", 10), tags="hud")

    # Aspect-ratio lock indicator (all modes). Grey "Free R" when unlocked;
    # amber "16:9 R" / "9:16 R" / … when a ratio is active. R cycles it.
    _ratio_id = canvas.create_text(HUD_X + 184, HUD_CY, text="Free R", anchor="w",
                                   fill="#555560", font=("Segoe UI", 11), tags="hud")

    # Recording-mode indicators — format (F) and backdrop (B). Placed in the gap
    # between the centred confirm hint (~HUD_W/2) and the Esc/Cancel cluster so
    # they never overlap the "↵ record" copy or each other.
    _fmt_id = canvas.create_text(HUD_X + 350, HUD_CY, text="GIF F", anchor="w",
                                  fill="#E0B341", font=("Segoe UI", 11),
                                  state="hidden", tags="hud")
    _bg_id  = canvas.create_text(HUD_X + 402, HUD_CY, text="BG B", anchor="w",
                                  fill="#E0B341", font=("Segoe UI", 11),
                                  state="hidden", tags="hud")

    _sep_x = HUD_X + HUD_W - 94
    canvas.create_line(_sep_x, HUD_Y + 14, _sep_x, HUD_Y + HUD_H - 14,
                       fill="#3A3A3A", width=1, tags="hud")

    canvas.create_text(HUD_X + HUD_W - 18, HUD_CY, text="Cancel", anchor="e",
                       fill="#888888", font=("Segoe UI", 11), tags=("hud", "hud_cancel"))
    canvas.create_text(_sep_x - 10, HUD_CY, text="Esc", anchor="e",
                       fill="#3A3A3A", font=("Segoe UI", 10), tags="hud")

    _confirm_id = canvas.create_text(HUD_X + HUD_W // 2 - 20, HUD_CY,
                                      text="↵  capture", anchor="center",
                                      fill="#AAAAAA", font=("Segoe UI", 11),
                                      state="hidden", tags="hud")

    root.update()
    _log("overlay rendered")

    # ── Monitor detection for dynamic HUD positioning ─────────────────────────
    class _MRECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top",  ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    class _MINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _MRECT),
                    ("rcWork",  _MRECT),         ("dwFlags",   ctypes.c_ulong)]
    class _MPOINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    _last_hud_mon = [None]

    def _reposition_hud(cx, cy):
        """Move the HUD to the top-centre of whichever monitor cx,cy is on."""
        sx, sy = cx + virt_x, cy + virt_y
        hmon = _u32.MonitorFromPoint(_MPOINT(sx, sy), 2)  # MONITOR_DEFAULTTONEAREST
        info = _MINFO()
        info.cbSize = ctypes.sizeof(_MINFO)
        _u32.GetMonitorInfoW(hmon, ctypes.byref(info))
        r = info.rcMonitor
        mon_key = (r.left, r.top, r.right, r.bottom)
        if mon_key == _last_hud_mon[0]:
            return
        _last_hud_mon[0] = mon_key
        cl      = r.left  - virt_x   # monitor rect in canvas coords
        ct      = r.top   - virt_y
        mon_w   = r.right - r.left
        new_hx  = cl + (mon_w - HUD_W) // 2
        new_hy  = ct + 36
        dx, dy  = new_hx - hud_pos[0], new_hy - hud_pos[1]
        if dx != 0 or dy != 0:
            canvas.move("hud", dx, dy)
            hud_pos[0] = new_hx
            hud_pos[1] = new_hy

    state      = {"x1": 0, "y1": 0, "x2": 0, "y2": 0,
                  "done": False, "cancelled": False, "mode": "Single",
                  "has_sel": False, "fmt": "GIF", "backdrop": "auto", "bd_i": 1,
                  "ratio": None, "ratio_i": 0}
    crop_ref   = [None]
    bd_ref     = [None]   # keeps the backdrop-preview PhotoImage alive
    drag_ci    = [None]   # None = new drag, 0-3 = corner, 'n'/'s'/'w'/'e' = edge, 'move' = move
    drag_start = [None]   # (x, y) anchor for move drags
    CORNER_HIT = 14       # px radius for corner hit detection
    EDGE_HIT   = 8        # px tolerance for border hit detection

    def _in_hud(ex, ey):
        return hud_pos[0] <= ex <= hud_pos[0] + HUD_W and hud_pos[1] <= ey <= hud_pos[1] + HUD_H

    _MODE_CYCLE = ["Single", "Scrollable", "Recording"]
    # Backdrop looks cycled with B (Shift+B backwards). "Off" = None; the rest
    # are recorder style names so the preview and the export render identically.
    _BACKDROP_CYCLE = [("Off", None), ("Auto", "auto"), ("Indigo", "indigo"),
                       ("Sunset", "sunset"), ("Light", "light"), ("Mono", "mono")]

    def _refresh_mode_ui():
        rec = state["mode"] == "Recording"
        vis = "normal" if rec else "hidden"
        canvas.itemconfig(_fmt_id, text=state["fmt"] + " F", state=vis)
        canvas.itemconfig(_bg_id,  text=_BACKDROP_CYCLE[state["bd_i"]][0] + " B",
                          state=vis,
                          fill="#E0B341" if state["backdrop"] else "#555560")
        canvas.itemconfig(_confirm_id, text=("↵  record" if rec else "↵  capture"))

    def _toggle_mode(event=None):
        i = (_MODE_CYCLE.index(state["mode"]) + 1) % len(_MODE_CYCLE)
        state["mode"] = _MODE_CYCLE[i]
        canvas.itemconfig(_mode_id, text=state["mode"])
        _refresh_mode_ui()
        _show_backdrop_preview()

    def _toggle_fmt(event=None):
        if state["mode"] != "Recording":
            return
        state["fmt"] = "GIF" if state["fmt"] == "MP4" else "MP4"
        _refresh_mode_ui()

    def _cycle_backdrop(event=None, step=1):
        if state["mode"] != "Recording":
            return
        state["bd_i"] = (state["bd_i"] + step) % len(_BACKDROP_CYCLE)
        label, style = _BACKDROP_CYCLE[state["bd_i"]]
        state["backdrop"] = style
        canvas.itemconfig(_bg_id, text=label + " B",
                          fill="#E0B341" if style else "#555560")
        _show_backdrop_preview()

    # ── Aspect-ratio lock ─────────────────────────────────────────────────────
    _RATIO_CYCLE = [("Free", None), ("16:9", 16 / 9), ("9:16", 9 / 16),
                    ("1:1", 1.0), ("4:3", 4 / 3)]

    def _con_ratio(ax, ay, px, py):
        """Constrain the moving point (px,py) so the rect anchored at (ax,ay)
        keeps the locked aspect ratio (w/h), fit INSIDE the dragged box so it
        never overshoots the cursor. Returns (px,py) unchanged when unlocked."""
        r = state["ratio"]
        if not r:
            return px, py
        dx, dy = px - ax, py - ay
        adx, ady = abs(dx), abs(dy)
        if adx < 1 and ady < 1:
            return px, py
        if adx <= ady * r:          # width is the limiting dimension
            w, h = adx, adx / r
        else:                        # height is the limiting dimension
            w, h = ady * r, ady
        sx = 1 if dx >= 0 else -1
        sy = 1 if dy >= 0 else -1
        return int(round(ax + sx * w)), int(round(ay + sy * h))

    def _cycle_ratio(event=None):
        state["ratio_i"] = (state["ratio_i"] + 1) % len(_RATIO_CYCLE)
        name, val = _RATIO_CYCLE[state["ratio_i"]]
        state["ratio"] = val
        canvas.itemconfig(_ratio_id, text=name + " R",
                          fill="#E0B341" if val else "#555560")
        # Reshape an existing selection to the new ratio, top-left anchored.
        if state["has_sel"] and val:
            lx_, ty_, rx_, by_ = _norm()
            nx, ny = _con_ratio(lx_, ty_, rx_, by_)
            state["x1"], state["y1"] = lx_, ty_
            state["x2"], state["y2"] = nx, ny
            redraw(*_norm())
        _show_backdrop_preview()

    def _norm():
        return (min(state["x1"], state["x2"]), min(state["y1"], state["y2"]),
                max(state["x1"], state["x2"]), max(state["y1"], state["y2"]))

    def _corner_at(ex, ey):
        if not state["has_sel"]:
            return None
        lx_, ty_, rx_, by_ = _norm()
        for i, (cx, cy) in enumerate([(lx_, ty_), (rx_, ty_), (lx_, by_), (rx_, by_)]):
            if abs(ex - cx) <= CORNER_HIT and abs(ey - cy) <= CORNER_HIT:
                return i
        return None

    def _edge_at(ex, ey):
        """Return 'n'/'s'/'w'/'e' if the pointer is on a border, else None.
        Corners take priority — call _corner_at first."""
        if not state["has_sel"]:
            return None
        lx_, ty_, rx_, by_ = _norm()
        if abs(ey - ty_) <= EDGE_HIT and lx_ + EDGE_HIT <= ex <= rx_ - EDGE_HIT:
            return 'n'
        if abs(ey - by_) <= EDGE_HIT and lx_ + EDGE_HIT <= ex <= rx_ - EDGE_HIT:
            return 's'
        if abs(ex - lx_) <= EDGE_HIT and ty_ + EDGE_HIT <= ey <= by_ - EDGE_HIT:
            return 'w'
        if abs(ex - rx_) <= EDGE_HIT and ty_ + EDGE_HIT <= ey <= by_ - EDGE_HIT:
            return 'e'
        return None

    def _inside_sel(ex, ey):
        if not state["has_sel"]:
            return False
        lx_, ty_, rx_, by_ = _norm()
        return lx_ + EDGE_HIT < ex < rx_ - EDGE_HIT and ty_ + EDGE_HIT < ey < by_ - EDGE_HIT

    def redraw(lx, ty, rx, by):
        cw_sel = rx - lx
        ch_sel = by - ty
        canvas.itemconfig(_bd_preview, state="hidden")   # cleared during drag
        try:
            _region = display_img.crop((lx, ty, rx, by))
            _bright = sum(_region.resize((8, 8), Image.BOX).convert("L").getdata()) / 64
            src   = display_grid_dark if _bright > 150 else display_grid_light
            crop  = src.crop((lx, ty, rx, by))
            photo = ImageTk.PhotoImage(crop)
            crop_ref[0] = photo
            canvas.coords(_sel_img, lx, ty)
            canvas.itemconfig(_sel_img, image=photo, state="normal")
        except Exception:
            pass
        canvas.coords(_sel_rect, lx, ty, rx, by)
        canvas.itemconfig(_sel_rect, state="normal")
        for i, (hx, hy) in enumerate([(lx, ty), (rx, ty), (lx, by), (rx, by)]):
            canvas.coords(_corners[i], hx - HS, hy - HS, hx + HS, hy + HS)
            canvas.itemconfig(_corners[i], state="normal")
        label = f"{cw_sel} × {ch_sel}"
        canvas.itemconfig(_pill_txt, text=label, state="normal")
        bb = canvas.bbox(_pill_txt)
        if bb:
            pw = (bb[2] - bb[0]) + PAD * 2
            ph = (bb[3] - bb[1]) + PAD * 2
            px = max(0, rx - pw)
            py = by + 6
            if py + ph > sh:
                py = ty - 6 - ph
            canvas.coords(_pill_bg, px, py, px + pw, py + ph)
            canvas.itemconfig(_pill_bg, state="normal")
            canvas.coords(_pill_txt, px + pw // 2, py + ph // 2)

    def _show_backdrop_preview():
        """Recording + backdrop: render the real backdrop (gradient + shadow +
        rounded corners) around the selection so you see the exported framing
        before recording. Otherwise hide it. Call on drag/resize RELEASE and on
        toggles — not per drag pixel (it's a full PIL composite, ~50-150ms)."""
        if not (state["mode"] == "Recording" and state["backdrop"] and state["has_sel"]):
            canvas.itemconfig(_bd_preview, state="hidden")
            if state["has_sel"]:
                canvas.itemconfig(_sel_img, state="normal")   # restore plain crop
            return
        try:
            import recorder
            lx_, ty_, rx_, by_ = _norm()
            content = display_img.crop((lx_, ty_, rx_, by_))
            prev, pad, _radius = recorder.render_backdrop_preview(content, state["backdrop"])
            photo = ImageTk.PhotoImage(prev)
            bd_ref[0] = photo
            canvas.coords(_bd_preview, lx_ - pad, ty_ - pad)
            canvas.itemconfig(_bd_preview, image=photo, state="normal")
            canvas.itemconfig(_sel_img, state="hidden")   # preview shows content
        except Exception:
            _log("exception")
            canvas.itemconfig(_bd_preview, state="hidden")

    def confirm(event=None):
        state["done"] = True
        root.quit()

    def _space_confirm(event=None):
        # In Recording mode, Space is reserved for pause during the recording
        # phase, so it must NOT confirm/start here. Enter starts recording.
        if state["mode"] != "Recording":
            confirm()

    def _bind_initial():
        canvas.config(cursor="crosshair")
        canvas.bind("<ButtonPress-1>",   on_press)
        canvas.bind("<B1-Motion>",       on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        canvas.bind("<Motion>",          lambda e: _reposition_hud(e.x, e.y))

    def _bind_adjust():
        canvas.bind("<ButtonPress-1>",   post_press)
        canvas.bind("<B1-Motion>",       post_drag)
        canvas.bind("<ButtonRelease-1>", post_release)
        canvas.bind("<Motion>",          post_motion)

    # ── Initial selection drag ─────────────────────────────────────────────────
    def on_press(e):
        _reposition_hud(e.x, e.y)
        if _in_hud(e.x, e.y):
            _toggle_mode()
            return
        state["x1"] = state["x2"] = e.x
        state["y1"] = state["y2"] = e.y

    def on_drag(e):
        state["x2"], state["y2"] = _con_ratio(state["x1"], state["y1"], e.x, e.y)
        redraw(min(state["x1"], state["x2"]), min(state["y1"], state["y2"]),
               max(state["x1"], state["x2"]), max(state["y1"], state["y2"]))

    def on_release(e):
        state["x2"], state["y2"] = e.x, e.y
        lx_, rx_ = min(state["x1"], state["x2"]), max(state["x1"], state["x2"])
        ty_, by_ = min(state["y1"], state["y2"]), max(state["y1"], state["y2"])
        if rx_ - lx_ < MIN_SIZE or by_ - ty_ < MIN_SIZE:
            return
        state["has_sel"] = True
        canvas.itemconfig(_confirm_id, state="normal")
        root.bind("<Return>", confirm)
        root.bind("<space>",  _space_confirm)
        _bind_adjust()
        _show_backdrop_preview()

    # ── Post-selection: corner/edge resize, move, or start a new drag ─────────
    def post_press(e):
        _reposition_hud(e.x, e.y)
        if _in_hud(e.x, e.y):
            _toggle_mode()
            return
        ci = _corner_at(e.x, e.y)
        if ci is not None:
            lx_, ty_, rx_, by_ = _norm()
            state["x1"], state["y1"] = lx_, ty_
            state["x2"], state["y2"] = rx_, by_
            drag_ci[0] = ci
            return
        edge = _edge_at(e.x, e.y)
        if edge is not None:
            lx_, ty_, rx_, by_ = _norm()
            state["x1"], state["y1"] = lx_, ty_
            state["x2"], state["y2"] = rx_, by_
            drag_ci[0] = edge
            return
        if _inside_sel(e.x, e.y):
            lx_, ty_, rx_, by_ = _norm()
            state["x1"], state["y1"] = lx_, ty_
            state["x2"], state["y2"] = rx_, by_
            drag_ci[0] = 'move'
            drag_start[0] = (e.x, e.y)
            return
        # Clicked outside — clear and start a new selection
        drag_ci[0] = None
        state["has_sel"] = False
        canvas.itemconfig(_confirm_id, state="hidden")
        for c in _corners:
            canvas.itemconfig(c, state="hidden")
        canvas.itemconfig(_sel_img,  state="hidden")
        canvas.itemconfig(_sel_rect, state="hidden")
        canvas.itemconfig(_pill_bg,  state="hidden")
        canvas.itemconfig(_pill_txt, state="hidden")
        canvas.itemconfig(_bd_preview, state="hidden")
        state["x1"] = state["x2"] = e.x
        state["y1"] = state["y2"] = e.y
        _bind_initial()

    def post_drag(e):
        ci = drag_ci[0]
        if ci == 'move':
            dx = e.x - drag_start[0][0]
            dy = e.y - drag_start[0][1]
            drag_start[0] = (e.x, e.y)
            state["x1"] += dx; state["x2"] += dx
            state["y1"] += dy; state["y2"] += dy
        # Edges: with a ratio locked, the perpendicular dimension follows so the
        # ratio holds (top-left stays put). Unlocked → single-axis as before.
        elif ci == 'n':
            state["y1"] = e.y
            if state["ratio"]:
                state["x2"] = state["x1"] + round((state["y2"] - state["y1"]) * state["ratio"])
        elif ci == 's':
            state["y2"] = e.y
            if state["ratio"]:
                state["x2"] = state["x1"] + round((state["y2"] - state["y1"]) * state["ratio"])
        elif ci == 'w':
            state["x1"] = e.x
            if state["ratio"]:
                state["y2"] = state["y1"] + round((state["x2"] - state["x1"]) / state["ratio"])
        elif ci == 'e':
            state["x2"] = e.x
            if state["ratio"]:
                state["y2"] = state["y1"] + round((state["x2"] - state["x1"]) / state["ratio"])
        # Corners: _con_ratio anchors the OPPOSITE corner (no-op when unlocked).
        elif ci == 0: state["x1"], state["y1"] = _con_ratio(state["x2"], state["y2"], e.x, e.y)  # tl
        elif ci == 1: state["x2"], state["y1"] = _con_ratio(state["x1"], state["y2"], e.x, e.y)  # tr
        elif ci == 2: state["x1"], state["y2"] = _con_ratio(state["x2"], state["y1"], e.x, e.y)  # bl
        elif ci == 3: state["x2"], state["y2"] = _con_ratio(state["x1"], state["y1"], e.x, e.y)  # br
        else:
            state["x2"], state["y2"] = e.x, e.y
        redraw(min(state["x1"], state["x2"]), min(state["y1"], state["y2"]),
               max(state["x1"], state["x2"]), max(state["y1"], state["y2"]))

    def post_release(e):
        ci = drag_ci[0]
        drag_ci[0] = None
        if ci is None:
            return  # new drag goes through on_release via _bind_initial
        lx_, rx_ = min(state["x1"], state["x2"]), max(state["x1"], state["x2"])
        ty_, by_ = min(state["y1"], state["y2"]), max(state["y1"], state["y2"])
        if rx_ - lx_ >= MIN_SIZE and by_ - ty_ >= MIN_SIZE:
            state["has_sel"] = True
            canvas.itemconfig(_confirm_id, state="normal")
            _show_backdrop_preview()
        else:
            canvas.itemconfig(_confirm_id, state="hidden")

    def post_motion(e):
        _reposition_hud(e.x, e.y)
        if drag_ci[0] is not None:
            return  # keep cursor stable during active drag
        ci = _corner_at(e.x, e.y)
        if ci is not None:
            canvas.config(cursor=("size_nw_se", "size_ne_sw", "size_ne_sw", "size_nw_se")[ci])
            return
        edge = _edge_at(e.x, e.y)
        if edge is not None:
            canvas.config(cursor={"n": "size_ns", "s": "size_ns",
                                   "w": "size_we", "e": "size_we"}[edge])
            return
        if _inside_sel(e.x, e.y):
            canvas.config(cursor="fleur")
            return
        if _in_hud(e.x, e.y):
            canvas.config(cursor="hand2")
            return
        canvas.config(cursor="crosshair")

    def on_escape(e=None):
        state["cancelled"] = True
        root.quit()

    def poll_escape():
        if ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000:
            on_escape()
            return
        try:
            root.after(50, poll_escape)
        except Exception:
            pass

    canvas.tag_bind("hud_cancel", "<ButtonPress-1>", lambda e: on_escape())
    canvas.bind("<ButtonPress-1>",   on_press)
    canvas.bind("<B1-Motion>",       on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    canvas.bind("<Motion>",          lambda e: _reposition_hud(e.x, e.y))
    root.bind("<Escape>",            on_escape)
    root.bind("<s>",                 _toggle_mode)
    root.bind("<S>",                 _toggle_mode)
    root.bind("<f>",                 _toggle_fmt)
    root.bind("<F>",                 _toggle_fmt)
    root.bind("<b>",                 _cycle_backdrop)
    root.bind("<B>",                 lambda e: _cycle_backdrop(step=-1))
    root.bind("<r>",                 _cycle_ratio)
    root.bind("<R>",                 _cycle_ratio)
    root.focus_force()
    root.after(200, poll_escape)

    root.mainloop()

    # Read state BEFORE modifying the overlay
    done      = state["done"]
    cancelled = state["cancelled"]
    mode      = state.get("mode", "Single")
    fmt       = state.get("fmt", "MP4")
    x1, y1, x2, y2 = state["x1"], state["y1"], state["x2"], state["y2"]

    if cancelled or not done:
        _log("cancelled or not done")
        try: root.destroy()
        except Exception: pass
        return

    lx, rx = min(x1, x2), max(x1, x2)
    ty, by = min(y1, y2), max(y1, y2)
    cw, ch = rx - lx, by - ty
    _log(f"selection {lx},{ty} -> {rx},{by} ({cw}x{ch}) mode={mode}")

    if cw < MIN_SIZE or ch < MIN_SIZE:
        _log("too small")
        try: root.destroy()
        except Exception: pass
        return

    if mode == "Scrollable":
        # Keep dimmed background outside selection; punch a transparent black hole inside it
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=dimmed_tk)   # keep dim bg
        root.attributes("-transparentcolor", "black")
        # Black fill = transparent (passes clicks through to live page) — no border/status UI
        canvas.create_rectangle(lx, ty, rx, by, fill="black", outline="")
        root.update()

        frames = _do_scrollable_capture(lx, ty, rx, by, virt_x, virt_y, scale_x, scale_y,
                                         root, canvas, None,
                                         on_bottom_confirmed=None)
        try: root.destroy()
        except Exception: pass
        if not frames:
            _log("scrollable capture returned no frames")
            return

        ts        = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        file_path = SAVE_DIR / f"{ts}_screenshot.png"
        _set_last_path(str(file_path))
        if len(frames) == 1:
            frames[0].save(file_path, "PNG")
            set_clipboard_image(str(file_path))
        else:
            threading.Thread(target=_stitch_and_save,
                             args=(frames, file_path), daemon=True).start()
        return
    elif mode == "Recording":
        # Keep the overlay alive — _run_record reuses it (dim surround + a
        # transparent live hole over the region) and destroys it when done.
        _run_record(lx, ty, rx, by, virt_x, virt_y, scale_x, scale_y, fmt,
                    state.get("backdrop", True), root, canvas, dimmed_tk, display_img)
        return
    else:
        try: root.destroy()
        except Exception: pass
        crop_box = (int(lx * scale_x), int(ty * scale_y),
                    int(rx * scale_x), int(by * scale_y))
        _log(f"crop box physical: {crop_box}")
        img = full_img.crop(crop_box)

    beep()   # fire before PNG save — PIL compression starves audio thread if called after
    ts        = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    file_path = SAVE_DIR / f"{ts}_screenshot.png"
    img.save(file_path, "PNG")
    set_clipboard_image(str(file_path))
    _set_last_path(str(file_path))

def _do_scrollable_capture(lx, ty, rx, by, virt_x, virt_y, scale_x, scale_y,
                            overlay_root=None, overlay_canvas=None, status_id=None,
                            on_bottom_confirmed=None):
    """Auto-scroll the selected region and stitch frames into one tall image."""
    import time
    from PIL import ImageChops

    u32 = ctypes.windll.user32

    cursor_x   = (lx + rx) // 2 + virt_x
    cursor_y   = (ty + by) // 2 + virt_y
    p_lx, p_ty = int(lx * scale_x), int(ty * scale_y)
    p_rx, p_by = int(rx * scale_x), int(by * scale_y)
    region_h   = by - ty

    # Target scroll ≈ 30% of selection height per step to guarantee overlap.
    # Worst-case browser scroll rate ≈ 120px per notch → NOTCHES = region_h // (3 * 120).
    # Capped 1–8 so short frames always use 1 notch and large frames stay fast.
    NOTCHES = max(1, min(8, region_h // 200))
    MAX_FRAMES = 60

    def _status(text):
        if overlay_root and overlay_canvas and status_id:
            try:
                overlay_canvas.itemconfig(status_id, text=text)
                overlay_root.update()
            except Exception:
                pass

    u32.SetCursorPos(cursor_x, cursor_y)
    time.sleep(0.2)

    # Withdraw the overlay so it's completely out of the way.
    if overlay_root:
        try:
            overlay_root.withdraw()
            overlay_root.update()
        except Exception:
            pass
    time.sleep(0.15)

    # Find the window at the cursor position and post WM_MOUSEWHEEL directly to it.
    # PostMessage bypasses focus routing entirely — works regardless of which monitor
    # or which window currently has keyboard focus.
    WM_MOUSEWHEEL = 0x020A

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    hwnd_scroll = None
    try:
        hwnd = u32.WindowFromPoint(_POINT(cursor_x, cursor_y))
        if hwnd:
            hwnd_scroll = u32.GetAncestor(hwnd, 2)  # GA_ROOT
    except Exception:
        pass

    def _scroll_down():
        # wParam: high word = -120 (one notch down), low word = 0 (no modifier keys)
        # lParam: cursor position packed as (y << 16 | x)
        wp = ((-120) & 0xFFFF) << 16
        lp = ((cursor_y & 0xFFFF) << 16) | (cursor_x & 0xFFFF)
        if hwnd_scroll:
            u32.PostMessageW(hwnd_scroll, WM_MOUSEWHEEL, wp, lp)
        else:
            u32.mouse_event(0x0800, 0, 0, ctypes.c_long(-120).value, 0)

    def _scroll_up():
        wp = (120 & 0xFFFF) << 16
        lp = ((cursor_y & 0xFFFF) << 16) | (cursor_x & 0xFFFF)
        if hwnd_scroll:
            u32.PostMessageW(hwnd_scroll, WM_MOUSEWHEEL, wp, lp)
        else:
            u32.mouse_event(0x0800, 0, 0, ctypes.c_long(120).value, 0)

    frames = []

    for i in range(MAX_FRAMES):
        grab  = ImageGrab.grab(all_screens=True)
        frame = grab.crop((p_lx, p_ty, p_rx, p_by))

        if frames:
            diff = ImageChops.difference(frames[-1], frame)
            diff_score = sum(diff.convert("L").getdata()) if diff.getbbox() else 0
            threshold  = (p_rx - p_lx) * (p_by - p_ty) * 0.0005 * 255
            if diff_score > 0:
                _log(f"scrollable: frame {i} score={diff_score:.0f} threshold={threshold:.0f}")
            if diff_score < threshold:
                # Looks like bottom — retry scroll once to rule out dropped events
                for _ in range(NOTCHES * 2):
                    _scroll_down()
                    time.sleep(0.02)
                time.sleep(0.5)
                retry_grab  = ImageGrab.grab(all_screens=True)
                retry_frame = retry_grab.crop((p_lx, p_ty, p_rx, p_by))
                retry_diff  = ImageChops.difference(frame, retry_frame)
                if not retry_diff.getbbox():
                    _log(f"scrollable: bottom confirmed at frame {i} diff_score={diff_score:.0f}")
                    # Only append if this frame has new content vs the previous.
                    # diff_score==0  → pixel-perfect duplicate → skip (adds nothing).
                    # diff_score>0   → small final scroll → include the sliver.
                    if diff_score > 0:
                        frames.append(frame)
                    if on_bottom_confirmed:
                        on_bottom_confirmed()   # fire beep at the moment of capture
                    break
                # Scroll landed on retry — treat retry_frame as the next frame
                _log(f"scrollable: retry scroll succeeded at frame {i}, continuing")
                frames.append(frame)
                _log(f"scrollable: frame {len(frames)}")
                frame = retry_frame

        frames.append(frame)
        _log(f"scrollable: frame {len(frames)}")

        for _ in range(NOTCHES):
            _scroll_down()
            time.sleep(0.02)
        time.sleep(0.45)

    if not frames:
        return None

    # Scroll back to original position
    total_notches = (len(frames) - 1) * NOTCHES
    for _ in range(total_notches):
        _scroll_up()
        time.sleep(0.01)

    return frames


def _stitch_frames(frames):
    """Stitch consecutive frames by finding the true overlap length.

    Old approach: slide a small strip (40px) from the bottom of the accumulated
    result across the next frame and find the best match position. This fails for
    text-heavy documents where 40px of bullet-point text looks identical in many
    places (false positives → visible duplicate rows).

    New approach: for each candidate overlap length N, compare the bottom-N pixels
    of the accumulated result against the top-N pixels of the next frame. The
    correct overlap length produces a near-zero normalised diff; false positives
    are much harder to fake because the ENTIRE region must match, not just a strip.
    """
    from PIL import ImageChops, Image as _Img

    result = frames[0].copy()
    w = result.width

    for nxt in frames[1:]:
        h = result.height

        # Overlap can be up to 100% of the next frame's height.
        # A confirmed-bottom frame that had a small final scroll may overlap
        # nearly completely — capping at 90% would miss the true seam.
        max_ov  = nxt.height
        min_ov  = max(10, min(40, nxt.height // 10))

        # ── Coarse pass (every STEP pixels, images downsampled 4×) ──────────
        # Downsampling makes each iteration fast; coarse step narrows the range.
        STEP    = 4
        SCALE   = 4
        sw      = max(1, w // SCALE)

        best_ov, best_norm = min_ov, float('inf')

        for ov in range(min_ov, max_ov, STEP):
            ph = max(1, ov // SCALE)
            prev_s = result.crop((0, h - ov, w, h)).resize((sw, ph), _Img.BOX)
            nxt_s  = nxt.crop((0, 0, w, ov)).resize((sw, ph), _Img.BOX)
            raw    = sum(ImageChops.difference(prev_s, nxt_s).convert("L").getdata())
            norm   = raw / (sw * ph)          # per-pixel avg diff (0–255 scale)
            if norm < best_norm:
                best_norm, best_ov = norm, ov

        # ── Fine pass (±STEP around coarse winner, full resolution) ─────────
        fine_lo = max(min_ov, best_ov - STEP)
        fine_hi = min(max_ov, best_ov + STEP + 1)
        for ov in range(fine_lo, fine_hi):
            prev_p = result.crop((0, h - ov, w, h))
            nxt_p  = nxt.crop((0, 0, w, ov))
            raw    = sum(ImageChops.difference(prev_p, nxt_p).convert("L").getdata())
            norm   = raw / max(1, w * ov)
            if norm < best_norm:
                best_norm, best_ov = norm, ov

        _log(f"stitch: overlap={best_ov}px norm_diff={best_norm:.2f} "
             f"frame_h={nxt.height} new_part_h={nxt.height - best_ov}")

        new_part = nxt.crop((0, best_ov, w, nxt.height))
        if new_part.height == 0:
            continue

        combined = _Img.new("RGB", (w, h + new_part.height))
        combined.paste(result,   (0, 0))
        combined.paste(new_part, (0, h))
        result = combined

    return result


def _stitch_and_save(frames, file_path):
    """Background thread: stitch frames, save, set clipboard."""
    try:
        img = _stitch_frames(frames)
        img.save(file_path, "PNG")
        set_clipboard_image(str(file_path))
        _log(f"stitch saved: {file_path}")
        beep()   # fire AFTER save+clipboard — image is ready when you hear it
    except Exception:
        _log("exception in _stitch_and_save")


# ── Region recording (video / GIF) ────────────────────────────────────────────
def _finalize_recording(rec, mp4_temp, fmt, backdrop):
    """Background: convert/move the temp recording into Downloads/Screenshots."""
    try:
        import recorder
        if not mp4_temp:
            _log("record: nothing captured")
            rec.cleanup()
            return
        ts   = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        ext  = "gif" if fmt.upper() == "GIF" else "mp4"
        out  = SAVE_DIR / f"{ts}_recording.{ext}"
        ok   = recorder.finalize(mp4_temp, str(out), fmt=fmt, gif_fps=24,
                                 backdrop=backdrop, tmpdir=rec._tmp, log=_log)
        rec.cleanup()
        if ok:
            _set_last_path(str(out))
            _log(f"record: saved {out}")
            beep()
        else:
            _log("record: finalize failed")
    except Exception:
        _log("exception in _finalize_recording")


def _run_record(lx, ty, rx, by, virt_x, virt_y, scale_x, scale_y, fmt,
                backdrop, root, canvas, dimmed_tk, display_img=None):
    """Record the selected region to MP4/GIF, keeping the existing overlay up.

    The dimmed selection overlay stays on screen for the whole recording: the
    region is punched out as a transparent, click-through live hole (same
    technique as Scrollable capture) so the surroundings stay dimmed and you can
    see exactly what is being filmed. When a backdrop is selected, its gradient
    frame is drawn AROUND the hole (centre = the live hole) so the look you framed
    in the preview persists while recording; a small control bar (timer +
    Pause/Stop) floats outside the region. When recording ends the overlay is
    destroyed. gdigrab films only the hole rectangle, so neither the dim surround,
    the gradient padding, nor the glow ever appear in the raw capture — the real
    rounded backdrop is composited in finalize().
    """
    import recorder

    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    ox, oy, w, h = recorder.region_to_gdigrab(lx, ty, rx, by, virt_x, virt_y, scale_x, scale_y)
    _log(f"record: region {w}x{h} @ {ox},{oy} fmt={fmt}")
    rec = recorder.RegionRecorder(ox, oy, w, h, framerate=30, log=_log)

    # ── Keep the overlay; punch a transparent live hole over the selection ──
    canvas.delete("all")
    canvas.create_image(0, 0, anchor="nw", image=dimmed_tk)   # dim surround stays
    canvas._dim = dimmed_tk
    root.attributes("-transparentcolor", "black")
    # Backdrop frame STAYS ON SCREEN during recording: the gradient padding is
    # drawn around the region (its centre is the transparent live hole) so what
    # you framed in the preview persists. gdigrab films only the hole, so the
    # gradient is on-screen continuity — the real rounded backdrop is composited
    # in finalize(). Off (backdrop None) → plain transparent hole as before.
    _bd_drawn = False
    if backdrop and display_img is not None:
        try:
            from PIL import ImageTk as _ImageTk
            _content = display_img.crop((lx, ty, rx, by))
            _bd_frame, _bd_pad, _bd_r = recorder.render_backdrop_frame(_content, backdrop)
            _bd_photo = _ImageTk.PhotoImage(_bd_frame)
            canvas.create_image(lx - _bd_pad, ty - _bd_pad, anchor="nw", image=_bd_photo)
            canvas._bd_frame = _bd_photo   # keep a ref alive
            _bd_drawn = True
        except Exception:
            _log("exception")
    if not _bd_drawn:
        canvas.create_rectangle(lx, ty, rx, by, fill="black", outline="")  # live hole
    # Glowing halo (Loom-style) — concentric strokes OUTSIDE the hole (so never
    # filmed), animated to a soft pulse. Offsets/colours go dim->bright inward.
    _GLOW_REC   = [(13, (42, 12, 10)),  (10, (90, 23, 20)),  (7, (158, 42, 34)),
                   (4, (255, 91, 80)),  (2, (255, 129, 120))]
    _GLOW_PAUSE = [(13, (42, 30, 6)),   (10, (92, 66, 12)),  (7, (170, 120, 20)),
                   (4, (255, 214, 10)), (2, (255, 230, 120))]
    _glow_ids = []
    for _off, _col in _GLOW_REC:
        _glow_ids.append(canvas.create_rectangle(
            lx - _off, ty - _off, rx + _off, by + _off,
            outline="#%02x%02x%02x" % _col, width=2))
    # Stale selection-mode key bindings would touch deleted canvas items.
    for _seq in ("<s>", "<S>", "<f>", "<F>", "<b>", "<B>", "<r>", "<R>"):
        try: root.unbind(_seq)
        except Exception: pass
    root.update()

    # ── Floating control bar (Toplevel of the existing root) ──────────────
    BAR_W, BAR_H = 250, 56
    bx     = max(0, lx)
    by_pos = ty - BAR_H - 14
    if by_pos < 0:
        by_pos = by + 14

    bar = tk.Toplevel(root)
    bar.overrideredirect(True)
    bar.attributes("-topmost", True)
    bar.configure(bg="#0C0C12")
    bar.geometry(f"{BAR_W}x{BAR_H}+{bx + virt_x}+{by_pos + virt_y}")

    frame = tk.Frame(bar, bg="#0C0C12", highlightbackground="#33333D",
                     highlightthickness=1)
    frame.pack(fill="both", expand=True)
    inner = tk.Frame(frame, bg="#0C0C12")
    inner.pack(fill="both", expand=True, padx=12, pady=9)

    dot    = tk.Canvas(inner, width=14, height=14, bg="#0C0C12", highlightthickness=0)
    dot.pack(side="left", padx=(0, 8))
    dot_id = dot.create_oval(2, 2, 12, 12, fill="#FF453A", outline="")

    tlabel = tk.Label(inner, text="0:00", bg="#0C0C12", fg="white",
                      font=("Segoe UI", 13, "bold"), width=5, anchor="w")
    tlabel.pack(side="left")

    st = {"sec": 0.0, "t0": None, "paused": False, "stopping": False, "blink": True}

    def _fmt_secs(s):
        s = int(s)
        return f"{s // 60}:{s % 60:02d}"

    _BTN = dict(font=("Segoe UI", 10, "bold"), relief="flat", bd=0,
                cursor="hand2", padx=12, pady=3, activeforeground="white")
    stop_btn  = tk.Button(inner, text="Stop",  bg="#3A1414", fg="#FF6B6B",
                          activebackground="#511B1B", **_BTN)
    pause_btn = tk.Button(inner, text="Pause", bg="#1A1A22", fg="#DDDDDD",
                          activebackground="#2A2A33", **_BTN)
    stop_btn.pack(side="right", padx=(6, 0))
    pause_btn.pack(side="right")

    def _tick():
        if st["stopping"]:
            return
        if not st["paused"] and st["t0"] is not None:
            now = time.monotonic()
            st["sec"] += now - st["t0"]
            st["t0"] = now
        tlabel.config(text=_fmt_secs(st["sec"]))
        if st["paused"]:
            dot.itemconfig(dot_id, fill="#FFD60A")          # solid amber = paused
        else:
            st["blink"] = not st["blink"]
            dot.itemconfig(dot_id, fill="#FF453A" if st["blink"] else "#5A1714")
        try:
            root.after(500, _tick)
        except Exception:
            pass

    def _toggle_pause(event=None):
        if st["stopping"]:
            return
        if st["paused"]:
            rec.resume()
            st["paused"] = False
            st["t0"] = time.monotonic()
            pause_btn.config(text="Pause")
        else:
            rec.pause()
            st["paused"] = True
            pause_btn.config(text="Resume")

    def _stop(event=None):
        if st["stopping"]:
            return
        st["stopping"] = True
        try:
            root.quit()
        except Exception:
            pass

    _glow = {"phase": 0.0}
    def _animate_glow():
        if st["stopping"]:
            return
        import math
        _glow["phase"] += 0.20
        f = 0.70 + 0.30 * (0.5 + 0.5 * math.sin(_glow["phase"]))   # 0.70 .. 1.0
        base = _GLOW_PAUSE if st["paused"] else _GLOW_REC
        for gid, (_o, col) in zip(_glow_ids, base):
            c = "#%02x%02x%02x" % tuple(min(255, int(ch * f)) for ch in col)
            try: canvas.itemconfig(gid, outline=c)
            except Exception: pass
        try: root.after(45, _animate_glow)
        except Exception: pass

    stop_btn.config(command=_stop)
    pause_btn.config(command=_toggle_pause)
    for _w in (root, bar):
        _w.bind("<Return>", _stop)
        _w.bind("<Escape>", _stop)
        _w.bind("<space>",  _toggle_pause)
    bar.focus_force()

    rec.start()
    st["t0"] = time.monotonic()
    root.after(500, _tick)
    root.after(45, _animate_glow)
    root.mainloop()

    try:
        root.destroy()
    except Exception:
        pass

    mp4_temp = rec.stop()
    threading.Thread(target=_finalize_recording,
                     args=(rec, mp4_temp, fmt, backdrop), daemon=True).start()

# ── Global hotkey listener ────────────────────────────────────────────────────
def start_hotkey_listener():
    from pynput import keyboard

    held = set()

    def norm(key):
        try:
            if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
                return "ctrl"
            if key in (keyboard.Key.shift, keyboard.Key.shift_r):
                return "shift"
        except Exception:
            pass
        return key

    def on_press(key):
        held.add(norm(key))
        ctrl  = "ctrl"  in held
        shift = "shift" in held
        vk    = getattr(key, "vk", None)

        if ctrl and shift and vk == 86:   # Ctrl+Shift+V
            exe = _foreground_exe()
            in_terminal = (not exe) or (exe in _TERMINAL_EXES)
            _log(f"Ctrl+Shift+V: exe={exe!r} in_terminal={in_terminal} last_path={bool(last_path)}")
            if in_terminal and last_path:
                path = last_path
                _clear_last_path()
                if _path_timer[0]:
                    _path_timer[0].cancel()
                    _path_timer[0] = None
                held.clear()
                # Empty clipboard NOW (synchronous, fires before terminal processes
                # the key) so the terminal's own Ctrl+Shift+V paste is a no-op.
                u32_cb = ctypes.windll.user32
                if u32_cb.OpenClipboard(None):
                    u32_cb.EmptyClipboard()
                    u32_cb.CloseClipboard()
                threading.Timer(0.15, lambda p=path: type_path(p)).start()

    def on_release(key):
        held.discard(norm(key))

    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()

# ── System tray ───────────────────────────────────────────────────────────────
def make_tray_icon():
    """Dark rounded-square with purple radial gradient + corner brackets + crosshair."""
    import numpy as np
    from PIL import ImageDraw
    sz = 64
    cx, cy  = sz * 0.45, sz * 0.38
    max_r   = sz * 0.60
    y, x    = np.mgrid[0:sz, 0:sz]
    dist    = np.clip(np.sqrt((x - cx)**2 + (y - cy)**2) / max_r, 0, 1)
    t1 = np.minimum(dist / 0.6, 1.0)
    t2 = np.clip((dist - 0.6) / 0.4, 0, 1)
    r  = np.where(dist <= 0.6, 110 + (45  - 110) * t1,  45 + (12 -  45) * t2)
    g  = np.where(dist <= 0.6,  80 + (35  -  80) * t1,  35 + (10 -  35) * t2)
    b  = np.where(dist <= 0.6, 220 + (140 - 220) * t1, 140 + (40 - 140) * t2)
    bg = Image.fromarray(
        np.stack([r, g, b, np.full_like(r, 255)], axis=-1).astype(np.uint8), 'RGBA')
    mask = Image.new('L', (sz, sz), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, sz-1, sz-1],
                                           radius=max(1, int(sz * 0.13)), fill=255)
    img = Image.new('RGBA', (sz, sz), (0, 0, 0, 0))
    img.paste(bg, (0, 0), mask)
    draw  = ImageDraw.Draw(img)
    glyph = (235, 228, 252, 255)
    pad, arm, lw = int(sz*0.215), int(sz*0.135), max(2, int(sz*0.055))
    P, A, E = pad, arm, sz - pad
    draw.line([(P+A, P), (P, P), (P, P+A)], fill=glyph, width=lw)
    draw.line([(E-A, P), (E, P), (E, P+A)], fill=glyph, width=lw)
    draw.line([(P, E-A), (P, E), (P+A, E)], fill=glyph, width=lw)
    draw.line([(E-A, E), (E, E), (E, E-A)], fill=glyph, width=lw)
    cc  = sz // 2
    gap = max(1, int(sz * 0.055))
    ca  = max(2, int(sz * 0.085))
    cw  = max(1, int(sz * 0.035))
    draw.line([(cc, cc-gap-ca), (cc, cc-gap)],    fill=glyph, width=cw)
    draw.line([(cc, cc+gap),    (cc, cc+gap+ca)], fill=glyph, width=cw)
    draw.line([(cc-gap-ca, cc), (cc-gap, cc)],    fill=glyph, width=cw)
    draw.line([(cc+gap, cc),    (cc+gap+ca, cc)], fill=glyph, width=cw)
    return img

def open_folder(_icon=None, _item=None):
    os.startfile(str(SAVE_DIR))

def exit_tool(icon, _item):
    icon.stop()
    os._exit(0)   # cleanly terminates all threads

def start_capture_event_watcher():
    """Watch for the named Windows event signalled by launcher.py (Ctrl+Shift+P)."""
    EVENT_NAME = "ScreenshotTool_CaptureEvent"
    h = ctypes.windll.kernel32.CreateEventW(None, False, False, EVENT_NAME)
    while True:
        result = ctypes.windll.kernel32.WaitForSingleObject(h, 500)
        if result == 0:     # WAIT_OBJECT_0 — event was signalled
            _request_capture()


def run_tray():
    menu = pystray.Menu(
        pystray.MenuItem("Capture  (Ctrl+Shift+P)",
                         lambda i, it: _request_capture()),
        pystray.MenuItem("Open Screenshots Folder", open_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", exit_tool),
    )
    icon = pystray.Icon("ScreenshotTool", make_tray_icon(), "Screenshot Tool", menu)

    threading.Thread(target=start_hotkey_listener,       daemon=True).start()
    threading.Thread(target=start_capture_event_watcher, daemon=True).start()

    # Pystray runs in a background thread so the main thread stays free for tkinter
    threading.Thread(target=icon.run, daemon=True).start()

    # Main thread: drives all tkinter UI — required by tkinter's thread model
    while True:
        try:
            _capture_queue.get(timeout=0.3)
            do_capture()
        except queue.Empty:
            pass

# ── Tool exe builder (proper name + icon in Task Manager) ─────────────────────
def _embed_icon_in_exe(exe_path: str, ico_path: str):
    """Embed an .ico file as RT_ICON/RT_GROUP_ICON resources in a Windows PE exe."""
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
    """Update FileDescription, InternalName, OriginalFilename, ProductName in a PE exe."""
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
    """Create/refresh a renamed pythonw.exe in the Python dir with custom icon + description."""
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

# ── Startup shortcut ──────────────────────────────────────────────────────────
def _set_lnk_aumid(lnk_path: str, aumid: str):
    """Embed AppUserModelID in an existing .lnk via IPropertyStore COM.
    Prevents Windows from grouping this shortcut with other pythonw.exe processes."""
    try:
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)

        def _gbuf(s):
            s = s.strip("{}").replace("-", "")
            return (ctypes.c_byte * 16)(*(
                int(s[0:8],  16).to_bytes(4, "little") +
                int(s[8:12], 16).to_bytes(2, "little") +
                int(s[12:16],16).to_bytes(2, "little") +
                bytes.fromhex(s[16:32])
            ))

        CLSID_SL = _gbuf("{00021401-0000-0000-C000-000000000046}")
        IID_PF   = _gbuf("{0000010B-0000-0000-C000-000000000046}")
        IID_PS   = _gbuf("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")

        ppv = ctypes.c_void_p()
        if ole32.CoCreateInstance(CLSID_SL, None, 1, IID_PF, ctypes.byref(ppv)):
            return
        vt = ctypes.cast(ppv, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]

        # IPersistFile::Load (vtable[5], STGM_READWRITE=2)
        ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong)(
            vt[5])(ppv.value, lnk_path, 2)

        # QI for IPropertyStore (IUnknown::QueryInterface = vtable[0])
        ppps = ctypes.c_void_p()
        if ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p,
                               ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p))(
                vt[0])(ppv.value, bytes(IID_PS), ctypes.byref(ppps)):
            return
        vps = ctypes.cast(ppps, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]

        # PKEY_AppUserModel_ID = {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, pid=5
        pkey = (ctypes.c_byte * 20)(*(
            bytes(_gbuf("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}")) + (5).to_bytes(4, "little")
        ))

        class _PV(ctypes.Structure):
            _fields_ = [("vt", ctypes.c_ushort), ("_pad", ctypes.c_byte * 6),
                        ("ptr", ctypes.c_wchar_p)]

        pv = _PV(vt=31, ptr=aumid)   # VT_LPWSTR = 31

        # SetValue (vtable[6]), Commit (vtable[7])
        ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p,
                           ctypes.POINTER(ctypes.c_byte * 20),
                           ctypes.POINTER(_PV))(vps[6])(ppps.value, ctypes.byref(pkey), ctypes.byref(pv))
        ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(vps[7])(ppps.value)

        # IPersistFile::Save (vtable[6], None = save to current path)
        ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_bool)(
            vt[6])(ppv.value, None, True)
    except Exception:
        pass   # never crash the tray tool over shortcut housekeeping


def ensure_startup():
    """Create/refresh both .lnk files and embed the AppUserModelID in each.

    ScreenshotTool.lnk  (Startup folder) — autostart at login
    Screenshot Tool.lnk (Programs folder) — what Windows Search surfaces

    Both must carry the same AppUserModelID so Windows never confuses this
    app with another pythonw.exe process (e.g. another Python tray tool).
    """
    appdata  = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    startup  = appdata / "Startup"
    launcher = Path(__file__).parent / "launcher.py"
    # WDAC-safe: use the original signed pythonw.exe, never a renamed copy.
    tool_exe = Path(sys.executable).parent / "pythonw.exe"
    icon     = Path(__file__).parent / "icon.ico"

    def _write_lnk(lnk_path: Path, description: str):
        ps1 = Path(os.environ["TEMP"]) / "make_ss_shortcut.ps1"
        icon_arg = f'$s.IconLocation = @"\n{icon},0\n"@\n' if icon.exists() else ""
        ps1.write_text(
            f'$ws = New-Object -ComObject WScript.Shell\n'
            f'$s = $ws.CreateShortcut(@"\n{lnk_path}\n"@)\n'
            f'$s.TargetPath = @"\n{tool_exe}\n"@\n'
            f'$s.Arguments = @"\n"{launcher}"\n"@\n'
            f'$s.WorkingDirectory = @"\n{launcher.parent}\n"@\n'
            f'$s.Description = "{description}"\n'
            f'{icon_arg}'
            f'$s.WindowStyle = 7\n'
            f'$s.Save()\n'
        )
        import subprocess
        try:
            subprocess.run(
                ["powershell", "-WindowStyle", "Hidden", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                capture_output=True,
                timeout=30,  # startup .lnk write must never hang the boot path
            )
        except Exception as e:
            _log(f"_write_lnk: {e}")
        _set_lnk_aumid(str(lnk_path), APP_USER_MODEL_ID)

    _write_lnk(startup / "ScreenshotTool.lnk",    "Screenshot Tool launcher")
    _write_lnk(appdata / "Screenshot Tool.lnk",   "Screenshot Tool - Ctrl+Shift+P to capture")

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass

    # Single-instance guard — exit immediately if already running.
    # use_last_error handle + explicit clear: a stale 183 from an earlier API
    # call (e.g. the AUMID call above) survives a SUCCESSFUL CreateMutexW, so
    # the bare windll.GetLastError() pattern made fresh instances exit 0 as if
    # a twin were running (2026-06-10 — same bug class as all the launchers).
    _k32_le = ctypes.WinDLL("kernel32", use_last_error=True)
    ctypes.set_last_error(0)
    _mutex = _k32_le.CreateMutexW(None, True, "ScreenshotTool_SingleInstance")
    if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
        sys.exit(0)

    # Top-level crash handler — pythonw has no stderr, so without this a
    # crash in tray setup/loop vanishes without a trace.
    try:
        ensure_startup()
        run_tray()
    except Exception:
        import traceback
        try:
            _log("FATAL: " + traceback.format_exc())
        except Exception:
            pass
        raise
