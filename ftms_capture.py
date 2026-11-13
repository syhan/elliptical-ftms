#!/usr/bin/env python3
"""YESOUL EF25 椭圆机 + BLE 心率带 — 数据采集器。

不依赖厂商 App，也不需要配对：直接通过 BlueZ 订阅 GATT 通知。

- 椭圆机：Cross Trainer Data `0x2ACE`（29 字节 / 1 Hz），协议见 PROTOCOL.md
- 心率：标准 Heart Rate Service `0x2A37`（COROS / Polar / 各种胸带通用）

两条流相位不对齐，所以 CSV 里保留「上一个心率 + 它的年龄」，
原始心率另存一份 1 Hz 全量 jsonl，不丢精度。

用法示例
--------
    python3 ftms_capture.py --scan
    python3 ftms_capture.py --name YS_ --duration 1800
    python3 ftms_capture.py --name YS_ --hr F5:AF:XX:XX:XX:X0
    python3 ftms_capture.py --name YS_ --hr-name COROS --age 30 --weight 70 --sex m
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
from dataclasses import dataclass

try:
    import tomllib
except ModuleNotFoundError:          # Python < 3.11
    tomllib = None

import ftms_tui

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

BLUEZ = "org.bluez"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
PROPERTIES = "org.freedesktop.DBus.Properties"

ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
CHAR_IFACE = "org.bluez.GattCharacteristic1"

FTMS_SERVICE = "00001826-0000-1000-8000-00805f9b34fb"
CROSS_TRAINER_DATA = "00002ace-0000-1000-8000-00805f9b34fb"
TRAINING_STATUS = "00002ad3-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

HRS_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HEART_RATE_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"

# 阻力旋钮标定：6.5 圈 ↔ raw 91（线性，每圈 14 个单位）
RES_RAW_PER_TURN = 14.0
RES_RAW_MAX = 91.0

FRAME_LEN = 29
DISCOVERY_SECONDS = 15.0
RECONNECT_INTERVAL = 5.0
# 连接正常但多久没收到 0x2ACE 就算「订阅失效」。设备闲置时也是 1 Hz 推零值，
# 所以超过这个秒数一定不正常 —— 通常是别的 D-Bus 客户端调 StopNotify
# 把共享的 CCCD 一起关掉了（bluetoothctl、我们的调试脚本都会这样）。
RESUBSCRIBE_AFTER = 20.0

# 导出 FIT 时的数据清洗阈值（与 export_fit.py 的默认值一致）
FIT_MAX_HR_AGE = 10.0
FIT_MAX_GAP = 10.0

# 功率换算成消耗的热量：机械功 / 效率。人踩椭圆机的机械效率约 22–25%，
# 4.184 kJ/kcal × 0.24 ≈ 1.0，所以工程上「1 kJ 机械功 ≈ 1 kcal 消耗」。
KCAL_PER_KJ = 1.0


# --------------------------------------------------------------------------- #
# 帧解析
# --------------------------------------------------------------------------- #

def _add_unix_signal(signum: int, handler) -> None:
    """把 Unix 信号接入 GLib 主循环（GLibUnix 优先，退回旧 API）。"""
    try:
        from gi.repository import GLibUnix
        GLibUnix.signal_add(GLib.PRIORITY_HIGH, signum, handler)
    except (ImportError, AttributeError):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signum, handler)


# 守护模式下会反复进出主循环，信号只接一次，靠这份共享状态传递“该停了”
_STOP = {"requested": False, "loop": None, "installed": False}


def _install_signals() -> None:
    """Ctrl+C / SIGTERM → 设标志 + 唤醒当前主循环（可重复调用）。"""
    if _STOP["installed"]:
        return

    def handler(*_a):
        if not _STOP["requested"]:
            _STOP["requested"] = True
        loop = _STOP["loop"]
        if loop is not None and loop.is_running():
            loop.quit()
        return GLib.SOURCE_CONTINUE

    _add_unix_signal(signal.SIGINT, handler)
    _add_unix_signal(signal.SIGTERM, handler)
    _STOP["installed"] = True


def _stop_requested() -> bool:
    return _STOP["requested"]


def _run_loop(loop: GLib.MainLoop) -> None:
    """跑主循环；如果信号已先到，立即返回（不复位标志）。"""
    if _STOP["requested"]:
        return
    _STOP["loop"] = loop
    try:
        loop.run()
    finally:
        _STOP["loop"] = None


def _u16(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off:off + 2], "little")


def _u24(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off:off + 3], "little")


@dataclass
class Frame:
    """0x2ACE Cross Trainer Data 的一帧。"""

    elapsed_s: int           # offset 27, u16, s
    speed_kmh: float         # offset  3, u16/100
    avg_speed_kmh: float     # offset  5, u16/100
    distance_m: int          # offset  7, u24, m
    cadence_spm: int         # offset 10, u16, steps/min
    resistance_raw: float    # offset 16, u16/10, 0..91
    power_w: int             # offset 18, u16, W
    kcal_active: float       # offset 14, u16/100, kcal
    kcal_total: int          # offset 22, u16, kcal (含基础代谢)
    f12: int                 # 未解字段
    f20: int                 # 未解字段

    @property
    def resistance_turns(self) -> float:
        return self.resistance_raw / RES_RAW_PER_TURN

    @property
    def resistance_pct(self) -> float:
        return self.resistance_raw / RES_RAW_MAX * 100.0

    @classmethod
    def parse(cls, buf: bytes) -> "Frame | None":
        if len(buf) < FRAME_LEN:
            return None
        return cls(
            elapsed_s=_u16(buf, 27),
            speed_kmh=_u16(buf, 3) / 100.0,
            avg_speed_kmh=_u16(buf, 5) / 100.0,
            distance_m=_u24(buf, 7),
            cadence_spm=_u16(buf, 10),
            resistance_raw=_u16(buf, 16) / 10.0,
            power_w=_u16(buf, 18),
            kcal_active=_u16(buf, 14) / 100.0,
            kcal_total=_u16(buf, 22),
            f12=_u16(buf, 12),
            f20=_u16(buf, 20),
        )


@dataclass
class HrSample:
    """0x2A37 Heart Rate Measurement 的一帧。"""

    bpm: int
    contact: bool            # flags bit1：传感器接触检测通过
    energy_kj: int | None    # flags bit3
    rr_ms: list[float]       # flags bit4，单位 1/1024 s

    @classmethod
    def parse(cls, buf: bytes) -> "HrSample | None":
        if len(buf) < 2:
            return None
        flags = buf[0]
        off = 1
        if flags & 0x01:                       # bit0: u16 心率
            if len(buf) < off + 2:
                return None
            bpm = int.from_bytes(buf[off:off + 2], "little")
            off += 2
        else:
            bpm = buf[off]
            off += 1
        contact = bool(flags & 0x02)           # bit1: 接触检测
        energy = None
        if flags & 0x08:                       # bit3: 能量消耗 kJ
            if len(buf) >= off + 2:
                energy = int.from_bytes(buf[off:off + 2], "little")
            off += 2
        rr: list[float] = []
        if flags & 0x10:                       # bit4: RR 间期
            while off + 1 < len(buf):
                rr.append(int.from_bytes(buf[off:off + 2], "little") / 1024.0 * 1000.0)
                off += 2
        return cls(bpm=bpm, contact=contact, energy_kj=energy, rr_ms=rr)


def keytel_kcal_per_min(hr: int, weight_kg: float, age: int, sex: str) -> float:
    """Keytel (2005) 心率估热量，返回 kcal/min。

    男: (-55.0969 + 0.6309·HR + 0.1988·W + 0.2017·A) / 4.184
    女: (-20.4022 + 0.4472·HR - 0.1263·W + 0.074·A) / 4.184
    """
    if sex.lower().startswith("f"):
        kj = -20.4022 + 0.4472 * hr - 0.1263 * weight_kg + 0.074 * age
    else:
        kj = -55.0969 + 0.6309 * hr + 0.1988 * weight_kg + 0.2017 * age
    return max(0.0, kj / 4.184)


CSV_COLUMNS = [
    "iso_time", "t_epoch", "elapsed_s", "speed_kmh", "avg_speed_kmh",
    "distance_m", "cadence_spm", "resistance_raw", "resistance_turns",
    "resistance_pct", "power_w", "kcal_active", "kcal_total",
    # 心率
    "hr", "hr_age_s", "hr_contact",
    # 三路卡路里对比
    "kcal_power", "kcal_keytel", "kcal_keytel_net",
    # 未解字段
    "f12", "f20", "raw_hex",
]


# --------------------------------------------------------------------------- #
# BlueZ / D-Bus 封装
# --------------------------------------------------------------------------- #

class Bluez:
    def __init__(self) -> None:
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    # -- 基础调用 ---------------------------------------------------------- #

    def call(self, path, iface, method, args=None, reply=None, timeout=15000):
        return self.bus.call_sync(
            BLUEZ, path, iface, method, args, reply,
            Gio.DBusCallFlags.NONE, timeout, None,
        )

    def properties(self, path: str, iface: str) -> dict:
        try:
            return self.call(path, PROPERTIES, "GetAll",
                             GLib.Variant("(s)", (iface,)),
                             GLib.VariantType("(a{sv})"), 8000).unpack()[0]
        except GLib.Error:
            return {}

    def managed_objects(self) -> dict:
        try:
            return self.call("/", OBJECT_MANAGER, "GetManagedObjects",
                             None,
                             GLib.VariantType("(a{oa{sa{sv}}})")).unpack()[0]
        except GLib.Error:
            return {}

    # -- 设备 / 适配器 ------------------------------------------------------ #

    def adapters(self) -> list[tuple[str, dict]]:
        return [(p, i[ADAPTER_IFACE])
                for p, i in self.managed_objects().items() if ADAPTER_IFACE in i]

    def devices(self) -> list[tuple[str, dict]]:
        return [(p, i[DEVICE_IFACE])
                for p, i in self.managed_objects().items() if DEVICE_IFACE in i]

    def characteristics(self, device_path: str) -> dict[str, str]:
        """返回 {uuid: object_path}，只看属于该设备的特征。"""
        out = {}
        prefix = device_path + "/"
        for path, ifaces in self.managed_objects().items():
            if not path.startswith(prefix) or CHAR_IFACE not in ifaces:
                continue
            out[str(ifaces[CHAR_IFACE].get("UUID", "")).lower()] = path
        return out

    def start_discovery(self) -> None:
        for path, _ in self.adapters():
            try:
                self.call(path, ADAPTER_IFACE, "SetDiscoveryFilter",
                          GLib.Variant("(a{sv})",
                                       ({"Transport": GLib.Variant("s", "le")},)))
            except GLib.Error:
                pass
            try:
                self.call(path, ADAPTER_IFACE, "StartDiscovery")
            except GLib.Error as exc:
                print(f"! StartDiscovery: {exc.message}", file=sys.stderr)

    def stop_discovery(self) -> None:
        for path, _ in self.adapters():
            try:
                self.call(path, ADAPTER_IFACE, "StopDiscovery", None, None, 5000)
            except GLib.Error:
                pass

    @staticmethod
    def advertises(props: dict, service: str) -> bool:
        return service in [str(u).lower() for u in props.get("UUIDs", [])]

    def find(self, address: str = "", name_prefix: str = "",
             service: str = FTMS_SERVICE) -> tuple[str, dict] | None:
        fallback = None
        for path, props in self.devices():
            if address and props.get("Address", "").upper() != address.upper():
                continue
            if name_prefix:
                name = props.get("Name") or props.get("Alias") or ""
                if not name.startswith(name_prefix):
                    continue
            if service and not self.advertises(props, service):
                continue
            if props.get("Connected"):
                return path, props
            if fallback is None:
                fallback = (path, props)
        return fallback

    # -- 连接 --------------------------------------------------------------- #

    def connect(self, path: str, timeout_ms: int = 25000) -> bool:
        if self.properties(path, DEVICE_IFACE).get("Connected"):
            return True
        try:
            self.call(path, DEVICE_IFACE, "Connect", None, None, timeout_ms)
            return True
        except GLib.Error as exc:
            print(f"! 连接失败: {exc.message}", file=sys.stderr)
            return False

    def wait_services(self, path: str, seconds: float = 12.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.properties(path, DEVICE_IFACE).get("ServicesResolved"):
                return True
            time.sleep(0.2)
        return False

    def start_notify(self, char_path: str) -> bool:
        try:
            self.call(char_path, CHAR_IFACE, "StartNotify", None, None, 8000)
            return True
        except GLib.Error as exc:
            print(f"! StartNotify 失败: {exc.message}", file=sys.stderr)
            return False

    def stop_notify(self, char_path: str) -> None:
        try:
            self.call(char_path, CHAR_IFACE, "StopNotify", None, None, 3000)
        except GLib.Error:
            pass

    def force_reconnect(self) -> None:
        """断链重连。BlueZ 的订阅状态被外部搞坏时，重连是最干净的复位。"""
        if not self.device_path:
            return
        for path in self.subscribed:
            self.stop_notify(path)
        self.subscribed = []
        self.bz.disconnect(self.device_path)
        self.connected = False
        self.disconnected_since = time.monotonic()
        self.last_frame_mono = None
        self.attach()

    def disconnect(self, path: str) -> None:
        try:
            self.call(path, DEVICE_IFACE, "Disconnect", None, None, 5000)
        except GLib.Error:
            pass


# --------------------------------------------------------------------------- #
# 采集器
# --------------------------------------------------------------------------- #

class Capture:
    def __init__(self, bluez: Bluez, args) -> None:
        self.bz = bluez
        self.args = args

        # 椭圆机
        self.device_path: str | None = None
        self.connected = False
        self.subscribed: list[str] = []
        self.frames = 0
        self.last: Frame | None = None
        self.last_elapsed: int | None = None
        self.last_frame_mono: float | None = None
        self.disconnected_since: float | None = None

        # 心率
        self.hr_path: str | None = None
        self.hr_connected = False
        self.hr_subscribed: list[str] = []
        self.hr_bpm: int | None = None
        self.hr_ts: float | None = None
        self.hr_contact: bool | None = None
        self.hr_frames = 0

        # 路径 -> uuid，回调里用来分辨是谁的通知
        self.notify_uuid: dict[str, str] = {}

        # 卡路里累加
        self.kcal_power = 0.0
        self.kcal_keytel = 0.0
        self.kcal_keytel_net = 0.0
        self._last_tick: float | None = None

        # 输出文件延迟到「真正开始」时才创建。守护模式下设备可能先广播、
        # 然后闲置五分钟才清零关机，那种空会话不该在磁盘上留下垃圾。
        os.makedirs(args.outdir, exist_ok=True)
        self.stamp = time.strftime("%Y%m%d_%H%M%S")
        self.csv_path = os.path.join(args.outdir, f"{args.prefix}_{self.stamp}.csv")
        self.jsonl_path = os.path.join(args.outdir, f"{args.prefix}_{self.stamp}.jsonl")
        self.hr_jsonl_path = os.path.join(args.outdir,
                                          f"{args.prefix}_{self.stamp}_hr.jsonl")
        self.csv_file = None
        self.writer = None
        self.jsonl_file = None
        self.hr_jsonl_file = None
        self.files_open = False

        # 会话状态
        self.started = False
        self.auto_mode = False
        self.session_no = 0
        self.end_reason = ""
        self.start_mode = getattr(args, "auto_start_on", "") or "signal"
        self.mqtt = None
        self.mqtt_topic = (getattr(args, "mqtt_topic", "") or "elliptical").rstrip("/")
        self._loop: GLib.MainLoop | None = None

        # 通知回调挂在全局 bus 上。注意必须保存订阅 id 并在收尾时退订，
        # 否则每开一局就泄漏一个回调：旧 Capture 会被新会话的主循环继续驱动，
        # 表现为日志里同一个「面板归零」每秒重复 N 次。
        self.closed = False
        self.sub_id = bluez.bus.signal_subscribe(
            BLUEZ, PROPERTIES, "PropertiesChanged", None, None,
            Gio.DBusSignalFlags.NONE, self._on_properties_changed,
        )

    # -- 文件 --------------------------------------------------------------- #

    def open_files(self) -> None:
        if self.files_open:
            return
        self.csv_file = open(self.csv_path, "w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.csv_file, fieldnames=CSV_COLUMNS)
        self.writer.writeheader()
        self.csv_file.flush()
        self.files_open = True

        if self.args.jsonl:
            self.jsonl_file = open(self.jsonl_path, "w", encoding="utf-8")
        if self.args.hr or getattr(self.args, "hr_any", False):
            self.hr_jsonl_file = open(self.hr_jsonl_path, "w", encoding="utf-8")

    def close_files(self) -> None:
        for fh in (self.csv_file, self.jsonl_file, self.hr_jsonl_file):
            if fh:
                try:
                    fh.close()
                except OSError:
                    pass
        self.csv_file = None
        self.writer = None
        self.jsonl_file = None
        self.hr_jsonl_file = None

    def ensure_started(self) -> None:
        """建文件、打日志、发 MQTT start。重复调用无副作用。"""
        if self.started:
            return
        self.started = True
        self.open_files()
        print(f"* 开始记录 {self.csv_path}", flush=True)
        if self.jsonl_file:
            print(f"* 原始帧 {self.jsonl_path}", flush=True)
        if self.hr_jsonl_file:
            print(f"* 心率帧 {self.hr_jsonl_path}", flush=True)
        if self.mqtt:
            self.mqtt.publish_json(f"{self.mqtt_topic}/session", {
                "event": "start",
                "session": self.stamp,
                "session_no": self.session_no,
                "device": self.args.address or self.args.name,
            })

    # -- MQTT --------------------------------------------------------------- #

    def publish_state(self, frame: Frame) -> None:
        if not self.mqtt:
            return
        self.mqtt.publish_json(f"{self.mqtt_topic}/state", {
            "ts": self.stamp,
            "elapsed": frame.elapsed_s,
            "speed": round(frame.speed_kmh, 2),
            "cadence": frame.cadence_spm,
            "power": frame.power_w,
            "resistance": round(frame.resistance_pct, 1),
            "resistance_raw": round(frame.resistance_raw, 1),
            "distance": frame.distance_m,
            "hr": self.hr_bpm or 0,
            "calories": round(self.kcal_keytel_net, 1),
            "calories_machine": round(frame.kcal_total, 1),
        }, retain=True)

    # -- 通知回调 ----------------------------------------------------------- #

    def _on_properties_changed(self, _conn, _sender, obj, iface, _sig,
                               params, _data=None):
        # GDBus 回调签名: (conn, sender, path, iface, signal_name, params, user_data)
        # 注意 params 是 GLib.Variant（支持下标协议），不是 Python tuple
        if iface != PROPERTIES or self.closed:
            return
        try:
            changed = params[1]
        except Exception:
            return
        if "Value" not in changed:
            return

        uuid = self.notify_uuid.get(obj, "")
        raw = bytes(changed["Value"])

        if uuid == CROSS_TRAINER_DATA:
            self._on_ftms(raw)
        elif uuid == HEART_RATE_MEASUREMENT:
            self._on_hr(raw)

    def _on_ftms(self, raw: bytes) -> None:
        frame = Frame.parse(raw)
        if frame is None:
            return

        now_mono = time.monotonic()
        self.last_frame_mono = now_mono

        # 守护模式：等真正踩起来再开文件。面板通电后闲置时也会推零值，
        # 那种数据录进去只会把 FIT 前面拉一条长尾。
        if not self.started:
            moving = frame.cadence_spm > 0 or frame.speed_kmh > 0.05
            if self.start_mode != "signal" and not moving:
                self.last_elapsed = frame.elapsed_s
                return
            self.ensure_started()

        # 面板闲置五分钟会自动清零。计数器倒退 = 上一局结束、新一局开始。
        prev_elapsed = self.last_elapsed
        panel_reset = (prev_elapsed is not None
                       and frame.elapsed_s + 5 < prev_elapsed)

        if panel_reset and self.auto_mode:
            # 守护模式：归零这一帧属于下一局，不计数、不写行。
            # （单次模式仍会把它写进去，好让 export_fit 的 --on-reset 有据可依）
            self.last_elapsed = frame.elapsed_s       # 先更新，避免反复触发
            self.end_reason = "面板清零"
            print(f"\n* 面板计时器归零（{prev_elapsed}s → {frame.elapsed_s}s），本局结束",
                  flush=True)
            if self._loop is not None:
                self._loop.quit()
            return

        self.frames += 1
        self.last = frame
        now = time.time()

        # 三路卡路里累加（用机器 elapsed 差值，避免主机时钟抖动）
        if self.last_elapsed is not None:
            dt = frame.elapsed_s - self.last_elapsed
            if dt < 0 or dt > 10:          # 机器重置或跳帧，跳过这一段
                dt = 0
        else:
            dt = 0
        self.last_elapsed = frame.elapsed_s
        self._last_tick = now

        self.kcal_power += frame.power_w * dt / 1000.0 * KCAL_PER_KJ

        if self.hr_bpm and self.args.weight and self.args.age:
            rate = keytel_kcal_per_min(self.hr_bpm, self.args.weight,
                                       self.args.age, self.args.sex)
            self.kcal_keytel += rate * dt / 60.0
            # 扣掉静息（按 60 bpm 估），只留运动净消耗
            rest = keytel_kcal_per_min(60, self.args.weight,
                                       self.args.age, self.args.sex)
            self.kcal_keytel_net += max(0.0, rate - rest) * dt / 60.0

        hr_age = f"{now - self.hr_ts:.1f}" if self.hr_ts else ""

        row = {
            "iso_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "t_epoch": f"{now:.3f}",
            "elapsed_s": frame.elapsed_s,
            "speed_kmh": f"{frame.speed_kmh:.2f}",
            "avg_speed_kmh": f"{frame.avg_speed_kmh:.2f}",
            "distance_m": frame.distance_m,
            "cadence_spm": frame.cadence_spm,
            "resistance_raw": f"{frame.resistance_raw:.1f}",
            "resistance_turns": f"{frame.resistance_turns:.2f}",
            "resistance_pct": f"{frame.resistance_pct:.1f}",
            "power_w": frame.power_w,
            "kcal_active": f"{frame.kcal_active:.2f}",
            "kcal_total": frame.kcal_total,
            "hr": self.hr_bpm if self.hr_bpm else "",
            "hr_age_s": hr_age,
            "hr_contact": ("" if self.hr_contact is None
                           else int(self.hr_contact)),
            "kcal_power": f"{self.kcal_power:.2f}",
            "kcal_keytel": f"{self.kcal_keytel:.2f}",
            "kcal_keytel_net": f"{self.kcal_keytel_net:.2f}",
            "f12": frame.f12,
            "f20": frame.f20,
            "raw_hex": raw.hex(),
        }
        self.writer.writerow(row)
        self.csv_file.flush()

        ftms_tui.LIVE.update_ftms(frame, self.hr_bpm or 0,
                                  self.kcal_keytel_net,
                                  started=self.started, session=self.stamp)
        if self.mqtt:
            self.publish_state(frame)

        if self.jsonl_file:
            self.jsonl_file.write(json.dumps({
                "t": round(now, 3), "tag": "2ace", "hex": raw.hex(),
            }) + "\n")
            self.jsonl_file.flush()

        if not self.args.quiet:
            hr_txt = ""
            if self.hr_bpm:
                stale = now - (self.hr_ts or now)
                mark = "!" if stale > 5 else " "
                hr_txt = f"  ♥{self.hr_bpm:3d}{mark}"
            print(
                f"{time.strftime('%H:%M:%S')}  "
                f"{frame.elapsed_s // 60:02d}:{frame.elapsed_s % 60:02d}  "
                f"{frame.speed_kmh:5.2f}km/h  均速{frame.avg_speed_kmh:5.2f}  "
                f"{frame.distance_m:6d}m  "
                f"步频{frame.cadence_spm:4d}  "
                f"阻力{frame.resistance_raw:5.1f}"
                f"({frame.resistance_turns:.1f}圈)  "
                f"{frame.power_w:4d}W  "
                f"{frame.kcal_active:7.2f}kcal"
                f"{hr_txt}",
                flush=True,
            )

    def _on_hr(self, raw: bytes) -> None:
        sample = HrSample.parse(raw)
        if sample is None:
            return
        self.hr_frames += 1
        self.hr_bpm = sample.bpm
        self.hr_ts = time.time()
        self.hr_contact = sample.contact
        ftms_tui.LIVE.update_hr(sample.bpm)

        if self.hr_jsonl_file:
            self.hr_jsonl_file.write(json.dumps({
                "t": round(self.hr_ts, 3),
                "bpm": sample.bpm,
                "contact": sample.contact,
                "energy_kj": sample.energy_kj,
                "rr_ms": [round(x, 1) for x in sample.rr_ms],
                "hex": raw.hex(),
            }) + "\n")
            self.hr_jsonl_file.flush()

    # -- 椭圆机连接 --------------------------------------------------------- #

    def attach(self) -> bool:
        if self.connected and self.subscribed:
            return True
        found = self.bz.find(self.args.address, self.args.name)
        if not found:
            return False
        path, props = found
        label = f"{props.get('Address')} {props.get('Name') or props.get('Alias') or ''}"
        print(f"* 连接椭圆机 {label}", flush=True)

        if not self.bz.connect(path):
            return False
        if not self.bz.wait_services(path):
            print("! 服务解析超时", file=sys.stderr)
            return False

        self.device_path = path
        self.connected = True
        ftms_tui.LIVE.set_device(
            props.get("Name") or props.get("Alias") or props.get("Address") or "")

        chars = self.bz.characteristics(path)
        target = chars.get(CROSS_TRAINER_DATA)
        if not target:
            print("! 设备没有 0x2ACE Cross Trainer Data 特征", file=sys.stderr)
            return False
        if self.bz.start_notify(target):
            self.subscribed = [target]
            self.notify_uuid[target] = CROSS_TRAINER_DATA
            print("* 已订阅 Cross Trainer Data，等待数据…", flush=True)
        else:
            return False
        return True

    def detach(self) -> None:
        for p in self.subscribed:
            self.bz.stop_notify(p)
        self.subscribed = []
        self.connected = False

    # -- 心率连接 ----------------------------------------------------------- #

    def attach_hr(self) -> bool:
        addr, name = self.args.hr, self.args.hr_name
        if not (addr or name) and getattr(self.args, "hr_any", False):
            addr, name = "", ""          # 自动模式：看到谁在广播心率就用谁
        found = self.bz.find(addr, name, service=HRS_SERVICE)
        if not found:
            return False
        path, props = found
        label = f"{props.get('Address')} {props.get('Name') or props.get('Alias') or ''}"
        print(f"* 连接心率设备 {label}", flush=True)

        if not self.bz.connect(path):
            return False
        if not self.bz.wait_services(path):
            print("! 心率设备服务解析超时", file=sys.stderr)
            return False

        self.hr_path = path
        self.hr_connected = True

        chars = self.bz.characteristics(path)
        target = chars.get(HEART_RATE_MEASUREMENT)
        if not target:
            print("! 设备没有 0x2A37 Heart Rate Measurement 特征",
                  file=sys.stderr)
            return False
        if self.bz.start_notify(target):
            self.hr_subscribed = [target]
            self.notify_uuid[target] = HEART_RATE_MEASUREMENT
            print("* 已订阅 Heart Rate Measurement", flush=True)
        else:
            return False
        return True

    def detach_hr(self) -> None:
        for p in self.hr_subscribed:
            self.bz.stop_notify(p)
        self.hr_subscribed = []
        self.hr_connected = False

    # -- 主循环 ------------------------------------------------------------- #

    def run(self) -> int:
        """单次采集：自己找设备、连上、采到设定时长 / Ctrl+C 为止。"""
        self.start_mode = "signal"
        self.bz.start_discovery()
        self.ensure_started()
        return self._capture_loop(deadline=self.args.duration or None)

    def run_session(self, idle_timeout: float, disconnect_after: float) -> int:
        """守护模式里的一局：设备已确定，采到信号消失 / 面板清零为止。"""
        self.auto_mode = True
        return self._capture_loop(idle_timeout=idle_timeout,
                                  disconnect_after=disconnect_after)

    def _capture_loop(self, deadline: float | None = None,
                      idle_timeout: float | None = None,
                      disconnect_after: float | None = None) -> int:
        if self.args.hr or self.args.hr_name or getattr(self.args, "hr_any", False):
            # 血泪教训：找到椭圆机后 AutoRunner 会停掉 discovery，这时如果手表
            # 才打开心率广播，BlueZ 根本看不到它，界面就永远没有心率。
            self.bz.start_discovery()

        loop = GLib.MainLoop()
        self._loop = loop
        self.last_frame_mono = None        # 上一局的帧龄不算进本局
        state = {
            "deadline": time.time() + deadline if deadline else None,
            "next_connect": 0.0,
            "hr_next": 0.0,
            "hr_warned": False,
            "silent_ref": time.monotonic(),
            "next_resub": 0.0,
            "resub": 0,
        }

        def end(reason: str) -> None:
            if not self.end_reason:
                self.end_reason = reason
            loop.quit()

        def supervisor() -> bool:
            if _stop_requested():
                print("\n* 收到中断信号，正在收尾…", flush=True)
                end("收到中断")
                return False
            if state["deadline"] and time.time() >= state["deadline"]:
                print("* 到达设定时长，结束", flush=True)
                end("到达设定时长")
                return False

            now_mono = time.monotonic()

            # 掉线判定：BLE 链路断了基本就是面板关机 / 走远。
            # 连续两次读到未连接才算数 —— 刚连上时偶有瞬时读数把连接判死，
            # 那会导致每秒重连一次。
            if self.connected:
                props = self.bz.properties(self.device_path, DEVICE_IFACE)
                if props and props.get("Connected") is False:
                    state["disc_hits"] = state.get("disc_hits", 0) + 1
                    if state["disc_hits"] >= 2:
                        print("* 椭圆机连接断开，尝试重连…", flush=True)
                        self.connected = False
                        self.disconnected_since = now_mono
                else:
                    state["disc_hits"] = 0
            if self.connected:
                self.disconnected_since = None
            elif (disconnect_after and self.disconnected_since
                  and now_mono - self.disconnected_since > disconnect_after):
                print(f"* 已断开 {now_mono - self.disconnected_since:.0f}s，本局结束",
                      flush=True)
                end("设备断开")
                return False

            # 数据静默判定：连接还在但 0x2ACE 不再推送（关机前的最后一道防线）
            if (idle_timeout and self.last_frame_mono
                    and now_mono - self.last_frame_mono > idle_timeout):
                print(f"* 已 {now_mono - self.last_frame_mono:.0f}s 没收到数据，本局结束",
                      flush=True)
                end("数据静默")
                return False

            # 订阅看门狗：连接明明是好的却收不到帧。别的客户端（bluetoothctl、
            # 调试脚本）调 StopNotify 会把 BlueZ 里共享的 CCCD 一起关掉，我们这边
            # 既不会报错也不会掉线，只是默默变聋 —— 不处理的话要等 5 分钟兜底超时。
            if self.connected:
                ref = self.last_frame_mono or state["silent_ref"]
                silent = now_mono - ref
                if silent <= RESUBSCRIBE_AFTER:
                    state["resub"] = 0
                elif now_mono >= state["next_resub"]:
                    state["next_resub"] = now_mono + RESUBSCRIBE_AFTER
                    state["resub"] += 1
                    if state["resub"] <= 2:
                        print(f"* 已 {silent:.0f}s 没收到数据（连接正常），重新订阅…",
                              flush=True)
                        for path in self.subscribed:
                            self.stop_notify(path)
                            self.start_notify(path)
                    else:
                        print(f"* 重新订阅仍无数据，强制重连…", flush=True)
                        state["resub"] = 0
                        state["next_resub"] = now_mono + RESUBSCRIBE_AFTER * 2
                        self.force_reconnect()

            # 重连（节流，避免 attach 阻塞把循环卡死）
            if not self.connected and now_mono >= state["next_connect"]:
                state["next_connect"] = now_mono + RECONNECT_INTERVAL
                self.attach()
                if self.connected and not self.disconnected_since:
                    self.disconnected_since = None

            # 心率设备跟随连接
            want_hr = self.args.hr or self.args.hr_name or getattr(self.args, "hr_any", False)
            if want_hr:
                if self.hr_connected:
                    props = self.bz.properties(self.hr_path, DEVICE_IFACE)
                    if props and not props.get("Connected"):
                        print("* 心率连接断开，重连中…", flush=True)
                        self.hr_connected = False
                elif now_mono >= state["hr_next"]:
                    state["hr_next"] = now_mono + RECONNECT_INTERVAL
                    if not self.attach_hr() and not state["hr_warned"]:
                        state["hr_warned"] = True
                        print("* 还没发现心率广播，每 "
                              f"{RECONNECT_INTERVAL:.0f}s 再找一次"
                              "（手表上要打开「心率广播」）", flush=True)
            return True

        GLib.timeout_add_seconds(1, supervisor)
        supervisor()                      # 先跑一次，立即发起连接
        if not self.connected:            # 一次都没连上也要有超时兜底
            self.disconnected_since = time.monotonic()
        try:
            _run_loop(loop)
        finally:
            self._loop = None
            self._shutdown()
        return 0

    def _shutdown(self) -> None:
        self.closed = True
        ftms_tui.LIVE.set_idle()
        if self.sub_id:
            try:
                self.bz.bus.signal_unsubscribe(self.sub_id)
            except Exception:
                pass
            self.sub_id = 0
        self.detach()
        self.detach_hr()
        if self.device_path:
            self.bz.disconnect(self.device_path)
        if self.hr_path:
            self.bz.disconnect(self.hr_path)
        self.bz.stop_discovery()
        self.close_files()

        if not self.started:
            print("* 本次没有踩动，未产生文件", flush=True)
            return

        reason = f"（{self.end_reason}）" if self.end_reason else ""
        msg = f"* 结束{reason}，FTMS {self.frames} 帧"
        if self.hr_frames:
            msg += f"，心率 {self.hr_frames} 帧"
        print(f"{msg} -> {self.csv_path}", flush=True)

        if self.mqtt:
            self.mqtt.publish_json(f"{self.mqtt_topic}/session", {
                "event": "end",
                "session": self.stamp,
                "session_no": self.session_no,
                "reason": self.end_reason,
                "frames": self.frames,
                "hr_frames": self.hr_frames,
            })
        self.write_fit()

    # -- 收尾：生成 FIT ------------------------------------------------------ #

    def write_fit(self) -> None:
        """把刚写完的 CSV 转成 FIT。任何失败都只警告，不影响采集结果。"""
        if not self.args.fit or not self.started:
            return

        min_frames = getattr(self.args, "auto_min_frames", 0) if self.auto_mode else 0
        if self.frames < max(2, min_frames):
            print(f"! 只采到 {self.frames} 帧（阈值 {max(2, min_frames)}），"
                  f"保留 CSV 但不生成 FIT", file=sys.stderr)
            return

        try:
            import zlib
            from pathlib import Path

            import export_fit as ef
        except Exception as exc:                     # 文件缺失 / 语法错误
            print(f"! 无法加载 export_fit.py，跳过 FIT 生成: {exc}", file=sys.stderr)
            return

        csv_path = Path(self.csv_path)
        try:
            issues = ef.Issues()
            samples = ef.load_samples(csv_path, issues, FIT_MAX_HR_AGE)
            if len(samples) < 2:
                print(f"! 有效样本只有 {len(samples)} 条，未生成 FIT", file=sys.stderr)
                return

            stats = ef.compute_stats(samples, FIT_MAX_GAP, self.args.fit_calories)
            fit_bytes = ef.build_fit(
                samples, stats,
                sport=ef.SPORT_FITNESS_EQUIPMENT,
                sub_sport=ef.SUB_SPORT_ELLIPTICAL,
                calories=self.args.fit_calories,
                resistance_mode=self.args.fit_resistance,
                serial=zlib.crc32(csv_path.name.encode()) or 1,
                device_name="ftms_capture",
                product=1,
                utc_offset=ef.local_utc_offset(samples),
            )

            problems = ef.verify_fit(fit_bytes)
            if problems:
                print("! FIT 结构自检未通过，未写出：", file=sys.stderr)
                for problem in problems:
                    print(f"    ✗ {problem}", file=sys.stderr)
                return

            out_path = csv_path.with_suffix(".fit")
            out_path.write_bytes(fit_bytes)
        except Exception as exc:                     # 绝不让收尾打断采集结果
            print(f"! 生成 FIT 失败: {exc}", file=sys.stderr)
            return

        print(f"* FIT  {out_path}  ({len(fit_bytes)} 字节)", flush=True)
        if stats.avg_hr is not None:
            print(f"   椭圆机 · {stats.timer_s:.0f}s · {stats.distance_m:.0f} m"
                  f" · 心率均 {stats.avg_hr:.0f} bpm", flush=True)
        else:
            print(f"   椭圆机 · {stats.timer_s:.0f}s · {stats.distance_m:.0f} m"
                  f" · 无心率数据", flush=True)
        dropped = dict(issues.dropped)
        if issues.hr_dropped:
            dropped["心率"] = issues.hr_dropped
        if dropped:
            detail = "，".join(f"{k} × {v}" for k, v in dropped.items())
            print(f"   数据清洗：{detail}", flush=True)


# --------------------------------------------------------------------------- #
# 守护模式：自动等设备 → 自动录 → 自动出 FIT
# --------------------------------------------------------------------------- #

class AutoRunner:
    """长驻监听：椭圆机一出现就开一局，信号消失 / 面板清零就收尾出 FIT。"""

    def __init__(self, bluez: Bluez, args) -> None:
        self.bz = bluez
        self.args = args
        self.mqtt = None
        self.sessions = 0
        # BlueZ 不会删除已经消失的设备对象，偏不记住就会反复去连一台关机的机器
        self.blocked: dict[str, float] = {}

    def _candidate(self):
        """当前可以直接开录的 FTMS 设备（跳过刚失败过的）。"""
        now = time.monotonic()
        cooldown = getattr(self.args, "auto_cooldown", 60.0)
        for path, props in self.bz.devices():
            if not self.bz.advertises(props, FTMS_SERVICE):
                continue
            address = props.get("Address", "")
            if self.args.address and address.upper() != self.args.address.upper():
                continue
            if self.args.name:
                label = props.get("Name") or props.get("Alias") or ""
                if not label.startswith(self.args.name):
                    continue
            failed_at = self.blocked.get(address)
            if failed_at and now - failed_at < cooldown:
                continue
            return path, props
        return None

    # -- MQTT --------------------------------------------------------------- #

    def setup_mqtt(self):
        host = getattr(self.args, "mqtt_host", "")
        if not host:
            return None
        try:
            from ftms_mqtt import MqttClient, ha_discovery
        except Exception as exc:
            print(f"! 加载 ftms_mqtt.py 失败，MQTT 关闭: {exc}", file=sys.stderr)
            return None

        topic = (self.args.mqtt_topic or "elliptical").rstrip("/")
        client = MqttClient(
            host, self.args.mqtt_port,
            client_id=self.args.mqtt_client_id,
            username=self.args.mqtt_user, password=self.args.mqtt_password,
            will_topic=f"{topic}/availability",
            verbose=getattr(self.args, "mqtt_debug", False),
        ).start()
        print(f"* MQTT   {host}:{self.args.mqtt_port} → {topic}/#", flush=True)
        if getattr(self.args, "mqtt_ha", False):
            ha_discovery(client, topic, self.args.prefix, f"{topic}/state")
            print("* MQTT   已发布 Home Assistant 自动发现", flush=True)
        return client

    # -- 等设备 ------------------------------------------------------------- #

    def wait_for_device(self):
        """等一台 FTMS 椭圆机出现，返回 (path, props)；被中断则返回 None。

        必须跑在 GLib 主循环里，不能用 while+sleep：信号是经 GLibUnix 接进来
        的，主循环不转就收不到 SIGTERM —— 表现为等待设备期间 systemctl stop
        要空等到 TimeoutStopSec 才被 SIGKILL。
        """
        self.bz.start_discovery()
        label = (self.args.address
                 or (f"name~{self.args.name}" if self.args.name else "任意 FTMS 设备"))
        print(f"* 等待椭圆机广播（{label}）…", flush=True)

        loop = GLib.MainLoop()
        found: dict = {"dev": None}
        state = {"beat": time.monotonic(), "check": time.monotonic()}

        def tick() -> bool:
            if _stop_requested():
                loop.quit()
                return False

            device = self._candidate()
            if device:
                found["dev"] = device
                loop.quit()
                return False

            now = time.monotonic()
            if now - state["check"] > 5.0:        # BlueZ 偶尔会自己停掉 discovery
                state["check"] = now
                for path, _ in self.bz.adapters():
                    props = self.bz.properties(path, ADAPTER_IFACE)
                    if props and not props.get("Discovering"):
                        self.bz.start_discovery()
                    break
            if now - state["beat"] > 300:         # 五分钟报一次平安
                state["beat"] = now
                print(f"* 仍在等待… {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
            return True

        GLib.timeout_add(500, tick)
        _run_loop(loop)
        self.bz.stop_discovery()
        return found["dev"]

    # -- 主循环 ------------------------------------------------------------- #

    def run(self) -> int:
        self.mqtt = self.setup_mqtt()
        print(f"* 守护模式：空闲 {self.args.auto_stop_after:.0f}s 无数据 / "
              f"断开 {self.args.auto_disconnect_after:.0f}s 即结束", flush=True)
        try:
            while not _stop_requested():
                found = self.wait_for_device()
                if not found:
                    break

                path, props = found
                address = props.get("Address", "")
                name = props.get("Name") or props.get("Alias") or "?"
                self.sessions += 1
                print(f"\n* [{time.strftime('%H:%M:%S')}] 发现 {name} ({address})"
                      f" → 第 {self.sessions} 局", flush=True)

                session_args = argparse.Namespace(**vars(self.args))
                session_args.address = address
                session_args.name = ""
                if not (session_args.hr or session_args.hr_name):
                    session_args.hr_any = True

                cap = Capture(self.bz, session_args)
                cap.mqtt = self.mqtt
                cap.session_no = self.sessions
                try:
                    cap.run_session(self.args.auto_stop_after,
                                    self.args.auto_disconnect_after)
                except Exception as exc:              # 一局出错不能拖垮守护
                    print(f"! 本局异常: {exc}", file=sys.stderr)

                if cap.started:
                    self.blocked.pop(address, None)
                else:                                  # 没连上就冷却一阵，别死循环
                    self.blocked[address] = time.monotonic()
                    print(f"* {name} 本局未连上，{getattr(self.args, 'auto_cooldown', 60.0):.0f}s "
                          f"内不再尝试", flush=True)

                if self.mqtt:                          # 立即拿起下一个
                    self.mqtt.publish_json(
                        f"{self.args.mqtt_topic}/availability", "online", retain=True)
                for _ in range(10):
                    if _stop_requested():
                        break
                    time.sleep(0.2)
        finally:
            if self.mqtt:
                self.mqtt.stop()
            print("* 守护模式结束", flush=True)
        return 0


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #

def scan(bluez: Bluez, seconds: float) -> int:
    bluez.start_discovery()
    print(f"* 扫描 {seconds:.0f} 秒…", flush=True)
    loop = GLib.MainLoop()
    GLib.timeout_add(int(seconds * 1000), lambda: (loop.quit(), False)[1])
    loop.run()
    bluez.stop_discovery()

    ftms, hrs = [], []
    for _path, props in bluez.devices():
        row = (props.get("RSSI") or -999,
               props.get("Address", "?"),
               props.get("Name") or props.get("Alias") or "",
               props.get("Connected", False))
        if bluez.advertises(props, FTMS_SERVICE):
            ftms.append(row)
        elif bluez.advertises(props, HRS_SERVICE):
            hrs.append(row)

    if not ftms and not hrs:
        print("没发现 FTMS 或心率设备。先踩几下椭圆机 / 打开心率广播再试。",
              file=sys.stderr)
        return 1

    for title, rows, flag in (("FTMS 椭圆机", ftms, "--address/--name"),
                              ("心率设备", hrs, "--hr/--hr-name")):
        if not rows:
            continue
        rows.sort(reverse=True)
        print(f"\n找到 {len(rows)} 台{title}：")
        for rssi, addr, name, conn in rows:
            print(f"  {addr}  {name:<24} rssi={rssi:4d}  "
                  f"{'已连接' if conn else ''}")
        print(f"  用 {flag} 采集")

    if ftms:
        a = ftms[0][1]
        if hrs:
            print(f"\n同时采椭圆机 + 心率：")
            print(f"  python3 ftms_capture.py --address {a} --hr {hrs[0][1]}")
        else:
            print(f"\n  python3 ftms_capture.py --address {a}")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "address": "", "name": "",
    "hr": "", "hr_name": "", "hr_any": False,
    "age": 0, "weight": 0.0, "sex": "m",
    "duration": 0.0, "scan_timeout": DISCOVERY_SECONDS,
    "outdir": "data", "prefix": "ride",
    "jsonl": False, "quiet": False,
    "fit": True, "fit_calories": "keytel_net", "fit_resistance": "percent",
    # 守护模式
    "auto": False,
    "auto_stop_after": 300.0,
    "auto_disconnect_after": 10.0,
    "auto_min_frames": 10,
    "auto_start_on": "motion",
    "auto_cooldown": 60.0,
    # MQTT
    "mqtt_host": "", "mqtt_port": 1883, "mqtt_topic": "elliptical",
    "mqtt_client_id": "elliptical-ftms",
    "mqtt_user": "", "mqtt_password": "",
    "mqtt_ha": False, "mqtt_debug": False,
    # 网络 TUI
    "tui_host": "0.0.0.0", "tui_port": 0, "tui_color": True,
}

# 配置文件里各个 section 能覆盖的键
CONFIG_SECTIONS = {
    "device": ("address", "name", "hr", "hr_name", "hr_any"),
    "profile": ("age", "weight", "sex"),
    "output": ("outdir", "prefix", "jsonl", "quiet", "duration",
               "fit", "fit_calories", "fit_resistance"),
    "auto": ("auto", "auto_stop_after", "auto_disconnect_after",
             "auto_min_frames", "auto_start_on", "auto_cooldown"),
    "mqtt": ("mqtt_host", "mqtt_port", "mqtt_topic", "mqtt_client_id",
             "mqtt_user", "mqtt_password", "mqtt_ha", "mqtt_debug"),
    "tui": ("tui_host", "tui_port", "tui_color"),
}


def load_config(path: str) -> dict:
    """读 TOML 配置文件，返回扁平化的 {key: value}。"""
    if tomllib is None:
        raise RuntimeError("需要 Python 3.11+ 才能读配置文件，请改用命令行参数")
    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    flat = {}
    for section, keys in CONFIG_SECTIONS.items():
        for key in keys:
            if key in data.get(section, {}):
                flat[key] = data[section][key]
    # 允许在顶层直接写键（但要跳过 [section] 表，否则 [auto] 这种同名
    # 段落会把 auto = true 覆盖成一整个 dict）
    for key in DEFAULTS:
        if key in data and not isinstance(data[key], dict):
            flat[key] = data[key]

    unknown = [s for s in data if s not in CONFIG_SECTIONS and s not in DEFAULTS]
    if unknown:
        print(f"! 配置文件里未知的 section: {', '.join(unknown)}",
              file=sys.stderr)
    return flat


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="YESOUL EF25 椭圆机 + BLE 心率带 数据采集器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default="",
                   help="TOML 配置文件路径，如 config.toml")
    p.add_argument("--scan", action="store_true",
                   help="扫描附近的 FTMS / 心率设备")
    p.add_argument("--scan-timeout", type=float, default=None,
                   help=f"扫描等待秒数（默认 {DISCOVERY_SECONDS:.0f}）")

    g = p.add_argument_group("椭圆机")
    g.add_argument("--address", default=None, help="椭圆机 MAC")
    g.add_argument("--name", default=None, help="椭圆机广播名前缀，如 YS_")

    h = p.add_argument_group("心率（可选）")
    h.add_argument("--hr", default=None, help="心率设备 MAC")
    h.add_argument("--hr-name", default=None, help="心率设备名前缀，如 COROS")
    h.add_argument("--hr-any", action="store_true", default=None,
                   help="不指定 MAC，用任何在广播心率（0x180D）的设备")
    h.add_argument("--age", type=int, default=None,
                   help="年龄，用于 Keytel 心率热量公式")
    h.add_argument("--weight", type=float, default=None,
                   help="体重 kg，用于 Keytel 心率热量公式")
    h.add_argument("--sex", default=None, choices=["m", "f"],
                   help="性别，用于 Keytel 公式（默认 m）")

    o = p.add_argument_group("输出")
    o.add_argument("--duration", type=float, default=None,
                   help="采集时长秒数，0 表示一直采")
    o.add_argument("--outdir", default=None, help="输出目录（默认 ./data）")
    o.add_argument("--prefix", default=None, help="文件名前缀（默认 ride）")
    o.add_argument("--jsonl", action="store_true", default=None,
                   help="额外输出原始帧 jsonl")
    o.add_argument("--quiet", action="store_true", default=None,
                   help="不打印实时行")
    o.add_argument("--fit", action=argparse.BooleanOptionalAction, default=None,
                   help="结束时自动生成 FIT 活动文件（默认开启）")
    o.add_argument("--fit-calories", default=None,
                   choices=["power", "keytel", "keytel_net", "machine", "none"],
                   help="FIT 里的卡路里用哪一路（默认 keytel_net）")
    o.add_argument("--fit-resistance", default=None,
                   choices=["percent", "raw", "turns"],
                   help="FIT 里 resistance 字段写哪个量（默认 percent）")

    a = p.add_argument_group("守护模式")
    a.add_argument("--auto", action="store_true", default=None,
                   help="长驻监听：等椭圆机出现自动开录，信号消失自动出 FIT")
    a.add_argument("--auto-stop-after", type=float, default=None,
                   help="多少秒没有 FTMS 数据算结束（默认 300）")
    a.add_argument("--auto-disconnect-after", type=float, default=None,
                   help="掉线多少秒算结束（默认 10）")
    a.add_argument("--auto-min-frames", type=int, default=None,
                   help="少于这么多帧不生成 FIT（默认 10）")
    a.add_argument("--auto-start-on", default=None, choices=["motion", "signal"],
                   help="motion=踩动才开始（默认） / signal=一看到广播就开")
    a.add_argument("--auto-cooldown", type=float, default=None,
                   help="连不上的设备冷却多少秒再试（默认 60）")

    m = p.add_argument_group("MQTT 广播（可选）")
    m.add_argument("--mqtt-host", default=None,
                   help="MQTT broker 地址，留空则不发")
    m.add_argument("--mqtt-port", type=int, default=None, help="默认 1883")
    m.add_argument("--mqtt-topic", default=None,
                   help="主题前缀（默认 elliptical）")
    m.add_argument("--mqtt-client-id", default=None,
                   help="client id（默认 elliptical-ftms）")
    m.add_argument("--mqtt-user", default=None, help="用户名")
    m.add_argument("--mqtt-password", default=None, help="密码")
    m.add_argument("--mqtt-ha", action="store_true", default=None,
                   help="额外发布 Home Assistant 自动发现")
    m.add_argument("--mqtt-debug", action="store_true", default=None,
                   help="打印 MQTT 连接日志")

    t = p.add_argument_group("网络 TUI（nc / telnet）")
    t.add_argument("--tui-port", type=int, default=None,
                   help="监听端口，0 = 关闭（默认）。例：2323")
    t.add_argument("--tui-host", default=None,
                   help="监听地址（默认 0.0.0.0，只给本机用就填 127.0.0.1）")
    t.add_argument("--tui-color", action=argparse.BooleanOptionalAction, default=None,
                   help="彩色输出（默认开）")

    args = p.parse_args(argv)

    # 三层合并：默认值 <- 配置文件 <- 命令行
    merged = dict(DEFAULTS)
    if args.config:
        try:
            merged.update(load_config(args.config))
        except (OSError, RuntimeError) as exc:
            p.error(f"读不了配置文件 {args.config}: {exc}")
        except Exception as exc:                     # TOML 语法错误等
            p.error(f"配置文件 {args.config} 解析失败: {exc}")
    for key, value in vars(args).items():
        if key == "config":
            continue
        if value is not None:                       # 命令行没给的保持配置文件的值
            merged[key] = value
    merged["config"] = args.config
    merged["scan"] = args.scan
    return argparse.Namespace(**merged)


def main(argv=None) -> int:
    args = parse_args(argv)
    _install_signals()
    try:
        bluez = Bluez()
    except GLib.Error as exc:
        print(f"无法连接系统 D-Bus: {exc.message}", file=sys.stderr)
        return 1

    if not bluez.adapters():
        print("没有可用的蓝牙适配器（检查 rfkill / bluetoothd）", file=sys.stderr)
        return 1

    if args.scan:
        return scan(bluez, args.scan_timeout)

    if (args.hr or args.hr_name) and not (args.age and args.weight):
        print("! 没给 profile（age/weight），kcal_keytel 两列会是 0",
              file=sys.stderr)

    if not args.quiet and not args.auto:
        dev = args.address or (args.name and f"name~{args.name}") or "?"
        hrs = args.hr or (args.hr_name and f"name~{args.hr_name}") or ""
        prof = (f"{args.age}y/{args.weight:g}kg/{'男' if args.sex == 'm' else '女'}"
                if args.age and args.weight else "未设置")
        print(f"* 椭圆机 {dev}", flush=True)
        print(f"* 心率   {hrs or '不采集'}", flush=True)
        print(f"* 个人参数 {prof}", flush=True)

    tui_server = None
    if args.tui_port:
        try:
            tui_server = ftms_tui.TuiServer(
                ftms_tui.LIVE, args.tui_host, args.tui_port,
                color=args.tui_color).start()
            print(f"* TUI   {args.tui_host}:{args.tui_port}"
                  f"   nc <这台机器的IP> {args.tui_port}", flush=True)
        except OSError as exc:
            print(f"! TUI 端口 {args.tui_port} 起不来: {exc}", file=sys.stderr)

    def _finish(code: int) -> int:
        if tui_server:
            tui_server.stop()
        return code

    if args.auto:
        if args.duration:
            print("! 守护模式下 --duration 不生效（每局由信号决定）",
                  file=sys.stderr)
        return _finish(AutoRunner(bluez, args).run())

    return _finish(Capture(bluez, args).run())


if __name__ == "__main__":
    sys.exit(main())
