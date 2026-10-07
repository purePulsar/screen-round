#!/usr/bin/env python3
"""屏幕四角圆角遮罩 —— 托盘设置入口

遮罩本身是鼠标完全穿透的窗口，**它自己没法右键**，所以托盘是唯一的控制面：
  1. 启用 / 停用遮罩
  2. 圆角半径（预设档位，单选）
  3. 开机自启
  4. 重启遮罩（遮罩假死时的救命项）
  5. 退出遮罩（托盘继续留着）/ 退出托盘

本程序只负责写设置文件，遮罩每 0.8 秒读一次，所以改完立刻生效。
两个程序互相独立：退出托盘不影响遮罩。
运行: python3 screen_tray.py
"""

import os
import signal
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk                                # noqa: E402

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
except (ValueError, ImportError) as exc:
    sys.exit("缺少 AyatanaAppIndicator3，请先安装托盘库：\n"
             "    sudo apt install -y gir1.2-ayatanaappindicator3-0.1\n"
             f"（{exc}）")

from corner_common import (                                        # noqa: E402
    R_PRESETS, acquire_singleton, autostart_enabled, is_running,
    load_settings, read_lock_pid, save_settings, set_autostart)

APP_ID = "screen-round"
ICON_NAME = "video-display"
MASK_LOCK = "mask"
RADIUS_DEBOUNCE_MS = 150     # 连点档位时等 150ms 再落盘，避免遮罩反复重裁
RESTART_WAIT_S = 2.0         # 重启时等旧遮罩让出单实例锁的上限（秒）

_HERE = os.path.dirname(os.path.abspath(__file__))
MASK_SCRIPT = os.path.join(_HERE, "screen_mask.py")


class TrayApp:
    def __init__(self):
        self.settings = load_settings()
        save_settings(self.settings)   # 首次运行时落下默认值，遮罩才有文件可读
        self._pending_radius = None
        self._radius_timer = None

        menu = Gtk.Menu()

        self.item_enabled = Gtk.CheckMenuItem(label="启用圆角遮罩")
        self.item_enabled.set_active(self.settings["enabled"])
        self.item_enabled.connect("toggled", self._on_enabled)
        menu.append(self.item_enabled)

        menu.append(self._build_radius_menu())
        menu.append(Gtk.SeparatorMenuItem())

        self.item_autostart = Gtk.CheckMenuItem(label="开机自启")
        self.item_autostart.set_active(autostart_enabled())
        self.item_autostart.connect("toggled", self._on_autostart)
        menu.append(self.item_autostart)

        menu.append(Gtk.SeparatorMenuItem())

        restart_item = Gtk.MenuItem(label="重启遮罩")
        restart_item.connect("activate", lambda _: self._restart_mask())
        menu.append(restart_item)

        stop_item = Gtk.MenuItem(label="退出圆角遮罩")
        stop_item.connect("activate", lambda _: self._signal_mask())
        menu.append(stop_item)

        quit_item = Gtk.MenuItem(label="退出托盘")
        quit_item.connect("activate", lambda _: Gtk.main_quit())
        menu.append(quit_item)

        menu.show_all()

        self.indicator = AppIndicator.Indicator.new(
            APP_ID, ICON_NAME,
            AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("屏幕圆角遮罩")
        self.indicator.set_menu(menu)

    def _build_radius_menu(self):
        """圆角半径子菜单：预设档位单选，默认选中与当前值最接近的档。"""
        submenu = Gtk.Menu()
        items, group = [], None
        for radius in R_PRESETS:
            item = Gtk.RadioMenuItem(label=f"{radius} px")
            if group is None:
                group = item
            else:
                item.join_group(group)
            submenu.append(item)
            items.append(item)

        # 先把选中项定下来，再连信号，免得初始化时误写文件（disk_tray 同款）
        current = self.settings["radius"]
        closest = min(range(len(R_PRESETS)),
                      key=lambda i: abs(R_PRESETS[i] - current))
        items[closest].set_active(True)
        for item, radius in zip(items, R_PRESETS):
            item.connect("toggled", self._on_radius, radius)
        submenu.show_all()

        top = Gtk.MenuItem(label="圆角半径")
        top.set_submenu(submenu)
        return top

    # ---------- 菜单回调 ----------
    def _on_enabled(self, item):
        self.settings["enabled"] = item.get_active()
        save_settings(self.settings)

    def _on_radius(self, item, radius):
        if not item.get_active():      # 单选组里每次会触发两次，只认被选中的那次
            return
        self._pending_radius = radius
        if self._radius_timer is not None:
            GLib.source_remove(self._radius_timer)
        self._radius_timer = GLib.timeout_add(RADIUS_DEBOUNCE_MS, self._flush_radius)

    def _flush_radius(self):
        self._radius_timer = None
        radius, self._pending_radius = self._pending_radius, None
        if radius is not None and radius != self.settings["radius"]:
            self.settings["radius"] = radius
            save_settings(self.settings)
        return False                   # 一次性计时器

    def _on_autostart(self, item):
        wanted = item.get_active()
        if set_autostart(wanted):
            return
        # 写失败就把勾恢复原状，别让用户以为设成功了
        item.handler_block_by_func(self._on_autostart)
        item.set_active(not wanted)
        item.handler_unblock_by_func(self._on_autostart)

    # ---------- 遮罩进程 ----------
    def _signal_mask(self):
        """给遮罩发 SIGTERM；返回是否真发出去了。

        靠锁文件里记的 pid 来 kill，不用 `pkill -f screen_mask.py`——那个模式串会
        连带匹配到发起命令自己的命令行，把调用者也一起杀掉（踩过）。
        """
        pid = read_lock_pid(MASK_LOCK)
        if pid is None or not is_running(MASK_LOCK):
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            print(f"[screen-round] 结束遮罩进程 {pid} 失败: {exc}", file=sys.stderr)
            return False
        return True

    def _restart_mask(self):
        """先结束旧的，等它让出单实例锁，再拉起新的。

        不等的话新进程会拿不到锁、立刻退出，看起来像"点了没反应"。
        """
        self._signal_mask()
        deadline = time.time() + RESTART_WAIT_S
        while time.time() < deadline and is_running(MASK_LOCK):
            time.sleep(0.05)
        if is_running(MASK_LOCK):
            print("[screen-round] 旧遮罩没退出，取消重启", file=sys.stderr)
            return
        subprocess.Popen([sys.executable, MASK_SCRIPT], start_new_session=True)


def main():
    # 单实例：自启和手动启动撞在一起时只留一个
    lock_fd = acquire_singleton("tray", pid=os.getpid())
    if lock_fd is None:
        print("[screen-round] 已经有一个托盘在运行，本次启动退出", file=sys.stderr)
        return
    TrayApp()
    Gtk.main()


if __name__ == "__main__":
    main()
