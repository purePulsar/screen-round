#!/usr/bin/env python3
"""screen-round 的共用部分：设置读写、半径钳制、单实例锁、开机自启项。

遮罩进程（Tk）和托盘进程（GTK）只通过设置文件通信，两边都 import 本模块，
所以这里**只能用标准库**——一旦 import 了 gi 或 tkinter，另一个进程就会被
拖进它根本不需要的依赖。

运行: 本模块不单独运行，由 screen_mask.py / screen_tray.py 导入。
"""

import fcntl
import json
import os
import sys

CONFIG_DIR = os.path.expanduser("~/.config/screen-round")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
AUTOSTART_DIR = os.path.expanduser("~/.config/autostart")

# 圆角半径：默认值、可选范围、托盘菜单里的档位
R_DEFAULT = 16
R_MIN = 4
R_MAX = 40
R_PRESETS = [8, 12, 16, 20, 24, 32]

DEFAULTS = {"enabled": True, "radius": R_DEFAULT}

# 脚本自身所在的目录，自启项的 Exec 指到这里
_HERE = os.path.dirname(os.path.abspath(__file__))

# 自启项：(文件名, 菜单里显示的名字, 说明, 要启动的脚本)
AUTOSTART_ENTRIES = (
    ("screen-round.desktop", "屏幕圆角遮罩", "把屏幕四角遮成圆角",
     os.path.join(_HERE, "screen_mask.py")),
    ("screen-round-tray.desktop", "屏幕圆角托盘", "屏幕圆角遮罩的托盘设置入口",
     os.path.join(_HERE, "screen_tray.py")),
)

# 与 disk-card 的写法保持一致：用系统 python3，环境里必须有 python3-gi / python3-tk
_PYTHON = "/usr/bin/python3"

_DESKTOP_TMPL = """[Desktop Entry]
Type=Application
Version=1.0
Name={name}
Comment={comment}
Exec={python} {script}
StartupNotify=false
Terminal=false
X-GNOME-Autostart-enabled=true
"""


def clamp_radius(value):
    """把半径钳进可用范围；非法值回退到默认值。"""
    try:
        radius = int(value)
    except (TypeError, ValueError):
        return R_DEFAULT
    return max(R_MIN, min(R_MAX, radius))


def load_settings():
    """读设置；文件不存在或损坏时用默认值。逐键合并，缺哪个补哪个。"""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("内容不是 JSON 对象")
    except FileNotFoundError:
        return dict(DEFAULTS)          # 还没跑过托盘，用默认值
    except (OSError, ValueError) as exc:
        print(f"[screen-round] 设置读取失败，用默认值: {exc}", file=sys.stderr)
        return dict(DEFAULTS)

    settings = dict(DEFAULTS)
    if "enabled" in raw:
        settings["enabled"] = bool(raw["enabled"])
    if "radius" in raw:
        settings["radius"] = clamp_radius(raw["radius"])
    return settings


def save_settings(settings):
    """原子写入：先写临时文件再改名，避免遮罩读到写了一半的内容。"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except OSError as exc:
        # 失败要看得见：否则用户以为设置生效了，其实没有
        print(f"[screen-round] 设置保存失败: {exc}", file=sys.stderr)


def acquire_singleton(name, pid=None):
    """拿单实例锁。返回 fd 表示拿到了（必须一直开着，锁才不释放）；None 表示已被占。

    用 flock 而不是"看 pidfile 里的 pid 还活着没"：后者在自启同时拉起两个进程时
    有竞态，而且 pid 被系统复用时判断是错的。锁文件里顺便记下 pid，托盘靠它来 kill。
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)
    path = os.path.join(CONFIG_DIR, f"{name}.lock")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    if pid is not None:
        os.ftruncate(fd, 0)
        os.write(fd, str(pid).encode())
    return fd


def read_lock_pid(name):
    """读锁文件里记的 pid；文件不存在或内容不是数字时返回 None。"""
    try:
        with open(os.path.join(CONFIG_DIR, f"{name}.lock"), encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def is_running(name):
    """靠"能不能拿到锁"判断对端进程是否还活着——比检查 pid 存活可靠。"""
    fd = acquire_singleton(name)
    if fd is None:
        return True
    os.close(fd)
    return False


def autostart_enabled():
    """两个自启项都在，才算开了自启。"""
    return all(os.path.exists(os.path.join(AUTOSTART_DIR, name))
               for name, _, _, _ in AUTOSTART_ENTRIES)


def set_autostart(enabled):
    """生成或删除 ~/.config/autostart/ 下的两个自启项。

    自启文件按脚本所在目录现算路径，所以项目里不会再预置一份——两处都有的话
    登录就会启动两次（disk-card README 里踩过这个坑）。
    """
    for name, label, comment, script in AUTOSTART_ENTRIES:
        path = os.path.join(AUTOSTART_DIR, name)
        try:
            if enabled:
                os.makedirs(AUTOSTART_DIR, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(_DESKTOP_TMPL.format(name=label, comment=comment,
                                                 python=_PYTHON, script=script))
            elif os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            print(f"[screen-round] 自启项写入失败 {path}: {exc}", file=sys.stderr)
            return False
    return True
