# screen-round — 屏幕四角圆角遮罩

让整块显示看起来像一块圆角屏幕：四个小窗口贴在屏幕四角，用 X11 Shape 把靠屏幕内侧的
四分之一圆挖掉，剩下的那一小条正好盖住屏幕原来的直角。托盘常驻，可调半径、开关、自启。

## 文件

| 文件 | 作用 |
|------|------|
| `screen_mask.py` | 遮罩本体：4 个 `r×r` 窗口贴住四角，裁圆角，每 0.8 秒读一次设置 |
| `screen_tray.py` | 托盘：启用/停用、半径档位、开机自启、重启遮罩、退出 |
| `corner_common.py` | 两进程共用：设置读写、半径钳制、flock 单实例、自启项生成。**只用标准库** |

两个进程互相独立，只通过 `~/.config/screen-round/settings.json` 通信，谁先启动都行。
**只有托盘写设置，遮罩只读。**

## 运行

```bash
python3 screen_mask.py     # 遮罩（无边框、完全鼠标穿透，没有自己的菜单）
python3 screen_tray.py     # 托盘 —— 遮罩唯一的控制面
```

依赖：`python3-tk`、`python3-gi`、`gir1.2-ayatanaappindicator3-0.1`（本机已装）。

## 打包成 .deb（拷到别的机器上装）

```bash
packaging/build-deb.sh          # 产物：dist/screen-round_0.1.0_all.deb
```

装到另一台 Ubuntu / Zorin / Debian 系机器（要 X11 会话）：

```bash
sudo apt install ./screen-round_0.1.0_all.deb    # 用 apt 才会自动补依赖
# 用 dpkg -i 也行，装完记得 sudo apt -f install
```

装完在应用菜单里搜「屏幕圆角遮罩」，命令行则是 `screen-round`（拉托盘）；
`screen-round-mask` 只起遮罩本体，正常不用手动跑。

包里的东西：

| 路径 | 内容 |
|------|------|
| `/usr/lib/screen-round/*.py` | 三个脚本。**必须同目录**——自启项的 Exec 和「重启遮罩」都是按 `__file__` 现算同目录路径的 |
| `/usr/bin/screen-round` | 托盘启动器（唯一控制面） |
| `/usr/bin/screen-round-mask` | 只起遮罩本体 |
| `/usr/share/applications/screen-round.desktop` | 应用菜单入口 |
| `/usr/share/icons/hicolor/scalable/apps/screen-round.svg` | 图标 |

包**不预置自启项**。`~/.config/autostart/` 下那两个 desktop 文件由托盘的「开机自启」
现生成——包里再放一份，登录就会启动两次。装完请用托盘里的开关自启。

依赖由 apt 自动装：`python3-tk`、`python3-gi`、`gir1.2-gtk-3.0`、
`gir1.2-ayatanaappindicator3-0.1`、`libx11-6`、`libxext6`。
GNOME 系桌面还要开「AppIndicator and KStatusNotifierItem Support」扩展，否则托盘图标不显示。
Wayland 会话下 X11 形状不生效，必须 X11。

打包脚本本身只用 `dpkg-deb`，不需要 debhelper；包压成 xz（不用 zstd），
Ubuntu 20.04 / Debian 11 那种老 dpkg 也装得上。要改版本号改 `packaging/build-deb.sh` 顶部，
顺手在 `packaging/changelog` 里补一条。

### 发版（GitHub Actions）

推一个 `v*` 标签就全自动了——打包、把包拆开真跑一遍、建 Release、挂上 `.deb` 和 `SHA256SUMS`：

```bash
git tag v0.1.1 && git push origin v0.1.1
```

版本号取自标签（`v0.1.1` → `0.1.1`），不用手改脚本；`packaging/changelog` 忘了同步也不要紧，
构建时发现对不上会在最前面自动补一条。Actions 页面手动触发 `Release` 也能打包，
但只留成 artifact、不发 Release。本地想跑同一套检查：`packaging/smoke-test.sh dist/*.deb`。

## 托盘菜单

| 菜单项 | 行为 |
|--------|------|
| 启用圆角遮罩（勾选） | 写 `enabled`，遮罩 withdraw / deiconify，进程不重启 |
| 圆角半径 ▸（单选） | 8 / 12 / 16 / 20 / 24 / 32 px，写 `radius`，150ms 去抖 |
| 开机自启（勾选） | 生成 / 删除 `~/.config/autostart/` 下的两个 desktop 文件 |
| 重启遮罩 | SIGTERM 旧进程，等它让出单实例锁，再拉起新的（假死时的救命项） |
| 退出圆角遮罩 | 只结束遮罩，托盘继续留着 |
| 退出托盘 | 只退托盘，遮罩继续运行 |

## 设置文件

```json
{ "enabled": true, "radius": 16 }
```

由托盘写入；`radius` 会被钳到 4–40，非法值回退到 16。
托盘只在**启动时**读一次、之后每次改动都写回整份文件，所以手改这个文件只在托盘本次运行
期间有效——下次你动托盘菜单，就会被托盘内存里的旧值盖掉。要长期用档位以外的半径，得先
给托盘加一步"写之前重读文件"。

## 这台机器上实测出来的结论（改代码前先看）

### 1. 窗口必须用 override-redirect，`-type dock` 不行

四角都落在 Zorin 的面板上，所以遮罩必须压在面板之上。实测对照（同尺寸窗口摆在底栏位置上，
看像素有没有变成遮罩色）：

| 窗口属性 | 能否压住底栏 `zorin-taskbar` |
|----------|------------------------------|
| `-type dock` + `-topmost` | ✗（能压住上面板，压不住底栏） |
| `-type dock`（去掉 topmost） | ✗ |
| `notification` / `splash` / `utility` / `normal` / `menu` / `tooltip` | ✗ |
| **override-redirect**（`overrideredirect(True)`） | **✓** |

override-redirect 是 Mutter 里最高的窗口层，底栏、面板一律盖得住。
代价是不再受 WM 管理——对本用途反而是好事，位置不会被 WM 改动。
注意 override-redirect 下 `-topmost` 不起作用，也不需要。

### 2. Tk 的每个 Toplevel 在 X 上是**两层**窗口，形状必须两层都裁

外层 wrapper（`xwininfo -root -tree` 里那个、带 WM_NAME 的）和内层（`winfo_id()` 返回的）。
只裁内层的话，不受 WM 管理时 wrapper 会拿自己的背景色把圆角盖成直角方块——实测内层 region
已经是 254 像素的圆弧，屏幕上看却是个 32×32 黑方块。所以 `apply_corner_mask()` 会把同一个
形状套在 `winfo_id()` 到 root 之间的每一层上（`_window_chain()`）。

### 3. X11 shape 的几个真正常量（disk-card 里的是错的）

`ShapeBounding=0`、`ShapeClip=1`、`ShapeInput=2`；操作 `ShapeSet=0`、`Union=1`、`Intersect=2`、
`Subtract=3`、`Invert=4`。实测（新窗口 + region 读回）：op=0 → 区域正确；op=8 → 两个 BadValue
且区域不变；op=3 → 正好等于「整窗口减去形状区域」。

`../disk-card/disk_viewer.py` 把 `_SHAPE_SET` 写成 8、把 `_SHAPE_INPUT` 写成 1（其实是
`ShapeClip`），**所以那边卡片的圆角从来没生效过**。本项目没去改它，但别再照抄那两个常量。

### 4. 圆弧别交给 `XFillArc` 光栅化

四个角的圆弧是**自己按像素中心采样**算出来的（`_corner_rects()`），不能交给 `XFillArc`
让 X 代劳。X 是拿像素的**左上角**去判在不在圆里的，而"取左上角"这条规则在 180° 旋转下
不是不变量（旋转会把一个像素的左上角变成对角像素的右下角）；四个圆弧的圆心又都正好落在
屏幕边线上，于是顶边的圆角比底边深了整整一行。实测四个角的遮罩像素数：左上 73、右上 57、
左下 57、右下 42——而按像素中心采样，四角都应当是 53。换掉之后四个角实测都是 53，
180° 旋转 / 左右镜像 / 上下镜像三项对称性检查全部相等。

顺带这也把 pixmap + GC 那一整套删掉了：`XShapeCombineRectangles` 直接吃矩形列表，输入区
给一个**空列表**就是空区域（鼠标穿透），不再需要那两张 1 位 pixmap。

### 5. 鼠标穿透

把 `ShapeInput` 单独置成一个 1×1 全 0 的遮罩，输入区就是空的。实测有效：点四角会直接穿到
底栏去（点左下角能打开应用网格）。

### 6. 还没上屏的窗口裁形状会被顶回来

对未映射的窗口调 `XShapeCombineMask`，X 会用 BadMatch 拒绝，形状静默不生效，窗口就变成
一个直角黑方块。所以 `_mask()` 里必须等 `<Map>` 之后再裁。

### 7. 别的坑

- 单实例用 `fcntl.flock`，不用裸 pidfile（pid 会被系统复用，判断是错的）。
- **结束遮罩不要用 `pkill -f screen_mask.py`**：那个模式串会连发起命令自己的命令行一起匹配，
  把调用者也杀掉。用锁文件里的 pid：`kill $(cat ~/.config/screen-round/mask.lock)`。
- 自启项由托盘**按脚本绝对路径现算**生成，项目里没有预置副本。两处都有的话登录会启动两次。

## 已知限制

- **硬边**：X11 形状是 1 位遮罩，圆弧没有抗锯齿。要平滑得改用 GTK3 + cairo 逐像素透明窗口，
  会放弃这套已验证可用的窗口定位路径，不划算。
- **颜色固定纯黑**：Zorin 面板是 `rgba(26,33,37,0.7)` 半透明、随壁纸变色，无法精确同色，
  纯黑当「显示器黑边框」最自然。常量在 `screen_mask.py: MASK_COLOR`。
- **多显示器**：`winfo_screenwidth/height` 是所有输出取并集，只有整个并集的四个角会是圆的。
  本机单显示器，没做也不打算做逐输出处理。
- **外壳的整屏层盖在遮罩之上**：实测打开应用网格时四角恢复成直角（露出概览的底色）。
  锁屏同理。这不是缺陷——概览/锁屏本来就铺满全屏，四角留黑反而怪。
- **全屏窗口**：按 Mutter 的窗口分层，override-redirect 在全屏层之上，理论上遮罩仍然可见；
  **没有实测**。
- **锁屏解锁后是否掉层**：**没有实测**。真掉了的话，加一个周期性重设窗口属性即可，
  但没确认之前不加（每几秒重设窗口类型有实打实的闪屏风险）。
- **录屏/截图会把黑圆角一起录进去**，这是遮罩的固有性质。
- 登录时会有一帧直角闪现（窗口要先上屏才能裁形状）。

## 常量

| 常量 | 位置 | 值 | 说明 |
|------|------|----|------|
| `MASK_COLOR` | screen_mask.py | `#000000` | 遮罩颜色 |
| `POLL_MS` | screen_mask.py | 800 | 轮询设置/分辨率的间隔 |
| `SETTLE_MS` | screen_mask.py | 200 | 窗口落定后补一次摆位的延迟 |
| `R_DEFAULT / R_MIN / R_MAX` | corner_common.py | 16 / 4 / 40 | 半径默认值与钳制范围 |
| `R_PRESETS` | corner_common.py | 8,12,16,20,24,32 | 托盘菜单档位 |
```
