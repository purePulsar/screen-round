#!/usr/bin/env python3
"""屏幕四角圆角遮罩 —— 让整块显示看起来像一块圆角屏幕。

四个 r×r 的小窗口贴在屏幕四角，每个用 X11 Shape 裁成「方块减去靠屏幕内侧的
四分之一圆」，剩下的那一小条正好盖住原来的直角，形成显示器边框般的圆角。

- 窗口用 override-redirect（Mutter 里最高的窗口层），才压得住 Zorin 的上下两条面板。
  面板占满整宽、四角都落在面板上，压不住就没意义。实测：dock+topmost 只能压住上面板，
  压不住下面的 zorin-taskbar；override-redirect 两条都压得住。
- 输入区置空 → 完全鼠标穿透，不抢 Activities 热区、不抢面板按钮
- 无边框、无标题栏；遮罩本身没有右键菜单，**所有控制都在托盘 screen_tray.py**
运行: python3 screen_mask.py
"""

import ctypes
import os
import signal
import sys
import tkinter as tk

from corner_common import SETTINGS_PATH, acquire_singleton, load_settings

MASK_COLOR = "#000000"     # 遮罩颜色。Zorin 面板是 rgba(26,33,37,0.7) 半透明，
                           # 随壁纸变色、没法同色，所以纯黑当"显示器黑边框"最自然。
POLL_MS = 800              # 轮询设置 / 分辨率的间隔（毫秒）
SETTLE_MS = 200            # 窗口尺寸落定后再补一次摆位的延迟（毫秒）

# 四角：(是否贴屏幕右边缘, 是否贴下边缘, 圆心的 x/y 系数)
# 屏幕四角的圆角圆心在屏幕上分别是 (r,r)、(W-r,r)、(r,H-r)、(W-r,H-r)；窗口宽高各 r
# 且贴在屏幕角上，所以圆心换算到窗口局部坐标就是 (kx*r, ky*r)。
CORNERS = {
    "左上": (False, False, 1, 1),      # 圆心在窗口右下
    "右上": (True, False, 0, 1),       # 圆心在窗口左下
    "左下": (False, True, 1, 0),       # 圆心在窗口右上
    "右下": (True, True, 0, 0),        # 圆心在窗口左上
}

# ---------- X11 圆角遮罩（ctypes 直调 libX11/libXext，无需额外依赖） ----------
# 与 disk-card/disk_viewer.py 的 apply_round_shape() 同源，这里用的是它的逆运算。
try:
    _x11 = ctypes.CDLL("libX11.so.6")
    _xext = ctypes.CDLL("libXext.so.6")
except OSError:
    _x11 = _xext = None

# X11 shape 的真实取值：ShapeBounding=0, ShapeClip=1, ShapeInput=2。
# disk-card/disk_viewer.py 写成 (0, 1) 并把 1 当成 Input，其实是 Clip。
# 我们这里必须用真正的 ShapeInput(2)：Clip 管的是"画到哪儿"，把它设成空区域
# 会把窗口整个画不出来。
_SHAPE_BOUNDING, _SHAPE_INPUT = 0, 2
# ShapeSet 就是 0。disk_viewer.py 里写的是 8，那个值不合法：X 会回 BadValue 且形状
# 完全不生效。实测（新窗口 + region 读回）：op=8 → 两个 BadValue、region 不变；
# op=0 → region 正确；op=3 → 正好等于"整窗口减去形状区域"。
_SHAPE_SET = 0
_SHAPE_UNSORTED = 0        # 我们给出的矩形本来就按 y 排好，但 ShapeUnsorted 最省心


class _XRectangle(ctypes.Structure):
    """XRectangle。XShapeCombineRectangles 要的是这个内存布局，不能直接给元组。"""
    _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short),
                ("width", ctypes.c_ushort), ("height", ctypes.c_ushort)]


class _XErrorEvent(ctypes.Structure):
    """XErrorEvent 的真实内存布局。

    别图省事按 int 数组取字段——那样拿到的根本不是 error_code，会一直查错方向。
    """
    _fields_ = [("type", ctypes.c_int),
                ("display", ctypes.c_void_p),
                ("resourceid", ctypes.c_ulong),
                ("serial", ctypes.c_ulong),
                ("error_code", ctypes.c_ubyte),
                ("request_code", ctypes.c_ubyte),
                ("minor_code", ctypes.c_ubyte)]


_SEEN_ERRORS = set()


def _on_x_error(_display, event):
    """X 报错既不静默吞掉（否则形状没生效也看不出来），也不刷屏：同类只提示一次。

    Xlib 默认 handler 会直接 abort 整个进程，所以 handler 必须有。
    """
    err = ctypes.cast(event, ctypes.POINTER(_XErrorEvent)).contents
    key = (err.error_code, err.request_code, err.minor_code)
    if key not in _SEEN_ERRORS:
        _SEEN_ERRORS.add(key)
        print(f"[screen-round] X 报错: code={err.error_code} "
              f"request={err.request_code} minor={err.minor_code}（同类只提示一次）",
              file=sys.stderr)


if _x11:
    # 必须留一个模块级引用。写成 XSetErrorHandler(CFUNCTYPE(...)(...)) 这种内联形式的话，
    # 回调对象在语句结束就被回收，Xlib 手里只剩一个野指针——一旦真出 X 错误就直接段错误，
    # 而且崩在 XCloseDisplay 上，极难定位（已实测踩过）。
    _X_ERROR_HANDLER = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)(
        _on_x_error)
    _x11.XSetErrorHandler(_X_ERROR_HANDLER)


def _c_types(fn, restype, *argtypes):
    fn.restype = restype
    fn.argtypes = list(argtypes)


if _x11:
    _c_types(_x11.XOpenDisplay, ctypes.c_void_p, ctypes.c_char_p)
    _c_types(_x11.XCloseDisplay, ctypes.c_int, ctypes.c_void_p)
    _c_types(_x11.XFlush, ctypes.c_int, ctypes.c_void_p)
    _c_types(_x11.XDefaultRootWindow, ctypes.c_ulong, ctypes.c_void_p)
    _c_types(_x11.XQueryTree, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
             ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
             ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
             ctypes.POINTER(ctypes.c_uint))
    _c_types(_x11.XFree, ctypes.c_int, ctypes.c_void_p)
    _c_types(_xext.XShapeCombineRectangles, None, ctypes.c_void_p, ctypes.c_ulong,
             ctypes.c_int, ctypes.c_int, ctypes.c_int,
             ctypes.POINTER(_XRectangle), ctypes.c_int, ctypes.c_int, ctypes.c_int)


def _window_chain(d, win_id):
    """win_id 自己 + 它到 root 之间的所有祖先窗口（不含 root）。

    Tk 的每个 Toplevel 在 X 上其实是**两层**窗口：外层 wrapper（`xwininfo` 里那个、
    带 WM_NAME 的）和内层（`winfo_id()` 返回的那个）。形状必须两层都裁——只裁内层
    的话，wrapper 会拿自己的背景色把圆角盖成直角方块（实测：内层 region 已经是
    254 像素的圆弧，屏幕上看却是个 32×32 黑方块）。受 WM 管理时 wrapper 被重挂进
    WM 的框、背景不画，所以只裁内层就够了；override-redirect 没有 WM 管，wrapper
    自己会画，于是必须连 wrapper 一起裁。
    """
    root = _x11.XDefaultRootWindow(d)
    chain, cur = [], win_id
    while True:
        children = ctypes.POINTER(ctypes.c_ulong)()
        nchild = ctypes.c_uint()
        r, p = ctypes.c_ulong(), ctypes.c_ulong()
        if not _x11.XQueryTree(d, cur, ctypes.byref(r), ctypes.byref(p),
                               ctypes.byref(children), ctypes.byref(nchild)):
            break
        if children:
            _x11.XFree(ctypes.cast(children, ctypes.c_void_p))
        chain.append(cur)
        if p.value == 0 or p.value == root:
            break
        cur = p.value
    return chain


def _corner_rects(radius, corner):
    """算出「方块减去四分之一圆」里那个方块（也就是要遮挡的区域）的矩形列表。

    采样点取像素**中心** (x+0.5, y+0.5)。这是光栅化的标准做法，也是四个角能严格
    对称的前提。原先这里交给 XFillArc 让 X 自己光栅化，而它按像素**左上角**采样，
    "取左上角"这条规则在 180° 旋转下不是不变量（旋转会把一个像素的左上角变成对角
    像素的右下角）；四个圆弧的圆心又都正好落在屏幕边线上，于是顶边的圆角比底边深
    了整整一行：实测左上 73px、右下 42px，而按像素中心采样四角都应当是 ~54px。
    """
    _, _, kx, ky = CORNERS[corner]
    cx, cy = kx * radius, ky * radius
    r2 = radius * radius
    rects = []
    for y in range(radius):
        run = None                         # 本行当前连续段的起点
        for x in range(radius):
            dx = x + 0.5 - cx
            dy = y + 0.5 - cy
            if dx * dx + dy * dy > r2:     # 在圆外 → 属于遮挡区
                if run is None:
                    run = x
            elif run is not None:
                rects.append((run, y, x - run, 1))
                run = None
        if run is not None:
            rects.append((run, y, radius - run, 1))
    return rects


def apply_corner_mask(win_id, radius, corner):
    """把 radius×radius 的窗口裁成「方块减去靠屏幕内侧的四分之一圆」。

    和 disk_viewer 的 apply_round_shape() 相反：那边是「全清再画可见区」，
    这里是「全可见再挖掉一个象限」。顺带把输入区置空（完全鼠标穿透）。
    同一个区域要套在 win_id 到 root 之间的**每一层**窗口上，见 _window_chain()。
    失败时静默返回 False——宁可留个直角，也不要因为裁圆角失败而崩掉。
    """
    if not _x11 or radius < 2:
        return False
    d = _x11.XOpenDisplay(None)
    if not d:
        return False
    try:
        rects = _corner_rects(radius, corner)
        arr = (_XRectangle * len(rects))(*rects)
        # 输入区：空矩形列表 → 空区域，遮罩完全不吃点击。不这么做的话，左上角会抢走
        # GNOME 的 Activities 热区、四角会抢走面板按钮。
        empty = (_XRectangle * 0)()

        for wid in _window_chain(d, win_id):
            _xext.XShapeCombineRectangles(d, wid, _SHAPE_BOUNDING, 0, 0, arr,
                                          len(rects), _SHAPE_SET, _SHAPE_UNSORTED)
            _xext.XShapeCombineRectangles(d, wid, _SHAPE_INPUT, 0, 0, empty, 0,
                                          _SHAPE_SET, _SHAPE_UNSORTED)

        _x11.XFlush(d)
        return True
    finally:
        _x11.XCloseDisplay(d)


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


class ScreenRoundMask:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()               # 只需要一个隐藏的 root 跑主循环和计时器
        self.settings = load_settings()
        self.radius = self.settings["radius"]
        self.screen = (self.root.winfo_screenwidth(),
                       self.root.winfo_screenheight())
        self.windows = {}                  # 角名 -> Toplevel
        self._settings_mtime = _mtime(SETTINGS_PATH)

        if self.settings["enabled"]:
            self._show()
        self.root.after(POLL_MS, self._poll)

    # ---------- 窗口 ----------
    def _ensure_windows(self):
        """创建四个角的窗口（只建一次，停用只是 withdraw，不销毁）。"""
        for corner in CORNERS:
            if corner in self.windows:
                continue
            win = tk.Toplevel(self.root)
            win.withdraw()                 # 先不上屏：否则会闪出四个直角黑方块
            # 必须 override-redirect。它对应 X 的 override_redirect，在 Mutter 的窗口
            # 分层里是最高的那一层，底栏、全屏窗口全都盖得住。实测对照：
            #   dock + -topmost        → 压得住上面板，压不住下面的 zorin-taskbar
            #   dock 去掉 -topmost     → 同样压不住
            #   notification/splash/utility/normal/menu/tooltip → 一律压不住
            #   override-redirect      → 四条边都压得住（底栏像素从"面板色"变成纯遮罩色）
            # 代价是不再受 WM 管理；对本用途反而是好事：位置不会被 WM 改动。
            # 顺带一提，override-redirect 下 "-topmost" 不起作用（也不需要）。
            win.overrideredirect(True)
            win.title(f"圆角遮罩-{corner}")   # 只为调试时好认，OR 窗口没有标题栏
            win.configure(bg=MASK_COLOR)
            win.bind("<Configure>", lambda e, c=corner: self._on_configure(e, c))
            win.bind("<Map>", lambda e, c=corner: self._on_map(e, c))
            self.windows[corner] = win

    def _place_all(self):
        """按当前半径摆好四个窗口：**先裁形状，再改尺寸**。

        顺序不能反：先把形状裁出来，X 会把超出窗口的部分剪掉，等 resize 落地正好
        吻合，中间不会出现一帧没裁过的直角黑方块。改半径时窗口已经上屏，这条成立。
        """
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        for corner, win in self.windows.items():
            right, bottom, _, _ = CORNERS[corner]
            x = sw - self.radius if right else 0
            y = sh - self.radius if bottom else 0
            self._mask(corner)
            win.geometry(f"{self.radius}x{self.radius}+{x}+{y}")
        self.root.update_idletasks()
        self.screen = (sw, sh)

    def _mask(self, corner):
        """给一个窗口裁圆角；窗口还没上屏就直接跳过。

        实测：对**未上屏**的窗口调 XShapeCombineMask，X 会用 BadMatch 顶回来，
        形状静默不生效——窗口就变成一个直角黑方块。所以必须等 <Map> 之后再裁。
        """
        win = self.windows[corner]
        if not win.winfo_viewable():
            return
        apply_corner_mask(win.winfo_id(), self.radius, corner)

    def _on_map(self, event, corner):
        """窗口上屏了：立刻补一次遮罩，稍后再补一次压保险。"""
        if event.widget is not self.windows[corner]:
            return
        self._place_all()
        self.root.after(SETTLE_MS, self._place_all)

    def _show(self):
        self._ensure_windows()
        self._place_all()
        for win in self.windows.values():
            win.deiconify()
        # 重新映射可能把位置/形状重置掉，落定后再补一次（disk_viewer 同款保险）
        self.root.after(SETTLE_MS, self._place_all)

    def _hide(self):
        for win in self.windows.values():
            win.withdraw()

    def _on_configure(self, event, corner):
        """窗口尺寸被 WM 改掉时才重摆一次。

        只比尺寸：尺寸对不上会露出没裁过的直角黑方块，是真正有害的情况；
        位置漂移由改半径/换分辨率/重新显示这几条路径顺带纠正。
        这里非要加个判断不可——若无条件调 _place_all，而 Tk 每次都回一个
        ConfigureNotify 的话，就会变成 200ms 一轮的死循环。
        """
        if (event.width, event.height) != (self.radius, self.radius):
            self.root.after(SETTLE_MS, self._place_all)

    # ---------- 设置轮询 ----------
    def _poll(self):
        """每 0.8 秒：设置变了就应用，分辨率变了就重新摆位。"""
        mtime = _mtime(SETTINGS_PATH)
        if mtime != self._settings_mtime:
            self._settings_mtime = mtime
            new = load_settings()
            if new != self.settings:
                self.settings = new
                self._apply_settings()
        # 改分辨率 / 改缩放 / 插拔显示器都走这里；不重摆就会错位
        size = (self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        if size != self.screen and self.windows:
            self._place_all()
        self.root.after(POLL_MS, self._poll)

    def _apply_settings(self):
        new_radius = self.settings["radius"]
        if new_radius != self.radius:
            self.radius = new_radius
            if self.windows:
                self._place_all()
        if self.settings["enabled"]:
            if not any(w.winfo_ismapped() for w in self.windows.values()):
                self._show()
        else:
            self._hide()


def main():
    # 单实例：自启和手动启动撞在一起时，只留一个，避免两层遮罩叠色
    lock_fd = acquire_singleton("mask", pid=os.getpid())
    if lock_fd is None:
        print("[screen-round] 已经有一个遮罩在运行，本次启动退出", file=sys.stderr)
        return

    app = ScreenRoundMask()
    # SIGTERM 交给主循环里的事件循环处理（有 0.8 秒的计时器，最迟 0.8 秒内响应）
    signal.signal(signal.SIGTERM, lambda *_: app.root.quit())
    app.root.mainloop()


if __name__ == "__main__":
    main()
