#!/usr/bin/env python3
"""把 ftms_capture.py 采集的 CSV 转换成标准 FIT 活动文件。

不依赖任何第三方库，自带 FIT 编码器。

用法示例::

    # 最简：自动生成同名 .fit
    python3 export_fit.py data/ride_20261109_235238.csv

    # 指定输出与运动类型
    python3 export_fit.py data/ride_20261109_235238.csv -o ~/elliptical.fit \\
        --sport 4 --sub-sport 15

字段说明见 README.md 的「导出 FIT」一节。
"""

from __future__ import annotations

import argparse
import csv
import math
import struct
import sys
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# FIT 基础常量
# --------------------------------------------------------------------------- #

# FIT 纪元 = 1989-12-31T00:00:00Z，与 Unix 纪元相差 631065600 秒。
FIT_EPOCH_OFFSET = 631065600

PROFILE_VERSION = 21221  # 21.221.0，与 Garmin 官方 profile 对齐

# base_type -> (类型码, 尺寸, 无效值, struct 格式)
BASE_TYPES = {
    "enum": (0x00, 1, 0xFF, "<B"),
    "uint8": (0x02, 1, 0xFF, "<B"),
    "uint16": (0x84, 2, 0xFFFF, "<H"),
    "sint32": (0x85, 4, 0x7FFFFFFF, "<i"),
    "uint32": (0x86, 4, 0xFFFFFFFF, "<I"),
    "uint32z": (0x8C, 4, 0xFFFFFFFF, "<I"),
    "string": (0x07, 0, 0x00, None),
}

# FIT 的 CRC-16，多项式查表与官方实现一致。
_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)


def fit_crc(data: bytes, crc: int = 0) -> int:
    """计算 FIT 文件使用的 CRC-16。"""
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc ^= tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc ^= tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


# --------------------------------------------------------------------------- #
# FIT 写入器
# --------------------------------------------------------------------------- #

Field = tuple  # (字段号, base_type, 字节数)


def _fld(num: int, base: str, size: int | None = None) -> Field:
    return (num, base, size if size is not None else BASE_TYPES[base][1])


class FitWriter:
    """极简 FIT 编码器：只负责定义消息 + 写数据消息。"""

    def __init__(self) -> None:
        self.body = bytearray()
        self._defined: dict[int, list[Field]] = {}

    def _write_definition(self, local: int, global_num: int, fields: list[Field]) -> None:
        self.body.append(0x40 | local)  # 定义消息头
        self.body.append(0x00)          # reserved
        self.body.append(0x00)          # 小端
        self.body += struct.pack("<H", global_num)
        self.body.append(len(fields))
        for num, base, size in fields:
            self.body.append(num)
            self.body.append(size)
            self.body.append(BASE_TYPES[base][0])
        # 注意：没有开发者字段时不写「开发者字段数量」字节（与官方 SDD 一致）
        self._defined[local] = fields

    def add(self, local: int, global_num: int, fields: list[Field], values: list) -> None:
        if local not in self._defined:
            self._write_definition(local, global_num, fields)
        self.body.append(local)
        for (_, base, size), value in zip(fields, values):
            self.body += self._pack(base, size, value)

    @staticmethod
    def _pack(base: str, size: int, value) -> bytes:
        _, _, invalid, fmt = BASE_TYPES[base]
        if value is None:
            return invalid.to_bytes(size, "little")
        if base == "string":
            raw = str(value).encode("utf-8")[: size - 1]
            return raw.ljust(size, b"\x00")
        return struct.pack(fmt, int(value))

    def tobytes(self) -> bytes:
        header = bytearray(14)
        header[0] = 14  # 头长度
        header[1] = 0x20  # 协议版本 2.0
        struct.pack_into("<H", header, 2, PROFILE_VERSION)
        struct.pack_into("<I", header, 4, len(self.body))
        header[8:12] = b".FIT"
        struct.pack_into("<H", header, 12, fit_crc(bytes(header[:12])))
        data = bytes(header) + bytes(self.body)
        return data + struct.pack("<H", fit_crc(data))


# --------------------------------------------------------------------------- #
# 消息定义（字段号来自 Garmin FIT profile 21.221）
# --------------------------------------------------------------------------- #

MSG_FILE_ID = 0
MSG_DEVICE_INFO = 23
MSG_EVENT = 21
MSG_RECORD = 20
MSG_LAP = 19
MSG_SESSION = 18
MSG_ACTIVITY = 34

LOCAL_FILE_ID = 0
LOCAL_DEVICE_INFO = 1
LOCAL_EVENT = 2
LOCAL_RECORD = 3
LOCAL_LAP = 4
LOCAL_SESSION = 5
LOCAL_ACTIVITY = 6

FIELDS_FILE_ID = [
    _fld(0, "enum"),        # type
    _fld(1, "uint16"),      # manufacturer
    _fld(2, "uint16"),      # product
    _fld(3, "uint32z"),     # serial_number
    _fld(4, "uint32"),      # time_created
]

FIELDS_DEVICE_INFO = [
    _fld(253, "uint32"),      # timestamp
    _fld(0, "uint8"),         # device_index
    _fld(2, "uint16"),        # manufacturer
    _fld(3, "uint32z"),       # serial_number
    _fld(4, "uint16"),        # product
    _fld(5, "uint16"),        # software_version (×100)
]

FIELDS_EVENT = [
    _fld(253, "uint32"),  # timestamp
    _fld(0, "enum"),      # event
    _fld(1, "enum"),      # event_type
    _fld(4, "uint8"),     # event_group
]

# 字段顺序按 FIT profile 的编号排列，与官方编码器输出保持一致
FIELDS_RECORD = [
    _fld(253, "uint32"),  # timestamp
    _fld(3, "uint8"),     # heart_rate
    _fld(4, "uint8"),     # cadence
    _fld(5, "uint32"),    # distance (×100, m)
    _fld(6, "uint16"),    # speed (×1000, m/s)
    _fld(7, "uint16"),    # power (W)
    _fld(10, "uint8"),    # resistance
    _fld(19, "uint32"),   # total_cycles
    _fld(33, "uint16"),   # calories (累计)
    _fld(73, "uint32"),   # enhanced_speed (×1000, m/s)
]

FIELDS_LAP = [
    _fld(253, "uint32"),  # timestamp
    _fld(2, "uint32"),    # start_time
    _fld(7, "uint32"),    # total_elapsed_time (×1000)
    _fld(8, "uint32"),    # total_timer_time (×1000)
    _fld(9, "uint32"),    # total_distance (×100)
    _fld(10, "uint32"),   # total_cycles
    _fld(11, "uint16"),   # total_calories
    _fld(13, "uint16"),   # avg_speed (×1000)
    _fld(14, "uint16"),   # max_speed (×1000)
    _fld(15, "uint8"),    # avg_heart_rate
    _fld(16, "uint8"),    # max_heart_rate
    _fld(63, "uint8"),    # min_heart_rate
    _fld(17, "uint8"),    # avg_cadence
    _fld(18, "uint8"),    # max_cadence
    _fld(19, "uint16"),   # avg_power
    _fld(20, "uint16"),   # max_power
    _fld(24, "enum"),     # lap_trigger
    _fld(25, "enum"),     # sport
    _fld(39, "enum"),     # sub_sport
    _fld(110, "uint32"),  # enhanced_avg_speed (×1000)
    _fld(111, "uint32"),  # enhanced_max_speed (×1000)
]

FIELDS_SESSION = [
    _fld(253, "uint32"),  # timestamp
    _fld(0, "enum"),      # event
    _fld(1, "enum"),      # event_type
    _fld(2, "uint32"),    # start_time
    _fld(5, "enum"),      # sport
    _fld(6, "enum"),      # sub_sport
    _fld(7, "uint32"),    # total_elapsed_time (×1000)
    _fld(8, "uint32"),    # total_timer_time (×1000)
    _fld(9, "uint32"),    # total_distance (×100)
    _fld(10, "uint32"),   # total_cycles
    _fld(11, "uint16"),   # total_calories
    _fld(14, "uint16"),   # avg_speed (×1000)
    _fld(15, "uint16"),   # max_speed (×1000)
    _fld(16, "uint8"),    # avg_heart_rate
    _fld(17, "uint8"),    # max_heart_rate
    _fld(18, "uint8"),    # avg_cadence
    _fld(19, "uint8"),    # max_cadence
    _fld(20, "uint16"),   # avg_power
    _fld(21, "uint16"),   # max_power
    _fld(25, "uint16"),   # first_lap_index
    _fld(26, "uint16"),   # num_laps
    _fld(28, "enum"),     # trigger
    _fld(48, "uint32"),   # total_work (J)
    _fld(64, "uint8"),    # min_heart_rate
    _fld(124, "uint32"),  # enhanced_avg_speed
    _fld(125, "uint32"),  # enhanced_max_speed
]

FIELDS_ACTIVITY = [
    _fld(253, "uint32"),  # timestamp
    _fld(0, "uint32"),    # total_timer_time (×1000)
    _fld(1, "uint16"),    # num_sessions
    _fld(2, "enum"),      # type
    _fld(3, "enum"),      # event
    _fld(4, "enum"),      # event_type
    _fld(5, "uint32"),    # local_timestamp
]

# 枚举值
SPORT_FITNESS_EQUIPMENT = 4
SUB_SPORT_ELLIPTICAL = 15
SUB_SPORT_BY_NAME = {
    "elliptical": 15,
    "indoor_cycling": 6,
    "spin": 5,
    "indoor_rowing": 14,
    "stair_climbing": 16,
    "treadmill": 1,
    "cardio_training": 26,
    "generic": 0,
}

EVENT_TIMER = 0
EVENT_SESSION = 8
EVENT_ACTIVITY = 26
EVENT_TYPE_START = 0
EVENT_TYPE_STOP = 1
LAP_TRIGGER_SESSION_END = 7
SESSION_TRIGGER_ACTIVITY_END = 0
ACTIVITY_TYPE_MANUAL = 0

MANUFACTURER_DEVELOPMENT = 255


# --------------------------------------------------------------------------- #
# CSV 读取与校验
# --------------------------------------------------------------------------- #

NAN = float("nan")


def _num(row: dict, key: str) -> float:
    raw = row.get(key)
    if raw is None or raw == "":
        return NAN
    try:
        return float(raw)
    except ValueError:
        return NAN


@dataclass
class Sample:
    t: float              # Unix 时间戳（秒，可为小数）
    iso: str              # 本地时间字符串，用于推导时区偏移
    speed_kmh: float
    distance_m: float
    cadence_spm: float
    resistance_raw: float
    resistance_pct: float
    resistance_turns: float
    power_w: float
    hr: float
    hr_age_s: float
    kcal_active: float
    kcal_power: float
    kcal_keytel: float
    kcal_keytel_net: float


@dataclass
class Issues:
    """记录清洗过程中发现的问题，用于生成报告。"""

    total_rows: int = 0
    bad_timestamp: int = 0
    duplicate_seconds: int = 0
    non_monotonic: int = 0
    bad_distance: int = 0
    dropped: dict = field(default_factory=dict)
    hr_dropped: int = 0

    def drop(self, name: str) -> None:
        self.dropped[name] = self.dropped.get(name, 0) + 1

    def report(self) -> list[str]:
        lines = [f"CSV 行数            {self.total_rows}"]
        if self.bad_timestamp:
            lines.append(f"  时间戳无效丢弃    {self.bad_timestamp}")
        if self.duplicate_seconds:
            lines.append(f"  同秒重复合并      {self.duplicate_seconds}")
        if self.non_monotonic:
            lines.append(f"  时间倒退重排      {self.non_monotonic}")
        if self.bad_distance:
            lines.append(f"  距离缺失修补      {self.bad_distance}")
        for name, count in self.dropped.items():
            lines.append(f"  越界已丢弃        {name} × {count}")
        if self.hr_dropped:
            lines.append(f"  心率过期/异常丢弃 {self.hr_dropped}")
        return lines


# 物理上限，超过就认为是脏数据。
LIMITS = {
    "speed_kmh": (0.0, 70.0),
    "cadence_spm": (0.0, 255.0),
    "resistance_raw": (0.0, 254.0),
    "power_w": (0.0, 2000.0),
}


def load_samples(path: Path, issues: Issues, max_hr_age: float) -> list[Sample]:
    samples: list[Sample] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            issues.total_rows += 1
            t = _num(row, "t_epoch")
            if not math.isfinite(t) or t <= 0:
                issues.bad_timestamp += 1
                continue
            samples.append(
                Sample(
                    t=t,
                    iso=row.get("iso_time", ""),
                    speed_kmh=_num(row, "speed_kmh"),
                    distance_m=_num(row, "distance_m"),
                    cadence_spm=_num(row, "cadence_spm"),
                    resistance_raw=_num(row, "resistance_raw"),
                    resistance_pct=_num(row, "resistance_pct"),
                    resistance_turns=_num(row, "resistance_turns"),
                    power_w=_num(row, "power_w"),
                    hr=_num(row, "hr"),
                    hr_age_s=_num(row, "hr_age_s"),
                    kcal_active=_num(row, "kcal_active"),
                    kcal_power=_num(row, "kcal_power"),
                    kcal_keytel=_num(row, "kcal_keytel"),
                    kcal_keytel_net=_num(row, "kcal_keytel_net"),
                )
            )

    # 按时间排序
    ordered = sorted(samples, key=lambda s: s.t)
    issues.non_monotonic = sum(1 for a, b in zip(samples, ordered) if a is not b)

    # 同一秒只保留最后一条（1 Hz 数据通常不会重复，这里防御性处理）
    deduped: list[Sample] = []
    for sample in ordered:
        if deduped and int(sample.t) == int(deduped[-1].t):
            issues.duplicate_seconds += 1
            deduped[-1] = sample
        else:
            deduped.append(sample)

    # 数值清洗
    for sample in deduped:
        for name, (low, high) in LIMITS.items():
            value = getattr(sample, name)
            if not math.isfinite(value):
                setattr(sample, name, 0.0)
                continue
            if value < low or value > high:
                # 越界不做截断，直接判为无效（截断会把 5000W 变成 2000W 污染最大值）
                issues.drop(name)
                setattr(sample, name, NAN)

        # 心率：无值 / 超生理范围 / 过期 → 置为未知
        hr_ok = (
            math.isfinite(sample.hr)
            and 25.0 <= sample.hr <= 250.0
            and (not math.isfinite(sample.hr_age_s) or sample.hr_age_s <= max_hr_age)
        )
        if not hr_ok:
            if math.isfinite(sample.hr) and sample.hr > 0:
                issues.hr_dropped += 1
            sample.hr = NAN

    # 距离必须是单调非减
    last = 0.0
    for sample in deduped:
        if not math.isfinite(sample.distance_m) or sample.distance_m < last:
            issues.bad_distance += 1
            sample.distance_m = last
        else:
            last = sample.distance_m

    return deduped


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #

@dataclass
class Stats:
    start_t: int
    end_t: int
    elapsed_s: float
    timer_s: float
    distance_m: float
    steps: float
    work_j: float
    calories: float
    avg_speed: float | None
    max_speed: float | None
    avg_power: float | None
    max_power: float | None
    avg_cadence: float | None
    max_cadence: float | None
    avg_hr: float | None
    max_hr: float | None
    min_hr: float | None
    max_resistance: float | None


def _weighted(values: list[tuple[float | None, float]]) -> float | None:
    total_w = 0.0
    total_v = 0.0
    for value, weight in values:
        if value is None:
            continue
        total_v += value * weight
        total_w += weight
    return total_v / total_w if total_w > 0 else None


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _zero(value: float) -> float:
    """把 NaN / inf 当 0 处理，避免污染累加结果。"""
    return value if math.isfinite(value) else 0.0


def local_utc_offset(samples: list[Sample]) -> int:
    """从 iso_time（本地墙钟）与 t_epoch（真实 UTC）的差值推导时区偏移。"""
    for sample in samples:
        if not sample.iso:
            continue
        try:
            stamp = datetime.fromisoformat(sample.iso)
        except ValueError:
            continue
        # iso_time 是不带时区的本地时间，当作 UTC 解释后再与真实 epoch 相减
        offset = round(stamp.replace(tzinfo=timezone.utc).timestamp() - sample.t)
        # 只接受整刻钟的时区，其余情况按 UTC 处理
        if abs(offset) <= 14 * 3600 and offset % 900 == 0:
            return offset
        return 0
    return 0


def compute_stats(samples: list[Sample], max_gap: float, calories: str) -> Stats:
    first, last = samples[0], samples[-1]
    start_t = int(first.t)
    end_t = int(last.t)

    timer = 0.0
    steps = 0.0
    work = 0.0
    speed_pairs: list[tuple[float | None, float]] = []
    power_pairs: list[tuple[float | None, float]] = []
    cad_pairs: list[tuple[float | None, float]] = []
    hr_pairs: list[tuple[float | None, float]] = []

    prev_t = first.t
    for i, sample in enumerate(samples):
        # 每条记录代表「到上一条记录为止」的这段区间，首条没有区间
        dt = sample.t - prev_t if i else 0.0
        prev_t = sample.t
        if dt <= 0 or dt > max_gap:
            continue  # 停顿段不计入计时与平均值
        timer += dt
        steps += _zero(sample.cadence_spm) * dt / 60.0
        work += _zero(sample.power_w) * dt
        speed_pairs.append((_finite(sample.speed_kmh), dt))
        power_pairs.append((_finite(sample.power_w), dt))
        cad_pairs.append((_finite(sample.cadence_spm), dt))
        if math.isfinite(sample.hr):
            hr_pairs.append((sample.hr, dt))

    speeds = [s for s, _ in speed_pairs if s is not None]
    powers = [p for p, _ in power_pairs if p is not None]
    cads = [c for c, _ in cad_pairs if c is not None]
    hrs = [h for h, _ in hr_pairs]

    if calories == "none":
        kcal = 0.0
    else:
        column = {
            "power": "kcal_power",
            "keytel": "kcal_keytel",
            "keytel_net": "kcal_keytel_net",
            "machine": "kcal_active",
        }[calories]
        raw = getattr(last, column)
        if not math.isfinite(raw) or raw <= 0:
            # 选定的口径没有数据时退回机器累计值
            raw = last.kcal_active
        kcal = raw if math.isfinite(raw) and raw > 0 else 0.0
        if calories == "keytel_net" and not hrs:
            # 没有心率数据时 Keytel 无意义，退回机器值
            kcal = last.kcal_active if math.isfinite(last.kcal_active) else 0.0

    resistances = [s.resistance_raw for s in samples if math.isfinite(s.resistance_raw)]

    return Stats(
        start_t=start_t,
        end_t=end_t,
        elapsed_s=last.t - first.t,
        timer_s=timer,
        distance_m=max(0.0, last.distance_m - first.distance_m),
        steps=steps,
        work_j=work,
        calories=kcal,
        avg_speed=_weighted(speed_pairs),
        max_speed=max(speeds) if speeds else None,
        avg_power=_weighted(power_pairs),
        max_power=max(powers) if powers else None,
        avg_cadence=_weighted(cad_pairs),
        max_cadence=max(cads) if cads else None,
        avg_hr=_weighted(hr_pairs),
        max_hr=max(hrs) if hrs else None,
        min_hr=min(hrs) if hrs else None,
        max_resistance=max(resistances) if resistances else None,
    )


# --------------------------------------------------------------------------- #
# FIT 组装
# --------------------------------------------------------------------------- #

def _u8(value: float | None) -> int | None:
    if value is None or not math.isfinite(value):
        return None
    return max(0, min(254, int(round(value))))


def _u16(value: float | None, scale: float = 1.0) -> int | None:
    if value is None or not math.isfinite(value):
        return None
    return max(0, min(65534, int(round(value * scale))))


def _u32(value: float | None, scale: float = 1.0) -> int | None:
    if value is None or not math.isfinite(value):
        return None
    return max(0, min(0xFFFFFFFE, int(round(value * scale))))


def _resistance(sample: Sample, mode: str) -> int | None:
    if mode == "percent":
        value = sample.resistance_pct
    elif mode == "turns":
        value = sample.resistance_turns
    else:  # raw
        value = sample.resistance_raw
    if not math.isfinite(value):
        return None
    return max(0, min(254, int(round(value))))


def build_fit(
    samples: list[Sample],
    stats: Stats,
    *,
    sport: int,
    sub_sport: int,
    calories: str,
    resistance_mode: str,
    serial: int,
    device_name: str,
    product: int,
    utc_offset: int = 0,
) -> bytes:
    writer = FitWriter()

    # 字符串字段长度必须是 名称长度 + 1（末尾 \0），与官方编码器保持一致
    name_field = [
        _fld(8, "string", len(device_name) + 1),
    ]
    file_id_fields = FIELDS_FILE_ID + name_field
    device_name_field = [_fld(27, "string", len(device_name) + 1)]
    device_info_fields = FIELDS_DEVICE_INFO + device_name_field

    start_fit = stats.start_t - FIT_EPOCH_OFFSET
    end_fit = stats.end_t - FIT_EPOCH_OFFSET

    # --- file_id ---
    writer.add(
        LOCAL_FILE_ID, MSG_FILE_ID, file_id_fields,
        [4, MANUFACTURER_DEVELOPMENT, product, serial, end_fit, device_name],
    )

    # --- device_info（创建者） ---
    writer.add(
        LOCAL_DEVICE_INFO, MSG_DEVICE_INFO, device_info_fields,
        [start_fit, 0, MANUFACTURER_DEVELOPMENT, serial, product, 100, device_name],
    )

    # --- 计时开始 ---
    writer.add(
        LOCAL_EVENT, MSG_EVENT, FIELDS_EVENT,
        [start_fit, EVENT_TIMER, EVENT_TYPE_START, 0],
    )

    # --- 逐秒记录 ---
    def record_values(sample: Sample, steps: float, cumulative_kcal: float) -> list:
        speed_ms = sample.speed_kmh / 3.6 if math.isfinite(sample.speed_kmh) else None
        # 累计卡路里四舍五入后为 0 时不写（FIT 约定 0 就是「无数据」）
        kcal_value = int(round(cumulative_kcal)) or None
        return [
            int(sample.t) - FIT_EPOCH_OFFSET,
            _u8(_finite(sample.hr)),
            _u8(_finite(sample.cadence_spm)),
            _u32(_finite(sample.distance_m), 100),
            _u16(speed_ms, 1000),
            _u16(_finite(sample.power_w)),
            _resistance(sample, resistance_mode),
            _u32(steps),
            kcal_value,
            _u32(speed_ms, 1000),
        ]

    # 记录里填累计卡路里，需要按记录时间点插值出累计值
    kcal_column = {
        "none": None,
        "power": "kcal_power",
        "keytel": "kcal_keytel",
        "keytel_net": "kcal_keytel_net",
        "machine": "kcal_active",
    }[calories]

    steps = 0.0
    prev_t = samples[0].t
    last_kcal = 0.0
    for i, sample in enumerate(samples):
        dt = sample.t - prev_t if i else 0.0
        prev_t = sample.t
        if 0 < dt <= 60:
            steps += _zero(sample.cadence_spm) * dt / 60.0
        cumulative = getattr(sample, kcal_column) if kcal_column else 0.0
        if not math.isfinite(cumulative):
            cumulative = last_kcal
        # 累计值必须单调非减，否则下游会认为数据错乱
        cumulative = max(cumulative, last_kcal)
        last_kcal = cumulative
        writer.add(
            LOCAL_RECORD, MSG_RECORD, FIELDS_RECORD,
            record_values(sample, steps, cumulative),
        )

    # --- 计时结束 ---
    writer.add(
        LOCAL_EVENT, MSG_EVENT, FIELDS_EVENT,
        [end_fit, EVENT_TIMER, EVENT_TYPE_STOP, 0],
    )

    avg_speed_ms = stats.avg_speed / 3.6 if stats.avg_speed is not None else None
    max_speed_ms = stats.max_speed / 3.6 if stats.max_speed is not None else None

    # --- lap ---
    writer.add(
        LOCAL_LAP, MSG_LAP, FIELDS_LAP,
        [
            end_fit,
            start_fit,
            _u32(stats.elapsed_s, 1000),
            _u32(stats.timer_s, 1000),
            _u32(stats.distance_m, 100),
            _u32(stats.steps),
            _u16(stats.calories),
            _u16(avg_speed_ms, 1000),
            _u16(max_speed_ms, 1000),
            _u8(stats.avg_hr),
            _u8(stats.max_hr),
            _u8(stats.min_hr),
            _u8(stats.avg_cadence),
            _u8(stats.max_cadence),
            _u16(stats.avg_power),
            _u16(stats.max_power),
            LAP_TRIGGER_SESSION_END,
            sport,
            sub_sport,
            _u32(avg_speed_ms, 1000),
            _u32(max_speed_ms, 1000),
        ],
    )

    # --- session ---
    writer.add(
        LOCAL_SESSION, MSG_SESSION, FIELDS_SESSION,
        [
            end_fit,
            EVENT_SESSION,
            EVENT_TYPE_STOP,
            start_fit,
            sport,
            sub_sport,
            _u32(stats.elapsed_s, 1000),
            _u32(stats.timer_s, 1000),
            _u32(stats.distance_m, 100),
            _u32(stats.steps),
            _u16(stats.calories),
            _u16(avg_speed_ms, 1000),
            _u16(max_speed_ms, 1000),
            _u8(stats.avg_hr),
            _u8(stats.max_hr),
            _u8(stats.avg_cadence),
            _u8(stats.max_cadence),
            _u16(stats.avg_power),
            _u16(stats.max_power),
            0,
            1,
            SESSION_TRIGGER_ACTIVITY_END,
            _u32(stats.work_j),
            _u8(stats.min_hr),
            _u32(avg_speed_ms, 1000),
            _u32(max_speed_ms, 1000),
        ],
    )

    # --- activity ---
    writer.add(
        LOCAL_ACTIVITY, MSG_ACTIVITY, FIELDS_ACTIVITY,
        [
            end_fit,
            _u32(stats.timer_s, 1000),
            1,
            ACTIVITY_TYPE_MANUAL,
            EVENT_ACTIVITY,
            EVENT_TYPE_STOP,
            end_fit + utc_offset,
        ],
    )

    return writer.tobytes()


# --------------------------------------------------------------------------- #
# 自检：把生成的字节重新解析一遍
# --------------------------------------------------------------------------- #

# 定义字节里的 base type = 类型号 & 0x1F（高位是大小端标志）
BASE_TYPE_SIZE = {0x00: 1, 0x02: 1, 0x04: 2, 0x05: 4, 0x06: 4, 0x0C: 4, 0x07: 1}
BASE_TYPE_MASK = 0x1F

def verify_fit(data: bytes) -> list[str]:
    """不依赖外部库地回读 FIT 字节，返回发现的问题列表（空 = 通过）。"""
    problems: list[str] = []

    if len(data) < 14:
        return ["文件短于头部长度"]
    header_size = data[0]
    if header_size not in (12, 14):
        problems.append(f"头部长度异常: {header_size}")
    if data[8:12] != b".FIT":
        problems.append(f"签名错误: {data[8:12]!r}")

    data_size = struct.unpack_from("<I", data, 4)[0]
    expected = header_size + data_size + 2
    if expected != len(data):
        problems.append(f"长度不符: 头部声明 {expected}，实际 {len(data)}")

    # 头部 CRC
    if header_size == 14:
        want = struct.unpack_from("<H", data, 12)[0]
        got = fit_crc(data[:12])
        if want != got:
            problems.append(f"头部 CRC 不符: {want:04x} != {got:04x}")

    # 文件 CRC
    want = struct.unpack_from("<H", data, len(data) - 2)[0]
    got = fit_crc(data[:-2])
    if want != got:
        problems.append(f"文件 CRC 不符: {want:04x} != {got:04x}")

    # 逐条走定义与数据
    pos = header_size
    end = header_size + data_size
    definitions: dict[int, list] = {}
    counts: dict[int, int] = {}
    while pos < end:
        header = data[pos]
        pos += 1
        if header & 0x80:  # 压缩时间戳头，本工具不使用
            problems.append(f"偏移 {pos - 1}: 出现压缩时间戳消息")
            break
        local = header & 0x0F
        if header & 0x40:  # 定义消息
            if pos + 5 > end:
                problems.append(f"偏移 {pos - 5}: 定义消息截断")
                break
            global_num = struct.unpack_from("<H", data, pos + 2)[0]
            num_fields = data[pos + 4]
            pos += 5
            fields = []
            for _ in range(num_fields):
                fields.append((data[pos], data[pos + 1], data[pos + 2]))
                pos += 3
            if header & 0x20:
                problems.append(f"local {local}: 本工具不应写开发者字段")
            definitions[local] = (global_num, fields)
        else:
            if local not in definitions:
                problems.append(f"偏移 {pos - 1}: 未定义 local {local}")
                break
            global_num, fields = definitions[local]
            for _, size, base in fields:
                if base & BASE_TYPE_MASK not in BASE_TYPE_SIZE:
                    problems.append(f"local {local}: 未知 base type {base:#x}")
                pos += size
            counts[global_num] = counts.get(global_num, 0) + 1

    if pos != end:
        problems.append(f"数据区解析后停在 {pos}，应为 {end}")

    # 结构完整性
    for num, name in ((0, "file_id"), (18, "session"), (19, "lap"),
                      (20, "record"), (34, "activity")):
        if counts.get(num, 0) == 0:
            problems.append(f"缺少必需的 {name} 消息")
    if counts.get(18, 0) > 1:
        problems.append(f"session 消息数量异常: {counts[18]}")
    if counts.get(19, 0) != counts.get(18, 0):
        problems.append(f"lap({counts.get(19, 0)}) 与 session({counts.get(18, 0)}) 数量不匹配")
    if counts.get(34, 0) != 1:
        problems.append(f"activity 消息应为 1 条，实为 {counts.get(34, 0)}")

    return problems


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把椭圆机采集 CSV 导出为 FIT 活动文件",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("csv", type=Path, help="ftms_capture.py 生成的 CSV")
    parser.add_argument("-o", "--output", type=Path, help="输出 .fit 路径（默认同名）")
    parser.add_argument(
        "--sport", type=int, default=SPORT_FITNESS_EQUIPMENT,
        help="FIT sport 枚举（4 = fitness_equipment）",
    )
    parser.add_argument(
        "--sub-sport", default="elliptical",
        help="FIT sub_sport 名称或数字（elliptical / indoor_cycling / ...）",
    )
    parser.add_argument(
        "--calories", default="keytel_net",
        choices=["power", "keytel", "keytel_net", "machine", "none"],
        help="写入 FIT 的卡路里来自哪一路",
    )
    parser.add_argument(
        "--resistance", default="percent",
        choices=["percent", "raw", "turns"],
        help="record.resistance 字段写哪个量",
    )
    parser.add_argument(
        "--max-hr-age", type=float, default=10.0,
        help="心率超过该秒数未刷新则视为无效",
    )
    parser.add_argument(
        "--max-gap", type=float, default=10.0,
        help="超过该秒数的空档视为停顿，不计入计时",
    )
    parser.add_argument("--device-name", default="ftms_capture", help="FIT 中的设备名")
    parser.add_argument("--product", type=int, default=1, help="FIT product 编号")
    parser.add_argument("--serial", type=int, help="FIT 序列号（默认由文件名推导）")
    parser.add_argument("-q", "--quiet", action="store_true", help="不打印报告")
    parser.add_argument(
        "--verify", action="store_true", default=True,
        help="生成后做结构自检（默认开启）",
    )
    parser.add_argument(
        "--no-verify", dest="verify", action="store_false", help="跳过结构自检",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="数据校验发现问题时以非零码退出（适合批量脚本）",
    )
    return parser.parse_args(argv)


def resolve_sub_sport(value: str) -> int:
    if value.isdigit():
        return int(value)
    try:
        return SUB_SPORT_BY_NAME[value]
    except KeyError:
        raise SystemExit(
            f"未知 sub_sport: {value}\n可选: {', '.join(SUB_SPORT_BY_NAME)} 或直接给数字"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    csv_path: Path = args.csv
    if not csv_path.is_file():
        raise SystemExit(f"找不到文件: {csv_path}")

    out_path = args.output or csv_path.with_suffix(".fit")
    sub_sport = resolve_sub_sport(str(args.sub_sport))

    issues = Issues()
    samples = load_samples(csv_path, issues, args.max_hr_age)
    if len(samples) < 2:
        raise SystemExit(f"有效样本不足（{len(samples)} 条），无法生成 FIT")

    stats = compute_stats(samples, args.max_gap, args.calories)
    serial = args.serial if args.serial is not None else zlib.crc32(csv_path.name.encode())

    fit = build_fit(
        samples, stats,
        sport=args.sport,
        sub_sport=sub_sport,
        calories=args.calories,
        resistance_mode=args.resistance,
        serial=serial or 1,
        device_name=args.device_name,
        product=args.product,
        utc_offset=local_utc_offset(samples),
    )
    # 结构自检失败绝不写盘，避免留下坏文件
    problems = verify_fit(fit) if args.verify else []
    if problems:
        print("== 结构校验 ==", file=sys.stderr)
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        print("结构校验未通过，未写出文件", file=sys.stderr)
        return 3
    out_path.write_bytes(fit)

    if args.strict and (issues.dropped or issues.bad_timestamp
                        or issues.bad_distance or issues.hr_dropped):
        print(f"数据存在质量问题，--strict 下退出（文件已写出：{out_path}）", file=sys.stderr)
        return 2

    if not args.quiet:
        if args.verify:
            print("== 结构校验 ==")
            print("  ✓ 头部 / 定义 / 消息 / CRC 全部通过")
            print()
        print("== 数据校验 ==")
        for line in issues.report():
            print("  " + line if not line.startswith("CSV") else line)
        print()
        print("== 活动统计 ==")
        print(f"  样本              {len(samples)} 条")
        print(f"  时间跨度          {stats.elapsed_s:.0f} s")
        print(f"  计时时间          {stats.timer_s:.0f} s")
        print(f"  距离              {stats.distance_m:.1f} m")
        print(f"  步数              {stats.steps:.0f}")
        print(f"  机械功            {stats.work_j / 1000:.1f} kJ")
        print(f"  卡路里({args.calories})  {stats.calories:.1f} kcal")
        if stats.avg_speed is not None:
            print(f"  速度              均 {stats.avg_speed:.1f} / 最大 {stats.max_speed:.1f} km/h")
        if stats.avg_power is not None:
            print(f"  功率              均 {stats.avg_power:.1f} / 最大 {stats.max_power:.0f} W")
        if stats.avg_cadence is not None:
            print(f"  步频              均 {stats.avg_cadence:.0f} / 最大 {stats.max_cadence:.0f} spm")
        if stats.avg_hr is not None:
            print(f"  心率              均 {stats.avg_hr:.0f} / {stats.min_hr:.0f}–{stats.max_hr:.0f} bpm")
        else:
            print("  心率              无有效数据")
        if stats.max_resistance is not None:
            print(f"  阻力              最大 {stats.max_resistance:.0f}")
        print()
        print(f"== 已写入 {out_path}  ({len(fit)} 字节) ==")
        print(f"   sport={args.sport} ({'fitness_equipment' if args.sport == 4 else '自定义'})"
              f"  sub_sport={sub_sport}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
