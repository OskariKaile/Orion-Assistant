"""Floating HUD for Orion: a Gemini-style glowing pill rendered as a Win32
layered window with true per-pixel alpha. Frames are painted with Pillow, so
the soft gradient glow is a real Gaussian blur and the corners are genuinely
transparent. Pinned near the bottom-centre of the screen. The public API
(set_state / set_heard / set_last / close / run) matches the old HUD."""

import ctypes
import math
import queue
import time
from ctypes import wintypes

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# --- layout ---------------------------------------------------------------
W, H = 820, 300             # full layered-window canvas (room for the glow)
PILL_W, PILL_H = 560, 78
PX1 = (W - PILL_W) // 2
PY1 = (H - PILL_H) // 2
PX2, PY2 = PX1 + PILL_W, PY1 + PILL_H
CY = (PY1 + PY2) // 2
RADIUS = PILL_H // 2
MARGIN_BOTTOM = 70          # breathing room above the taskbar
FPS = 30

# --- per-state styling ----------------------------------------------------
# Canonical palette -- must stay in sync with orion.STATE_COLORS and the
# colour map in hub/app.js. See orion.py for the master copy.
STATES = {
    "idle":         {"color": (59, 130, 246),  "label": "Ask Orion",     "energy": 0.30},
    "wake":         {"color": (236, 72, 153),  "label": 'Say "Orion"...', "energy": 0.25},
    "listening":    {"color": (34, 197, 94),   "label": "Listening",     "energy": 0.80},
    "transcribing": {"color": (250, 204, 21),  "label": "Transcribing",  "energy": 0.70},
    "thinking":     {"color": (249, 115, 22),  "label": "Thinking",      "energy": 0.95},
    "synthesizing": {"color": (168, 85, 247),  "label": "Preparing",     "energy": 0.85},
    "speaking":     {"color": (6, 182, 212),   "label": "Speaking",      "energy": 1.00},
    "muted":        {"color": (107, 114, 128), "label": "Muted",         "energy": 0.05},
}
PINK = (236, 72, 153)
PILL_BG = (20, 20, 26)

# --- function-call chips (shown above the pill while the model runs tools) --
CHIP_H = 32
CHIP_GAP = 10
CHIP_BOTTOM = PY1 - 14       # chips sit just above the pill
CHIP_LINGER = 3.0            # seconds a finished set stays before fading
CHIP_FADE = 0.4
CHIP_ORANGE = (249, 115, 22)
CHIP_GREEN = (74, 222, 128)
CHIP_RED = (248, 113, 113)

_F = "C:/Windows/Fonts/"


def _font(name, size):
    try:
        return ImageFont.truetype(_F + name, size)
    except Exception:
        return ImageFont.load_default()


# --- Win32 plumbing -------------------------------------------------------
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020   # clicks pass straight through to apps below
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.DefWindowProcW.restype = ctypes.c_long
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
ctypes.windll.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HANDLE
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]


class HUD:
    def __init__(self, start_hidden=False):
        self._events = queue.Queue()
        self._visible = not start_hidden
        self._state = "idle"
        self._frame = 0
        self._sub_text = ""
        self._sub_color = (203, 213, 225)
        self._quit = False
        self._glow_cache = {}
        self._expand = 0.0          # 0 = collapsed orb, 1 = full pill
        self._now = 0.0             # seconds since start (time-based anim)
        self._dt = 1.0 / FPS        # seconds since previous frame

        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
        self._sw = user32.GetSystemMetrics(0)
        self._sh = user32.GetSystemMetrics(1)
        self._x = (self._sw - W) // 2
        self._y = self._sh - H - MARGIN_BOTTOM

        self._font_big = _font("segoeui.ttf", 19)
        self._font_lbl = _font("segoeuib.ttf", 12)
        self._font_sub = _font("segoeui.ttf", 14)
        self._font_chip = _font("consola.ttf", 13)
        self._font_fn = _font("segoeuib.ttf", 10)
        self._chips = []            # [{"label", "status", "t0", "t1"}]

        self._create_window()

    # --- window ---------------------------------------------------------
    def _create_window(self):
        self._wndproc = WNDPROC(self._on_message)
        cls = WNDCLASS()
        cls.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        cls.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        cls.lpszClassName = "OrionHUD"
        cls.hCursor = user32.LoadCursorW(None, 32512)  # IDC_ARROW
        user32.RegisterClassW(ctypes.byref(cls))

        self._hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
            | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            "OrionHUD", "Orion", WS_POPUP,
            self._x, self._y, W, H,
            None, None, cls.hInstance, None)
        user32.ShowWindow(self._hwnd, 8 if self._visible else 0)  # SW_SHOWNA / SW_HIDE

    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == 0x0002:  # WM_DESTROY
            user32.PostQuitMessage(0)
            return 0
        if msg == 0x00A1:  # WM_NCLBUTTONDOWN -> let the user drag the pill
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # --- rendering ------------------------------------------------------
    def _build_glow(self, color, px1, px2):
        """Cached: blurred bi-tone halo + soft drop shadow, sized to the
        current pill bounds (works for the collapsed orb too)."""
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        mid = (px1 + px2) / 2

        # drop shadow
        d.rounded_rectangle([px1, PY1 + 12, px2, PY2 + 18],
                            radius=RADIUS, fill=(0, 0, 0, 150))

        # bi-tone aura hugging the pill (kept well inside the canvas so the
        # blur never reaches an edge -> no visible rectangular cutoff)
        d.ellipse([px1 - 44, PY1 - 30, mid + 70, PY2 + 30], fill=PINK + (120,))
        d.ellipse([mid - 70, PY1 - 30, px2 + 44, PY2 + 30], fill=color + (135,))
        return img.filter(ImageFilter.GaussianBlur(38))

    def _draw_mic(self, d, cx, cy, col):
        d.rounded_rectangle([cx - 4, cy - 10, cx + 4, cy + 3], radius=4, fill=col)
        d.arc([cx - 9, cy - 7, cx + 9, cy + 9], 20, 160, fill=col, width=2)
        d.line([cx, cy + 9, cx, cy + 13], fill=col, width=2)
        d.line([cx - 5, cy + 13, cx + 5, cy + 13], fill=col, width=2)

    def _render(self):
        meta = STATES.get(self._state, STATES["idle"])
        color = meta["color"]
        energy = meta["energy"]
        phase = (math.sin(2 * math.pi * self._now / 2.6) + 1) / 2
        pulse = 0.35 + 0.65 * phase * (0.4 + 0.6 * energy)

        # collapsed orb <-> expanded pill animation (time-based easing, so it
        # runs at a constant speed regardless of the actual frame rate)
        target = 1.0 if self._state in ("listening", "transcribing", "thinking", "synthesizing", "speaking") else 0.0
        self._expand += (target - self._expand) * (1 - math.exp(-self._dt * 6.5))
        if abs(self._expand - target) < 0.003:
            self._expand = target
        e = self._expand
        ease = e * e * (3 - 2 * e)                  # smoothstep
        cur_w = PILL_H + (PILL_W - PILL_H) * ease
        px1 = (W - cur_w) / 2
        px2 = px1 + cur_w
        fade = max(0.0, min(1.0, (ease - 0.45) / 0.55))   # contents fade-in

        # base canvas + alpha-scaled glow (cached per state + expand step;
        # coarse 0.1 quantisation keeps the costly blur off the hot path)
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gkey = (self._state, round(ease, 1))
        if gkey not in self._glow_cache:
            self._glow_cache[gkey] = self._build_glow(color, px1, px2)
        glow = self._glow_cache[gkey].copy()
        ga = glow.split()[3].point(lambda v: int(v * (0.55 + 0.45 * pulse)))
        glow.putalpha(ga)
        canvas.alpha_composite(glow)

        d = ImageDraw.Draw(canvas)

        # the pill body (a circle when collapsed)
        edge = tuple(int(PILL_BG[i] + (color[i] - PILL_BG[i]) * (0.25 + 0.5 * pulse))
                     for i in range(3))
        d.rounded_rectangle([px1, PY1, px2, PY2], radius=RADIUS,
                            fill=PILL_BG + (255,), outline=edge + (255,), width=2)

        # blue waveform orb -- exactly centred in the collapsed circle, then
        # slides to the right edge as the pill grows
        pill_mid = (px1 + px2) / 2
        wcx = pill_mid + (px2 - 44 - pill_mid) * ease
        d.ellipse([wcx - 27, CY - 27, wcx + 27, CY + 27], fill=color + (255,))
        for i in range(4):
            bp = math.sin(2 * math.pi * (self._now / 0.55 + i / 4))
            bh = 5 + (7 + 10 * energy) * (0.5 + 0.5 * bp)
            bx = wcx + (i - 1.5) * 7
            d.rounded_rectangle([bx - 2, CY - bh, bx + 2, CY + bh], radius=2,
                                fill=(255, 255, 255, 255))

        # pill contents on their own layer, so the fade-in composites cleanly
        # instead of punching holes in the pill
        if fade > 0.01:
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)

            cx = px1 + 40
            ld.ellipse([cx - 19, CY - 19, cx + 19, CY + 19],
                       fill=(36, 36, 44, 255), outline=(60, 60, 74, 255))
            ld.line([cx - 8, CY, cx + 8, CY], fill=(226, 232, 240, 255), width=3)
            ld.line([cx, CY - 8, cx, CY + 8], fill=(226, 232, 240, 255), width=3)

            tx = px1 + 74
            ld.rounded_rectangle([tx, CY - 13, tx + 3, CY + 13], radius=2,
                                 fill=color + (255,))

            text_x = tx + 16
            if self._sub_text:
                ld.text((text_x, CY - 14), meta["label"], font=self._font_lbl,
                        fill=color + (255,))
                sub = self._sub_text
                while sub and self._font_sub.getlength(sub) > px2 - text_x - 130:
                    sub = sub[:-1]
                ld.text((text_x, CY), sub, font=self._font_sub,
                        fill=self._sub_color + (255,))
            else:
                ld.text((text_x, CY), meta["label"], font=self._font_big,
                        fill=(139, 147, 167, 255), anchor="lm")

            self._draw_mic(ld, px2 - 86, CY, (170, 179, 197, 255))

            la = layer.split()[3].point(lambda v: int(v * fade))
            layer.putalpha(la)
            canvas.alpha_composite(layer)

        if self._chips:
            self._draw_chips(canvas)

        return canvas

    def _draw_chips(self, canvas):
        """Tool-call chips ("fn  open(app='spotify')  ✓") centred above the
        pill. Each fades in when the model calls the tool; the set lingers
        CHIP_LINGER seconds after the last one finishes, then fades out."""
        now = self._now
        if all(c["status"] != "run" for c in self._chips):
            gone = now - max(c["t1"] for c in self._chips) - CHIP_LINGER
            if gone >= CHIP_FADE:
                self._chips = []
                return
            group = 1.0 - max(0.0, gone) / CHIP_FADE
        else:
            group = 1.0

        fn_w = 22
        max_label = (W - 40) // max(1, len(self._chips)) - (fn_w + 58)
        items = []
        for c in self._chips:
            label = c["label"]
            if self._font_chip.getlength(label) > max_label:
                while label and self._font_chip.getlength(label + "…") > max_label:
                    label = label[:-1]
                label += "…"
            lw = self._font_chip.getlength(label)
            items.append((c, label, int(12 + fn_w + 8 + lw + 10 + 14 + 12)))
        total = sum(w for *_, w in items) + CHIP_GAP * (len(items) - 1)
        x = (W - total) / 2
        y1, y2 = CHIP_BOTTOM - CHIP_H, CHIP_BOTTOM
        cy = (y1 + y2) / 2

        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        for c, label, w in items:
            a = group * min(1.0, (now - c["t0"]) / 0.25)
            if a <= 0.01:
                x += w + CHIP_GAP
                continue
            chip = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(chip)
            d.rounded_rectangle([x, y1, x + w, y2], radius=9, fill=(17, 24, 38, 235),
                                outline=CHIP_ORANGE + (110,), width=1)
            bx = x + 12
            d.rounded_rectangle([bx, cy - 9, bx + fn_w, cy + 9], radius=5,
                                fill=(70, 38, 22, 255))
            d.text((bx + fn_w / 2, cy), "fn", font=self._font_fn,
                   fill=(251, 146, 60, 255), anchor="mm")
            tx = bx + fn_w + 8
            d.text((tx, cy), label, font=self._font_chip,
                   fill=(254, 215, 170, 255), anchor="lm")
            sx = x + w - 12 - 7          # status glyph centre
            if c["status"] == "run":
                ang = (now * 540) % 360
                d.arc([sx - 7, cy - 7, sx + 7, cy + 7], 0, 360,
                      fill=CHIP_ORANGE + (70,), width=2)
                d.arc([sx - 7, cy - 7, sx + 7, cy + 7], ang, ang + 100,
                      fill=CHIP_ORANGE + (255,), width=2)
            elif c["status"] == "ok":
                d.line([(sx - 6, cy), (sx - 2, cy + 4), (sx + 6, cy - 5)],
                       fill=CHIP_GREEN + (255,), width=2)
            else:
                d.line([(sx - 5, cy - 5), (sx + 5, cy + 5)], fill=CHIP_RED + (255,), width=2)
                d.line([(sx - 5, cy + 5), (sx + 5, cy - 5)], fill=CHIP_RED + (255,), width=2)
            ca = chip.split()[3].point(lambda v, a=a: int(v * a))
            chip.putalpha(ca)
            layer.alpha_composite(chip)
            x += w + CHIP_GAP
        canvas.alpha_composite(layer)

    def _blit(self, img):
        # premultiply alpha (required for ULW_ALPHA) and pack as BGRA
        r, g, b, a = img.split()
        img = Image.merge("RGBA", (ImageChops.multiply(r, a),
                                   ImageChops.multiply(g, a),
                                   ImageChops.multiply(b, a), a))
        data = img.tobytes("raw", "BGRA")

        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = W
        bmi.bmiHeader.biHeight = -H          # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0      # BI_RGB
        ppv = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0,
                                      ctypes.byref(ppv), None, 0)
        ctypes.memmove(ppv, data, len(data))
        old = gdi32.SelectObject(mem_dc, hbmp)

        size = wintypes.SIZE(W, H)
        src = wintypes.POINT(0, 0)
        dst = wintypes.POINT(self._x, self._y)
        blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        user32.UpdateLayeredWindow(self._hwnd, screen_dc, ctypes.byref(dst),
                                   ctypes.byref(size), mem_dc,
                                   ctypes.byref(src), 0,
                                   ctypes.byref(blend), 2)  # ULW_ALPHA

        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)

    # --- event queue ----------------------------------------------------
    def _drain(self):
        # Every event is applied inside its own guard. This loop runs on the
        # process's main thread, so an exception escaping it used to end the
        # whole of Orion -- one malformed caption would take the assistant
        # down with the overlay. A bad event is now dropped instead.
        while True:
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "state":
                    self._state = value if value in STATES else "idle"
                    if self._state == "listening":
                        self._chips = []    # a new turn starts clean
                elif kind == "tool":
                    # a fresh model round after a finished set replaces it
                    if self._chips and all(c["status"] != "run" for c in self._chips) \
                            and self._now - max(c["t1"] for c in self._chips) > 1.0:
                        self._chips = []
                    if len(self._chips) < 4:
                        self._chips.append({"label": str(value)[:80], "status": "run",
                                            "t0": self._now, "t1": self._now})
                elif kind == "tool_done":
                    label, ok = value
                    for c in self._chips:
                        if c["status"] == "run" and c["label"] == str(label)[:80]:
                            c["status"] = "ok" if ok else "fail"
                            c["t1"] = self._now
                            break
                elif kind == "heard":
                    self._sub_text = "“" + str(value)[:90] + "”"
                    self._sub_color = (125, 211, 252)
                elif kind == "last":
                    self._sub_text = str(value)[:160]
                    self._sub_color = (203, 213, 225)
                elif kind == "visible":
                    want = (not self._visible) if value is None else bool(value)
                    if want != self._visible:
                        self._visible = want
                        user32.ShowWindow(self._hwnd, 8 if want else 0)
                elif kind == "quit":
                    self._quit = True
            except Exception:
                continue

    # --- thread-safe API ------------------------------------------------
    def set_state(self, state):
        self._events.put(("state", state))

    def set_heard(self, text):
        self._events.put(("heard", text))

    def set_last(self, text):
        self._events.put(("last", text))

    def tool_start(self, label):
        """Show a function-call chip above the pill (spinner until done)."""
        self._events.put(("tool", label))

    def tool_done(self, label, ok=True):
        self._events.put(("tool_done", (label, ok)))

    def show(self):
        self._events.put(("visible", True))

    def hide(self):
        self._events.put(("visible", False))

    def toggle(self):
        self._events.put(("visible", None))

    def close(self):
        self._events.put(("quit", None))

    def run(self):
        """Blocks on the calling (main) thread, pumping messages + frames."""
        msg = wintypes.MSG()
        period = 1.0 / FPS
        start = time.perf_counter()
        last = start
        while not self._quit:
            t0 = time.perf_counter()
            try:
                self._now = t0 - start
                self._dt = min(0.1, t0 - last)  # clamp so a stall can't jump anim
                last = t0
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                self._drain()
                if self._quit:
                    break
                self._frame += 1
                try:
                    self._blit(self._render())
                except Exception:
                    pass
            except Exception:
                # The overlay is decoration. Whatever went wrong with a frame,
                # it must never be the reason Orion stops listening.
                last = t0
            dt = time.perf_counter() - t0
            if dt < period:
                time.sleep(period - dt)
        try:
            user32.DestroyWindow(self._hwnd)
        except Exception:
            pass
