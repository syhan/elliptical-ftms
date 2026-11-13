#!/usr/bin/env python3
"""网络 TUI：用 nc / telnet 连进来就能看椭圆机的实时数据。

设计要点
--------
**终端尺寸从哪来。** 裸 TCP 连接没有终端尺寸这种概念，所以：

- 客户端是 `telnet` 时，标准 NAWS 协商（option 31）会带过来行列数；
- 客户端是 `nc` 时，发 XTWINOPS 查询 `ESC[18t`，终端会把
  `ESC[8;行;列t` 写回自己的输入流，于是顺着 nc 传回服务端。

两者都拿不到就退回 80x24。连上后会定期重新查询，所以中途改窗口大小
也能跟上（重新连一次也行）。

**为什么不用 curses。** curses 需要控制真实的 tty；这里是对着若干条
socket 同时渲染，自己拼 ANSI 反而更简单，也方便把 render() 写成纯函数
单独测试。

用法
----
    python3 ftms_tui.py --demo            # 假数据，本地体验
    nc 10.0.0.177 2323                    # 看真实数据

按 q 退出，Ctrl+C 断开也行。
"""

from __future__ import annotations

import argparse
import random
import re
import select
import socket
import sys
import threading
import unicodedata
import time
from collections import deque

# --------------------------------------------------------------------------- #
# ANSI 与宽度
# --------------------------------------------------------------------------- #

CSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

RESET = "\x1b[0m"

PALETTE = {
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "grey": "\x1b[90m",
    "red": "\x1b[91m",
    "green": "\x1b[92m",
    "yellow": "\x1b[93m",
    "blue": "\x1b[94m",
    "magenta": "\x1b[95m",
    "cyan": "\x1b[96m",
    "white": "\x1b[97m",
}


def char_width(ch: str) -> int:
    """终端里占几列。中文/全角是 2 列，组合符是 0 列。"""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def vlen(text: str) -> int:
    """可见宽度（去掉 ANSI 转义，按实际列数算）。"""
    return sum(char_width(ch) for ch in CSI_RE.sub("", text))


def paint(text: str, *styles: str) -> str:
    codes = "".join(PALETTE.get(s, "") for s in styles)
    return f"{codes}{text}{RESET}" if codes else text


def clip(text: str, width: int) -> str:
    """按可见宽度截断，保留 ANSI 序列（否则颜色会把后续内容吃掉）。"""
    if vlen(text) <= width:
        return text
    out = []
    used = 0
    i = 0
    while i < len(text):
        match = CSI_RE.match(text, i)
        if match:
            out.append(match.group(0))
            i = match.end()
            continue
        step = char_width(text[i])
        if used + step > width:
            break
        out.append(text[i])
        used += step
        i += 1
    tail = "".join(out)
    return tail + RESET if "\x1b" in tail else tail


def ljust(text: str, width: int) -> str:
    gap = width - vlen(text)
    return text + " " * gap if gap > 0 else text


def center(text: str, width: int) -> str:
    gap = width - vlen(text)
    if gap <= 0:
        return text
    left = gap // 2
    return " " * left + text + " " * (gap - left)


# 七段风格的大号数字，每个字符 4 列 x 5 行
BIG_FONT = {
    "0": ("████", "█  █", "█  █", "█  █", "████"),
    "1": ("   █", "   █", "   █", "   █", "   █"),
    "2": ("████", "   █", "████", "█   ", "████"),
    "3": ("████", "   █", "████", "   █", "████"),
    "4": ("█  █", "█  █", "████", "   █", "   █"),
    "5": ("████", "█   ", "████", "   █", "████"),
    "6": ("████", "█   ", "████", "█  █", "████"),
    "7": ("████", "   █", "   █", "   █", "   █"),
    "8": ("████", "█  █", "████", "█  █", "████"),
    "9": ("████", "█  █", "████", "   █", "████"),
    ".": ("    ", "    ", "    ", "    ", "██  "),
    ":": ("    ", " ██ ", "    ", " ██ ", "    "),
    "-": ("    ", "    ", "████", "    ", "    "),
    " ": ("    ", "    ", "    ", "    ", "    "),
}

BIG_ROWS = 5
BIG_COLS = 4


BIG_GAP = 1          # 字间留白，否则小数点会和相邻数字粘在一起


def big(text: str) -> list[str]:
    """把字符串渲染成 5 行大字，每个字符之间留 BIG_GAP 列。"""
    rows = ["", "", "", "", ""]
    pad = " " * BIG_GAP
    for ch in text:
        glyph = BIG_FONT.get(ch, BIG_FONT[" "])
        for i in range(BIG_ROWS):
            rows[i] += glyph[i] + pad
    return rows


def big_width(text: str) -> int:
    return len(text) * (BIG_COLS + BIG_GAP)


# --------------------------------------------------------------------------- #
# 实时状态
# --------------------------------------------------------------------------- #

class LiveState:
    """采集进程写、TUI 线程读的共享快照。"""

    def __init__(self, history: int = 600) -> None:
        self._lock = threading.Lock()
        self._ver = 0
        self.device = ""
        self.session = ""
        self.started = False
        self.elapsed = 0
        self.speed = 0.0
        self.avg_speed = 0.0
        self.cadence = 0
        self.resistance = 0.0
        self.power = 0
        self.distance = 0
        self.kcal = 0.0
        self.hr = 0
        self.hr_at = 0.0
        self.frame_at = 0.0
        self.hist_speed: deque[float] = deque(maxlen=history)
        self.hist_hr: deque[int] = deque(maxlen=history)

    def update_ftms(self, frame, hr: int = 0, kcal: float = 0.0,
                    started: bool = True, session: str = "") -> None:
        with self._lock:
            self._ver += 1
            self.started = started
            if session and self.session and session != self.session:
                # 换了一局，曲线不能和上一局接在一起
                self.hist_speed.clear()
                self.hist_hr.clear()
            if session:
                self.session = session
            self.elapsed = frame.elapsed_s
            self.speed = frame.speed_kmh
            self.avg_speed = frame.avg_speed_kmh
            self.cadence = frame.cadence_spm
            self.resistance = frame.resistance_pct
            self.power = frame.power_w
            self.distance = frame.distance_m
            self.kcal = kcal
            self.frame_at = time.time()
            self.hist_speed.append(float(frame.speed_kmh))
            if hr:
                self.hr = hr
                self.hr_at = time.time()
                self.hist_hr.append(int(hr))

    def update_hr(self, bpm: int) -> None:
        with self._lock:
            self._ver += 1
            self.hr = bpm
            self.hr_at = time.time()

    def set_device(self, label: str) -> None:
        with self._lock:
            self.device = label
            self._ver += 1

    def set_idle(self) -> None:
        """一局结束后清空读数。

        只改 started 是不够的：设备关机后 TUI 会一直显示上一局冻结的
        距离/时间，第二天连进来还以为数据没清零。
        """
        with self._lock:
            self.started = False
            self.elapsed = 0
            self.speed = 0.0
            self.avg_speed = 0.0
            self.cadence = 0
            self.resistance = 0.0
            self.power = 0
            self.distance = 0
            self.kcal = 0.0
            self.hr = 0
            self.frame_at = 0.0
            self.hr_at = 0.0
            self.hist_speed.clear()
            self.hist_hr.clear()
            self.session = ""
            self._ver += 1

    def snapshot(self) -> tuple[int, dict]:
        with self._lock:
            now = time.time()
            return self._ver, {
                "device": self.device,
                "session": self.session,
                "started": self.started,
                "age": now - self.frame_at if self.frame_at else None,
                "elapsed": self.elapsed,
                "speed": self.speed,
                "avg_speed": self.avg_speed,
                "cadence": self.cadence,
                "resistance": self.resistance,
                "power": self.power,
                "distance": self.distance,
                "kcal": self.kcal,
                "hr": self.hr,
                "hr_age": now - self.hr_at if self.hr_at else None,
                "hist_speed": list(self.hist_speed),
                "hist_hr": list(self.hist_hr),
                "now": now,
            }


LIVE = LiveState()


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #

STALE_AFTER = 15.0          # 超过这个秒数没数据就认为设备不在了


def _is_stale(snap: dict) -> bool:
    age = snap.get("age")
    return age is None or age > STALE_AFTER


def _status_of(snap: dict) -> tuple[str, str]:
    age = snap["age"]
    if age is None or age > 15:
        return "等待椭圆机", "grey"
    if not snap["started"]:
        return "待踩动", "yellow"
    if age > 3:
        return "信号弱", "yellow"
    return "记录中", "green"


def _fmt_clock(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _spark(values: list[float], width: int) -> str:
    """单行迷你图，8 级灰度。"""
    blocks = "▁▂▃▄▅▆▇█"
    if not values or width <= 0:
        return " " * width
    vals = values[-width:]
    peak = max(vals) or 1.0
    low = min(vals)
    if peak - low < 1e-9:                 # 数值恒定，别画成一堵实心墙
        return (blocks[len(blocks) // 2] * len(vals)).rjust(width)
    out = "".join(blocks[min(len(blocks) - 1,
                             int((v - low) / (peak - low) * (len(blocks) - 1)))]
                  for v in vals)
    return out.rjust(width)


def _area(values: list[float], width: int, height: int,
          vmax: float | None = None) -> list[str]:
    """用块字符画一块填充面积图，返回 height 行。"""
    if width <= 0 or height <= 0:
        return [""] * max(0, height)
    vals = list(values)[-width:] if values else [0.0]
    peak = vmax or (max(vals) if vals and max(vals) > 0 else 1.0)
    lines = []
    for row in range(height, 0, -1):
        threshold = peak * row / height
        lines.append("".join("█" if v >= threshold else " " for v in vals).ljust(width))
    return lines


def _rule(width: int, left: str = "├", right: str = "┤") -> str:
    inner = max(0, width - 2)
    return left + "─" * inner + right


def _title_line(width: int, title: str, date: str, wall: str, status: str,
                color: bool = True) -> str:
    """标题栏。

    右边从「日期 时间 状态」开始，宽度不够就依次丢掉日期、状态 ——
    时间永远保留，否则窄终端上就看不到现在几点了。
    """
    wall_txt = paint(wall, "bold", "white") if color else wall
    for right in (f"{date} {wall_txt} {status}", f"{wall_txt} {status}", wall_txt):
        suffix_len = 1 + vlen(right) + 2
        if width - len("┌─ ") - 1 - suffix_len >= 8:
            return _titled(width, title, right)
    return _titled(width, "", wall_txt)


def _titled(width: int, title: str, right_text: str = "") -> str:
    prefix = "┌─ "
    suffix = f" {right_text} ┐" if right_text else " ┐"
    room = width - len(prefix) - 1 - vlen(suffix)
    if room < 0:                                  # 右边内容太长，丢掉标题
        suffix = " ┐"
        room = width - len(prefix) - 1 - vlen(suffix)
    title = clip(title, max(0, room))
    fill = max(0, room - vlen(title))
    return prefix + title + " " + "─" * fill + suffix


def _row(content: str, width: int) -> str:
    inner = max(0, width - 2)
    return "│" + ljust(clip(content, inner), inner) + "│"


def _box_lines(lines: list[str], width: int) -> list[str]:
    return [_row(" " + line, width) for line in lines]


def render_compact(snap: dict, cols: int, rows: int, color: bool = True) -> list[str]:
    """窄屏 / 矮屏：一行或几行纯文本。"""
    status, scolor = _status_of(snap)
    stale = _is_stale(snap)
    hr = snap["hr"] if not stale else 0
    if stale:
        parts = ["-- km/h", "-- spm", "-- W", "-- m"]
    else:
        parts = [
            f"{snap['speed']:5.2f}km/h",
            f"{snap['cadence']:3d}spm",
            f"{snap['resistance']:4.0f}%",
            f"{snap['power']:4d}W",
            f"{snap['distance']:5d}m",
            _fmt_clock(snap["elapsed"]),
        ]
    parts.append(time.strftime("%H:%M"))
    if hr:
        parts.append(f"♥{hr}")
    # 按宽度贪心换行，尽量用满可用行数
    lines: list[str] = []
    current = ""
    for part in parts:
        if current and vlen(current) + 1 + vlen(part) > cols:
            lines.append(current)
            current = part
        else:
            current = f"{current} {part}".strip()
        if len(lines) >= rows - 1:
            break
    if current:
        lines.append(current)
    if len(lines) < rows:
        lines.append(paint(status, scolor) if color else status)
    return [ljust(clip(line, cols), cols) for line in lines[:rows]]


def render_medium(snap: dict, cols: int, rows: int, color: bool = True) -> list[str]:
    """中等屏幕：有边框，没有大字。"""
    status, scolor = _status_of(snap)
    stale = _is_stale(snap)
    hr = snap["hr"]
    has_hr = hr > 0 and not stale

    title = snap["device"] or "FTMS"
    status_txt = paint(status, scolor) if color else status
    out = [_title_line(cols, title, time.strftime("%m-%d"),
                       time.strftime("%H:%M:%S"), status_txt, color)]

    # 主数值行
    inner = cols - 2
    num = "--" if stale else f"{snap['speed']:6.2f}"
    left = (paint(num, "bold", "cyan") if color else num) + " km/h"
    heart = _heart(has_hr, snap["now"], color)
    if has_hr:
        hnum = f"{hr}"
        right = heart + " " + (paint(hnum, "bold", "red") if color else hnum) + " bpm"
    else:
        right = heart + " -- bpm"
    gap = max(1, inner - 1 - vlen(left) - vlen(right))
    out.append(_row(" " + left + " " * gap + right, cols))

    out.append(_rule(cols))
    cad = "--" if stale else f"{snap['cadence']:3d}"
    res = "--" if stale else f"{snap['resistance']:3.0f}"
    pwr = "--" if stale else f"{snap['power']:3d}"
    dist = "--" if stale else f"{snap['distance']:5d}"
    tme = "--" if stale else _fmt_clock(snap["elapsed"])
    kcal = "--" if stale else f"{snap['kcal']:.0f}"
    out.append(_row(" " + f"步频 {cad} spm   阻力 {res}%   功率 {pwr} W", cols))
    out.append(_row(" " + f"距离 {dist} m   时间 {tme}   卡路里 {kcal}", cols))
    out.append(_rule(cols))

    # 剩下的空间给迷你图
    left_rows = max(0, rows - len(out) - 1)
    if left_rows > 0 and snap["hist_speed"] and not stale:
        speed_line = _spark(snap["hist_speed"], inner - 8)
        out.append(_row(" 速度 " + (paint(speed_line, "cyan") if color else speed_line), cols))
        left_rows -= 1
    if left_rows > 0 and snap["hist_hr"]:
        hr_line = _spark([float(v) for v in snap["hist_hr"]], inner - 8)
        out.append(_row(" 心率 " + (paint(hr_line, "red") if color else hr_line), cols))
        left_rows -= 1

    out.append(_rule(cols, "└", "┘"))
    while len(out) < rows:
        out.insert(len(out) - 1, _row("", cols))
    return out[:rows]


CELL_ROWS = 5


def _fit_big(value: str, width: int) -> str | None:
    """放得下就返回大字用的字符串，放不下返回 None（退回普通文本）。"""
    if big_width(value) <= width:
        return value
    if "." in value:
        head = value.split(".")[0]
        if big_width(head) <= width:
            return head
    return None


RES_LEVELS = " ▁▂▃▄▅▆▇█"


def res_meter(pct: float, cells: int = 10) -> list[tuple[str, bool]]:
    """阻力格栅：高度递增的一排小竖条，像信号强度那样。

    未点亮的格子统一画最低那一格 ▁，所以即使在无色终端里也能一眼看出
    点亮到第几格（否则未点亮也画自己的高度，看着永远是一整条楼梯）。
    """
    pct = max(0.0, min(100.0, pct))
    lit = int(round(pct / 100.0 * cells))
    out = []
    for i in range(cells):
        if i < lit:
            height = 1 + round(i * (len(RES_LEVELS) - 2) / max(1, cells - 1))
            out.append((RES_LEVELS[height], True))
        else:
            out.append((RES_LEVELS[1], False))
    return out


def _heart(has_hr: bool, now: float, color: bool) -> str:
    """有心率时红心按 0.8 秒一拍闪，没有心率就灰色常亮。"""
    if not has_hr:
        return paint("♥", "grey") if color else "♥"
    if int(now * 1.25) % 2 == 0:
        return paint("♥", "red", "bold") if color else "♥"
    return " "


def _band_lines(cells: list[dict], inner: int, color: bool, now: float) -> list[str]:
    """一排读数：5 行大字 + 1 行标签/仪表。"""
    n = len(cells)
    base = inner // n
    widths = [base] * (n - 1) + [inner - base * (n - 1)]

    body: list[str] = []
    for r in range(CELL_ROWS):
        parts = []
        for i, cell in enumerate(cells):
            w = widths[i]
            shown = _fit_big(cell["value"], w)
            if shown is None and cell.get("fallback"):
                shown = _fit_big(cell["fallback"], w)
            if shown is None:
                text = cell["value"] if r == CELL_ROWS // 2 else ""
                parts.append(ljust(clip(center(text, w), w), w))
                continue
            text = center(big(shown)[r], w)
            style = cell.get("style")
            parts.append(paint(text, style) if (color and style) else text)
        body.append("".join(parts))

    parts = []
    for i, cell in enumerate(cells):
        w = widths[i]
        if cell.get("meter") is not None:
            label = cell.get("label", "")
            cell_count = 10 if w >= 18 else 5
            seg = "".join(
                (paint(ch, "green", "bold") if on else paint(ch, "grey")) if color else ch
                for ch, on in res_meter(cell["meter"], cell_count)
            )
            text = center(f"{label} {seg}" if label else seg, w)
        else:
            label = cell.get("label", "")
            unit = cell.get("unit", "")
            heart = ""
            if cell.get("heart"):
                heart = _heart(cell.get("has_hr", False), now, color) + " "
            text = center(f"{heart}{label} {unit}".rstrip(), w)
        parts.append(clip(text, w) if vlen(text) > w else text)
    body.append("".join(parts))
    return body


def render_full(snap: dict, cols: int, rows: int, color: bool = True) -> list[str]:
    """大屏：全部指标都上大字，图表只占底下一点点。"""
    status, scolor = _status_of(snap)
    hr = snap["hr"]
    has_hr = hr > 0
    now = snap["now"]

    title = snap["device"] or "FTMS"
    if snap["session"]:
        title += f"  ·  {snap['session']}"
    wall = time.strftime("%H:%M:%S")
    date = time.strftime("%m-%d")
    status_txt = paint(status, scolor) if color else status
    out = [_title_line(cols, title, date, wall, status_txt, color)]
    inner = cols - 2

    stale = _is_stale(snap)
    has_hr = has_hr and not stale

    def val(text: str) -> str:
        """数据过期就显示占位符，别把上一局的数值一直挂在屏幕上。"""
        return "--" if stale else text

    clock = _fmt_clock(snap["elapsed"])
    minutes = str(int(snap["elapsed"]) // 60)

    band1 = [
        {"label": "瞬时速度", "value": val(f"{snap['speed']:.2f}"), "unit": "km/h",
         "style": "cyan"},
        {"label": "心率", "value": str(hr) if has_hr else "--", "unit": "bpm",
         "style": "red" if has_hr else "grey", "heart": True, "has_hr": has_hr},
    ]
    band2 = [
        {"label": "步频", "value": val(str(snap["cadence"])), "unit": "spm",
         "style": "yellow"},
        {"label": "阻力", "value": val(f"{snap['resistance']:.0f}"), "unit": "%",
         "style": "green", "meter": 0.0 if stale else snap["resistance"]},
        {"label": "功率", "value": val(str(snap["power"])), "unit": "W",
         "style": "magenta"},
    ]
    band3 = [
        {"label": "距离", "value": val(str(snap["distance"])), "unit": "m",
         "style": "blue"},
        {"label": "消耗", "value": val(f"{snap['kcal']:.0f}"), "unit": "kcal",
         "style": "red"},
    ]
    # 屏幕够高才有第四排。左上角那个大字本来就是瞬时速度，所以这里放
    # 用时和均速，跟第一排形成「瞬时 vs 平均」的对照，不重复。
    band4 = [
        {"label": "用时", "value": val(clock), "unit": "", "style": "white",
         "fallback": minutes},
        {"label": "均速", "value": val(f"{snap['avg_speed']:.2f}"), "unit": "km/h",
         "style": "cyan"},
    ]

    bands = [band1]
    if rows >= 17:
        bands.append(band2)
    if rows >= 23:
        bands.append(band3)
    if rows >= 29:
        bands.append(band4)

    for index, band in enumerate(bands):
        if index:
            out.append(_rule(cols))
        out.extend(_row(line, cols) for line in _band_lines(band, inner, color, now))

    # 图表只留给剩下的空间，最多 3 行
    remaining = min(3, rows - len(out) - 1)
    if remaining > 0 and snap["hist_speed"] and not stale:
        spark = _spark(snap["hist_speed"], inner - 7)
        out.append(_row(" 速度 " + (paint(spark, "cyan") if color else spark), cols))
        remaining -= 1
    if remaining > 0 and snap["hist_hr"] and not stale:
        spark = _spark([float(v) for v in snap["hist_hr"]], inner - 7)
        out.append(_row(" 心率 " + (paint(spark, "red") if color else spark), cols))

    while len(out) < rows - 1:
        out.append(_row("", cols))
    out.append(_rule(cols, "└", "┘"))
    return out[:rows]


def render(snap: dict, cols: int, rows: int, color: bool = True) -> list[str]:
    cols = max(16, int(cols))
    rows = max(2, int(rows))
    # 行末补满，保证整屏刷新不残留
    if rows < 8 or cols < 46:
        lines = render_compact(snap, cols, rows, color)
    elif rows < 18 or cols < 72:
        lines = render_medium(snap, cols, rows, color)
    else:
        lines = render_full(snap, cols, rows, color)
    return ([ljust(clip(line, cols), cols) for line in lines]
            + [" " * cols] * (rows - len(lines)))


# --------------------------------------------------------------------------- #
# 终端尺寸协商
# --------------------------------------------------------------------------- #

IAC, SB, SE = 0xFF, 0xFA, 0xF0
WILL, WONT, DO, DONT = 0xFB, 0xFC, 0xFD, 0xFE
OPT_ECHO, OPT_SGA, OPT_NAWS = 0x01, 0x03, 0x1F

XTWINOPS_RE = re.compile(rb"\x1b\[8;(\d+);(\d+)")
SIZE_QUERY = b"\x1b[18t"


class TermInfo:
    """从客户端输入流里抠出终端尺寸。"""

    def __init__(self) -> None:
        self.cols = 80
        self.rows = 24
        self.size_known = False
        self.is_telnet = False
        self._pending = b""

    # -- 解析 --------------------------------------------------------------- #

    def feed(self, data: bytes) -> bytes:
        """吞掉 telnet 协商与尺寸回报，返回剩下的按键字节。"""
        buf = self._pending + data
        self._pending = b""

        if IAC in buf:
            buf, leftover = self._strip_telnet(buf)
            if leftover is not None:
                self._pending = leftover
                buf += b""

        # XTWINOPS 回报
        for match in XTWINOPS_RE.finditer(buf):
            self.rows = int(match.group(1))
            self.cols = int(match.group(2))
            self.size_known = True
        buf = XTWINOPS_RE.sub(b"", buf)
        # 顺手清掉其它 CSI 序列，免得被当成按键
        buf = CSI_RE.sub("", buf.decode("latin-1")).encode("latin-1")
        return buf

    def _strip_telnet(self, buf: bytes) -> tuple[bytes, bytes | None]:
        self.is_telnet = True
        out = bytearray()
        i = 0
        n = len(buf)
        while i < n:
            byte = buf[i]
            if byte != IAC:
                out.append(byte)
                i += 1
                continue
            if i + 1 >= n:
                return bytes(out), buf[i:]          # 半个 IAC，留到下次
            cmd = buf[i + 1]
            if cmd == IAC:                          # 转义的 0xFF
                out.append(IAC)
                i += 2
            elif cmd in (WILL, WONT, DO, DONT):
                i += 3                              # IAC CMD OPT
            elif cmd == SB:
                end = buf.find(bytes([IAC, SE]), i + 2)
                if end < 0:
                    return bytes(out), buf[i:]
                payload = buf[i + 2:end]
                if payload and payload[0] == OPT_NAWS and len(payload) >= 5:
                    self.cols = (payload[1] << 8) | payload[2]
                    self.rows = (payload[3] << 8) | payload[4]
                    self.size_known = True
                i = end + 2
            else:
                i += 2
        return bytes(out), None

    # -- 协商报文 ----------------------------------------------------------- #

    def telnet_handshake(self) -> bytes:
        """只在确认对方是 telnet 后才发，否则 nc 会看到乱码。"""
        return bytes([IAC, WILL, OPT_ECHO,
                      IAC, WILL, OPT_SGA,
                      IAC, DO, OPT_NAWS])


# --------------------------------------------------------------------------- #
# 服务端
# --------------------------------------------------------------------------- #

FRAME_INTERVAL = 0.25          # 最快刷新间隔
SIZE_QUERY_INTERVAL = 3.0      # 定期复查终端尺寸


class TuiServer:
    def __init__(self, live: LiveState = LIVE, host: str = "0.0.0.0",
                 port: int = 2323, color: bool = True) -> None:
        self.live = live
        self.host = host
        self.port = port
        self.color = color
        self.sock: socket.socket | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.clients = 0
        self.peak = 0

    def start(self) -> "TuiServer":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(8)
        sock.settimeout(0.5)
        self.sock = sock
        thread = threading.Thread(target=self._serve, name="tui", daemon=True)
        thread.start()
        self._threads.append(thread)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def _serve(self) -> None:
        assert self.sock is not None
        while not self._stop.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.clients += 1
            self.peak = max(self.peak, self.clients)
            threading.Thread(target=self._client, args=(conn, addr),
                             name=f"tui-{addr[1]}", daemon=True).start()

    def _client(self, conn: socket.socket, addr) -> None:
        term = TermInfo()
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # 顺序很重要：先进备用屏，再发 telnet 协商（nc 会把它当乱码画出来），
            # 然后 2J 清掉，最后查尺寸。这样 nc 看不到任何伤痕。
            conn.sendall(b"\x1b[?1049h" + term.telnet_handshake()
                         + b"\x1b[2J\x1b[?25l" + SIZE_QUERY)
            last_render = 0.0
            last_query = time.monotonic()
            last_ver = -1
            # 先给 200ms 收集尺寸回报，拿不到就用默认值
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline and not term.size_known:
                if not self._poll(conn, term, 0.05):
                    return
            while not self._stop.is_set():
                if not self._poll(conn, term, 0.05):
                    break
                now = time.monotonic()
                if now - last_query > SIZE_QUERY_INTERVAL:
                    last_query = now
                    try:
                        conn.sendall(SIZE_QUERY)
                    except OSError:
                        break
                ver, snap = self.live.snapshot()
                if ver != last_ver or now - last_render > FRAME_INTERVAL:
                    last_ver = ver
                    last_render = now
                    lines = render(snap, term.cols, term.rows, self.color)
                    # \x1b[J 清掉上次残留（窗口缩小后底下会留旧行）
                    payload = "\x1b[H" + "\r\n".join(lines) + "\x1b[J"
                    try:
                        conn.sendall(payload.encode("utf-8", "replace"))
                    except OSError:
                        break
        except OSError:
            pass
        finally:
            try:
                conn.sendall(b"\x1b[0m\x1b[?25h\x1b[?1049l")
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
            self.clients = max(0, self.clients - 1)

    def _poll(self, conn: socket.socket, term: TermInfo, timeout: float) -> bool:
        """读一次输入；返回 False 表示该断开。"""
        try:
            ready, _, _ = select.select([conn], [], [], timeout)
        except (OSError, ValueError):
            return False
        if not ready:
            return True
        try:
            data = conn.recv(4096)
        except OSError:
            return False
        if not data:
            return False

        keys = term.feed(data)
        if b"q" in keys.lower() or b"\x03" in keys or b"\x1b" == keys[:1]:
            return False
        return True


# --------------------------------------------------------------------------- #
# 演示：假数据跑一个本地服务
# --------------------------------------------------------------------------- #

def demo(port: int = 2323) -> int:
    stop = threading.Event()

    def feeder() -> None:
        t0 = time.time()
        elapsed = 0
        distance = 0
        speed = 0.0
        while not stop.is_set():
            elapsed += 1
            if random.random() < 0.05:              # 偶尔歇一下
                speed = 0.0
            else:
                speed = max(0.0, min(16.0, speed + random.uniform(-1.2, 1.2)))
            distance += int(speed * 1000 / 3600)
            frame = type("F", (), {
                "elapsed_s": elapsed,
                "speed_kmh": speed,
                "avg_speed_kmh": speed,
                "cadence_spm": int(speed * 6.2) if speed > 0.3 else 0,
                "resistance_pct": 35.0,
                "power_w": int(speed * 11.5),
                "distance_m": distance,
            })()
            hr = int(110 + 14 * (speed / 12) + random.uniform(-3, 3)) if speed > 0.3 else 92
            LIVE.update_ftms(frame, hr, kcal=elapsed * 0.16,
                             started=True, session="demo")
            time.sleep(1)

    threading.Thread(target=feeder, daemon=True).start()
    server = TuiServer(LIVE, "0.0.0.0", port).start()
    print(f"* 演示服务已启动：nc 127.0.0.1 {port}   (Ctrl+C 退出)", flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.stop()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="椭圆机实时数据 TUI（nc / telnet）")
    parser.add_argument("--demo", action="store_true", help="用假数据本地体验")
    parser.add_argument("--port", type=int, default=2323, help="监听端口")
    args = parser.parse_args(argv)
    if args.demo:
        return demo(args.port)
    parser.error("需要 --demo（真实数据由 ftms_capture.py --tui-port 提供）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
