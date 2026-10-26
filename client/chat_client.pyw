"""局域网聊天客户端：登录窗口 + 好友列表 + 单独聊天窗口，Tkinter GUI。"""
import base64
import ctypes
import ctypes.wintypes
import io
import json
import os
import queue
import socket
import ssl
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import uuid
from tkinter import filedialog, messagebox, scrolledtext, ttk

from PIL import Image, ImageGrab, ImageTk

from chat_common import CHAT_PASSPHRASE, DEFAULT_PORT, create_client_ssl_context, decrypt_text, encrypt_text, recv_msg, send_msg

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGIN_CONFIG_FILE = os.path.join(BASE_DIR, "login_config.json")
WINDOW_CONFIG_FILE = os.path.join(BASE_DIR, "window_config.json")
EMOJI_CACHE_DIR = os.path.join(BASE_DIR, "emoji_cache")
MAX_FILE_SIZE = 8 * 1024 * 1024  # 单个文件/图片最大 8MB
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
THUMBNAIL_SIZE = (220, 220)
TYPING_REPEAT_SEC = 2.0    # 持续敲字时重发通知的最小间隔
TYPING_IDLE_MS = 3000      # 停手这么久后告知对方已结束输入
TYPING_EXPIRE_MS = 8000    # 收不到后续通知时自动收起提示，防止掉包卡住
AI_SENDER = "AI"  # 服务器保留发送者名，用于识别 AI 回复气泡
# 空闲连接会被路由器/防火墙/云 NAT 悄悄回收且两端都收不到通知，需定时心跳保活并检测死连接
HEARTBEAT_INTERVAL_MS = 20_000
HEARTBEAT_TIMEOUT_SEC = 60  # 服务器支持 pong 时，超过这么久没收到任何数据就判定连接已死
RECONNECT_DELAYS_SEC = (1, 3, 5, 10, 30)
DISCONNECTED_SUFFIX = "（连接断开，正在重连...）"


class _FlashInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hwnd", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint),
        ("uCount", ctypes.c_uint),
        ("dwTimeout", ctypes.c_uint),
    ]


_FLASHW_ALL = 0x00000003
_FLASHW_TIMERNOFG = 0x0000000C
_GA_ROOT = 2

# DwmSetWindowAttribute 属性号：20 为 Win10 2004+/Win11，19 为更早的 Win10 预览版
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36

_user32 = ctypes.windll.user32 if sys.platform == "win32" else None
_dwmapi = ctypes.windll.dwmapi if sys.platform == "win32" else None
if _user32 is not None:
    _user32.GetAncestor.restype = ctypes.c_void_p
    _user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.FlashWindowEx.argtypes = [ctypes.POINTER(_FlashInfo)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork", ctypes.wintypes.RECT),
        ("dwFlags", ctypes.c_uint),
    ]


_MONITOR_DEFAULTTONEAREST = 2
if _user32 is not None:
    _user32.MonitorFromPoint.restype = ctypes.c_void_p
    _user32.MonitorFromPoint.argtypes = [ctypes.wintypes.POINT, ctypes.c_uint]
    _user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfo)]


def monitor_work_area(win, x: int, y: int) -> tuple:
    """返回包含点 (x, y) 的显示器工作区 (left, top, right, bottom)，多屏时坐标可为负。"""
    if _user32 is not None:
        try:
            hmon = _user32.MonitorFromPoint(ctypes.wintypes.POINT(int(x), int(y)), _MONITOR_DEFAULTTONEAREST)
            info = _MonitorInfo()
            info.cbSize = ctypes.sizeof(_MonitorInfo)
            if hmon and _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                rc = info.rcWork
                return rc.left, rc.top, rc.right, rc.bottom
        except (OSError, ctypes.ArgumentError):
            pass
    return 0, 0, win.winfo_screenwidth(), win.winfo_screenheight()


def flash_window(win) -> None:
    """窗口不是当前前台窗口时闪烁任务栏图标，直到用户点击聚焦（仅 Windows）。

    winfo_id() 返回的是 Tk 内嵌绘图区域的句柄而非真正的顶层窗口，
    需用 GetAncestor 找到真正会在任务栏上闪烁的顶层句柄。
    """
    if _user32 is None:
        return
    try:
        hwnd = _user32.GetAncestor(win.winfo_id(), _GA_ROOT)
        if not hwnd or hwnd == _user32.GetForegroundWindow():
            return
        info = _FlashInfo(ctypes.sizeof(_FlashInfo), hwnd, _FLASHW_ALL | _FLASHW_TIMERNOFG, 0, 0)
        _user32.FlashWindowEx(ctypes.byref(info))
    except (tk.TclError, OSError, AttributeError, ctypes.ArgumentError):
        pass


# 参照 VS Code Dark+ / Light+ 主题的配色
DARK_PALETTE = {
    "name": "dark",
    "bg": "#1e1e1e",
    "panel": "#252526",
    "input_bg": "#3c3c3c",
    "btn_bg": "#3c3c3c",
    "btn_fg": "#ffffff",
    "fg": "#ffffff",
    "border": "#454545",
    "accent": "#0e639c",
    "accent_active": "#1177bb",
    "select_bg": "#264f78",
    "select_fg": "#ffffff",
    "titlebar": "#1e1e1e",
    "scroll_thumb": "#4a4a4a",
    "scroll_thumb_hover": "#6b6b6b",
    "hint": "#4fc1ff",
    "ai": "#4EC9B0",
}

LIGHT_PALETTE = {
    "name": "light",
    "bg": "#f3f3f3",
    "panel": "#ffffff",
    "input_bg": "#ffffff",
    "btn_bg": "#e4e4e4",
    "btn_fg": "#1f1f1f",
    "fg": "#1f1f1f",
    "border": "#c4c4c4",
    "accent": "#0078d4",
    "accent_active": "#106ebe",
    "select_bg": "#cce4f7",
    "select_fg": "#1f1f1f",
    "titlebar": "#f3f3f3",
    "scroll_thumb": "#c2c2c2",
    "scroll_thumb_hover": "#9a9a9a",
    "hint": "#0067c0",
    "ai": "#0e7a67",
}


class Theme:
    """当前配色，切换时原地更新属性，所有取色处都在调用时读取即可自动跟随。"""

    def __init__(self, palette: dict):
        self.__dict__.update(palette)

    @property
    def is_dark(self) -> bool:
        return self.name == "dark"


THEME = Theme(DARK_PALETTE)
_themed_windows = []


def register_themed_window(win, on_theme_change=None) -> None:
    _themed_windows.append((win, on_theme_change))


def toggle_theme() -> None:
    """在暗色/亮色之间切换，刷新所有已登记窗口，并记住选择。"""
    THEME.__dict__.update(LIGHT_PALETTE if THEME.is_dark else DARK_PALETTE)
    alive = [item for item in _themed_windows if _window_alive(item[0])]
    _themed_windows[:] = alive
    for win, callback in alive:
        apply_theme(win)
        restyle_widgets(win)
        if callback is not None:
            callback()
    save_theme(THEME.name)


def load_saved_theme() -> None:
    if load_login_config().get("theme") == LIGHT_PALETTE["name"]:
        THEME.__dict__.update(LIGHT_PALETTE)


def _window_alive(win) -> bool:
    try:
        return bool(win.winfo_exists())
    except tk.TclError:
        return False


def theme_icon() -> str:
    """月亮=当前暗色，太阳=当前亮色。"""
    return "\U0001F319" if THEME.is_dark else "\U0001F31E"


def _hex_to_colorref(hex_color: str) -> int:
    """Win32 COLORREF 是 0x00BBGGRR，与 #RRGGBB 的字节序相反。"""
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    return (b << 16) | (g << 8) | r


def apply_titlebar(win) -> None:
    """让 Windows 系统标题栏（含最小化/关闭按钮区域）跟随当前主题。"""
    if _dwmapi is None or _user32 is None:
        return
    try:
        win.update_idletasks()
        hwnd = _user32.GetAncestor(win.winfo_id(), _GA_ROOT)
        if not hwnd:
            return
        hwnd = ctypes.c_void_p(hwnd)
        flag = ctypes.c_int(1 if THEME.is_dark else 0)
        for attr in (_DWMWA_USE_IMMERSIVE_DARK_MODE, _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
            if _dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(flag), ctypes.sizeof(flag)) == 0:
                break
        # Win11 可直接指定标题栏/边框/标题文字颜色，旧系统会返回错误码，忽略即可
        for attr, color in (
            (_DWMWA_CAPTION_COLOR, THEME.titlebar),
            (_DWMWA_BORDER_COLOR, THEME.titlebar),
            (_DWMWA_TEXT_COLOR, THEME.fg),
        ):
            value = ctypes.c_int(_hex_to_colorref(color))
            _dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
    except (tk.TclError, OSError, AttributeError, ctypes.ArgumentError, ValueError):
        pass


def apply_theme(win) -> None:
    """把窗口及其 ttk 控件整体切换为当前主题。"""
    win.configure(bg=THEME.bg)
    apply_titlebar(win)
    # 窗口刚创建时句柄可能尚未真正映射，稍后再设一次保证标题栏一定跟随
    win.after(60, lambda: apply_titlebar(win))
    style = ttk.Style(win)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=THEME.bg, foreground=THEME.fg, fieldbackground=THEME.input_bg)
    style.configure("TFrame", background=THEME.bg)
    style.configure("TLabel", background=THEME.bg, foreground=THEME.fg)
    style.configure("TButton", background=THEME.btn_bg, foreground=THEME.btn_fg, borderwidth=1)
    style.map("TButton", background=[("active", THEME.accent), ("pressed", THEME.accent_active)])
    style.configure("TEntry", fieldbackground=THEME.input_bg, foreground=THEME.fg, insertcolor=THEME.fg)
    style.configure("TCheckbutton", background=THEME.bg, foreground=THEME.fg)


def restyle_widgets(widget) -> None:
    """递归刷新经典 tk 控件的颜色；ttk 控件由 ttk.Style 统一接管，无需遍历。"""
    for child in widget.winfo_children():
        if isinstance(child, ModernScrollbar):
            child.restyle()
        elif isinstance(child, tk.Button):
            child.configure(bg=THEME.btn_bg, fg=THEME.btn_fg,
                            activebackground=THEME.accent, activeforeground="#ffffff")
        elif isinstance(child, tk.Entry):
            child.configure(bg=THEME.input_bg, fg=THEME.fg, insertbackground=THEME.fg,
                            highlightbackground=THEME.border, highlightcolor=THEME.accent)
        elif isinstance(child, tk.Listbox):
            style_listbox(child)
        elif isinstance(child, tk.Text):
            child.configure(bg=THEME.panel, fg=THEME.fg, insertbackground=THEME.fg,
                            selectbackground=THEME.select_bg, selectforeground=THEME.select_fg)
        elif isinstance(child, tk.Label):
            child.configure(bg=_parent_bg(widget), fg=THEME.fg)
        elif isinstance(child, (tk.Frame, tk.Canvas, tk.Toplevel)):
            child.configure(bg=THEME.bg)
        restyle_widgets(child)


def _parent_bg(widget) -> str:
    """内嵌在聊天框里的标签要跟随聊天框底色，ttk 容器则回退到窗口底色。"""
    try:
        return widget.cget("bg")
    except tk.TclError:
        return THEME.bg


def center_window(win, width: int = 0, height: int = 0) -> None:
    """把窗口摆到屏幕中央偏上，避免默认落在左上角。"""
    win.update_idletasks()
    width = width or win.winfo_reqwidth()
    height = height or win.winfo_reqheight()
    x = max(0, (win.winfo_screenwidth() - width) // 2)
    y = max(0, (win.winfo_screenheight() - height) // 3)
    win.geometry(f"{width}x{height}+{x}+{y}")


def hide_until_ready(win) -> None:
    """先隐藏并铺上主题底色，等界面搭完再显示，避免出现系统默认的白色空窗。"""
    win.withdraw()
    win.configure(bg=THEME.bg)


def place_beside(win, parent, gap: int = 8) -> None:
    """把弹窗贴在父窗口右侧，右边放不下就改放左侧，并保证不越出屏幕。"""
    win.update_idletasks()
    parent.update_idletasks()
    width, height = win.winfo_reqwidth(), win.winfo_reqheight()
    # geometry 的 +x+y 是外框坐标，宽度却是客户区宽度，需补上左右边框
    border = max(0, parent.winfo_rootx() - parent.winfo_x())
    outer_w = width + 2 * border
    left, top, right, bottom = monitor_work_area(
        parent, parent.winfo_x() + parent.winfo_width() // 2, parent.winfo_y() + parent.winfo_height() // 2,
    )
    x = parent.winfo_x() + parent.winfo_width() + 2 * border + gap
    if x + outer_w > right:
        x = parent.winfo_x() - gap - outer_w
    x = max(left, min(x, right - outer_w))
    y = max(top, min(parent.winfo_y(), bottom - height))
    win.geometry(f"{width}x{height}+{x}+{y}")


class ModernScrollbar(tk.Canvas):
    """仿微信/QQ 的滚动条：无箭头、无浅色凹槽、圆角滑块，悬停或拖动时变亮。

    ttk/tk 自带滚动条在 Windows 上总会残留浅色凹槽与箭头，只能自绘。
    """

    MIN_THUMB_LEN = 28

    def __init__(self, master, command, width=10):
        super().__init__(master, width=width, highlightthickness=0, bd=0,
                         bg=THEME.panel, takefocus=0)
        self.command = command
        self._thumb = THEME.scroll_thumb
        self._thumb_hover = THEME.scroll_thumb_hover
        self._first = 0.0
        self._last = 1.0
        self._hover = False
        self._drag_delta = None
        self.bind("<Configure>", lambda event: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<MouseWheel>", self._on_wheel)

    def restyle(self) -> None:
        self._thumb = THEME.scroll_thumb
        self._thumb_hover = THEME.scroll_thumb_hover
        self.configure(bg=THEME.panel)
        self._redraw()

    def set(self, first, last) -> None:
        self._first, self._last = float(first), float(last)
        self._redraw()

    def _thumb_bounds(self):
        height = self.winfo_height()
        span = self._last - self._first
        if height <= 1 or span >= 1.0 or span <= 0:
            return None
        length = max(self.MIN_THUMB_LEN, height * span)
        top = self._first / (1.0 - span) * (height - length)
        return top, top + length

    def _redraw(self) -> None:
        self.delete("thumb")
        bounds = self._thumb_bounds()
        if bounds is None:
            return
        top, bottom = bounds
        width = self.winfo_width()
        pad = max(2, width // 4)
        active = self._hover or self._drag_delta is not None
        self._rounded_thumb(pad, top + 1, width - pad, bottom - 1,
                            self._thumb_hover if active else self._thumb)

    def _rounded_thumb(self, x1, y1, x2, y2, color) -> None:
        radius = max(1.0, min((x2 - x1) / 2, (y2 - y1) / 2))
        self.create_oval(x1, y1, x2, y1 + 2 * radius, fill=color, outline=color, tags="thumb")
        self.create_oval(x1, y2 - 2 * radius, x2, y2, fill=color, outline=color, tags="thumb")
        self.create_rectangle(x1, y1 + radius, x2, y2 - radius, fill=color, outline=color, tags="thumb")

    def _on_enter(self, _event=None):
        self._hover = True
        self._redraw()

    def _on_leave(self, _event=None):
        self._hover = False
        self._redraw()

    def _on_press(self, event):
        bounds = self._thumb_bounds()
        if bounds is None:
            return
        top, bottom = bounds
        if top <= event.y <= bottom:
            self._drag_delta = event.y - top
        else:
            # 点击空白处让滑块中心跳到鼠标位置，和微信一致
            self._drag_delta = (bottom - top) / 2
            self._scroll_to(event.y)
        self._redraw()

    def _on_drag(self, event):
        if self._drag_delta is not None:
            self._scroll_to(event.y)

    def _on_release(self, _event=None):
        self._drag_delta = None
        self._redraw()

    def _on_wheel(self, event):
        self.command("scroll", -1 if event.delta > 0 else 1, "units")
        return "break"

    def _scroll_to(self, y) -> None:
        bounds = self._thumb_bounds()
        if bounds is None:
            return
        track = max(1.0, self.winfo_height() - (bounds[1] - bounds[0]))
        position = (y - self._drag_delta) / track * (1.0 - (self._last - self._first))
        self.command("moveto", max(0.0, min(1.0, position)))


class WindowScaler:
    """把窗口宽高比锁死为设计比例，并让字体、内边距随窗口尺寸等比例缩放。"""

    def __init__(self, win, base_w: int, base_h: int, lock_aspect: bool = True):
        self.win = win
        self.base_w = base_w
        self.base_h = base_h
        self.scale = 1.0
        self._fonts = []
        self._options = []
        self._callbacks = []
        center_window(win, base_w, base_h)
        win.minsize(int(base_w * 0.65), int(base_h * 0.65))
        if lock_aspect:
            win.aspect(base_w, base_h, base_w, base_h)
        win.bind("<Configure>", self._on_configure, add="+")

    def font(self, size: int = 10, family: str = "Microsoft YaHei UI", weight: str = "normal"):
        font_obj = tkfont.Font(family=family, size=size, weight=weight)
        self._fonts.append((font_obj, size))
        return font_obj

    def track(self, widget, **base_options):
        """登记随缩放变化的数值型选项，如 padx / pady / padding / width。"""
        for option, value in base_options.items():
            self._options.append((widget, option, value))
        return widget

    def on_scale(self, callback) -> None:
        self._callbacks.append(callback)

    def _on_configure(self, event):
        if event.widget is not self.win:
            return
        scale = min(event.width / self.base_w, event.height / self.base_h)
        # 下限不完全按比例走，否则窗口缩小后字号小到看不清
        scale = max(0.75, min(3.0, scale))
        if abs(scale - self.scale) < 0.02:
            return
        self.scale = scale
        self._apply()

    def _apply(self) -> None:
        for font_obj, base_size in self._fonts:
            font_obj.configure(size=max(9, round(base_size * self.scale)))
        for widget, option, base_value in self._options:
            try:
                widget.configure(**{option: max(0, round(base_value * self.scale))})
            except tk.TclError:
                pass
        for callback in self._callbacks:
            callback(self.scale)


def style_listbox(listbox: tk.Listbox) -> None:
    listbox.configure(
        bg=THEME.input_bg, fg=THEME.fg, selectbackground=THEME.select_bg,
        selectforeground=THEME.select_fg, highlightthickness=1,
        highlightbackground=THEME.border, highlightcolor=THEME.accent, borderwidth=0,
    )


def style_text(text_widget, scrollbar_width: int = 10):
    text_widget.configure(
        bg=THEME.panel, fg=THEME.fg, insertbackground=THEME.fg,
        selectbackground=THEME.select_bg, selectforeground=THEME.select_fg,
        highlightthickness=0, borderwidth=0,
    )
    # tk/ttk 自带滚动条在 Windows 上会残留浅色凹槽和箭头，换成自绘的细滚动条
    vbar = getattr(text_widget, "vbar", None)
    frame = getattr(text_widget, "frame", None)
    if vbar is None or frame is None:
        return None
    frame.configure(bg=THEME.panel)
    vbar.destroy()
    new_vbar = ModernScrollbar(frame, command=text_widget.yview, width=scrollbar_width)
    # ScrolledText 把自身的 pack/grid 换成了外层 frame 的，要通过 tk.Text 才能操作内部 Text 的布局
    tk.Text.pack_forget(text_widget)
    new_vbar.pack(side="right", fill="y")
    tk.Text.pack(text_widget, side="left", fill="both", expand=True)
    text_widget.configure(yscrollcommand=new_vbar.set)
    text_widget.vbar = new_vbar
    return new_vbar


def dark_button(parent, **kwargs) -> tk.Button:
    """ttk.Button 在 Windows 原生主题下会忽略背景/文字颜色配置，改用经典 tk.Button 确保样式生效。"""
    kwargs.setdefault("bg", THEME.btn_bg)
    kwargs.setdefault("fg", THEME.btn_fg)
    kwargs.setdefault("activebackground", THEME.accent)
    kwargs.setdefault("activeforeground", "#ffffff")
    kwargs.setdefault("relief", "flat")
    kwargs.setdefault("bd", 0)
    kwargs.setdefault("highlightthickness", 0)
    kwargs.setdefault("cursor", "hand2")
    kwargs.setdefault("padx", 10)
    kwargs.setdefault("pady", 4)
    return tk.Button(parent, **kwargs)


def dark_entry(parent, **kwargs) -> tk.Entry:
    """同样原因，用经典 tk.Entry 代替 ttk.Entry 以确保背景/文字颜色生效。"""
    kwargs.setdefault("bg", THEME.input_bg)
    kwargs.setdefault("fg", THEME.fg)
    kwargs.setdefault("insertbackground", THEME.fg)
    kwargs.setdefault("relief", "flat")
    kwargs.setdefault("highlightthickness", 1)
    kwargs.setdefault("highlightbackground", THEME.border)
    kwargs.setdefault("highlightcolor", THEME.accent)
    return tk.Entry(parent, **kwargs)


def load_login_config() -> dict:
    if not os.path.exists(LOGIN_CONFIG_FILE):
        return {}
    try:
        with open(LOGIN_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_login_config(host: str, port, username: str, password: str) -> None:
    # 仅写入本机文件用于下次自动填充，不做任何网络传输
    data = load_login_config()
    data.update({"host": host, "port": port, "username": username, "password": password})
    _write_login_config(data)


def save_theme(name: str) -> None:
    data = load_login_config()
    data["theme"] = name
    _write_login_config(data)


def _write_login_config(data: dict) -> None:
    try:
        with open(LOGIN_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def load_window_config() -> dict:
    try:
        with open(WINDOW_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def window_center(win) -> tuple:
    return win.winfo_x() + win.winfo_width() // 2, win.winfo_y() + win.winfo_height() // 2


def remember_window(kind: str, win) -> None:
    """记录窗口大小（按 kind 区分好友/聊天窗口）和共用的窗口中心点。"""
    try:
        if not win.winfo_viewable():
            return
        data = load_window_config()
        data[kind] = {"width": win.winfo_width(), "height": win.winfo_height()}
        data.pop("x", None)
        data.pop("y", None)
        data["cx"], data["cy"] = window_center(win)
        with open(WINDOW_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except (tk.TclError, OSError):
        pass


def restore_window(kind: str, win, center=None) -> None:
    """按配置恢复窗口大小，并把窗口中心对齐到 center（未指定时用上次保存的中心）。"""
    data = load_window_config()
    win.update_idletasks()
    # 隐藏中的窗口 winfo_width 可能是 1，从已设定的 geometry 字符串取尺寸更可靠
    width, height = (int(v) for v in win.geometry().split("+")[0].split("x"))
    if width <= 1 or height <= 1:
        width, height = win.winfo_reqwidth(), win.winfo_reqheight()
    try:
        size = data.get(kind) or {}
        if int(size["width"]) > 1 and int(size["height"]) > 1:
            width, height = int(size["width"]), int(size["height"])
    except (KeyError, TypeError, ValueError):
        pass
    geometry = f"{width}x{height}"
    try:
        cx, cy = center if center is not None else (int(data["cx"]), int(data["cy"]))
        # 只在中心点所在的显示器内收敛，左侧副屏的负坐标不能被当成越界
        left, top, right, bottom = monitor_work_area(win, cx, cy)
        x = max(left, min(cx - width // 2, right - width))
        y = max(top, min(cy - height // 2, bottom - height))
        geometry += f"+{x}+{y}"
    except (KeyError, TypeError, ValueError):
        pass
    win.geometry(geometry)


def friends_file(username: str) -> str:
    return os.path.join(BASE_DIR, f"friends_{username}.json")


def load_friends(username: str) -> list:
    path = friends_file(username)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def save_friends(username: str, friends: list) -> None:
    with open(friends_file(username), "w", encoding="utf-8") as f:
        json.dump(friends, f, ensure_ascii=False, indent=2)


def sync_emoji_cache(files: dict) -> None:
    """把服务器下发的表情包存入本地缓存目录，已存在的文件不重复写入。"""
    if not files:
        return
    os.makedirs(EMOJI_CACHE_DIR, exist_ok=True)
    for name, b64_data in files.items():
        path = os.path.join(EMOJI_CACHE_DIR, name)
        if os.path.exists(path):
            continue
        try:
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64_data))
        except (OSError, ValueError):
            pass


def load_emoji_images(size: int = 22) -> dict:
    """从本地表情缓存加载图片，返回 {表情字符: PhotoImage}，需在 Tk 根窗口建立后调用。"""
    images = {}
    if not os.path.isdir(EMOJI_CACHE_DIR):
        return images
    for name in os.listdir(EMOJI_CACHE_DIR):
        if not name.lower().endswith(".png"):
            continue
        try:
            emoji_char = chr(int(name[:-4], 16))
            img = Image.open(os.path.join(EMOJI_CACHE_DIR, name)).convert("RGBA")
            img = img.resize((size, size), Image.LANCZOS)
            images[emoji_char] = ImageTk.PhotoImage(img)
        except (ValueError, OSError):
            continue
    return images


def is_image_filename(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in IMAGE_EXTS


def make_file_payload(filename: str, data: bytes) -> dict:
    return {
        "kind": "file",
        "filename": filename,
        "mime": "image" if is_image_filename(filename) else "file",
        "size": len(data),
        "data": base64.b64encode(data).decode("ascii"),
    }


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}MB"


class LoginError(Exception):
    """服务器明确拒绝登录（如密码错误），重试也没用。"""


def enable_keepalive(sock: socket.socket) -> None:
    """开启 TCP 保活，作为应用层心跳之外的兜底。"""
    try:
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30_000, 5_000))
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except (OSError, AttributeError, ValueError):
        pass


def connect_and_login(host: str, port: int, username: str, password: str, invite_code: str = ""):
    """建立 TLS 连接并登录，返回 (sock, login_ok 响应)。

    网络问题抛 OSError（含 ssl.SSLError），服务器拒绝抛 LoginError。
    """
    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw_sock.settimeout(5)
    try:
        raw_sock.connect((host, port))
        enable_keepalive(raw_sock)
        sock = create_client_ssl_context().wrap_socket(raw_sock, server_hostname=host)
    except BaseException:
        raw_sock.close()
        raise
    try:
        send_msg(sock, {
            "type": "login", "username": username, "password": password,
            "invite_code": invite_code,
        })
        response = recv_msg(sock)
        if response is None:
            raise OSError("服务器无响应")
        if response.get("type") == "login_fail":
            raise LoginError(response.get("reason", "登录失败"))
        if response.get("type") != "login_ok":
            raise LoginError("登录响应异常")
        # 服务器在 login_ok 后会等一条消息，必须发出去，否则会被当作超时断开
        try:
            send_msg(sock, {"type": "get_emoji_pack"})
            emoji_resp = recv_msg(sock)
            if emoji_resp and emoji_resp.get("type") == "emoji_pack":
                sync_emoji_cache(emoji_resp.get("files", {}))
        except OSError:
            pass
    except BaseException:
        sock.close()
        raise
    sock.settimeout(None)
    return sock, response


class LoginWindow:
    def __init__(self, root):
        self.root = root
        hide_until_ready(root)
        root.title("登录 - 局域网聊天")
        root.resizable(False, False)
        apply_theme(root)
        register_themed_window(root)

        saved = load_login_config()

        main = ttk.Frame(root, padding=12)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="服务器地址:").grid(row=0, column=0, sticky="w", pady=4)
        self.host_entry = dark_entry(main)
        self.host_entry.insert(0, saved.get("host", "127.0.0.1"))
        self.host_entry.grid(row=0, column=1, pady=4)

        ttk.Label(main, text="端口:").grid(row=1, column=0, sticky="w", pady=4)
        self.port_entry = dark_entry(main)
        self.port_entry.insert(0, str(saved.get("port", DEFAULT_PORT)))
        self.port_entry.grid(row=1, column=1, pady=4)

        ttk.Label(main, text="用户名:").grid(row=2, column=0, sticky="w", pady=4)
        self.user_entry = dark_entry(main)
        self.user_entry.insert(0, saved.get("username", ""))
        self.user_entry.grid(row=2, column=1, pady=4)

        ttk.Label(main, text="密码:").grid(row=3, column=0, sticky="w", pady=4)
        self.pwd_entry = dark_entry(main, show="*")
        self.pwd_entry.insert(0, saved.get("password", ""))
        self.pwd_entry.grid(row=3, column=1, pady=4)

        ttk.Label(main, text="邀请码:").grid(row=4, column=0, sticky="w", pady=4)
        self.invite_entry = dark_entry(main)
        self.invite_entry.grid(row=4, column=1, pady=4)
        ttk.Label(main, text="（仅新用户注册需要，已注册账号留空）", foreground=THEME.hint).grid(
            row=5, column=0, columnspan=2, sticky="w")

        self.status_label = ttk.Label(main, text="", foreground="red")
        self.status_label.grid(row=6, column=0, columnspan=2, pady=4)

        self.login_btn = dark_button(main, text="登录", command=self.on_login)
        self.login_btn.grid(row=7, column=0, columnspan=2, pady=8)

        root.bind("<Return>", lambda event: self.on_login())

        center_window(root, 300, 280)
        root.deiconify()

        # 本地存有完整账号信息时自动登录，失败会停留在此界面供手动重试
        if saved.get("host") and saved.get("username") and saved.get("password"):
            self.status_label.configure(text="正在自动登录...", foreground=THEME.hint)
            self.root.after(200, self.on_login)

    def on_login(self):
        host = self.host_entry.get().strip()
        port_text = self.port_entry.get().strip()
        username = self.user_entry.get().strip()
        password = self.pwd_entry.get()
        invite_code = self.invite_entry.get().strip()

        if not host or not port_text or not username or not password:
            self.status_label.configure(text="请填写完整信息")
            return
        try:
            port = int(port_text)
        except ValueError:
            self.status_label.configure(text="端口必须是数字")
            return

        self.login_btn.configure(state="disabled")
        self.status_label.configure(text="正在连接...", foreground=THEME.hint)
        threading.Thread(
            target=self._login_worker, args=(host, port, username, password, invite_code), daemon=True
        ).start()

    def _login_worker(self, host, port, username, password, invite_code):
        try:
            sock, response = connect_and_login(host, port, username, password, invite_code)
        except FileNotFoundError:
            self.root.after(0, self._login_failed, "缺少 server.crt 证书文件，请向服务器管理员索取")
            return
        except ssl.SSLError as exc:
            self.root.after(0, self._login_failed, f"TLS 握手失败: {exc}")
            return
        except OSError as exc:
            self.root.after(0, self._login_failed, f"连接失败: {exc}")
            return
        except LoginError as exc:
            self.root.after(0, self._login_failed, str(exc))
            return

        self.root.after(
            0, self._login_success, sock, username, response.get("history", []),
            response.get("recent", []), response.get("groups", []),
            response.get("group_history", []), response.get("group_recent", []),
            response.get("broadcasts", []), response.get("friends", []),
            response.get("friend_requests", []), host, port, password
        )

    def _login_failed(self, reason):
        self.status_label.configure(text=reason, foreground="red")
        self.login_btn.configure(state="normal")

    def _login_success(self, sock, username, history, recent, groups, group_history, group_recent, broadcasts, friends, friend_requests, host, port, password):
        save_login_config(host, port, username, password)
        self.root.destroy()
        friends_root = tk.Tk()
        FriendsWindow(
            friends_root, sock, username, history, recent, groups, group_history, group_recent,
            broadcasts, friends, friend_requests, login_info=(host, port, password),
        )
        friends_root.mainloop()


class FriendsWindow:
    """好友列表主界面：好友申请/同意、未读数量提示、点击进入聊天、群聊管理。"""

    def __init__(self, root, sock, username, history, recent, groups=None, group_history=None, group_recent=None, broadcasts=None, friends=None, friend_requests=None, login_info=None):
        self.root = root
        self.sock = sock
        self.username = username
        self.login_info = login_info  # (host, port, password)，断线后自动重连用
        self.connected = True
        self._closing = False
        self._sock_gen = 0  # 每次换新连接自增，用来忽略旧连接收尾时发来的事件
        self._last_recv = time.monotonic()
        self._server_pongs = False  # 旧版服务器不回 pong，此时只靠发送失败来发现断线
        self._reconnect_attempt = 0

        # 服务器为好友关系的权威数据源，登录时下发的列表直接覆盖本地缓存
        self.friends = list(friends) if friends is not None else load_friends(username)
        save_friends(username, self.friends)
        self.unread = {}
        self._unread_order = []  # ("friend", 名字) / ("group", 群id)，越靠后越新
        self.chat_logs = {}
        self.open_chats = {}
        self.pending_ids = {}
        self.pending_wire = {}  # msg_id -> 原始报文，重连后用同一个 msg_id 补发，服务器据此去重
        self.pending_group_ids = {}  # msg_id -> group_id，等待 group_msg_ack
        self.incoming_queue = queue.Queue()
        self.active_search_dialog = None
        self.request_dialogs = {}

        self.groups = {g["id"]: g for g in (groups or [])}
        self.group_chat_logs = {}
        self.open_group_chats = {}
        self.group_unread = {}
        self._group_order = []

        hide_until_ready(root)
        self.base_title = f"好友列表 - {username}"
        root.title(self.base_title)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        apply_theme(root)
        register_themed_window(root, self._on_theme_change)
        self.scaler = WindowScaler(root, 300, 420)
        restore_window("friends", root)
        self.ui_font = self.scaler.font(size=10)
        self.icon_font = self.scaler.font(size=11, family="Segoe UI Emoji")
        self.emoji_images = load_emoji_images()

        main = ttk.Frame(root, padding=8)
        main.pack(fill="both", expand=True)
        self.scaler.track(main, padding=8)

        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill="x", pady=(0, 6))
        add_btn = dark_button(btn_frame, text="添加好友", command=self.open_add_friend_dialog, font=self.ui_font)
        add_btn.pack(side="left")
        del_btn = dark_button(btn_frame, text="删除好友", command=self.remove_selected_friend, font=self.ui_font)
        del_btn.pack(side="left", padx=4)
        group_btn = dark_button(btn_frame, text="创建群聊", command=self.open_create_group_dialog, font=self.ui_font)
        group_btn.pack(side="left", padx=4)
        self.theme_btn = dark_button(btn_frame, text=theme_icon(), command=toggle_theme, font=self.icon_font)
        self.theme_btn.pack(side="right")

        notebook = ttk.Notebook(main)
        notebook.pack(fill="both", expand=True)
        friend_tab = ttk.Frame(notebook)
        group_tab = ttk.Frame(notebook)
        notebook.add(friend_tab, text="好友")
        notebook.add(group_tab, text="群聊")

        # 底部按钮先占位，否则窗口缩小时会被可伸缩的列表挤没
        open_btn = dark_button(friend_tab, text="打开聊天", command=self.open_chat_selected, font=self.ui_font)
        open_btn.pack(side="bottom", fill="x", pady=(6, 0))

        self.friend_list = tk.Listbox(friend_tab, height=1, font=self.ui_font)
        self.friend_list.pack(fill="both", expand=True)
        self.friend_list.bind("<Double-Button-1>", lambda event: self.open_chat_selected())
        style_listbox(self.friend_list)

        group_open_btn = dark_button(group_tab, text="进入群聊", command=self.open_group_chat_selected, font=self.ui_font)
        group_open_btn.pack(side="bottom", fill="x", pady=(6, 0))

        self.group_list = tk.Listbox(group_tab, height=1, font=self.ui_font)
        self.group_list.pack(fill="both", expand=True)
        self.group_list.bind("<Double-Button-1>", lambda event: self.open_group_chat_selected())
        style_listbox(self.group_list)

        for button in (add_btn, del_btn, group_btn, open_btn, group_open_btn, self.theme_btn):
            self.scaler.track(button, padx=10, pady=4)

        self._refresh_friend_list()
        self._refresh_group_list()

        # 先按时间合并未读消息与最近20条近期历史，避免重复并保证展示顺序正确
        undelivered_ids = {r["id"] for r in history}
        combined = {r["id"]: r for r in recent}
        combined.update({r["id"]: r for r in history})
        for record in sorted(combined.values(), key=lambda r: r["timestamp"]):
            if record["id"] in undelivered_ids:
                self._route_incoming(record)
            else:
                self._preload_recent(record)

        # 群聊历史同理：先合并未读与最近记录，按时间顺序回放
        group_undelivered_ids = {r["id"] for r in (group_history or [])}
        group_combined = {r["id"]: r for r in (group_recent or [])}
        group_combined.update({r["id"]: r for r in (group_history or [])})
        for record in sorted(group_combined.values(), key=lambda r: r["timestamp"]):
            if record["id"] in group_undelivered_ids:
                self._route_group_incoming(record, notify=False)
            else:
                self._preload_group_recent(record)
        self._refresh_group_list()

        self._start_recv_thread()
        self.root.after(100, self._poll_queue)
        self.root.after(HEARTBEAT_INTERVAL_MS, self._heartbeat)
        self.root.deiconify()
        if broadcasts:
            self.root.after(300, self._show_broadcasts, list(broadcasts))
        if friend_requests:
            self.root.after(300, self._show_friend_requests, list(friend_requests))

    def _show_broadcasts(self, broadcasts: list) -> None:
        """依次弹窗展示未阅读的管理员公告，确认后自动消失。"""
        for record in sorted(broadcasts, key=lambda r: r["timestamp"]):
            messagebox.showinfo("系统公告", record.get("text", ""))

    def _show_friend_requests(self, requests: list) -> None:
        """依次弹窗处理登录前累积的待处理好友申请。"""
        for record in sorted(requests, key=lambda r: r["created_at"]):
            self._prompt_friend_request(record.get("from", ""))

    def _prompt_friend_request(self, requester: str) -> None:
        if not requester:
            return
        dialog = self.request_dialogs.get(requester)
        if dialog is not None:
            dialog.show()
            return
        self.request_dialogs[requester] = FriendRequestDialog(self.root, self, requester)

    def respond_friend_request(self, requester: str, accept: bool) -> bool:
        try:
            send_msg(self.sock, {"type": "friend_response", "from": requester, "accept": accept})
        except OSError:
            messagebox.showerror("错误", "回复好友申请失败：与服务器的连接已断开")
            return False
        if accept:
            self.add_friend(requester)
        return True

    def _on_theme_change(self) -> None:
        self.theme_btn.configure(text=theme_icon())

    def _refresh_friend_list(self):
        self.friend_list.delete(0, "end")
        for friend in self.friends:
            count = self.unread.get(friend, 0)
            label = friend if count == 0 else f"{friend} ({count}条未读)"
            self.friend_list.insert("end", label)
        self._update_unread_notices()

    def _touch_unread(self, source: tuple) -> None:
        if source in self._unread_order:
            self._unread_order.remove(source)
        self._unread_order.append(source)

    def _unread_count(self, source: tuple) -> int:
        kind, key = source
        if kind == "friend":
            return self.unread.get(key, 0) if key in self.friends else 0
        return self.group_unread.get(key, 0) if key in self.groups else 0

    def _unread_sources(self) -> list:
        self._unread_order = [s for s in self._unread_order if self._unread_count(s) > 0]
        return self._unread_order

    def _source_name(self, source: tuple) -> str:
        kind, key = source
        return key if kind == "friend" else self.groups.get(key, {}).get("name", "群聊")

    def _update_unread_notices(self) -> None:
        """在已打开的聊天窗口顶部提示其他会话的未读消息。"""
        sources = self._unread_sources()
        if not sources:
            text = ""
        else:
            total = sum(self._unread_count(s) for s in sources)
            latest = self._source_name(sources[-1])
            if len(sources) == 1:
                text = f"{latest} 发来{total}条新消息 >"
            else:
                text = f"{latest} 等{len(sources)}个会话 {total}条新消息 >"
        for chat in list(self.open_chats.values()) + list(self.open_group_chats.values()):
            chat.set_unread_notice(text)

    def jump_to_latest_unread(self, from_chat) -> None:
        sources = self._unread_sources()
        if not sources:
            return
        kind, key = sources[-1]
        position = window_center(from_chat.root)
        from_chat.dispose()
        if kind == "friend":
            self.open_chat_with(key, position)
        else:
            self.open_group_chat_with(key, position)

    def open_add_friend_dialog(self):
        if self.active_search_dialog is None:
            self.active_search_dialog = AddFriendDialog(self.root, self)

    def add_friend(self, name: str):
        """仅在服务器确认对方已同意好友申请后调用，不再支持直接添加。"""
        if name and name != self.username and name not in self.friends:
            self.friends.append(name)
            save_friends(self.username, self.friends)
            self._refresh_friend_list()

    def send_friend_request(self, target: str) -> None:
        try:
            send_msg(self.sock, {"type": "friend_request", "to": target})
        except OSError:
            messagebox.showerror("错误", "发送好友申请失败：与服务器的连接已断开")

    def remove_selected_friend(self):
        selection = self.friend_list.curselection()
        if not selection:
            return
        friend = self.friends[selection[0]]
        if not messagebox.askyesno("删除好友", f"确认删除好友 {friend} 吗？"):
            return
        try:
            send_msg(self.sock, {"type": "unfriend", "target": friend})
        except OSError:
            pass
        self.friends.remove(friend)
        self.unread.pop(friend, None)
        save_friends(self.username, self.friends)
        self._refresh_friend_list()
        chat = self.open_chats.pop(friend, None)
        if chat:
            chat.root.destroy()

    def open_chat_selected(self):
        selection = self.friend_list.curselection()
        if not selection:
            return
        self.open_chat_with(self.friends[selection[0]])

    def _place_chat(self, chat_win, position=None) -> None:
        """聊天窗口与好友界面（或指定中心点）居中对齐打开。"""
        if position is None and self.root.winfo_viewable():
            position = window_center(self.root)
        restore_window("chat", chat_win, position)

    def open_chat_with(self, friend: str, position=None):
        remember_window("friends", self.root)
        self.unread[friend] = 0
        self._refresh_friend_list()
        if friend in self.open_chats:
            self._place_chat(self.open_chats[friend].root, position)
            self.open_chats[friend].root.deiconify()
            self.open_chats[friend].root.lift()
            self.open_chats[friend].root.focus_force()
        else:
            chat_win = tk.Toplevel(self.root)
            chat = ChatWindow(chat_win, self, friend)
            self.open_chats[friend] = chat
            chat.refresh()
            self._place_chat(chat_win, position)
            chat_win.deiconify()  # 先填完内容再显示，避免闪一下空聊天框
        self._update_unread_notices()
        self._apply_connection_status()
        self.root.withdraw()

    def show_friends(self, position=None) -> None:
        """从聊天窗口返回时重新显示好友列表窗口，position 为聊天窗口的中心点。"""
        if position is not None:
            restore_window("friends", self.root, position)
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def request_search(self, query: str, dialog):
        self.active_search_dialog = dialog
        try:
            send_msg(self.sock, {"type": "search_users", "query": query})
        except OSError:
            messagebox.showerror("错误", "搜索失败：与服务器的连接已断开")

    def send_typing(self, friend: str, active: bool) -> None:
        """输入状态丢了也无所谓，发失败不弹提示。"""
        try:
            send_msg(self.sock, {"type": "typing", "to": friend, "state": bool(active)})
        except OSError:
            pass

    def send_payload(self, friend: str, payload: dict, encrypted: bool = True):
        """发送文字/表情/文件负载：默认整体 JSON 序列化后加密；@AI 消息按 encrypted=False 明文发送。

        断线期间也先记入"发送中"，重连成功后自动补发，不会丢。
        """
        msg_id = str(uuid.uuid4())
        plaintext_json = json.dumps(payload, ensure_ascii=False)
        ciphertext = encrypt_text(plaintext_json, CHAT_PASSPHRASE) if encrypted else plaintext_json
        wire = {
            "type": "msg", "msg_id": msg_id, "to": friend,
            "ciphertext": ciphertext, "encrypted": encrypted,
        }
        self.pending_ids[msg_id] = friend
        self.pending_wire[msg_id] = wire
        entry = {"direction": "out", "payload": payload, "status": "发送中...", "msg_id": msg_id}
        self.chat_logs.setdefault(friend, []).append(entry)
        if self.connected:
            try:
                send_msg(self.sock, wire)
            except OSError:
                self._drop_connection()
        return entry

    def _decode_payload(self, record: dict) -> dict:
        ciphertext = record.get("ciphertext", "")
        if not record.get("encrypted", True):
            try:
                payload = json.loads(ciphertext)
                if isinstance(payload, dict) and "kind" in payload:
                    return payload
            except json.JSONDecodeError:
                pass
            return {"kind": "text", "text": ciphertext}
        try:
            plaintext = decrypt_text(ciphertext, CHAT_PASSPHRASE)
        except ValueError:
            return {"kind": "text", "text": "<解密失败：密钥不一致或消息被篡改>"}
        try:
            payload = json.loads(plaintext)
            if isinstance(payload, dict) and "kind" in payload:
                return payload
        except json.JSONDecodeError:
            pass
        return {"kind": "text", "text": plaintext}

    def _start_recv_thread(self) -> None:
        self._sock_gen += 1
        self._last_recv = time.monotonic()
        threading.Thread(target=self._recv_loop, args=(self.sock, self._sock_gen), daemon=True).start()

    def _recv_loop(self, sock, gen: int):
        while True:
            try:
                data = recv_msg(sock)
            except OSError:
                data = None
            if data is None:
                self.incoming_queue.put({"type": "disconnected", "_gen": gen})
                return
            self._last_recv = time.monotonic()
            data["_gen"] = gen
            self.incoming_queue.put(data)

    def _heartbeat(self) -> None:
        if self._closing:
            return
        if self.connected:
            if self._server_pongs and time.monotonic() - self._last_recv > HEARTBEAT_TIMEOUT_SEC:
                self._drop_connection()
            else:
                try:
                    send_msg(self.sock, {"type": "ping"})
                except OSError:
                    self._drop_connection()
        self.root.after(HEARTBEAT_INTERVAL_MS, self._heartbeat)

    def _drop_connection(self) -> None:
        """主动废弃当前连接（发送失败或心跳超时），进入自动重连。"""
        if not self.connected:
            return
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self._on_disconnected()

    def _on_disconnected(self) -> None:
        if not self.connected or self._closing:
            return
        self.connected = False
        self._reconnect_attempt = 0
        self._apply_connection_status()
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self.login_info is None:
            messagebox.showwarning("提示", "与服务器的连接已断开")
            return
        delay = RECONNECT_DELAYS_SEC[min(self._reconnect_attempt, len(RECONNECT_DELAYS_SEC) - 1)]
        self._reconnect_attempt += 1
        self.root.after(delay * 1000, self._start_reconnect)

    def _start_reconnect(self) -> None:
        if self.connected or self._closing:
            return
        host, port, password = self.login_info
        threading.Thread(
            target=self._reconnect_worker, args=(host, port, password), daemon=True
        ).start()

    def _reconnect_worker(self, host, port, password) -> None:
        try:
            sock, response = connect_and_login(host, port, self.username, password)
        except LoginError as exc:
            self.incoming_queue.put({"type": "_reconnect_failed", "fatal": True, "reason": str(exc)})
            return
        except OSError as exc:
            self.incoming_queue.put({"type": "_reconnect_failed", "fatal": False, "reason": str(exc)})
            return
        self.incoming_queue.put({"type": "_reconnected", "sock": sock, "response": response})

    def _on_reconnected(self, sock, response: dict) -> None:
        if self._closing:
            sock.close()
            return
        self.sock = sock
        self.connected = True
        self._start_recv_thread()
        self._apply_connection_status()

        self.friends = list(response.get("friends", self.friends))
        save_friends(self.username, self.friends)
        self.groups = {g["id"]: g for g in response.get("groups", [])}
        # 断线期间别人发来的消息，服务器会作为未送达记录在登录响应里补发
        for record in sorted(response.get("history", []), key=lambda r: r["timestamp"]):
            self._route_incoming(record)
        for record in sorted(response.get("group_history", []), key=lambda r: r["timestamp"]):
            self._route_group_incoming(record)
        self._refresh_friend_list()
        self._refresh_group_list()
        for record in response.get("friend_requests", []):
            if record.get("from") not in self.request_dialogs:
                self._prompt_friend_request(record.get("from", ""))

        for wire in list(self.pending_wire.values()):
            try:
                send_msg(self.sock, wire)
            except OSError:
                self._drop_connection()
                return

    def _apply_connection_status(self) -> None:
        suffix = "" if self.connected else DISCONNECTED_SUFFIX
        self.root.title(self.base_title + suffix)
        for chat in list(self.open_chats.values()) + list(self.open_group_chats.values()):
            chat.root.title(chat.base_title + suffix)

    def _poll_queue(self):
        try:
            while True:
                data = self.incoming_queue.get_nowait()
                self._handle_event(data)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle_event(self, data: dict) -> None:
        msg_type = data.get("type")
        if msg_type == "incoming":
            self._route_incoming(data)
        elif msg_type == "typing":
            chat = self.open_chats.get(data.get("from"))
            if chat is not None:
                chat.show_typing(bool(data.get("state")))
        elif msg_type == "msg_ack":
            self._route_ack(data)
        elif msg_type == "group_msg_ack":
            self._route_group_ack(data)
        elif msg_type == "search_result":
            if self.active_search_dialog is not None:
                self.active_search_dialog.show_results(data.get("users", []))
        elif msg_type == "group_created":
            self._on_group_created(data)
        elif msg_type == "group_update":
            self._on_group_update(data)
        elif msg_type == "group_incoming":
            self._route_group_incoming(data)
        elif msg_type == "broadcast":
            messagebox.showinfo("系统公告", data.get("text", ""))
        elif msg_type == "friend_request":
            self._prompt_friend_request(data.get("from", ""))
        elif msg_type == "friend_request_sent":
            messagebox.showinfo("提示", f"已向 {data.get('target', '')} 发送好友申请，等待对方同意")
        elif msg_type == "friend_request_fail":
            messagebox.showerror("好友申请失败", data.get("reason", "未知错误"))
        elif msg_type == "friend_added":
            self.add_friend(data.get("friend", ""))
            messagebox.showinfo("提示", f"{data.get('friend', '')} 已同意你的好友申请")
        elif msg_type == "friend_request_rejected":
            messagebox.showinfo("提示", f"{data.get('friend', '')} 拒绝了你的好友申请")
        elif msg_type == "unfriended":
            friend = data.get("by", "")
            if friend in self.friends:
                self.friends.remove(friend)
                save_friends(self.username, self.friends)
                self._refresh_friend_list()
            messagebox.showinfo("提示", f"{friend} 已将你从好友中移除")
        elif msg_type == "disconnected":
            if data.get("_gen") == self._sock_gen:
                self._on_disconnected()
        elif msg_type == "pong":
            self._server_pongs = True
        elif msg_type == "kicked":
            if data.get("_gen") != self._sock_gen:
                return  # 自己重连时顶掉的是本客户端的旧连接，不算被别人顶号
            self._closing = True  # 账号在别处登录，不能自动重连，否则两边会互相顶号
            self.connected = False
            self.root.title(self.base_title + "（已离线）")
            messagebox.showwarning("提示", "你的账号已在其他地方登录，本客户端已离线")
        elif msg_type == "_reconnected":
            self._on_reconnected(data["sock"], data["response"])
        elif msg_type == "_reconnect_failed":
            if data.get("fatal"):
                messagebox.showerror("重连失败", f"自动重连被服务器拒绝：{data.get('reason', '')}\n请重新启动客户端登录")
            else:
                self._schedule_reconnect()

    def _route_incoming(self, record: dict) -> None:
        is_ai = record["from"] == AI_SENDER
        friend = record.get("peer", record["from"]) if is_ai else record["from"]

        payload = self._decode_payload(record)
        entry = {"direction": "ai" if is_ai else "in", "payload": payload}
        self.chat_logs.setdefault(friend, []).append(entry)

        chat = self.open_chats.get(friend)
        if chat is not None:
            chat.show_typing(False)
            chat.refresh()
            if not chat.is_focused:
                flash_window(chat.root)
        else:
            self.unread[friend] = self.unread.get(friend, 0) + 1
            self._touch_unread(("friend", friend))
            flash_window(self.root)
            for other in list(self.open_chats.values()) + list(self.open_group_chats.values()):
                if not other.is_focused:
                    flash_window(other.root)
        self._refresh_friend_list()

    def _preload_recent(self, record: dict) -> None:
        """登录时静默加载已读的历史消息，仅填充聊天记录，不计未读也不闪烁。"""
        if record["from"] == AI_SENDER:
            friend, direction = record.get("peer", ""), "ai"
        elif record["from"] == self.username:
            friend, direction = record["to"], "out"
        else:
            friend, direction = record["from"], "in"
        if not friend or friend == self.username:
            return

        payload = self._decode_payload(record)
        entry = {"direction": direction, "payload": payload}
        if direction == "out":
            entry["msg_id"] = record["id"]
            entry["status"] = "已送达" if record.get("delivered") else "已保存(对方离线)"
        self.chat_logs.setdefault(friend, []).append(entry)

    def _route_ack(self, data: dict) -> None:
        msg_id = data.get("msg_id")
        self.pending_wire.pop(msg_id, None)
        friend = self.pending_ids.pop(msg_id, None)
        if friend is None:
            return
        if data.get("status") == "not_friend":
            status_text = "发送失败(对方不是好友)"
        elif data.get("status") == "delivered":
            status_text = "已送达"
        else:
            status_text = "已保存(对方离线)"
        for entry in self.chat_logs.get(friend, []):
            if entry.get("msg_id") == msg_id:
                entry["status"] = status_text
                break
        if friend in self.open_chats:
            self.open_chats[friend].refresh()

    def _preview_text(self, payload: dict) -> str:
        if payload.get("kind") == "file":
            filename = payload.get("filename", "文件")
            return f"[图片] {filename}" if payload.get("mime") == "image" else f"[文件] {filename}"
        text = payload.get("text", "")
        return text if len(text) <= 24 else text[:24] + "..."

    def _refresh_group_list(self) -> None:
        self.group_list.delete(0, "end")
        self._group_order = list(self.groups.keys())
        for group_id in self._group_order:
            group = self.groups[group_id]
            count = self.group_unread.get(group_id, 0)
            label = group["name"] if count == 0 else f'{group["name"]} ({count}条未读)'
            self.group_list.insert("end", label)
        self._update_unread_notices()

    def open_create_group_dialog(self) -> None:
        if not self.friends:
            messagebox.showinfo("提示", "请先添加好友，才能拉人建群")
            return
        CreateGroupDialog(self.root, self)

    def open_group_chat_selected(self) -> None:
        selection = self.group_list.curselection()
        if not selection:
            return
        self.open_group_chat_with(self._group_order[selection[0]])

    def open_group_chat_with(self, group_id: str, position=None) -> None:
        remember_window("friends", self.root)
        self.group_unread[group_id] = 0
        self._refresh_group_list()
        if group_id in self.open_group_chats:
            self._place_chat(self.open_group_chats[group_id].root, position)
            self.open_group_chats[group_id].root.deiconify()
            self.open_group_chats[group_id].root.lift()
            self.open_group_chats[group_id].root.focus_force()
        else:
            chat_win = tk.Toplevel(self.root)
            chat = GroupChatWindow(chat_win, self, group_id)
            self.open_group_chats[group_id] = chat
            chat.refresh()
            self._place_chat(chat_win, position)
            chat_win.deiconify()
        self._update_unread_notices()
        self._apply_connection_status()
        self.root.withdraw()

    def request_create_group(self, name: str, members: list) -> None:
        try:
            send_msg(self.sock, {"type": "create_group", "name": name, "members": members})
        except OSError:
            messagebox.showerror("错误", "创建群聊失败：与服务器的连接已断开")

    def request_invite_group(self, group_id: str, member: str) -> None:
        try:
            send_msg(self.sock, {"type": "invite_group", "group_id": group_id, "member": member})
        except OSError:
            messagebox.showerror("错误", "邀请失败：与服务器的连接已断开")

    def request_leave_group(self, group_id: str) -> None:
        try:
            send_msg(self.sock, {"type": "leave_group", "group_id": group_id})
        except OSError:
            pass
        chat = self.open_group_chats.pop(group_id, None)
        position = None
        if chat:
            position = window_center(chat.root)
            chat.root.destroy()
        self.groups.pop(group_id, None)
        self.group_unread.pop(group_id, None)
        self._refresh_group_list()
        self.show_friends(position)

    def send_group_payload(self, group_id: str, payload: dict):
        """群聊消息复用私聊同一套加密口令，服务器只负责按成员名单转发不解密。

        和私聊一样先记为"发送中"，等服务器 group_msg_ack 确认；断线期间的消息重连后自动补发。
        """
        msg_id = str(uuid.uuid4())
        plaintext_json = json.dumps(payload, ensure_ascii=False)
        ciphertext = encrypt_text(plaintext_json, CHAT_PASSPHRASE)
        wire = {
            "type": "group_msg", "msg_id": msg_id, "group_id": group_id,
            "ciphertext": ciphertext, "encrypted": True,
        }
        self.pending_group_ids[msg_id] = group_id
        self.pending_wire[msg_id] = wire
        entry = {"direction": "out", "from": self.username, "payload": payload, "status": "发送中...", "msg_id": msg_id}
        self.group_chat_logs.setdefault(group_id, []).append(entry)
        if self.connected:
            try:
                send_msg(self.sock, wire)
            except OSError:
                self._drop_connection()
        return entry

    def _route_group_ack(self, data: dict) -> None:
        msg_id = data.get("msg_id")
        self.pending_wire.pop(msg_id, None)
        group_id = self.pending_group_ids.pop(msg_id, None)
        if group_id is None:
            return
        status_text = "已发送" if data.get("status") == "sent" else "发送失败(你已不在该群)"
        for entry in self.group_chat_logs.get(group_id, []):
            if entry.get("msg_id") == msg_id:
                entry["status"] = status_text
                break
        if group_id in self.open_group_chats:
            self.open_group_chats[group_id].refresh()

    def _on_group_created(self, data: dict) -> None:
        group = {k: v for k, v in data.items() if k != "type"}
        self.groups[group["id"]] = group
        self.group_chat_logs.setdefault(group["id"], [])
        self._refresh_group_list()

    def _on_group_update(self, data: dict) -> None:
        group = {k: v for k, v in data.items() if k != "type"}
        self.groups[group["id"]] = group
        self._refresh_group_list()

    def _route_group_incoming(self, record: dict, notify: bool = True) -> None:
        group_id = record.get("group_id", "")
        if group_id not in self.groups:
            return
        payload = self._decode_payload(record)
        entry = {"direction": "in", "from": record.get("from", ""), "payload": payload}
        self.group_chat_logs.setdefault(group_id, []).append(entry)

        chat = self.open_group_chats.get(group_id)
        if chat is not None:
            chat.refresh()
            if notify and not chat.is_focused:
                flash_window(chat.root)
        else:
            if notify:
                self.group_unread[group_id] = self.group_unread.get(group_id, 0) + 1
                self._touch_unread(("group", group_id))
                flash_window(self.root)
                for other in list(self.open_chats.values()) + list(self.open_group_chats.values()):
                    if not other.is_focused:
                        flash_window(other.root)
        self._refresh_group_list()

    def _preload_group_recent(self, record: dict) -> None:
        """登录时静默加载已读的群聊历史，仅填充记录，不计未读也不弹提醒。"""
        group_id = record.get("group_id", "")
        if group_id not in self.groups:
            return
        direction = "out" if record.get("from") == self.username else "in"
        entry = {"direction": direction, "from": record.get("from", ""), "payload": self._decode_payload(record)}
        self.group_chat_logs.setdefault(group_id, []).append(entry)

    def on_close(self):
        self._closing = True
        remember_window("friends", self.root)
        try:
            self.sock.close()
        except OSError:
            pass
        self.root.destroy()


class AddFriendDialog:
    """按完整用户名精确搜索账号，找到后可发送好友申请（需对方同意才算好友）。"""

    def __init__(self, parent, friends_window: FriendsWindow):
        self.friends_window = friends_window
        self.found_user = None

        self.win = tk.Toplevel(parent)
        hide_until_ready(self.win)
        self.win.title("添加好友")
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        apply_theme(self.win)
        register_themed_window(self.win)

        frame = ttk.Frame(self.win, padding=8)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="请输入完整用户名（需与账号名完全一致）:").pack(fill="x")
        search_frame = ttk.Frame(frame)
        search_frame.pack(fill="x", pady=(4, 0))
        self.query_entry = dark_entry(search_frame)
        self.query_entry.pack(side="left", fill="x", expand=True)
        self.query_entry.bind("<Return>", lambda event: self.search())
        dark_button(search_frame, text="搜索", command=self.search).pack(side="left", padx=4)

        self.status_label = ttk.Label(frame, text="", wraplength=220)
        self.status_label.pack(fill="x", pady=8)

        self.request_btn = dark_button(frame, text="发送好友申请", command=self.send_request, state="disabled")
        self.request_btn.pack(fill="x")

        center_window(self.win, 260, 160)
        self.win.deiconify()

    def search(self):
        self.found_user = None
        self.request_btn.configure(state="disabled")
        query = self.query_entry.get().strip()
        if not query:
            self.status_label.configure(text="请输入用户名")
            return
        self.friends_window.request_search(query, self)

    def show_results(self, users: list) -> None:
        if users:
            self.found_user = users[0]
            self.status_label.configure(text=f"找到用户：{self.found_user}")
            self.request_btn.configure(state="normal")
        else:
            self.found_user = None
            self.status_label.configure(text="未找到该用户（需完全匹配用户名）")
            self.request_btn.configure(state="disabled")

    def send_request(self):
        if not self.found_user:
            return
        self.friends_window.send_friend_request(self.found_user)
        self.close()

    def close(self):
        if self.friends_window.active_search_dialog is self:
            self.friends_window.active_search_dialog = None
        self.win.destroy()


class FriendRequestDialog:
    """非模态的好友申请提醒。客户端在后台时系统不允许抢焦点，所以用置顶+任务栏闪烁保证对方能看到；
    直接关闭视为稍后处理，申请仍保留在服务器，下次登录会再次提醒。"""

    def __init__(self, parent, friends_window: FriendsWindow, requester: str):
        self.friends_window = friends_window
        self.requester = requester

        self.win = tk.Toplevel(parent)
        hide_until_ready(self.win)
        self.win.title("好友申请")
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        apply_theme(self.win)
        register_themed_window(self.win)

        frame = ttk.Frame(self.win, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"{requester} 请求添加你为好友，是否同意？", wraplength=240).pack(fill="x", pady=(0, 12))
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x")
        dark_button(btn_frame, text="同意", command=lambda: self.respond(True)).pack(side="left", fill="x", expand=True, padx=(0, 4))
        dark_button(btn_frame, text="拒绝", command=lambda: self.respond(False)).pack(side="left", fill="x", expand=True)

        center_window(self.win, 280, 120)
        self.win.attributes("-topmost", True)
        self.win.deiconify()
        self.show()

    def show(self) -> None:
        self.win.deiconify()
        self.win.lift()
        self.win.after(100, lambda: flash_window(self.win))

    def respond(self, accept: bool) -> None:
        if self.friends_window.respond_friend_request(self.requester, accept):
            self.close()

    def close(self) -> None:
        self.friends_window.request_dialogs.pop(self.requester, None)
        self.win.destroy()


class CreateGroupDialog:
    """从好友列表多选成员创建群聊。"""

    def __init__(self, parent, friends_window: FriendsWindow):
        self.friends_window = friends_window

        self.win = tk.Toplevel(parent)
        hide_until_ready(self.win)
        self.win.title("创建群聊")
        apply_theme(self.win)
        register_themed_window(self.win)

        frame = ttk.Frame(self.win, padding=8)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="群名称(留空自动生成):").pack(anchor="w")
        self.name_entry = dark_entry(frame)
        self.name_entry.pack(fill="x", pady=(2, 8))

        ttk.Label(frame, text="选择好友:").pack(anchor="w")
        member_frame = ttk.Frame(frame)
        member_frame.pack(fill="both", expand=True, pady=(2, 8))
        self.member_vars = {}
        for friend in friends_window.friends:
            var = tk.IntVar(value=0)
            ttk.Checkbutton(member_frame, text=friend, variable=var).pack(anchor="w")
            self.member_vars[friend] = var

        dark_button(frame, text="创建", command=self.create).pack(fill="x")

        center_window(self.win, 240, 320)
        self.win.deiconify()

    def create(self):
        members = [name for name, var in self.member_vars.items() if var.get()]
        if not members:
            messagebox.showwarning("提示", "请至少选择一位好友")
            return
        self.friends_window.request_create_group(self.name_entry.get().strip(), members)
        self.win.destroy()


class GroupMembersDialog:
    """展示群成员列表，支持从好友里挑选邀请入群，以及退出该群聊。"""

    def __init__(self, parent, friends_window: FriendsWindow, group_id: str):
        self.friends_window = friends_window
        self.group_id = group_id

        self.win = tk.Toplevel(parent)
        hide_until_ready(self.win)
        self.win.title("群成员")
        apply_theme(self.win)
        register_themed_window(self.win)

        frame = ttk.Frame(self.win, padding=8)
        frame.pack(fill="both", expand=True)

        group = friends_window.groups.get(group_id, {})
        owner = group.get("owner", "")
        ttk.Label(frame, text="成员列表:").pack(anchor="w")
        member_list = tk.Listbox(frame, height=8)
        member_list.pack(fill="both", expand=True, pady=(2, 6))
        style_listbox(member_list)
        for member in group.get("members", []):
            member_list.insert("end", f"{member} (群主)" if member == owner else member)

        candidates = [f for f in friends_window.friends if f not in group.get("members", [])]
        invite_frame = ttk.Frame(frame)
        invite_frame.pack(fill="x", pady=(0, 6))
        self.invite_var = tk.StringVar(value=candidates[0] if candidates else "")
        self.invite_combo = ttk.Combobox(
            invite_frame, textvariable=self.invite_var, values=candidates, state="readonly",
        )
        self.invite_combo.pack(side="left", fill="x", expand=True)
        dark_button(invite_frame, text="邀请入群", command=self.invite).pack(side="left", padx=4)

        dark_button(frame, text="退出群聊", command=self.leave).pack(fill="x", pady=(4, 0))

        center_window(self.win, 260, 320)
        self.win.deiconify()

    def invite(self):
        member = self.invite_var.get()
        if not member:
            return
        self.friends_window.request_invite_group(self.group_id, member)
        self.win.destroy()

    def leave(self):
        if not messagebox.askyesno("退出群聊", "确认退出该群聊吗？"):
            return
        self.friends_window.request_leave_group(self.group_id)
        self.win.destroy()


class EmojiPicker:
    """表情选择弹窗：显示服务器下发的彩色表情图片，点击后插入并自动关闭。"""

    def __init__(self, parent, emoji_images: dict, on_pick):
        self.win = tk.Toplevel(parent)
        hide_until_ready(self.win)
        self.win.title("选择表情")
        self.win.resizable(False, False)
        self.win.transient(parent)
        apply_theme(self.win)

        frame = ttk.Frame(self.win, padding=6)
        frame.pack(fill="both", expand=True)

        if not emoji_images:
            ttk.Label(frame, text="表情包尚未下载，请重新登录后再试").pack(padx=10, pady=10)
            place_beside(self.win, parent)
            self.win.deiconify()
            return

        columns = 8
        for index, (emoji, photo) in enumerate(emoji_images.items()):
            btn = tk.Button(
                frame, image=photo, bg=THEME.btn_bg, activebackground=THEME.accent,
                relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                command=lambda e=emoji: self._pick(on_pick, e),
            )
            btn.image = photo  # 防止图片被垃圾回收
            btn.grid(row=index // columns, column=index % columns, padx=2, pady=2)

        place_beside(self.win, parent)
        self.win.deiconify()

    def _pick(self, on_pick, emoji: str) -> None:
        on_pick(emoji)
        self.win.destroy()


class ImageViewer:
    """查看原图弹窗：按屏幕尺寸缩放展示，另外提供另存为按钮。"""

    def __init__(self, parent, filename: str, image: Image.Image, raw_bytes: bytes):
        self.filename = filename
        self.raw_bytes = raw_bytes

        self.win = tk.Toplevel(parent)
        hide_until_ready(self.win)
        self.win.title(filename)
        apply_theme(self.win)

        max_w = self.win.winfo_screenwidth() - 160
        max_h = self.win.winfo_screenheight() - 200
        display_img = image
        if image.width > max_w or image.height > max_h:
            display_img = image.copy()
            display_img.thumbnail((max_w, max_h))
        self.photo = ImageTk.PhotoImage(display_img)

        tk.Label(self.win, image=self.photo, bg=THEME.bg).pack(padx=8, pady=8)

        btn_frame = ttk.Frame(self.win)
        btn_frame.pack(fill="x", padx=8, pady=(0, 8))
        dark_button(btn_frame, text="关闭", command=self.win.destroy).pack(side="left")
        dark_button(btn_frame, text="另存为", command=self.save_as).pack(side="right")

        center_window(self.win)
        self.win.deiconify()

    def save_as(self):
        path = filedialog.asksaveasfilename(initialfile=self.filename)
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(self.raw_bytes)
        except OSError as exc:
            messagebox.showerror("错误", f"保存失败: {exc}")


class _UnreadNoticeMixin:
    """聊天窗口顶部的"其他会话新消息"提示，点击跳转到最近来消息的会话。"""

    def _build_unread_notice(self, top_bar) -> None:
        self.notice_font = self.scaler.font(size=9)
        self.notice_font.configure(underline=True)
        self.notice_label = ttk.Label(
            top_bar, text="", font=self.notice_font, foreground=THEME.hint, cursor="hand2",
        )
        self.notice_label.pack(side="right", padx=6)
        self.notice_label.bind("<Button-1>", lambda event: self.friends_window.jump_to_latest_unread(self))

    def set_unread_notice(self, text: str) -> None:
        self.notice_label.configure(text=text)


class ChatWindow(_UnreadNoticeMixin):
    """单个好友的聊天窗口：文字/表情/文件/图片，支持 Ctrl+V 直接粘贴剪贴板图片发送。"""

    def __init__(self, root, friends_window: FriendsWindow, friend: str):
        self.root = root
        self.friends_window = friends_window
        self.friend = friend
        self._image_refs = []  # 防止内嵌图片被垃圾回收
        self._emoji_picker = None
        self._typing_sent_at = 0.0
        self._typing_stop_job = None
        self._typing_hide_job = None
        self.is_focused = True
        root.bind("<FocusIn>", self._on_focus_in, add="+")
        root.bind("<FocusOut>", self._on_focus_out, add="+")

        hide_until_ready(root)
        self.base_title = f"与 {friend} 聊天"
        root.title(self.base_title)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        apply_theme(root)
        register_themed_window(root, self._on_theme_change)
        self.scaler = WindowScaler(root, 460, 420)
        self.ui_font = self.scaler.font(size=10)
        self.title_font = self.scaler.font(size=10, weight="bold")
        self.emoji_font = self.scaler.font(size=11, family="Segoe UI Emoji")

        main = ttk.Frame(root, padding=8)
        main.pack(fill="both", expand=True)
        self.scaler.track(main, padding=8)

        top_bar = ttk.Frame(main)
        top_bar.pack(fill="x", pady=(0, 6))
        back_btn = dark_button(top_bar, text="< 返回", command=self.on_close, font=self.ui_font)
        back_btn.pack(side="left")
        ttk.Label(top_bar, text=friend, font=self.title_font).pack(side="left", padx=6)
        self.typing_label = ttk.Label(top_bar, text="", font=self.ui_font, foreground=THEME.hint)
        self.typing_label.pack(side="left")
        self.theme_btn = dark_button(top_bar, text=theme_icon(), command=toggle_theme, font=self.emoji_font)
        self.theme_btn.pack(side="right")
        self._build_unread_notice(top_bar)

        # 输入栏先占位，否则窗口缩小时会被可伸缩的聊天区挤没
        input_frame = ttk.Frame(main)
        input_frame.pack(side="bottom", fill="x")
        self.msg_entry = dark_entry(input_frame, font=self.ui_font)
        self.msg_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.msg_entry.bind("<Return>", lambda event: self.send())
        self.msg_entry.bind("<Control-v>", self.on_paste)
        self.msg_entry.bind("<KeyRelease>", self._on_typing)
        emoji_btn = dark_button(
            input_frame, text="\U0001F60A", font=self.emoji_font, command=self.open_emoji_picker,
        )
        emoji_btn.pack(side="left", padx=(0, 4))
        file_btn = dark_button(input_frame, text="\U0001F4CE", command=self.send_file_dialog, font=self.ui_font)
        file_btn.pack(side="left", padx=(0, 4))
        send_btn = dark_button(input_frame, text="发送", command=self.send, font=self.ui_font)
        send_btn.pack(side="left")

        self.chat_box = scrolledtext.ScrolledText(
            main, wrap="word", state="disabled", height=1, font=self.ui_font,
        )
        self.chat_box.pack(fill="both", expand=True, pady=(0, 6))
        self.chat_box.tag_configure("left", justify="left", foreground=THEME.fg)
        self.chat_box.tag_configure("right", justify="right", foreground=THEME.fg)
        self.chat_box.tag_configure("ai", justify="left", foreground=THEME.ai)
        self.scrollbar = style_text(self.chat_box)

        for button in (back_btn, self.theme_btn, emoji_btn, file_btn, send_btn):
            self.scaler.track(button, padx=10, pady=4)
        self.scaler.on_scale(self._on_scale)
        self.msg_entry.focus_set()

    def _on_scale(self, scale: float) -> None:
        if self.scrollbar is not None:
            self.scrollbar.configure(width=max(8, round(10 * scale)))

    def _on_focus_in(self, event=None) -> None:
        self.is_focused = True

    def _on_focus_out(self, event=None) -> None:
        self.is_focused = False

    def _on_theme_change(self) -> None:
        self.theme_btn.configure(text=theme_icon())
        self.typing_label.configure(foreground=THEME.hint)
        self.notice_label.configure(foreground=THEME.hint)
        self.chat_box.frame.configure(bg=THEME.panel)
        self.chat_box.tag_configure("left", foreground=THEME.fg)
        self.chat_box.tag_configure("right", foreground=THEME.fg)
        self.chat_box.tag_configure("ai", foreground=THEME.ai)
        self.refresh()  # 重绘以刷新内嵌图片/文件控件的底色

    def _on_typing(self, event=None):
        """敲键时节流上报输入状态，停手 3 秒后自动上报结束。"""
        if not self.msg_entry.get():
            self._stop_typing()
            return
        now = time.monotonic()
        if now - self._typing_sent_at > TYPING_REPEAT_SEC:
            self._typing_sent_at = now
            self.friends_window.send_typing(self.friend, True)
        self._cancel_job("_typing_stop_job")
        self._typing_stop_job = self.root.after(TYPING_IDLE_MS, self._stop_typing)

    def _stop_typing(self) -> None:
        self._cancel_job("_typing_stop_job")
        if self._typing_sent_at:
            self._typing_sent_at = 0.0
            self.friends_window.send_typing(self.friend, False)

    def show_typing(self, active: bool) -> None:
        self._cancel_job("_typing_hide_job")
        self.typing_label.configure(text=f"{self.friend} 正在输入..." if active else "")
        if active:
            self._typing_hide_job = self.root.after(TYPING_EXPIRE_MS, self._expire_typing)

    def _expire_typing(self) -> None:
        self._typing_hide_job = None
        self.typing_label.configure(text="")

    def _cancel_job(self, attr: str) -> None:
        job = getattr(self, attr)
        if job is None:
            return
        setattr(self, attr, None)
        try:
            self.root.after_cancel(job)
        except tk.TclError:
            pass

    def open_emoji_picker(self):
        if self._emoji_picker is not None and self._emoji_picker.win.winfo_exists():
            return
        self._emoji_picker = EmojiPicker(self.root, self.friends_window.emoji_images, self.insert_emoji)

    def insert_emoji(self, emoji: str) -> None:
        self.msg_entry.insert(tk.INSERT, emoji)
        self.msg_entry.focus_set()

    def refresh(self) -> None:
        """按 chat_logs 中的完整记录重绘聊天内容（含表情、图片、文件），避免维护复杂的增量编辑。"""
        self._image_refs.clear()
        self.chat_box.configure(state="normal")
        self.chat_box.delete("1.0", "end")
        for entry in self.friends_window.chat_logs.get(self.friend, []):
            self._render_entry(entry)
        self.chat_box.configure(state="disabled")
        self.chat_box.see("end")

    def _render_entry(self, entry: dict) -> None:
        direction = entry.get("direction")
        if direction == "ai":
            tag, prefix = "ai", "\U0001F916 AI: "
        else:
            is_out = direction == "out"
            tag = "right" if is_out else "left"
            prefix = "我: " if is_out else f"{self.friend}: "
        payload = entry.get("payload", {})

        self.chat_box.insert("end", prefix, (tag,))
        if payload.get("kind") == "file":
            self._render_file(payload, tag)
        else:
            self._render_text(payload.get("text", ""), tag)
        if direction == "out":
            status = entry.get("status", "发送中...")
            self.chat_box.insert("end", f" ({status})", (tag,))
        self.chat_box.insert("end", "\n", (tag,))

    def _render_text(self, text: str, align: str) -> None:
        emoji_images = self.friends_window.emoji_images
        for ch in text:
            photo = emoji_images.get(ch)
            if photo is not None:
                self.chat_box.image_create("end", image=photo)
            else:
                self.chat_box.insert("end", ch, (align,))

    def _render_file(self, payload: dict, align: str) -> None:
        filename = payload.get("filename", "文件")
        size_text = format_size(payload.get("size", 0))
        if payload.get("mime") == "image":
            photo = self._decode_thumbnail(payload.get("data", ""))
            if photo is not None:
                self._image_refs.append(photo)
                label = tk.Label(self.chat_box, image=photo, bg=THEME.panel, cursor="hand2")
                label.bind("<Button-1>", lambda event, p=payload: self._view_image(p))
                self.chat_box.window_create("end", window=label)
                self.chat_box.insert("end", f"  [图片] {filename}", (align,))
                return
        btn = tk.Button(
            self.chat_box, text=f"\U0001F4CE {filename} ({size_text})",
            bg=THEME.btn_bg, fg=THEME.btn_fg, activebackground=THEME.accent,
            relief="flat", bd=0, cursor="hand2",
            command=lambda p=payload: self._save_payload(p),
        )
        self.chat_box.window_create("end", window=btn)

    def _decode_thumbnail(self, b64_data: str):
        try:
            raw = base64.b64decode(b64_data)
            img = Image.open(io.BytesIO(raw))
            img.thumbnail(THUMBNAIL_SIZE)
            return ImageTk.PhotoImage(img)
        except (ValueError, OSError):
            return None

    def _view_image(self, payload: dict) -> None:
        try:
            raw = base64.b64decode(payload.get("data", ""))
            img = Image.open(io.BytesIO(raw))
            img.load()
        except (ValueError, OSError):
            messagebox.showerror("错误", "图片数据已损坏")
            return
        ImageViewer(self.root, payload.get("filename", "图片"), img, raw)

    def _save_payload(self, payload: dict) -> None:
        try:
            raw = base64.b64decode(payload.get("data", ""))
        except ValueError:
            messagebox.showerror("错误", "文件数据已损坏")
            return
        filename = payload.get("filename", "file")
        path = filedialog.asksaveasfilename(initialfile=filename)
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(raw)
        except OSError as exc:
            messagebox.showerror("错误", f"保存失败: {exc}")

    def send(self):
        text = self.msg_entry.get().strip()
        if not text:
            return
        encrypted = not text.startswith("@")
        entry = self.friends_window.send_payload(self.friend, {"kind": "text", "text": text}, encrypted=encrypted)
        if entry is None:
            messagebox.showerror("错误", "与服务器断开连接，发送失败")
            return
        self.refresh()
        self.msg_entry.delete(0, "end")
        self._stop_typing()

    def send_file_dialog(self):
        path = filedialog.askopenfilename()
        if not path:
            return
        self._send_file_path(path)

    def _send_file_path(self, path: str) -> None:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            messagebox.showerror("错误", f"读取文件失败: {exc}")
            return
        self._send_file_bytes(os.path.basename(path), data)

    def _send_file_bytes(self, filename: str, data: bytes) -> None:
        if len(data) > MAX_FILE_SIZE:
            messagebox.showwarning("文件过大", f"文件超过 {MAX_FILE_SIZE // (1024 * 1024)}MB 限制，无法发送")
            return
        payload = make_file_payload(filename, data)
        entry = self.friends_window.send_payload(self.friend, payload)
        if entry is None:
            messagebox.showerror("错误", "与服务器断开连接，发送失败")
            return
        self.refresh()

    def on_paste(self, event=None):
        """检测剪贴板是否为图片/文件，是则直接发送；否则放行默认文本粘贴。"""
        try:
            clip = ImageGrab.grabclipboard()
        except Exception:
            return None
        if isinstance(clip, Image.Image):
            buffer = io.BytesIO()
            clip.save(buffer, format="PNG")
            filename = f"clipboard_{uuid.uuid4().hex[:8]}.png"
            self._send_file_bytes(filename, buffer.getvalue())
            return "break"
        if isinstance(clip, list) and clip and os.path.isfile(clip[0]):
            self._send_file_path(clip[0])
            return "break"
        return None

    def dispose(self):
        self._stop_typing()
        self._cancel_job("_typing_hide_job")
        self.friends_window.open_chats.pop(self.friend, None)
        remember_window("chat", self.root)
        self.root.destroy()

    def on_close(self):
        position = window_center(self.root)
        self.dispose()
        self.friends_window.show_friends(position)


class GroupChatWindow(_UnreadNoticeMixin):
    """群聊窗口：结构与 ChatWindow 类似，区别是发送人不固定，需按消息自带的 from 字段左右对齐。"""

    def __init__(self, root, friends_window: FriendsWindow, group_id: str):
        self.root = root
        self.friends_window = friends_window
        self.group_id = group_id
        self._image_refs = []  # 防止内嵌图片被垃圾回收
        self._emoji_picker = None
        self.is_focused = True
        root.bind("<FocusIn>", self._on_focus_in, add="+")
        root.bind("<FocusOut>", self._on_focus_out, add="+")

        group_name = friends_window.groups.get(group_id, {}).get("name", "群聊")

        hide_until_ready(root)
        self.base_title = f"群聊 - {group_name}"
        root.title(self.base_title)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        apply_theme(root)
        register_themed_window(root, self._on_theme_change)
        self.scaler = WindowScaler(root, 460, 420)
        self.ui_font = self.scaler.font(size=10)
        self.title_font = self.scaler.font(size=10, weight="bold")
        self.emoji_font = self.scaler.font(size=11, family="Segoe UI Emoji")

        main = ttk.Frame(root, padding=8)
        main.pack(fill="both", expand=True)
        self.scaler.track(main, padding=8)

        top_bar = ttk.Frame(main)
        top_bar.pack(fill="x", pady=(0, 6))
        back_btn = dark_button(top_bar, text="< 返回", command=self.on_close, font=self.ui_font)
        back_btn.pack(side="left")
        ttk.Label(top_bar, text=group_name, font=self.title_font).pack(side="left", padx=6)
        members_btn = dark_button(top_bar, text="群成员", command=self.show_members, font=self.ui_font)
        members_btn.pack(side="left", padx=4)
        self.theme_btn = dark_button(top_bar, text=theme_icon(), command=toggle_theme, font=self.emoji_font)
        self.theme_btn.pack(side="right")
        self._build_unread_notice(top_bar)

        input_frame = ttk.Frame(main)
        input_frame.pack(side="bottom", fill="x")
        self.msg_entry = dark_entry(input_frame, font=self.ui_font)
        self.msg_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.msg_entry.bind("<Return>", lambda event: self.send())
        self.msg_entry.bind("<Control-v>", self.on_paste)
        emoji_btn = dark_button(
            input_frame, text="\U0001F60A", font=self.emoji_font, command=self.open_emoji_picker,
        )
        emoji_btn.pack(side="left", padx=(0, 4))
        file_btn = dark_button(input_frame, text="\U0001F4CE", command=self.send_file_dialog, font=self.ui_font)
        file_btn.pack(side="left", padx=(0, 4))
        send_btn = dark_button(input_frame, text="发送", command=self.send, font=self.ui_font)
        send_btn.pack(side="left")

        self.chat_box = scrolledtext.ScrolledText(
            main, wrap="word", state="disabled", height=1, font=self.ui_font,
        )
        self.chat_box.pack(fill="both", expand=True, pady=(0, 6))
        self.chat_box.tag_configure("left", justify="left", foreground=THEME.fg)
        self.chat_box.tag_configure("right", justify="right", foreground=THEME.fg)
        self.scrollbar = style_text(self.chat_box)

        for button in (back_btn, members_btn, self.theme_btn, emoji_btn, file_btn, send_btn):
            self.scaler.track(button, padx=10, pady=4)
        self.scaler.on_scale(self._on_scale)
        self.msg_entry.focus_set()

    def _on_scale(self, scale: float) -> None:
        if self.scrollbar is not None:
            self.scrollbar.configure(width=max(8, round(10 * scale)))

    def _on_focus_in(self, event=None) -> None:
        self.is_focused = True

    def _on_focus_out(self, event=None) -> None:
        self.is_focused = False

    def _on_theme_change(self) -> None:
        self.theme_btn.configure(text=theme_icon())
        self.notice_label.configure(foreground=THEME.hint)
        self.chat_box.frame.configure(bg=THEME.panel)
        self.chat_box.tag_configure("left", foreground=THEME.fg)
        self.chat_box.tag_configure("right", foreground=THEME.fg)
        self.refresh()  # 重绘以刷新内嵌图片/文件控件的底色

    def show_members(self):
        GroupMembersDialog(self.root, self.friends_window, self.group_id)

    def open_emoji_picker(self):
        if self._emoji_picker is not None and self._emoji_picker.win.winfo_exists():
            return
        self._emoji_picker = EmojiPicker(self.root, self.friends_window.emoji_images, self.insert_emoji)

    def insert_emoji(self, emoji: str) -> None:
        self.msg_entry.insert(tk.INSERT, emoji)
        self.msg_entry.focus_set()

    def refresh(self) -> None:
        self._image_refs.clear()
        self.chat_box.configure(state="normal")
        self.chat_box.delete("1.0", "end")
        for entry in self.friends_window.group_chat_logs.get(self.group_id, []):
            self._render_entry(entry)
        self.chat_box.configure(state="disabled")
        self.chat_box.see("end")

    def _render_entry(self, entry: dict) -> None:
        is_out = entry.get("direction") == "out"
        tag = "right" if is_out else "left"
        prefix = "我: " if is_out else f'{entry.get("from", "")}: '
        payload = entry.get("payload", {})

        self.chat_box.insert("end", prefix, (tag,))
        if payload.get("kind") == "file":
            self._render_file(payload, tag)
        else:
            self._render_text(payload.get("text", ""), tag)
        if is_out and entry.get("status"):
            self.chat_box.insert("end", f' ({entry["status"]})', (tag,))
        self.chat_box.insert("end", "\n", (tag,))

    def _render_text(self, text: str, align: str) -> None:
        emoji_images = self.friends_window.emoji_images
        for ch in text:
            photo = emoji_images.get(ch)
            if photo is not None:
                self.chat_box.image_create("end", image=photo)
            else:
                self.chat_box.insert("end", ch, (align,))

    def _render_file(self, payload: dict, align: str) -> None:
        filename = payload.get("filename", "文件")
        size_text = format_size(payload.get("size", 0))
        if payload.get("mime") == "image":
            photo = self._decode_thumbnail(payload.get("data", ""))
            if photo is not None:
                self._image_refs.append(photo)
                label = tk.Label(self.chat_box, image=photo, bg=THEME.panel, cursor="hand2")
                label.bind("<Button-1>", lambda event, p=payload: self._view_image(p))
                self.chat_box.window_create("end", window=label)
                self.chat_box.insert("end", f"  [图片] {filename}", (align,))
                return
        btn = tk.Button(
            self.chat_box, text=f"\U0001F4CE {filename} ({size_text})",
            bg=THEME.btn_bg, fg=THEME.btn_fg, activebackground=THEME.accent,
            relief="flat", bd=0, cursor="hand2",
            command=lambda p=payload: self._save_payload(p),
        )
        self.chat_box.window_create("end", window=btn)

    def _decode_thumbnail(self, b64_data: str):
        try:
            raw = base64.b64decode(b64_data)
            img = Image.open(io.BytesIO(raw))
            img.thumbnail(THUMBNAIL_SIZE)
            return ImageTk.PhotoImage(img)
        except (ValueError, OSError):
            return None

    def _view_image(self, payload: dict) -> None:
        try:
            raw = base64.b64decode(payload.get("data", ""))
            img = Image.open(io.BytesIO(raw))
            img.load()
        except (ValueError, OSError):
            messagebox.showerror("错误", "图片数据已损坏")
            return
        ImageViewer(self.root, payload.get("filename", "图片"), img, raw)

    def _save_payload(self, payload: dict) -> None:
        try:
            raw = base64.b64decode(payload.get("data", ""))
        except ValueError:
            messagebox.showerror("错误", "文件数据已损坏")
            return
        filename = payload.get("filename", "file")
        path = filedialog.asksaveasfilename(initialfile=filename)
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(raw)
        except OSError as exc:
            messagebox.showerror("错误", f"保存失败: {exc}")

    def send(self):
        text = self.msg_entry.get().strip()
        if not text:
            return
        entry = self.friends_window.send_group_payload(self.group_id, {"kind": "text", "text": text})
        if entry is None:
            messagebox.showerror("错误", "与服务器断开连接，发送失败")
            return
        self.refresh()
        self.msg_entry.delete(0, "end")

    def send_file_dialog(self):
        path = filedialog.askopenfilename()
        if not path:
            return
        self._send_file_path(path)

    def _send_file_path(self, path: str) -> None:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            messagebox.showerror("错误", f"读取文件失败: {exc}")
            return
        self._send_file_bytes(os.path.basename(path), data)

    def _send_file_bytes(self, filename: str, data: bytes) -> None:
        if len(data) > MAX_FILE_SIZE:
            messagebox.showwarning("文件过大", f"文件超过 {MAX_FILE_SIZE // (1024 * 1024)}MB 限制，无法发送")
            return
        payload = make_file_payload(filename, data)
        entry = self.friends_window.send_group_payload(self.group_id, payload)
        if entry is None:
            messagebox.showerror("错误", "与服务器断开连接，发送失败")
            return
        self.refresh()

    def on_paste(self, event=None):
        """检测剪贴板是否为图片/文件，是则直接发送；否则放行默认文本粘贴。"""
        try:
            clip = ImageGrab.grabclipboard()
        except Exception:
            return None
        if isinstance(clip, Image.Image):
            buffer = io.BytesIO()
            clip.save(buffer, format="PNG")
            filename = f"clipboard_{uuid.uuid4().hex[:8]}.png"
            self._send_file_bytes(filename, buffer.getvalue())
            return "break"
        if isinstance(clip, list) and clip and os.path.isfile(clip[0]):
            self._send_file_path(clip[0])
            return "break"
        return None

    def dispose(self):
        self.friends_window.open_group_chats.pop(self.group_id, None)
        remember_window("chat", self.root)
        self.root.destroy()

    def on_close(self):
        position = window_center(self.root)
        self.dispose()
        self.friends_window.show_friends(position)


if __name__ == "__main__":
    load_saved_theme()
    root = tk.Tk()
    LoginWindow(root)
    root.mainloop()
