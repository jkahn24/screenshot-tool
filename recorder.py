"""
RECORDER — screen-region video/GIF engine for Screenshot Tool.

Captures a fixed screen rectangle with ffmpeg's native gdigrab (low CPU,
smooth) and finalises to MP4 or animated GIF. Pause/resume is implemented as
stop-segment / start-new-segment, then a lossless concat on final stop, so
paused wall-clock time never appears in the output.

ffmpeg binary comes from the pip-managed `imageio-ffmpeg` package — no PATH
dependency, no manual binary to commit. Audio is intentionally out of scope.

Coordinate convention
---------------------
The caller passes the region in the SAME units the capture overlay uses
(GetSystemMetrics / canvas coords) plus the virtual-desktop origin and the
physical scale factors, exactly as `_run_capture` already computes them:

    ox_logical = virt_x + lx      oy_logical = virt_y + ty
    physical   = logical * scale

gdigrab offsets are relative to the PRIMARY monitor's physical top-left and
may be negative (monitors left of / above primary), which gdigrab supports.
"""

import os
import colorsys
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg

CREATE_NO_WINDOW = 0x08000000

# ── Encoding quality knobs ────────────────────────────────────────────────
# How we shrink the file WITHOUT a visible quality drop, two facts driving it:
#   1. At a fixed CRF the file size is set by the CRF value + content, NOT the
#      preset — a slower preset spends the SAME bytes, just more wisely. So the
#      real size lever is the CRF number (crf 23->26 ≈ 25% smaller).
#   2. A slower preset gives better quality-per-bit, which is exactly what lets
#      us raise the CRF a notch and NOT see it: `slow` @ crf 26 holds the
#      perceptual quality of the old `veryfast` @ crf 23, at ~25% fewer bytes.
# So: capture real-time at a near-transparent CRF (gdigrab can't wait on a slow
# encoder — that would drop frames = real quality loss), then re-encode ONCE off
# the hot path (background finalize thread) with the slow preset + higher CRF.
# The clean CAPTURE_CRF source keeps that second pass free of generation loss.
# Values are strings (fed straight to the ffmpeg argv). Knobs, if needed:
#   • text looks soft  -> lower FINAL_CRF toward 23 (bigger, sharper)
#   • want it smaller  -> raise FINAL_CRF to 28 (screen content tolerates it)
#   • finalize too slow on 4K regions -> FINAL_PRESET = "medium"
CAPTURE_CRF    = "18"        # near-transparent capture → clean source to compress
CAPTURE_PRESET = "veryfast"  # real-time safe; never drops frames
FINAL_CRF      = "26"        # visually lossless for screen/UI; ~25% under old crf 23
FINAL_PRESET   = "slow"      # better quality-per-bit → buys back the higher CRF


def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def _even(n: int) -> int:
    """yuv420p requires even width/height."""
    n = int(round(n))
    return n if n % 2 == 0 else n + 1


def region_to_gdigrab(lx, ty, rx, by, virt_x, virt_y, scale_x, scale_y):
    """Map an overlay selection to gdigrab (offset_x, offset_y, width, height)
    in primary-relative physical pixels, with even dimensions."""
    ox = int(round((virt_x + lx) * scale_x))
    oy = int(round((virt_y + ty) * scale_y))
    w  = _even((rx - lx) * scale_x)
    h  = _even((by - ty) * scale_y)
    return ox, oy, max(2, w), max(2, h)


class RegionRecorder:
    """One recording session over a fixed physical rectangle.

    Usage:
        r = RegionRecorder(ox, oy, w, h)
        r.start()
        ... r.pause() / r.resume() any number of times ...
        mp4_temp = r.stop()          # path to a finalised temp .mp4
        recorder.finalize(mp4_temp, out_path, fmt="GIF"|"MP4")
    """

    def __init__(self, ox, oy, w, h, framerate=30, log=None):
        self.ox, self.oy, self.w, self.h = ox, oy, w, h
        self.framerate = framerate
        self._log = log or (lambda m: None)
        self._tmp = Path(tempfile.mkdtemp(prefix="ss_rec_"))
        self._segments = []          # finalised segment paths, in order
        self._proc = None            # current ffmpeg process
        self._seg_index = 0
        self.recording = False
        self.paused = False

    # ── segment lifecycle ────────────────────────────────────────────────
    def _start_segment(self):
        seg = self._tmp / f"seg_{self._seg_index:03d}.mp4"
        self._seg_index += 1
        cmd = [
            ffmpeg_exe(), "-y",
            "-f", "gdigrab",
            "-framerate", str(self.framerate),
            "-offset_x", str(self.ox), "-offset_y", str(self.oy),
            "-video_size", f"{self.w}x{self.h}",
            "-i", "desktop",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-crf", CAPTURE_CRF, "-preset", CAPTURE_PRESET,
            "-movflags", "+faststart",
            str(seg),
        ]
        self._log(f"recorder: start segment {seg.name} {self.w}x{self.h}@{self.ox},{self.oy}")
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
        )
        self._cur_seg = seg

    def _stop_segment(self):
        """Gracefully stop the current ffmpeg so the segment is a valid mp4."""
        if not self._proc:
            return
        try:
            # 'q' on stdin = clean shutdown (writes the moov atom). terminate()
            # would leave an unplayable file with no moov.
            self._proc.communicate(input=b"q", timeout=10)
        except Exception as e:
            self._log(f"recorder: graceful stop failed ({e}) — terminating")
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                pass
        self._proc = None
        try:
            if self._cur_seg.exists() and self._cur_seg.stat().st_size > 0:
                self._segments.append(self._cur_seg)
        except Exception:
            pass

    # ── public API ───────────────────────────────────────────────────────
    def start(self):
        if self.recording:
            return
        self.recording = True
        self.paused = False
        self._start_segment()

    def pause(self):
        if not self.recording or self.paused:
            return
        self._stop_segment()
        self.paused = True
        self._log("recorder: paused")

    def resume(self):
        if not self.recording or not self.paused:
            return
        self.paused = False
        self._start_segment()
        self._log("recorder: resumed")

    def toggle_pause(self):
        self.resume() if self.paused else self.pause()

    def stop(self):
        """Finalise: stop current segment, concat all segments → one temp mp4.
        Returns the temp mp4 path, or None if nothing was captured."""
        if not self.recording:
            return None
        if not self.paused:
            self._stop_segment()
        self.recording = False

        if not self._segments:
            self._log("recorder: stop with no segments")
            return None
        if len(self._segments) == 1:
            return str(self._segments[0])

        # Multiple segments (pause/resume happened) → lossless concat.
        out = self._tmp / "joined.mp4"
        listfile = self._tmp / "concat.txt"
        listfile.write_text(
            "".join(f"file '{s.as_posix()}'\n" for s in self._segments),
            encoding="utf-8",
        )
        cmd = [
            ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0",
            "-i", str(listfile), "-c", "copy", str(out),
        ]
        self._log(f"recorder: concat {len(self._segments)} segments")
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW, timeout=120)
        except Exception as e:
            self._log(f"recorder: concat failed ({e}) — returning first segment")
            return str(self._segments[0])
        return str(out) if out.exists() else str(self._segments[0])

    def cleanup(self):
        """Remove the temp working dir. Call AFTER finalize has copied the result."""
        try:
            import shutil
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:
            pass


def compress_mp4(in_mp4: str, out_mp4: str, log=None) -> bool:
    """Re-encode an mp4 with the slow, size-efficient FINAL preset. Used when no
    backdrop is applied (the backdrop path already re-encodes) so the delivered
    file isn't the bulky real-time capture. Quality is preserved: the capture is
    a clean CAPTURE_CRF source and this compresses it once at FINAL_CRF."""
    _log = log or (lambda m: None)
    cmd = [ffmpeg_exe(), "-y", "-i", in_mp4,
           "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-crf", FINAL_CRF, "-preset", FINAL_PRESET,
           "-movflags", "+faststart", out_mp4]
    _log(f"recorder: compress -> crf {FINAL_CRF} preset {FINAL_PRESET}")
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=CREATE_NO_WINDOW, timeout=300)
        return os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 0
    except Exception as e:
        _log(f"recorder: compress failed ({e})")
        return False


def to_gif(mp4_path: str, gif_path: str, fps: int = 24, log=None) -> bool:
    """Convert an mp4 to a palette-optimised animated GIF (no dithering banding)."""
    _log = log or (lambda m: None)
    vf = (f"fps={fps},split[s0][s1];"
          f"[s0]palettegen=stats_mode=diff[p];"
          f"[s1][p]paletteuse=dither=bayer:bayer_scale=3")
    cmd = [ffmpeg_exe(), "-y", "-i", mp4_path, "-vf", vf, "-loop", "0", gif_path]
    _log(f"recorder: mp4 -> gif @ {fps}fps")
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=CREATE_NO_WINDOW, timeout=180)
        return os.path.exists(gif_path) and os.path.getsize(gif_path) > 0
    except Exception as e:
        _log(f"recorder: gif conversion failed ({e})")
        return False


# ── Adaptive backdrop (Screen Studio-style padded gradient) ───────────────
def _extract_frame(mp4_path, log=None):
    """Grab the first frame of a recording as a PIL RGB image (for colour sampling)."""
    from PIL import Image
    tmp = str(Path(tempfile.gettempdir()) / "ss_rec_frame.png")
    cmd = [ffmpeg_exe(), "-y", "-i", mp4_path, "-frames:v", "1", tmp]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=CREATE_NO_WINDOW, timeout=30)
        if os.path.exists(tmp):
            img = Image.open(tmp).convert("RGB")
            img.load()
            return img
    except Exception as e:
        if log: log(f"recorder: frame extract failed ({e})")
    return None


def _dominant_colors(img, n=3):
    """Top-n dominant colours of a PIL image as (r,g,b) tuples."""
    small = img.resize((48, 48))
    q = small.quantize(colors=8)
    pal = q.getpalette() or []
    out = []
    for _cnt, idx in sorted(q.getcolors() or [], reverse=True):
        out.append(tuple(pal[idx * 3: idx * 3 + 3]))
    return out[:n] or [(40, 40, 48)]


def _mix(c, t, target):
    """Move colour c a fraction t toward target."""
    return tuple(int(c[i] + (target[i] - c[i]) * t) for i in range(3))


def _stop(h, s, v):
    """HSV (0-1) -> (r,g,b) 0-255."""
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


# Hue used when the clip is essentially greyscale (no colour to borrow). Indigo.
_DEFAULT_HUE = 0.66


def _adaptive_colors(frame):
    """Two gradient stops derived from the content but NORMALISED to a visible
    mid-tone (Screen Studio-style), so the backdrop always shows.

    The previous version mixed the dominant colours ~55-80% toward black, which
    crushed the backdrop to RGB ~(1-15) — invisible, especially on dark pages or
    over dark content. Instead we borrow only the *hue* from the clip's most
    colourful region and set saturation/value ourselves, guaranteeing a backdrop
    that reads clearly while the rounded clip + drop shadow still pop."""
    if frame is None:
        return _stop(_DEFAULT_HUE, 0.45, 0.42), _stop(_DEFAULT_HUE, 0.55, 0.24)
    # Pick the hue from the most saturated dominant colour (ignore near-black
    # noise); fall back to indigo if the clip is basically greyscale.
    best_h, best_s = _DEFAULT_HUE, 0.0
    for c in _dominant_colors(frame, 4):
        hh, ss, vv = colorsys.rgb_to_hsv(c[0] / 255, c[1] / 255, c[2] / 255)
        if ss > best_s and vv > 0.12:
            best_h, best_s = hh, ss
    sat = 0.40 if best_s < 0.15 else min(0.52, max(0.36, best_s))
    # Values kept high (and the gradient narrow) so that even after h264's
    # limited-range yuv encoding (which darkens) the dimmest corner still reads
    # clearly on a dark page — a visible backdrop, not a heavy vignette.
    c1 = _stop(best_h, sat,                 0.56)   # brighter top stop
    c2 = _stop(best_h, min(0.60, sat + 0.08), 0.42)  # deeper bottom stop
    return c1, c2


# Selectable backdrop looks. "auto" derives from the clip; the rest are fixed.
# Cycled with `B` in the UI; the same names drive the export so preview == output.
BACKDROP_STYLES = ("auto", "indigo", "sunset", "light", "mono")


def _backdrop_colors(frame, style):
    """Two gradient stops (c1 top, c2 bottom) for the given backdrop style."""
    if style == "indigo":
        return _stop(0.66, 0.55, 0.56), _stop(0.71, 0.62, 0.34)
    if style == "sunset":
        return _stop(0.92, 0.55, 0.62), _stop(0.06, 0.66, 0.52)   # pink -> orange
    if style == "light":
        return (230, 232, 237), (198, 202, 212)
    if style == "mono":
        return (46, 47, 52), (30, 31, 35)
    return _adaptive_colors(frame)   # "auto" (default)


def _make_gradient(W, H, c1, c2):
    """Diagonal two-stop gradient as a PIL RGB image (numpy-accelerated)."""
    import numpy as np
    from PIL import Image
    x = np.linspace(0.0, 1.0, W, dtype=np.float32)[None, :]
    y = np.linspace(0.0, 1.0, H, dtype=np.float32)[:, None]
    t = (x + y) * 0.5
    g = np.empty((H, W, 3), np.uint8)
    for i in range(3):
        g[..., i] = (c1[i] + (c2[i] - c1[i]) * t).astype(np.uint8)
    return Image.fromarray(g, "RGB")


def _make_backdrop_bg(frame, w, h, pad, radius, style="auto"):
    """Gradient background (per `style`) with a soft drop shadow baked in where
    the recording will be overlaid. Returns (RGB image, W, H)."""
    from PIL import Image, ImageDraw, ImageFilter
    W = w + 2 * pad + (w % 2)
    H = h + 2 * pad + (h % 2)
    c1, c2 = _backdrop_colors(frame, style)
    bg = _make_gradient(W, H, c1, c2).convert("RGBA")
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    off = max(4, pad // 3)
    x0, y0 = pad, pad + off
    ImageDraw.Draw(sh).rounded_rectangle([x0, y0, x0 + w, y0 + h],
                                         radius=radius, fill=(0, 0, 0, 150))
    sh = sh.filter(ImageFilter.GaussianBlur(radius=max(6, pad // 2)))
    bg = Image.alpha_composite(bg, sh)
    return bg.convert("RGB"), W, H


def _make_mask(w, h, radius):
    from PIL import Image, ImageDraw
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    return m


def backdrop_geometry(w, h):
    """The pad + corner radius the export uses for a w×h clip. Single source of
    truth so the live preview lines up exactly with the rendered output."""
    pad = max(24, int(round(max(w, h) * 0.07)))
    pad += pad % 2
    radius = max(8, int(round(min(w, h) * 0.04)))
    return pad, radius


def render_backdrop_preview(content_img, style="auto"):
    """Composite `content_img` onto the backdrop EXACTLY as the export pipeline
    does (same gradient, shadow, pad, rounded corners) — for the live selection
    preview. Returns (preview RGB image, pad, radius)."""
    from PIL import Image
    content_img = content_img.convert("RGB")
    w, h = content_img.size
    pad, radius = backdrop_geometry(w, h)
    bg_img, W, H = _make_backdrop_bg(content_img, w, h, pad, radius, style)
    base = bg_img.convert("RGBA")
    clip = content_img.convert("RGBA")
    clip.putalpha(_make_mask(w, h, radius))
    base.alpha_composite(clip, (pad, pad))
    return base.convert("RGB"), pad, radius


def render_backdrop_frame(content_img, style="auto", hole=(0, 0, 0)):
    """Like render_backdrop_preview, but the centre (where the live recording
    region sits) is filled with `hole` — the overlay's transparent-colour — so it
    stays a live, filmed hole while the gradient padding shows AROUND it during
    recording. gdigrab films only the hole; the real rounded backdrop is added in
    finalize(). Returns (RGB image, pad, radius). The hole is a full rectangle
    (square corners) so the entire filmed region is live; rounding is post-only."""
    from PIL import ImageDraw
    content_img = content_img.convert("RGB")
    w, h = content_img.size
    pad, radius = backdrop_geometry(w, h)
    bg_img, W, H = _make_backdrop_bg(content_img, w, h, pad, radius, style)
    ImageDraw.Draw(bg_img).rectangle([pad, pad, pad + w - 1, pad + h - 1], fill=hole)
    return bg_img, pad, radius


def apply_backdrop(in_mp4, out_mp4, frame, tmpdir, style="auto", log=None):
    """Composite the recording (rounded corners) centred on a `style` gradient
    backdrop with shadow. Returns True on success."""
    _log = log or (lambda m: None)
    w, h   = frame.size
    pad, radius = backdrop_geometry(w, h)
    bg_img, W, H = _make_backdrop_bg(frame, w, h, pad, radius, style)
    bg_path   = str(Path(tmpdir) / "bd_bg.png");   bg_img.save(bg_path)
    mask_path = str(Path(tmpdir) / "bd_mask.png"); _make_mask(w, h, radius).save(mask_path)
    fc = (f"[0:v]format=rgba[v];"
          f"[2:v]format=gray[m];"
          f"[v][m]alphamerge[rc];"
          f"[1:v][rc]overlay={pad}:{pad}:format=auto,format=yuv420p[out]")
    cmd = [ffmpeg_exe(), "-y", "-i", in_mp4, "-i", bg_path, "-i", mask_path,
           "-filter_complex", fc, "-map", "[out]",
           "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-crf", FINAL_CRF, "-preset", FINAL_PRESET,
           "-movflags", "+faststart", out_mp4]
    _log(f"recorder: backdrop {W}x{H} pad={pad} r={radius} style={style}")
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=CREATE_NO_WINDOW, timeout=300)
        return os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 0
    except Exception as e:
        _log(f"recorder: backdrop failed ({e})")
        return False


def finalize(mp4_temp: str, out_path: str, fmt: str = "MP4", gif_fps: int = 24,
             backdrop=False, tmpdir: str = None, log=None) -> bool:
    """Move/convert the temp recording to its final destination, optionally
    compositing it onto a backdrop first. `backdrop` may be a style name
    ('auto'/'indigo'/'sunset'/'light'/'mono'), True (= 'auto'), or a falsey
    value (no backdrop). fmt 'MP4' → move; fmt 'GIF' → convert."""
    import shutil
    _log = log or (lambda m: None)
    if not mp4_temp or not os.path.exists(mp4_temp):
        return False

    src = mp4_temp
    if backdrop:
        style = backdrop if isinstance(backdrop, str) else "auto"
        frame = _extract_frame(mp4_temp, log=_log)
        if frame is not None:
            td   = tmpdir or tempfile.mkdtemp(prefix="ss_bd_")
            comp = str(Path(td) / "composited.mp4")
            if apply_backdrop(mp4_temp, comp, frame, td, style=style, log=_log):
                src = comp
            else:
                _log("recorder: backdrop fell back to raw recording")

    if fmt.upper() == "GIF":
        return to_gif(src, out_path, fps=gif_fps, log=_log)

    # MP4. If a backdrop was applied, `src` is already the slow-preset re-encode
    # (apply_backdrop) — just move it. Otherwise `src` is the raw real-time
    # capture; compress it once now to cut the file size at the same quality.
    if src == mp4_temp:
        if compress_mp4(src, out_path, log=_log):
            return True
        _log("recorder: compress failed — falling back to raw capture")
    try:
        shutil.move(src, out_path)
        return True
    except Exception as e:
        _log(f"recorder: finalize move failed ({e})")
        try:
            shutil.copy2(src, out_path)
            return True
        except Exception:
            return False


# ── standalone smoke test ────────────────────────────────────────────────
if __name__ == "__main__":
    import time, sys
    print("ffmpeg:", ffmpeg_exe())
    # Record a 600x400 region at primary (100,100): 2s, pause 1s, 2s more → GIF + MP4.
    r = RegionRecorder(100, 100, 600, 400, log=lambda m: print(m))
    r.start();  time.sleep(2)
    r.pause();  time.sleep(1)
    r.resume(); time.sleep(2)
    mp4 = r.stop()
    print("temp mp4:", mp4)
    out_dir = Path(tempfile.gettempdir())
    ok_mp4 = finalize(mp4, str(out_dir / "rec_test.mp4"), "MP4", log=print) if mp4 else False
    # finalize moved the temp; re-record quickly for a gif test
    r2 = RegionRecorder(100, 100, 600, 400, log=lambda m: print(m))
    r2.start(); time.sleep(2); mp4b = r2.stop()
    ok_gif = finalize(mp4b, str(out_dir / "rec_test.gif"), "GIF", gif_fps=24, log=print) if mp4b else False
    r.cleanup(); r2.cleanup()
    print("MP4 ok:", ok_mp4, "| GIF ok:", ok_gif)
    sys.exit(0 if (ok_mp4 and ok_gif) else 1)
