"""ScrnShr - DeskCreen-inspired region/window screen sharing over plain HTTP/MJPEG.

Run with run.bat (no console window), or `venv\\Scripts\\python.exe scrnshr.py`
directly if you want to see console/debug output. See README.md.
"""

import ctypes
import hmac
import io
import os
import secrets
import socket
import threading
import time
import tkinter as tk
from ctypes import wintypes

import mss
import qrcode
from flask import Flask, Response, abort, redirect, render_template_string, request
from PIL import Image, ImageTk

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(_APP_DIR, "icon.ico")
ICON_LIVE_PATHS = [
    os.path.join(_APP_DIR, "icon_live1.ico"),
    os.path.join(_APP_DIR, "icon_live2.ico"),
]
APP_NAME = "ScrnShr"

# ============================================================
# Win32 helpers: DPI awareness, window enumeration, PrintWindow capture
# ============================================================

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi
shcore = ctypes.windll.shcore

PW_RENDERFULLCONTENT = 0x00000002
DWMWA_CLOAKED = 14
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
MB_ICONERROR = 0x10


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def enable_dpi_awareness():
    try:
        shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        pass  # Not on Windows, or an older Windows without shcore.


def get_dpi_scale():
    try:
        return user32.GetDpiForWindow(user32.GetDesktopWindow()) / 96.0
    except (AttributeError, OSError):
        return 1.0


def fatal_error_dialog(message):
    """A plain Win32 message box - works even with no console and before Tk starts."""
    user32.MessageBoxW(0, message, APP_NAME, MB_ICONERROR)


def _is_cloaked(hwnd):
    """DWM hides some UWP/background windows (e.g. Cortana, hidden Settings pages)
    behind a normal-looking HWND; DWMWA_CLOAKED flags these so we can skip them."""
    cloaked = wintypes.DWORD()
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return bool(cloaked.value)


def list_windows(exclude_pid=None):
    """Visible, titled, non-tool, non-cloaked top-level windows, as [(hwnd, title), ...]."""
    windows = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        if exclude_pid is not None:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == exclude_pid:
                return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if _is_cloaked(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        windows.append((hwnd, buf.value))
        return True

    user32.EnumWindows(enum_proc, 0)
    return windows


def is_window_valid(hwnd):
    return bool(user32.IsWindow(hwnd))


def capture_window(hwnd):
    """Grab a window's current pixels via PrintWindow, regardless of occlusion/z-order.
    Returns a PIL Image, or None if the window is gone or the capture failed."""
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None

    hwnd_dc = user32.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None

    image = None
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    old_obj = gdi32.SelectObject(mem_dc, bitmap)
    try:
        if user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT):
            bmi = _BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = width
            bmi.bmiHeader.biHeight = -height  # negative = top-down DIB
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0  # BI_RGB

            buffer = ctypes.create_string_buffer(width * height * 4)
            gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(bmi), 0)
            image = Image.frombuffer("RGB", (width, height), buffer.raw, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(mem_dc, old_obj)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)

    return image


# ============================================================
# Capture worker
# ============================================================

TARGET_FPS = 12
FRAME_INTERVAL = 1.0 / TARGET_FPS
JPEG_QUALITY = 70


class FrameBuffer:
    """Holds the latest encoded frame. Single writer (CaptureWorker), many readers (viewer streams)."""

    def __init__(self):
        self._condition = threading.Condition()
        self._jpeg = None
        self._version = 0

    def update(self, jpeg_bytes):
        with self._condition:
            self._jpeg = jpeg_bytes
            self._version += 1
            self._condition.notify_all()

    def get_latest(self, last_seen_version=-1, timeout=None):
        with self._condition:
            if self._version == last_seen_version:
                self._condition.wait(timeout=timeout)
            return self._jpeg, self._version


class CaptureWorker(threading.Thread):
    """Captures region or window frames, re-reading the shared mode/target every frame so the
    source can be switched live without restarting the thread (and without viewers reconnecting).
    Owns the single mss instance for its lifetime; mss is not thread-safe across threads."""

    def __init__(self, state):
        super().__init__(daemon=True)
        self._state = state

    def run(self):
        with mss.mss() as sct:
            while self._state.running.is_set():
                start = time.monotonic()
                if self._state.get_mode() == "window":
                    self._capture_window_frame()
                else:
                    self._capture_region_frame(sct)
                self._throttle(start)

    def _capture_region_frame(self, sct):
        region = self._state.get_region()
        try:
            shot = sct.grab(region)
            image = Image.frombytes("RGB", shot.size, shot.rgb)
            self._encode_and_store(image)
        except mss.exception.ScreenShotError:
            pass  # Can happen transiently while the region is mid-resize.

    def _capture_window_frame(self):
        hwnd = self._state.get_target_hwnd()
        if hwnd is None or not is_window_valid(hwnd):
            return  # No valid window selected right now - wait for the user to pick one.
        image = capture_window(hwnd)
        if image is not None:
            self._encode_and_store(image)

    def _encode_and_store(self, image):
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        self._state.frame_buffer.update(buffer.getvalue())

    def _throttle(self, start):
        elapsed = time.monotonic() - start
        remaining = FRAME_INTERVAL - elapsed
        if remaining > 0:
            time.sleep(remaining)


# ============================================================
# Access code + rate limiting
# ============================================================

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L - avoids misreads
CODE_LENGTH = 6
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_LOCKOUT_SECONDS = 60


def generate_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


class RateLimiter:
    """Locks out an IP for a while after too many wrong-code guesses in a short window."""

    def __init__(self, max_attempts=RATE_LIMIT_MAX_ATTEMPTS, window=RATE_LIMIT_WINDOW_SECONDS, lockout=RATE_LIMIT_LOCKOUT_SECONDS):
        self._max_attempts = max_attempts
        self._window = window
        self._lockout = lockout
        self._lock = threading.Lock()
        self._failures = {}  # ip -> [timestamps]
        self._locked_until = {}  # ip -> timestamp

    def is_locked(self, ip):
        with self._lock:
            until = self._locked_until.get(ip)
            return until is not None and time.time() < until

    def record_failure(self, ip):
        with self._lock:
            now = time.time()
            attempts = [t for t in self._failures.get(ip, []) if now - t < self._window]
            attempts.append(now)
            if len(attempts) >= self._max_attempts:
                self._locked_until[ip] = now + self._lockout
                attempts = []
            self._failures[ip] = attempts

    def record_success(self, ip):
        with self._lock:
            self._failures.pop(ip, None)
            self._locked_until.pop(ip, None)


# ============================================================
# Connected-viewer tracking
# ============================================================


def describe_user_agent(user_agent):
    ua = user_agent or ""
    if "iPhone" in ua:
        os_name = "iPhone"
    elif "iPad" in ua:
        os_name = "iPad"
    elif "Android" in ua:
        os_name = "Android"
    elif "Mac OS X" in ua:
        os_name = "Mac"
    elif "Windows" in ua:
        os_name = "Windows"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = "Unknown device"

    if "Edg/" in ua:
        browser = "Edge"
    elif "Chrome/" in ua:
        browser = "Chrome"
    elif "Firefox/" in ua:
        browser = "Firefox"
    elif "Safari/" in ua and "Chrome" not in ua:
        browser = "Safari"
    else:
        browser = "browser"

    return f"{os_name} · {browser}"


class ViewerRegistry:
    """Tracks who has connected to /stream this sharing session (by IP), live and cumulative.

    Entries are kept (not deleted) after disconnect so the cumulative total stays accurate;
    `active_connections` distinguishes who is currently watching.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._viewers = {}

    def reset(self):
        with self._lock:
            self._viewers.clear()

    def add(self, ip, user_agent):
        is_new = False
        with self._lock:
            entry = self._viewers.get(ip)
            if entry is None:
                is_new = True
                entry = {
                    "user_agent": user_agent,
                    "hostname": None,
                    "first_seen": time.time(),
                    "active_connections": 0,
                }
                self._viewers[ip] = entry
            entry["active_connections"] += 1
            entry["user_agent"] = user_agent
        if is_new:
            threading.Thread(target=self._resolve_hostname, args=(ip,), daemon=True).start()

    def remove(self, ip):
        with self._lock:
            entry = self._viewers.get(ip)
            if entry is not None:
                entry["active_connections"] = max(0, entry["active_connections"] - 1)

    def _resolve_hostname(self, ip):
        try:
            hostname = socket.gethostbyaddr(ip)[0]
        except (socket.herror, socket.gaierror, OSError):
            return
        with self._lock:
            entry = self._viewers.get(ip)
            if entry is not None:
                entry["hostname"] = hostname

    def snapshot(self):
        """Returns (rows, active_count, total_count). rows are sorted, newest first."""
        with self._lock:
            now = time.time()
            rows = [
                {
                    "ip": ip,
                    "label": describe_user_agent(v["user_agent"]),
                    "hostname": v["hostname"],
                    "seconds": now - v["first_seen"],
                    "active": v["active_connections"] > 0,
                }
                for ip, v in self._viewers.items()
            ]
            total = len(rows)
            active = sum(1 for r in rows if r["active"])
        rows.sort(key=lambda r: r["seconds"])
        return rows, active, total


# ============================================================
# Flask server
# ============================================================

VIEWER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{{ app_name }}</title>
    <style>
        body { margin: 0; background: #111; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
        img { max-width: 100vw; max-height: 100vh; }
        p { position: fixed; top: 8px; left: 8px; color: #aaa; font-family: sans-serif; font-size: 12px; }
    </style>
</head>
<body>
    <p>View-only &mdash; {{ app_name }}</p>
    <img src="/stream?code={{ code }}" alt="Live screen share">
</body>
</html>
"""

JOIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{{ app_name }}</title>
    <style>
        body {
            margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
            background: #111; font-family: -apple-system, "Segoe UI", sans-serif; color: #eee;
        }
        .card { text-align: center; padding: 24px; }
        h1 { font-size: 20px; margin: 0 0 8px; }
        p { color: #aaa; margin: 0 0 16px; }
        input {
            font-size: 22px; letter-spacing: 4px; text-align: center; text-transform: uppercase;
            padding: 8px 10px; width: 8ch; border-radius: 6px; border: 1px solid #444;
            background: #222; color: #fff;
        }
        button {
            font-size: 16px; padding: 8px 18px; margin-left: 8px; border-radius: 6px;
            border: 1px solid #444; background: #2563eb; color: #fff; cursor: pointer;
        }
        .error { color: #ff6b6b; margin-top: 14px; }
    </style>
</head>
<body>
    <div class="card">
        <h1>{{ app_name }}</h1>
        <p>Enter the code shown on the host's screen</p>
        <form method="post" action="/join">
            <input type="text" name="code" maxlength="{{ code_length }}" autocomplete="off" autofocus>
            <button type="submit">Join</button>
        </form>
        {% if error %}<p class="error">{{ error }}</p>{% endif %}
    </div>
</body>
</html>
"""


def create_app(state):
    app = Flask(__name__)

    def check_code(candidate, ip):
        if state.rate_limiter.is_locked(ip):
            return False
        normalized = (candidate or "").strip().upper()
        ok = hmac.compare_digest(normalized, state.code)
        if ok:
            state.rate_limiter.record_success(ip)
        else:
            state.rate_limiter.record_failure(ip)
        return ok

    @app.route("/")
    def join_form():
        return render_template_string(JOIN_HTML, app_name=APP_NAME, code_length=CODE_LENGTH, error=None)

    @app.route("/join", methods=["POST"])
    def join():
        code = request.form.get("code", "")
        ip = request.remote_addr
        if state.rate_limiter.is_locked(ip):
            return render_template_string(
                JOIN_HTML, app_name=APP_NAME, code_length=CODE_LENGTH,
                error="Too many attempts. Try again in a minute.",
            )
        if not check_code(code, ip):
            return render_template_string(
                JOIN_HTML, app_name=APP_NAME, code_length=CODE_LENGTH, error="That code didn't work."
            )
        return redirect(f"/view?code={code.strip().upper()}")

    @app.route("/view")
    def view():
        if not check_code(request.args.get("code", ""), request.remote_addr):
            abort(403)
        return render_template_string(VIEWER_HTML, app_name=APP_NAME, code=state.code)

    @app.route("/stream")
    def stream():
        if not check_code(request.args.get("code", ""), request.remote_addr):
            abort(403)

        ip = request.remote_addr
        user_agent = request.headers.get("User-Agent", "")
        state.viewers.add(ip, user_agent)

        def generate():
            try:
                version = -1
                while True:
                    jpeg, version = state.frame_buffer.get_latest(version, timeout=5)
                    if jpeg is None:
                        continue
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
            finally:
                state.viewers.remove(ip)

        return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")

    return app


# ============================================================
# Shared state
# ============================================================

DEFAULT_REGION = {"left": 100, "top": 100, "width": 800, "height": 600}


class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        self.region = dict(DEFAULT_REGION)
        self.mode = "region"  # "region" or "window"
        self.target_hwnd = None
        self.code = generate_code()
        self.running = threading.Event()
        self.frame_buffer = FrameBuffer()
        self.capture_worker = None
        self.rate_limiter = RateLimiter()
        self.viewers = ViewerRegistry()

    def get_region(self):
        with self.lock:
            return dict(self.region)

    def set_region(self, left, top, width, height):
        with self.lock:
            self.region = {"left": left, "top": top, "width": width, "height": height}

    def get_mode(self):
        with self.lock:
            return self.mode

    def set_mode(self, mode):
        with self.lock:
            self.mode = mode

    def get_target_hwnd(self):
        with self.lock:
            return self.target_hwnd

    def set_target_hwnd(self, hwnd):
        with self.lock:
            self.target_hwnd = hwnd

    def start_capture(self):
        if self.capture_worker is not None:
            return
        self.viewers.reset()
        self.running.set()
        self.capture_worker = CaptureWorker(self)
        self.capture_worker.start()

    def stop_capture(self):
        self.running.clear()
        if self.capture_worker is not None:
            self.capture_worker.join(timeout=2)
            self.capture_worker = None


# ============================================================
# Overlay UI: region selector + control panel
# ============================================================

BORDER = 3
HANDLE = 10
MIN_WIDTH = 120
MIN_HEIGHT = 90
TRANSPARENT_COLOR = "magenta"
BORDER_COLOR = "#ff3b30"

RESIZE_CURSORS = {
    "nw": "size_nw_se",
    "se": "size_nw_se",
    "ne": "size_ne_sw",
    "sw": "size_ne_sw",
}


class _ListboxTooltip:
    """Shows the full text for a possibly-truncated Listbox row on hover."""

    def __init__(self, listbox, text_for_index, font):
        self._listbox = listbox
        self._text_for_index = text_for_index
        self._font = font
        self._tip = None
        self._shown_index = None
        listbox.bind("<Motion>", self._on_motion)
        listbox.bind("<Leave>", self._hide)

    def _on_motion(self, event):
        index = self._listbox.nearest(event.y)
        bbox = self._listbox.bbox(index)
        if bbox is None or not (bbox[1] <= event.y <= bbox[1] + bbox[3]):
            self._hide()
            return
        if index == self._shown_index:
            return
        self._shown_index = index
        self._show(event, self._text_for_index(index))

    def _show(self, event, text):
        self._hide()
        if not text:
            return
        tip = tk.Toplevel(self._listbox)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        x = self._listbox.winfo_rootx() + event.x + 16
        y = self._listbox.winfo_rooty() + event.y + 12
        tip.geometry(f"+{x}+{y}")
        tk.Label(
            tip, text=text, bg="#ffffe0", fg="#000000", relief="solid",
            borderwidth=1, font=self._font, padx=6, pady=3,
        ).pack()
        self._tip = tip

    def _hide(self, _event=None):
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None
        self._shown_index = None


class Overlay:
    """Transparent, click-through, draggable/resizable region selector plus a control panel."""

    NOT_SHARING_TEXT = "Choose Region or Window, then Start Sharing."

    def __init__(self, state, viewer_url, landing_url):
        self._state = state
        self._viewer_url = viewer_url
        self._landing_url = landing_url
        self._drag = None
        self._pulse_on = False
        self._pulse_index = 0
        self._viewers_expanded = False
        self._source_warning_active = False

        self._scale = get_dpi_scale()
        self._border = self._px(BORDER)
        self._handle = self._px(HANDLE)
        self._min_width = self._px(MIN_WIDTH)
        self._min_height = self._px(MIN_HEIGHT)

        self._font_normal = ("Segoe UI", 10)
        self._font_bold = ("Segoe UI", 13, "bold")
        self._font_mode = ("Segoe UI", 11)
        self._font_code = ("Consolas", 20, "bold")
        self._font_small = ("Segoe UI", 8)

        self._root = tk.Tk()
        if os.path.exists(ICON_PATH):
            self._root.iconbitmap(default=ICON_PATH)
        self._qr_photo = self._make_qr_photo(viewer_url)
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.attributes("-transparentcolor", TRANSPARENT_COLOR)
        self._root.configure(bg=TRANSPARENT_COLOR)

        region = state.get_region()
        self._root.geometry(
            f"{region['width'] + 2 * self._border}x{region['height'] + 2 * self._border}"
            f"+{region['left'] - self._border}+{region['top'] - self._border}"
        )

        self._build_border()
        self._build_handles()
        self._root.bind("<Configure>", self._on_configure)
        self._root.withdraw()  # Hidden until the user picks "Region" in the control panel.

        self._build_control_panel()
        self._push_region()

    def _px(self, n):
        return max(1, round(n * self._scale))

    def _make_qr_photo(self, url):
        qr = qrcode.QRCode(border=2, box_size=max(2, round(4 * self._scale)))
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        return ImageTk.PhotoImage(img)

    @staticmethod
    def _format_code(code):
        return f"{code[:3]} {code[3:]}" if len(code) == 6 else code

    def run(self):
        self._root.mainloop()

    # ---- border / handles ----

    def _build_border(self):
        b = self._border
        edges = {
            "top": dict(relx=0, rely=0, relwidth=1, height=b, anchor="nw"),
            "bottom": dict(relx=0, rely=1, relwidth=1, height=b, anchor="sw"),
            "left": dict(relx=0, rely=0, relheight=1, width=b, anchor="nw"),
            "right": dict(relx=1, rely=0, relheight=1, width=b, anchor="ne"),
        }
        for place_kwargs in edges.values():
            frame = tk.Frame(self._root, bg=BORDER_COLOR, cursor="fleur")
            frame.place(**place_kwargs)
            frame.bind("<ButtonPress-1>", self._start_move)
            frame.bind("<B1-Motion>", self._do_move)

    def _build_handles(self):
        anchors = {
            "nw": dict(relx=0, rely=0, anchor="nw"),
            "ne": dict(relx=1, rely=0, anchor="ne"),
            "sw": dict(relx=0, rely=1, anchor="sw"),
            "se": dict(relx=1, rely=1, anchor="se"),
        }
        for corner, place_kwargs in anchors.items():
            handle = tk.Frame(self._root, bg=BORDER_COLOR, cursor=RESIZE_CURSORS[corner])
            handle.place(width=self._handle, height=self._handle, **place_kwargs)
            handle.bind("<ButtonPress-1>", lambda e, c=corner: self._start_resize(e, c))
            handle.bind("<B1-Motion>", self._do_resize)

    # ---- move ----

    def _start_move(self, event):
        self._drag = {
            "mode": "move",
            "start_x": event.x_root,
            "start_y": event.y_root,
            "orig_x": self._root.winfo_x(),
            "orig_y": self._root.winfo_y(),
        }

    def _do_move(self, event):
        if not self._drag or self._drag["mode"] != "move":
            return
        dx = event.x_root - self._drag["start_x"]
        dy = event.y_root - self._drag["start_y"]
        new_x = self._drag["orig_x"] + dx
        new_y = self._drag["orig_y"] + dy
        w = self._root.winfo_width()
        h = self._root.winfo_height()
        self._root.geometry(f"{w}x{h}+{new_x}+{new_y}")

    # ---- resize ----

    def _start_resize(self, event, corner):
        self._drag = {
            "mode": "resize",
            "corner": corner,
            "start_x": event.x_root,
            "start_y": event.y_root,
            "orig_x": self._root.winfo_x(),
            "orig_y": self._root.winfo_y(),
            "orig_w": self._root.winfo_width(),
            "orig_h": self._root.winfo_height(),
        }

    def _do_resize(self, event):
        drag = self._drag
        if not drag or drag["mode"] != "resize":
            return
        dx = event.x_root - drag["start_x"]
        dy = event.y_root - drag["start_y"]
        corner = drag["corner"]

        x, y, w, h = drag["orig_x"], drag["orig_y"], drag["orig_w"], drag["orig_h"]

        if corner in ("ne", "se"):
            w = drag["orig_w"] + dx
        if corner in ("nw", "sw"):
            w = drag["orig_w"] - dx
            x = drag["orig_x"] + dx
        if corner in ("sw", "se"):
            h = drag["orig_h"] + dy
        if corner in ("nw", "ne"):
            h = drag["orig_h"] - dy
            y = drag["orig_y"] + dy

        if w < self._min_width:
            if corner in ("nw", "sw"):
                x = drag["orig_x"] + drag["orig_w"] - self._min_width
            w = self._min_width
        if h < self._min_height:
            if corner in ("nw", "ne"):
                y = drag["orig_y"] + drag["orig_h"] - self._min_height
            h = self._min_height

        self._root.geometry(f"{w}x{h}+{x}+{y}")

    # ---- region sync ----

    def _on_configure(self, _event):
        self._push_region()

    def _push_region(self):
        x = self._root.winfo_rootx()
        y = self._root.winfo_rooty()
        w = self._root.winfo_width()
        h = self._root.winfo_height()
        self._state.set_region(
            left=x + self._border,
            top=y + self._border,
            width=max(w - 2 * self._border, 1),
            height=max(h - 2 * self._border, 1),
        )

    # ---- control panel ----

    def _build_control_panel(self):
        panel = tk.Toplevel(self._root)
        panel.title(f"{APP_NAME} — Not Sharing")
        panel.attributes("-topmost", True)
        panel.resizable(True, True)
        panel.protocol("WM_DELETE_WINDOW", self._on_close)
        self._panel = panel

        pad = self._px(10)

        self._status_var = tk.StringVar(value=self.NOT_SHARING_TEXT)
        tk.Label(panel, textvariable=self._status_var, font=self._font_normal, anchor="w", justify="left").pack(
            fill="x", padx=pad, pady=(pad, 0)
        )

        self._mode_var = tk.StringVar(value="")  # No source chosen yet.
        mode_row = tk.Frame(panel)
        mode_row.pack(pady=(self._px(10), 0))

        self._region_btn = tk.Button(
            mode_row, text="Region", font=self._font_mode, width=10,
            padx=self._px(4), pady=self._px(4), command=lambda: self._set_mode("region"),
        )
        self._region_btn.pack(side="left")
        self._window_btn = tk.Button(
            mode_row, text="Window", font=self._font_mode, width=10,
            padx=self._px(4), pady=self._px(4), command=lambda: self._set_mode("window"),
        )
        self._window_btn.pack(side="left", padx=(self._px(6), 0))

        # Packed only while mode == "window" (see _on_mode_change).
        self._window_picker = tk.Frame(panel)
        self._window_listbox = tk.Listbox(self._window_picker, height=6, exportselection=False, font=self._font_normal)
        self._window_listbox.pack(side="left", fill="both", expand=True)
        self._window_listbox.bind("<<ListboxSelect>>", self._on_window_selected)
        self._tooltip = _ListboxTooltip(
            self._window_listbox,
            lambda i: self._window_list[i][1] if i < len(self._window_list) else None,
            self._font_normal,
        )
        self._refresh_btn = tk.Button(self._window_picker, text="Refresh", font=self._font_mode, command=self._refresh_windows)
        self._refresh_btn.pack(side="left", padx=(self._px(6), 0), fill="y")
        self._window_list = []

        self._button_row = tk.Frame(panel)
        self._button_row.pack(pady=(0, pad))

        self._start_btn = tk.Button(
            self._button_row, text="Start Sharing", font=self._font_mode, state="disabled", command=self._on_start
        )
        self._start_btn.pack(side="left", padx=self._px(5))

        self._stop_btn = tk.Button(
            self._button_row, text="Stop Sharing", font=self._font_mode, state="disabled", command=self._on_stop
        )
        self._stop_btn.pack(side="left", padx=self._px(5))

        # Packed only while actively sharing (see _on_start / _on_stop).
        self._sharing_info = tk.Frame(panel)

        share_row = tk.Frame(self._sharing_info)
        share_row.pack(fill="x", pady=(0, self._px(8)))

        self._qr_label = tk.Label(share_row, image=self._qr_photo)
        self._qr_label.pack(side="left", padx=(0, self._px(10)))

        info_col = tk.Frame(share_row)
        info_col.pack(side="left", fill="both", expand=True)

        tk.Label(info_col, text=self._format_code(self._state.code), font=self._font_code).pack(anchor="w")
        tk.Label(
            info_col, text=f"or visit {self._landing_url} and enter the code",
            font=self._font_small, fg="#777777",
        ).pack(anchor="w", pady=(self._px(2), self._px(6)))

        link_row = tk.Frame(info_col)
        link_row.pack(fill="x")
        self._url_var = tk.StringVar(value=self._viewer_url)
        self._url_entry = tk.Entry(link_row, textvariable=self._url_var, font=self._font_normal, state="readonly")
        self._url_entry.pack(side="left", fill="x", expand=True)
        self._copy_btn = tk.Button(link_row, text="Copy", font=self._font_mode, command=self._on_copy_url)
        self._copy_btn.pack(side="left", padx=(self._px(6), 0))

        self._viewers_toggle = tk.Label(
            self._sharing_info, text="", font=self._font_normal, fg="#2563eb", cursor="hand2"
        )
        self._viewers_toggle.pack(anchor="w")
        self._viewers_toggle.bind("<Button-1>", lambda _e: self._toggle_viewer_details())

        self._viewers_detail = tk.Listbox(self._sharing_info, height=4, font=("Consolas", 9))
        # Packed only when expanded (see _toggle_viewer_details).

        panel.update_idletasks()
        panel.minsize(panel.winfo_reqwidth(), panel.winfo_reqheight())

    def _set_mode(self, mode):
        self._mode_var.set(mode)
        self._update_mode_buttons()
        self._on_mode_change()
        self._maybe_switch_live_source()

    def _update_mode_buttons(self):
        mode = self._mode_var.get()
        self._region_btn.config(
            relief="sunken" if mode == "region" else "raised",
            bg="#cfe8ff" if mode == "region" else "SystemButtonFace",
        )
        self._window_btn.config(
            relief="sunken" if mode == "window" else "raised",
            bg="#cfe8ff" if mode == "window" else "SystemButtonFace",
        )

    def _on_mode_change(self):
        if self._mode_var.get() == "window":
            self._root.withdraw()  # The region frame is irrelevant in window mode.
            self._window_picker.pack(
                fill="both", expand=True, padx=self._px(10), pady=(self._px(6), 0), before=self._button_row
            )
            self._refresh_windows()
        else:
            self._window_picker.pack_forget()
            self._root.deiconify()
        self._update_start_state()

    def _refresh_windows(self):
        self._window_list = list_windows(exclude_pid=os.getpid())
        self._window_listbox.delete(0, tk.END)
        for _hwnd, title in self._window_list:
            self._window_listbox.insert(tk.END, title)
        self._update_start_state()

    def _selected_hwnd(self):
        selection = self._window_listbox.curselection()
        if not selection:
            return None
        return self._window_list[selection[0]][0]

    def _on_window_selected(self, _event=None):
        self._update_start_state()
        self._maybe_switch_live_source()

    def _update_start_state(self):
        mode = self._mode_var.get()
        if mode == "region":
            can_start = True
        elif mode == "window":
            can_start = self._selected_hwnd() is not None
        else:
            can_start = False
        self._start_btn.config(state="normal" if can_start else "disabled")

    def _commit_source(self):
        """Push the currently chosen mode/target into shared state.
        Returns False if window mode has no selection yet (nothing changed)."""
        mode = self._mode_var.get()
        if mode == "window":
            hwnd = self._selected_hwnd()
            if hwnd is None:
                return False
            self._state.set_target_hwnd(hwnd)
            self._state.set_mode("window")
        else:
            self._state.set_mode("region")
        return True

    def _maybe_switch_live_source(self):
        """If already sharing, retarget the running capture worker immediately - no restart,
        no new code/link, viewers never reconnect."""
        if not self._state.running.is_set():
            return
        if not self._commit_source():
            return  # e.g. clicked "Window" but hasn't picked one yet - keep showing the old source.
        if self._mode_var.get() == "window":
            hwnd = self._selected_hwnd()
            title = next((t for h, t in self._window_list if h == hwnd), "the window")
            message = f"Switched to sharing: {title}"
        else:
            message = "Switched to sharing your region."
        self._status_var.set(message)
        self._panel.after(2000, lambda: self._status_var.set("Share the code, link, or QR code below:"))

    def _on_start(self):
        if not self._commit_source():
            self._status_var.set("Pick a window from the list first.")
            return

        self._state.start_capture()
        self._source_warning_active = False
        self._panel.title(f"{APP_NAME} — Sharing")
        self._status_var.set("Share the code, link, or QR code below:")
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._start_pulse()

        self._viewers_expanded = False
        self._viewers_detail.pack_forget()
        self._sharing_info.pack(fill="x", padx=self._px(10), pady=(0, self._px(4)), before=self._button_row)
        self._update_viewers_display()

        self._panel.after(1000, self._poll_worker)

    def _poll_worker(self):
        """Safety net for the capture thread crashing outright; also watches for the shared
        window disappearing (which no longer stops the session - see _check_source_validity)."""
        worker = self._state.capture_worker
        if worker is None:
            return
        if not worker.is_alive():
            self._on_stop(auto=True)
            return
        self._update_viewers_display()
        self._check_source_validity()
        if self._state.running.is_set():
            self._panel.after(1000, self._poll_worker)

    def _check_source_validity(self):
        hwnd = self._state.get_target_hwnd()
        invalid_now = self._state.get_mode() == "window" and (hwnd is None or not is_window_valid(hwnd))
        if invalid_now and not self._source_warning_active:
            self._source_warning_active = True
            self._status_var.set("The shared window was closed — pick a window above or switch to Region.")
        elif not invalid_now and self._source_warning_active:
            self._source_warning_active = False
            self._status_var.set("Share the code, link, or QR code below:")

    def _on_stop(self, auto=False):
        self._state.stop_capture()
        self._stop_pulse()
        self._panel.title(f"{APP_NAME} — Not Sharing")
        self._status_var.set(
            "Sharing stopped unexpectedly. Pick a source and Start Sharing." if auto else self.NOT_SHARING_TEXT
        )
        self._sharing_info.pack_forget()
        self._update_start_state()
        self._stop_btn.config(state="disabled")

    def _on_copy_url(self):
        self._panel.clipboard_clear()
        self._panel.clipboard_append(self._viewer_url)
        self._copy_btn.config(text="Copied!")
        self._panel.after(1200, lambda: self._copy_btn.config(text="Copy"))

    def _toggle_viewer_details(self):
        self._viewers_expanded = not self._viewers_expanded
        if self._viewers_expanded:
            self._viewers_detail.pack(fill="x", pady=(self._px(4), 0))
        else:
            self._viewers_detail.pack_forget()
        self._update_viewers_display()

    def _update_viewers_display(self):
        rows, active, total = self._state.viewers.snapshot()
        arrow = "▾" if self._viewers_expanded else "▸"
        self._viewers_toggle.config(text=f"Viewers: {active} active · {total} total  {arrow}")

        if not self._viewers_expanded:
            return
        self._viewers_detail.delete(0, tk.END)
        if not rows:
            self._viewers_detail.insert(tk.END, "No viewers yet")
            return
        for row in rows:
            status = "connected" if row["active"] else "left"
            host = f" ({row['hostname']})" if row["hostname"] else ""
            minutes, seconds = divmod(int(row["seconds"]), 60)
            duration = f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"
            self._viewers_detail.insert(
                tk.END, f"{row['ip']}{host} — {row['label']} — {status} · {duration}"
            )

    def _start_pulse(self):
        self._pulse_on = True
        self._pulse_index = 0
        self._pulse_step()

    def _pulse_step(self):
        if not self._pulse_on:
            return
        path = ICON_LIVE_PATHS[self._pulse_index % len(ICON_LIVE_PATHS)]
        self._pulse_index += 1
        if os.path.exists(path):
            self._panel.iconbitmap(default=path)
        self._panel.after(600, self._pulse_step)

    def _stop_pulse(self):
        self._pulse_on = False
        if os.path.exists(ICON_PATH):
            self._panel.iconbitmap(default=ICON_PATH)

    def _on_close(self):
        self._state.stop_capture()
        self._root.destroy()


# ============================================================
# App entry point
# ============================================================

PORT = 5000


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # No packet actually sent; just picks the outbound interface.
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


def _port_available(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    enable_dpi_awareness()

    if not _port_available(PORT):
        fatal_error_dialog(
            f"Port {PORT} is already in use — another {APP_NAME} instance is "
            "probably still running. Close it and try again."
        )
        return

    state = SharedState()

    app = create_app(state)
    server_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    lan_ip = get_lan_ip()
    landing_url = f"http://{lan_ip}:{PORT}/"
    viewer_url = f"{landing_url}view?code={state.code}"
    print(f"{APP_NAME} ready. Direct link (share after clicking Start): {viewer_url}")
    print(f"Or viewers can go to {landing_url} and enter code: {state.code}")

    overlay = Overlay(state, viewer_url, landing_url)
    overlay.run()

    state.stop_capture()


if __name__ == "__main__":
    main()
