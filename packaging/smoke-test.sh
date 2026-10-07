#!/usr/bin/env bash
# 冒烟测试：把打好的 .deb 拆开，在虚拟显示器上真的跑一遍。
#
#   packaging/smoke-test.sh dist/screen-round_0.1.0_all.deb
#
# 不需要 root，也不往系统里装任何东西：直接 dpkg-deb -x 到临时目录再执行。
# 需要 Xvfb（可选 xwininfo / dbus-run-session），缺哪个就跳过对应的检查。
set -uo pipefail

DEB="${1:-}"
if [ -z "$DEB" ]; then
    echo "用法: $0 <deb 路径>" >&2
    exit 2
fi
DEB="$(readlink -f -- "$DEB")"
if [ ! -f "$DEB" ]; then
    echo "找不到 $DEB" >&2
    exit 2
fi

EX="$(mktemp -d)"
FAKE_HOME="$(mktemp -d)"
XVFB_DISPLAY=":${SMOKE_DISPLAY:-91}"
XVFB_PID=""
PIDS=()
FAIL=0

cleanup() {
    if [ "${#PIDS[@]}" -gt 0 ]; then
        for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done
    fi
    [ -n "$XVFB_PID" ] && kill "$XVFB_PID" 2>/dev/null
    rm -rf -- "$EX" "$FAKE_HOME"
}
trap cleanup EXIT

ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=1; }
skip(){ printf '  \033[90m–\033[0m %s\n' "$*"; }

LIBDIR="$EX/usr/lib/screen-round"

echo "== 拆包 =="
dpkg-deb -x "$DEB" "$EX" || { bad "dpkg-deb -x 失败"; exit 1; }
ok "$(basename "$DEB") 拆开了"

for f in corner_common.py screen_mask.py screen_tray.py; do
    [ -f "$LIBDIR/$f" ] || bad "缺 $LIBDIR/$f"
done
[ -x "$EX/usr/bin/screen-round" ] && ok "启动器可执行" || bad "缺 /usr/bin/screen-round"
[ -f "$EX/usr/share/applications/screen-round.desktop" ] || bad "缺桌面入口"
[ -f "$EX/usr/share/icons/hicolor/scalable/apps/screen-round.svg" ] || bad "缺图标"

echo "== 语法 =="
if /usr/bin/python3 -m py_compile "$LIBDIR"/*.py; then ok "三个脚本都能编译"; else bad "py_compile 失败"; fi

if command -v desktop-file-validate >/dev/null; then
    if desktop-file-validate "$EX/usr/share/applications/screen-round.desktop"; then
        ok "桌面入口能过 desktop-file-validate"
    else
        bad "桌面入口校验没通过"
    fi
else
    skip "没装 desktop-file-validate"
fi

ALLOW_WRITE_OK=$(HOME="$FAKE_HOME" /usr/bin/python3 - "$LIBDIR" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1])
import corner_common as c
ok = c.set_autostart(True)
mask = os.path.join(c.AUTOSTART_DIR, "screen-round.desktop")
tray = os.path.join(c.AUTOSTART_DIR, "screen-round-tray.desktop")
good = ok and os.path.exists(mask) and os.path.exists(tray)
if good:
    good = "screen_mask.py" in open(mask, encoding="utf-8").read() \
       and "screen_tray.py" in open(tray, encoding="utf-8").read()
c.set_autostart(False)
if os.path.exists(mask):
    good = False
print("ok" if good else "no")
PY
)
[ "$ALLOW_WRITE_OK" = "ok" ] && ok "自启项能生成、指向同目录脚本、也能删干净" \
                             || bad "自启项生成/删除有问题"

if ! command -v Xvfb >/dev/null; then
    skip "没装 Xvfb，跳过实际运行检查"
elif ! command -v xwininfo >/dev/null; then
    skip "没装 xwininfo，跳过实际运行检查"
else
    echo "== 实跑 =="
    Xvfb "$XVFB_DISPLAY" -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 &
    XVFB_PID=$!
    for _ in $(seq 1 50); do
        DISPLAY="$XVFB_DISPLAY" xwininfo -root >/dev/null 2>&1 && break
        sleep 0.2
    done

    SW=$(DISPLAY="$XVFB_DISPLAY" xwininfo -root | awk '/-geometry/{print $2}' | cut -dx -f1)
    SH=$(DISPLAY="$XVFB_DISPLAY" xwininfo -root | awk '/-geometry/{print $2}' | cut -dx -f2 | cut -d+ -f1)
    R=16   # corner_common.R_DEFAULT，遮罩没设置文件时用这个半径

    HOME="$FAKE_HOME" DISPLAY="$XVFB_DISPLAY" /usr/bin/python3 "$LIBDIR/screen_mask.py" \
        >"$FAKE_HOME/mask.log" 2>&1 &
    PIDS+=($!)
    sleep 4

    TREE="$(DISPLAY="$XVFB_DISPLAY" xwininfo -root -tree 2>/dev/null)"
    N="$(printf '%s\n' "$TREE" | grep -c '圆角遮罩-')"
    [ "$N" = "4" ] && ok "遮罩起了 4 个角窗口" || bad "角窗口数量是 $N，应该是 4"

    for geo in "${R}x${R}+0+0" \
               "${R}x${R}+$((SW-R))+0" \
               "${R}x${R}+0+$((SH-R))" \
               "${R}x${R}+$((SW-R))+$((SH-R))"; do
        if printf '%s\n' "$TREE" | grep -qF " $geo"; then
            ok "角窗口位置 $geo"
        else
            bad "没找到位置为 $geo 的角窗口"
        fi
    done

    if command -v dbus-run-session >/dev/null && /usr/bin/python3 -c 'import gi' 2>/dev/null; then
        HOME="$FAKE_HOME" DISPLAY="$XVFB_DISPLAY" dbus-run-session -- \
            /usr/bin/python3 "$LIBDIR/screen_tray.py" >"$FAKE_HOME/tray.log" 2>&1 &
        TPID=$!
        PIDS+=($TPID)
        sleep 5
        if kill -0 "$TPID" 2>/dev/null \
           && [ -f "$FAKE_HOME/.config/screen-round/settings.json" ]; then
            ok "托盘能起来并写下 settings.json"
        else
            bad "托盘没起来：$(tr '\n' ' ' <"$FAKE_HOME/tray.log" | tail -c 300)"
        fi
    else
        skip "缺 dbus-run-session / python3-gi，跳过托盘检查"
    fi
fi

echo
if [ "$FAIL" = "0" ]; then
    echo "==> 冒烟测试通过：$(basename "$DEB")"
else
    echo "==> 冒烟测试失败" >&2
fi
exit "$FAIL"
