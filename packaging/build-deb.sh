#!/usr/bin/env bash
# 把 screen-round 打成一个 .deb，方便拷到别的机器上装。
#
# 不依赖 debhelper（本机没装 dh）：手工铺一棵安装树，再交给 dpkg-deb 打包。
# 产物：dist/screen-round_<版本>_all.deb
# 用法：packaging/build-deb.sh
set -euo pipefail

PKG="screen-round"
# 版本号优先级：命令行第一个参数 > 环境变量 SCREEN_ROUND_VERSION > 下面的默认值。
# 推 tag 时 GitHub Actions 会用环境变量把版本号传进来（v1.2.3 → 1.2.3）。
VERSION="${1:-${SCREEN_ROUND_VERSION:-0.1.0}}"
ARCH="all"
MAINTAINER="purePulsar <purePulsar@users.noreply.github.com>"
HOMEPAGE="https://github.com/purePulsar/screen-round"

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname -- "$HERE")"
DIST="$ROOT/dist"
STAGE="$ROOT/build/${PKG}_${VERSION}_${ARCH}"

LIBDIR="/usr/lib/$PKG"
APPDIR="/usr/share/applications"
ICONDIR="/usr/share/icons/hicolor/scalable/apps"
DOCDIR="/usr/share/doc/$PKG"

# 三个 .py 必须待在同一个目录里：自启项的 Exec 和"重启遮罩"都是按 __file__
# 现算出同目录的 screen_mask.py / screen_tray.py 的。
rm -rf -- "$STAGE"
mkdir -p -- "$STAGE$LIBDIR" "$STAGE/usr/bin" "$STAGE$APPDIR" \
           "$STAGE$ICONDIR" "$STAGE$DOCDIR" "$STAGE/DEBIAN"

install -m 0644 "$ROOT/corner_common.py" "$STAGE$LIBDIR/corner_common.py"
install -m 0755 "$ROOT/screen_mask.py"   "$STAGE$LIBDIR/screen_mask.py"
install -m 0755 "$ROOT/screen_tray.py"   "$STAGE$LIBDIR/screen_tray.py"

install -m 0755 "$HERE/bin/screen-round"      "$STAGE/usr/bin/screen-round"
install -m 0755 "$HERE/bin/screen-round-mask" "$STAGE/usr/bin/screen-round-mask"

install -m 0644 "$HERE/applications/screen-round.desktop" "$STAGE$APPDIR/screen-round.desktop"
install -m 0644 "$HERE/icons/screen-round.svg"            "$STAGE$ICONDIR/screen-round.svg"

install -m 0644 "$HERE/copyright" "$STAGE$DOCDIR/copyright"
install -m 0644 "$ROOT/README.md" "$STAGE$DOCDIR/README.md"

# 包里 changelog 的版本必须跟包本身的版本对得上。tag 触发的构建版本号来自 tag，
# packaging/changelog 未必同步改过，对不上就在最前面补一条（相当于自动 dch）。
CHANGELOG_SRC="$HERE/changelog"
TOP_VERSION="$(sed -n '1s/^[^(]*(\([^)]*\)).*/\1/p' "$CHANGELOG_SRC")"
if [ "$TOP_VERSION" = "$VERSION" ]; then
    # -n：不要把时间戳塞进 gzip 头，同一个源重复打出来的包才是同一份
    gzip -9n -c "$CHANGELOG_SRC" > "$STAGE$DOCDIR/changelog.gz"
else
    {
        printf '%s (%s) unstable; urgency=medium\n\n' "$PKG" "$VERSION"
        printf '  * 由标签触发的自动构建。\n\n'
        printf ' -- %s  %s\n\n' "$MAINTAINER" "$(date -R)"
        cat "$CHANGELOG_SRC"
    } | gzip -9n -c > "$STAGE$DOCDIR/changelog.gz"
fi
chmod 0644 "$STAGE$DOCDIR/changelog.gz"

install -m 0755 "$HERE/postinst" "$STAGE/DEBIAN/postinst"

INSTALLED_SIZE="$(du -k -s --exclude=DEBIAN "$STAGE" | cut -f1)"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Architecture: $ARCH
Maintainer: $MAINTAINER
Installed-Size: $INSTALLED_SIZE
Depends: python3, python3-tk, python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, libx11-6, libxext6
Section: utils
Priority: optional
Homepage: $HOMEPAGE
Description: 屏幕四角圆角遮罩 —— X11 四角挖圆 + 托盘控制
 让整块显示看起来像一块圆角屏幕：四个小窗口贴在屏幕四角，用 X11 Shape 把
 靠屏幕内侧的四分之一圆挖掉，剩下的那一小条正好盖住屏幕原来的直角。
 .
 遮罩窗口完全鼠标穿透、压在面板之上；托盘常驻，可调半径档位、开关遮罩、
 切换开机自启、重启或退出遮罩。需要 X11 会话——Wayland 下 X11 形状不生效。
EOF

# 逐文件校验和，装完能核对完整性
( cd "$STAGE" && find . -type f ! -path './DEBIAN/*' -printf '%P\n' \
    | sort | xargs -d '\n' md5sum > DEBIAN/md5sums )

mkdir -p -- "$DIST"
OUT="$DIST/${PKG}_${VERSION}_${ARCH}.deb"
# -Zxz：zstd 包在 Ubuntu 20.04 / Debian 11 上 dpkg 不认，xz 到处都能装
dpkg-deb --build --root-owner-group -Zxz "$STAGE" "$OUT"

echo
echo "==> $OUT"
dpkg-deb --info "$OUT" | sed -n '1,22p'
