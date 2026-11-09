# elliptical-ftms

YESOUL EF25 椭圆机（FTMS 设备）+ BLE 心率设备的 Linux 数据采集工具。

不依赖厂商 App、不需要配对，直接用 BlueZ 订阅 GATT 通知，把椭圆机的实时数据
（速度 / 距离 / 步频 / 阻力 / 功率 / 卡路里 / 时间）和独立心率带的数据一起落成 CSV。

协议细节、字段逆向过程和标定数据见 **[PROTOCOL.md](PROTOCOL.md)**。

---

## 环境要求

- Linux + BlueZ 5.x（`bluetoothd` 运行中，适配器已上电）
- Python 3.11+（3.9/3.10 也能跑，但没有 `tomllib`，配置文件不可用）
- PyGObject（`gi`）与 Gio，即发行版的 `python-gobject`

检查：

```bash
python3 -c "import gi; gi.require_version('Gio','2.0'); from gi.repository import Gio; print('ok')"
```

---

## 快速开始

```bash
# 1. 扫一下附近有哪些 FTMS / 心率设备
python3 ftms_capture.py --scan

# 2. 用配置文件跑（推荐）
cp config.toml.example config.toml   # 然后填自己的设备 MAC 和年龄体重
python3 ftms_capture.py --config config.toml --duration 1800

# 3. 或全部用命令行
python3 ftms_capture.py --name YS_ --hr-name COROS --age 40 --weight 62 --sex m
```

采集时终端会每秒滚动一行：

```
23:53:57  00:41  2.78km/h  均速1.93   23m  步频116  阻力 0.0(0.0圈)   75W  2.90kcal  ♥ 90
```

末尾的 `♥ 90` 是心率；数字后的 `!` 表示心率数据超过 5 秒没更新（信号弱 / 掉线）。

按 `Ctrl-C` 正常收尾，CSV 会自动 flush。

---

## 配置文件

模板见 `config.toml.example`，复制一份改名为 `config.toml` 再填：

```bash
cp config.toml.example config.toml
```

> `config.toml` 已在 `.gitignore` 里——里面是设备 MAC 和个人年龄体重，不该进仓库。

```toml
[device]
address = "EB:80:03:XX:XX:XX"   # 椭圆机 MAC
name    = ""                     # 或者用广播名前缀，如 "YS_"
hr      = ""                     # 心率设备 MAC，留空则不采心率
hr_name = ""                     # 或者用前缀，如 "COROS"

[profile]
age    = 40
weight = 62.0
sex    = "m"                     # "m" 男 / "f" 女

[output]
outdir = "data"
prefix = "ride"
jsonl  = true
quiet  = false
```

**优先级：命令行参数 > 配置文件 > 内置默认值。**

```bash
# 临时把时长改成 5 分钟，其余照配置文件
python3 ftms_capture.py --config config.toml --duration 300

# 临时关掉心率
python3 ftms_capture.py --config config.toml --hr ""
```

`age` / `weight` 是 Keytel 心率热量公式要用的，不填则 `kcal_keytel` 两列为 0。

---

## 心率设备

椭圆机**没有**心率传感器（握把是塑料的，`Fitness Machine Feature` 的 HR 位为 0），
所以心率必须外接。任何走标准 Heart Rate Service（`0x180D` / `0x2A37`）的设备都行：

- 胸带：Polar H10、Garmin HRM-Dual、Magene H64（精度最好）
- 臂带：Polar Verity Sense
- 手表广播：COROS（设置里打开「心率广播」）、Garmin（「广播心率」）

已在 **COROS APEX 2 Pro** 上验证可用：广播 `0x180D`，1 Hz，flags `0x06`
（u8 心率 + 接触检测），不含 RR 间期。

> 腕式光电在椭圆机上精度存疑（手臂摆动干扰）。实测 5 分钟静阻骑行
> 相邻秒变化最大 4 bpm、无尖刺、接触标志全程正常，但那是低强度场景。
> 高强度或抓握手柄时建议换胸带。

---

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--config FILE` | — | TOML 配置文件 |
| `--scan` | — | 扫描并列出附近的 FTMS / 心率设备，然后退出 |
| `--address MAC` | — | 椭圆机地址 |
| `--name PREFIX` | — | 椭圆机广播名前缀（如 `YS_`） |
| `--hr MAC` | — | 心率设备地址 |
| `--hr-name PREFIX` | — | 心率设备广播名前缀（如 `COROS`） |
| `--age N` | `0` | 年龄，Keytel 公式用 |
| `--weight KG` | `0` | 体重，Keytel 公式用 |
| `--sex m\|f` | `m` | 性别，Keytel 公式用 |
| `--duration SEC` | `0`（不限） | 采集时长，到点自动结束 |
| `--outdir DIR` | `./data` | 输出目录 |
| `--prefix NAME` | `ride` | 文件名前缀 |
| `--jsonl` | 关 | 额外把原始帧写成 jsonl，便于事后重新解析 |
| `--quiet` | 关 | 不在终端打印实时行 |
| `--scan-timeout SEC` | `15` | 扫描等待时间 |

`--address` 和 `--name` 都不给时，会自动选中第一个广播里带 `0x1826` 的设备。

---

## 输出

每次采集产出 2–3 个文件：

| 文件 | 内容 |
|---|---|
| `ride_<stamp>.csv` | 逐秒合并行：FTMS 字段 + 最近一次心率 |
| `ride_<stamp>.jsonl` | 原始 `0x2ACE` 帧（`--jsonl` 时） |
| `ride_<stamp>_hr.jsonl` | 原始心率帧，1 Hz 全量，含 RR/接触标志 |

### CSV 列

| 列 | 含义 | 单位 |
|---|---|---|
| `iso_time` / `t_epoch` | 时间戳 | — |
| `elapsed_s` | 机器计时 | s |
| `speed_kmh` | 瞬时速度 | km/h |
| `avg_speed_kmh` | 平均速度 | km/h |
| `distance_m` | 总距离 | m |
| `cadence_spm` | 步频 | steps/min |
| `resistance_raw` | 阻力原始值 | 0–91 |
| `resistance_turns` | 折算旋钮圈数 | turn |
| `resistance_pct` | 折算百分比 | % |
| `power_w` | 功率（机器估算，非实测） | W |
| `kcal_active` | 机器报的运动消耗 | kcal |
| `kcal_total` | 机器报的总消耗 | kcal |
| `hr` | 心率 | bpm |
| `hr_age_s` | 这个心率值有多旧 | s |
| `hr_contact` | 传感器接触检测 | 0/1 |
| `kcal_power` | 功率积分折算的消耗 | kcal |
| `kcal_keytel` | Keytel 心率公式（含静息） | kcal |
| `kcal_keytel_net` | Keytel 心率公式（扣静息） | kcal |
| `f12` / `f20` | 尚未确定的原生字段 | — |
| `raw_hex` | 原始 29 字节帧 | — |

**保留 `raw_hex` 是有意为之**：协议里还有两个字段（offset 12、20）没解开，
留着原始帧以后可以直接重解析，不用重新采集。

### 三路卡路里的意义

机器给的 `kcal_active` 是用它自己算出来的功率积分的，而那个功率本身又是
`f(步频, 阻力)` 推出来的（见 PROTOCOL.md §9），所以整条链路都是估算。
`kcal_power` 用同一套功率但口径明确（1 kJ 机械功 ≈ 1 kcal 消耗）。
`kcal_keytel` 是唯一带独立生理输入的（心率）。三列并排存着，方便事后比较。

---

## 注意事项

- **静止约 5 分钟机器会整场重置**，距离/时间/卡路里归零。要完整记录一场就别停太久。
- **阻力不用踩也能读到**（offset 16 照常上报），可以拿来调旋钮做标定。
- 机器**不带心率传感器**，心率必须外接。
- 功率不是真实功率计：实测 `power ≈ 1.16×步频 + 0.65×阻力 − 54`（R²=0.971），
  是算出来的不是测出来的，见 PROTOCOL.md §9。
- 阻力是机械旋钮的电位器读数，`6.5 圈 = 91`，换算见 `PROTOCOL.md` §4。
- 不需要配对；如果连接超时，多为 BlueZ 缓存问题，重跑一次即可。
- **COROS 手表关掉「心率广播」后 `0x180D` 就消失了**，脚本会一直重连不上。

---

## 许可

[MIT](LICENSE) © 2026 Yihan Song

协议分析结果（字段表、偏移、标定公式等）属于事实性信息，任何人可以自由使用；
但请注意，这些结论是在一台具体的 YESOUL EF25 上验证的，其他型号/固件可能不同。

---

## 文件

```
elliptical-ftms/
├── README.md              # 本文件
├── PROTOCOL.md            # 完整协议逆向记录（字段表、标定、证据、踩坑）
├── LICENSE                # MIT
├── config.toml.example    # 配置模板（复制成 config.toml 使用）
├── ftms_capture.py        # 采集脚本
└── data/                  # 采集输出（已 gitignore）
```
