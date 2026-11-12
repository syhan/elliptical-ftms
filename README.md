# elliptical-ftms

YESOUL EF25 椭圆机（FTMS 设备）+ BLE 心率设备的 Linux 数据采集工具。

不依赖厂商 App、不需要配对，直接用 BlueZ 订阅 GATT 通知，把椭圆机的实时数据
（速度 / 距离 / 步频 / 阻力 / 功率 / 卡路里 / 时间）和独立心率带的数据一起落成 CSV。

也可以挂成**长驻守护**：人一踩就自动开录，机器关机就自动收尾并生成 FIT，
同时把实时数据通过 **MQTT** 广播到局域网。

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
| `--fit` / `--no-fit` | 开 | 结束时自动生成 FIT |
| `--fit-calories` | `keytel_net` | FIT 里的卡路里用哪一路 |
| `--fit-resistance` | `percent` | FIT 里 resistance 写哪个量 |
| `--tui-port` | `0`（关） | 起网络 TUI 的端口，如 `2323` |
| `--tui-host` | `0.0.0.0` | TUI 监听地址，只给本机用就填 `127.0.0.1` |
| `--tui-color` / `--no-tui-color` | 开 | TUI 彩色输出 |

`--address` 和 `--name` 都不给时，会自动选中第一个广播里带 `0x1826` 的设备。

---

## 输出

每次采集产出 3–4 个文件：

| 文件 | 内容 |
|---|---|
| `ride_<stamp>.csv` | 逐秒合并行：FTMS 字段 + 最近一次心率 |
| `ride_<stamp>.fit` | **可导入 COROS 的活动文件**（结束时自动生成） |
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

## 守护模式（自动开录 / 自动收尾）

```bash
python3 ftms_capture.py --config config.toml --auto
```

跑起来之后什么都不用管：

```
* 等待椭圆机广播（name~YS_）…
* [20:03:12] 发现 YS_EF25_00156 (EB:80:03:5D:73:AF) → 第 1 局
* 连接椭圆机 EB:80:03:5D:73:AF YS_EF25_00156
* 已订阅 Cross Trainer Data，等待数据…
* 开始记录 data/ride_20261112_200318.csv
2026-11-12 20:03:20  00:02   8.42km/h  均速 8.40   17m  步频  52  阻力 12.0(0.9圈)   96W    0.21kcal  ♥118
...
* 面板计时器归零（1150s → 1s），本局结束
* 结束（面板清零），FTMS 1148 帧，心率 1160 帧 -> data/ride_20261112_200318.csv
* FIT  data/ride_20261112_200318.fit  (38241 字节)
   椭圆机 · 1148s · 3210 m · 心率均 128 bpm
* 等待椭圆机广播（name~YS_）…
```

一局结束的判定，按优先级：

| 信号 | 含义 | 默认 |
|---|---|---|
| **面板计时器归零** | 闲置 5 分钟机器自动清零，这一局结束 | 立即 |
| **蓝牙链路断开** | 面板关机 / 走远 | 10 s |
| **FTMS 数据静默** | 连接还在但不推数据 | 300 s |
| `Ctrl+C` | 手动停止 | 立即 |

蓝牙链路断开是最主要的信号，数据静默只是兜底 —— 中间歇一会儿不会把一局切断。

### 为什么是「踩动才开始」而不是「看到信号就开始」

面板通电后即使没人踩也会推零值。默认 `--auto-start-on motion` 会等第一次
`步频 > 0` 或 `速度 > 0` 才建文件，否则你开个机走开五分钟就能录出一坨零。
想改成「一看到广播就开」，用 `--auto-start-on signal`。

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--auto` | 关 | 长驻监听 |
| `--auto-start-on` | `motion` | `motion` 踩动才开 / `signal` 见到广播就开 |
| `--auto-disconnect-after` | `10` | 掉线多少秒算结束 |
| `--auto-stop-after` | `300` | 多久没数据算结束 |
| `--auto-min-frames` | `10` | 少于这么多帧不生成 FIT |
| `--auto-cooldown` | `60` | 连不上的设备冷却多久再试 |
| `--hr-any` | 关 | 不指定 MAC，用任何广播心率的设备 |

守护模式里如果没配 `hr` / `hr-name`，会自动用**任何**广播 `0x180D` 的设备
（比如手表开了心率广播）。

### 一个坑：BlueZ 不删已消失的设备

设备关机后 `org.bluez` 里那个对象还在，直接按它去连会一直失败。所以连不上的
设备会被冷却 `auto_cooldown` 秒，避免每 10 秒重试一次的垃圾循环。

---

## 网络 TUI（nc / telnet 连进来就看）

```bash
python3 ftms_capture.py --config config.toml --auto --tui-port 2323
```

然后局域网里任何设备：

```bash
nc 10.0.0.177 2323
# 或
telnet 10.0.0.177 2323
```

屏幕会按你的终端尺寸自适应铺满，全部指标都用大字。下面是 96×30 的满配版：

```
┌─ YS_EF25_00156  ·  ride_20261113_000448 ────────────────────────────── 11-13 00:17:44 记录中 ┐
│             ████      █  █ ████                                  █ ████ ████                 │
│             █  █      █  █    █                                  █    █    █                 │
│             ████      ████ ████                                  █ ████ ████                 │
│                █         █    █                                  █ █    █                    │
│             ████ ██      █ ████                                  █ ████ ████                 │
│                 瞬时速度 km/h                                     心率 bpm                   │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│          ████ █  █                      ████ ████                       █ ████ ████          │
│          █    █  █                      █    █                          █ █  █ █  █          │
│          ████ ████                      ████ ████                       █ █  █ ████          │
│             █    █                         █    █                       █ █  █ █  █          │
│          ████    █                      ████ ████                       █ ████ ████          │
│           步频 spm                    阻力 ▁▂▃▃▄▅▁▁▁▁                     功率 W             │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│             ████ ████ ████ ████                                  █    █ ████                 │
│                █ █  █ █  █ █  █                                  █    █ █  █                 │
│             ████ ████ ████ █  █                                  █    █ ████                 │
│             █       █ █  █ █  █                                  █    █ █  █                 │
│             ████ ████ ████ ████                                  █    █ ████                 │
│                    距离 m                                        消耗 kcal                   │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│           ████ ████         █ █  █                         ████      ████ ████               │
│              █ █     ██     █ █  █                         █  █      █       █               │
│           ████ ████         █ ████                         ████      ████ ████               │
│           █       █  ██     █    █                         █  █      █  █ █                  │
│           ████ ████         █    █                         ████ ██   ████ ████               │
│                     用时                                         均速 km/h                   │
│ 速度 ▆▆▆▅▆▆▅▆▆▆▇▇▇▇▇█▇▇▇▇▆▆▅▅▅▄▄▄▄▄▄▄▄▄▄▄▄▄▃▃▂▂▁▁▁▁▁▁▁▁▁▁▂▂▂▂▂▂▂▂▁▁▁▁▁▁▂▂▃▃▄▄▅▅▅▅▅▆▅▅▅▅▅▅▅▅▆ │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

几个细节：

- **标题栏右边永远有时间**（`11-13 00:17:44`）。窄到放不下时依次丢掉日期、
  再丢状态，**时间永远保留**
- **阻力是格栅条**（`▁▂▃▃▄▅▁▁▁▁`）：亮起的格子高度递增，露出一格就是 10%
- **心率前面的红心会闪**（0.8 秒一拍）；手表没广播时是灰色常亮的 `♥`，
  数值显示 `--`
- 第一排左边是**瞬时速度**，第四排的**均速**跟它对照；两处都写清楚标签，
  不会再有「这是瞬时还是平均」的疑问
- 屏幕高度决定排数：`≥29` 四排、`23–28` 三排（少了 用时/均速）、
  `17–22` 两排（再少 距离/消耗），再矮就退到中号布局
- 剩下的行给底部迷你曲线，最多 3 行，不抢地方

### 尺寸是怎么拿到的

裸 TCP 连接没有「终端尺寸」这种东西，所以分两条路：

| 客户端 | 机制 |
|---|---|
| `telnet` | 标准 NAWS 协商（option 31），服务端主动发起 |
| `nc` | 发 XTWINOPS 查询 `ESC[18t`，终端把 `ESC[8;行;列t` 写回自己的输入流，顺着 nc 回来 |

两个都拿不到就退回 80×24。连上后每 3 秒复查一次，所以中途拖窗口大小也能跟上。

服务端一连接上就主动发 telnet 协商（不管对方是谁），紧接着切备用屏再清屏，
所以 `nc` 用户不会看到乱码残影。

### 三档布局

| 条件 | 布局 |
|---|---|
| 宽 ≥ 72 且 高 ≥ 18 | 大字仪表：按行数铺 2–4 排大号读数，底部留最多 3 行迷你曲线 |
| 宽 ≥ 46 且 高 ≥ 8 | 中号：彩色数值行 + 指标 + 单行迷你图 |
| 更小 | 紧凑：纯文本，按宽度贪心换行 |

大号布局里**每排几个格子**也随之变化：两格（瞬时速度/心率）、三格（步频/阻力/功率）、
两格（距离/消耗）、两格（用时/均速）。行宽不够时格子会退回普通文本而不是溢出。

行宽按**显示列数**算（中文算 2 列），所以中英文混排的边框不会歪。

### 只想本地先看看

```bash
python3 ftms_tui.py --demo     # 假数据，不用连椭圆机
nc 127.0.0.1 2323
```

---

## MQTT 广播（可选）

```toml
[mqtt]
mqtt_host = "127.0.0.1"       # 留空则完全关闭
mqtt_port = 1883
mqtt_topic = "elliptical"
mqtt_ha = false               # true = 额外发 Home Assistant 自动发现
```

发布的内容：

| 主题 | 保留 | 内容 |
|---|---|---|
| `elliptical/state` | 是 | 1 Hz 实时状态（见下） |
| `elliptical/session` | 否 | `{"event":"start"\|"end", ...}` |
| `elliptical/availability` | 是 | `online` / `offline`（LWT 遗嘱） |

`state` 的 JSON：

```json
{"ts":"20261112_200318","elapsed":142,"speed":8.42,"cadence":52,
 "power":96,"resistance":13.2,"resistance_raw":12.0,"distance":120,
 "hr":118,"calories":2.4,"calories_machine":3.1}
```

`availability` 用 LWT 实现：进程被 kill -9 或断电时，broker 会替你发 `offline`。

broker 有两种选择：指向已有的（Home Assistant 之类），或者**让被记录的机器
自己当 broker** —— `deploy/install.sh` 默认会装 `mosquitto` 并配置成局域网可读，
这样最省事：机器常开，局域网里谁都能订阅。

**客户端是自带实现的**（`ftms_mqtt.py`，MQTT 3.1.1，只做发布），
不需要 `pip install paho-mqtt` —— 目标机器是 Debian 12，pip 有 PEP 668 限制，
少一个依赖少一堆事。支持用户名密码、QoS 0/1、retain、断线指数退避重连。

---

## 部署成后台服务

```bash
ssh syhan@10.0.0.177
~/elliptical-ftms/deploy/install.sh
```

之后更新代码**不要**在目标机上 `git pull` —— 它访问 GitHub 经常超时。用：

```bash
./deploy/sync.sh syhan@10.0.0.177
```

这条命令走 SSH 直推（目标机需要先 `git config receive.denyCurrentBranch updateInstead`），
推完自动重启服务，整个过程不依赖 GitHub。

`install.sh` 会装 `bluez` + `python3-gi`、克隆仓库、生成 `config.toml`、
装一个 **systemd 用户服务**并 `enable-linger`（不登录也随开机启动）。

```bash
journalctl --user -u elliptical-ftms -f     # 看日志
systemctl --user restart elliptical-ftms    # 重启
systemctl --user stop elliptical-ftms       # 停止（会先写完 FIT）
```

服务收到 SIGTERM 会走正常收尾流程（关文件、写 FIT），`TimeoutStopSec=60`。

## 导出 FIT

**采集结束时（Ctrl+C 或到达 `--duration`）会自动生成同名 `.fit`，不需要额外操作。**

```
$ python3 ftms_capture.py --config config.toml
…
^C
* 收到中断信号，正在收尾…
* 结束，FTMS 1180 帧，心率 1195 帧 -> data/ride_20261112_200312.csv
* FIT  data/ride_20261112_200312.fit  (38241 字节)
   椭圆机 · 1198s · 3210 m · 心率均 128 bpm
```

一次踩踏产出三个文件：

```
data/ride_20261112_200312.csv    ← 逐秒原始数据
data/ride_20261112_200312.fit    ← 可直接导入 COROS
data/ride_20261112_200312.jsonl  ← 原始帧（--jsonl 时）
```

Ctrl+C 会**立即**响应（信号接进了 GLib 主循环，不用等 5 秒巡检），
收尾阶段平均 0.3 秒完成。

如果只想拿到 CSV 不要 FIT，加 `--no-fit`，或在 `config.toml` 里设 `fit = false`。

### 手动导出

想改口径重新导一次，或者导旧文件：

```bash
# 最简：生成同名 .fit
python3 export_fit.py data/ride_20261109_235238.csv

# 指定输出
python3 export_fit.py data/ride_20261109_235238.csv -o ~/elliptical.fit
```

### 写入 FIT 的字段

| FIT 消息 | 字段 | 来源 |
|---|---|---|
| `file_id` | `type=activity`、`manufacturer=development`、`serial_number` | 自动 |
| `device_info` | `device_index=creator`、`software_version` | 自动 |
| `event` | timer start / timer stop | 首末样本时间 |
| `record` | `timestamp` | `t_epoch` |
| | `heart_rate` | `hr`（做过生理范围与新鲜度过滤） |
| | `cadence` | `cadence_spm` |
| | `power` | `power_w` |
| | `speed` / `enhanced_speed` | `speed_kmh` ÷ 3.6 |
| | `distance` | `distance_m` |
| | `resistance` | `resistance_pct`（可用 `--resistance` 换口径） |
| | `total_cycles` | 步频积分得到的累计步数 |
| | `accumulated_power` | 功率积分得到的累计值 |
| | `calories` | 累计卡路里 |
| | `activity_type` | 由 `--sport` 推导（椭圆机 = 4），COROS 每条 record 都写这个 |
| `lap` / `session` | `sport=fitness_equipment`、`sub_sport=elliptical` | **这就是「椭圆机」类型** |
| | 平均/最大 心率、步频、功率、速度 | 按时间加权 |
| | `total_cycles`、`total_distance`、`total_calories`、`total_work` | 统计 |
| `activity` | `local_timestamp` | 自动从 `iso_time` 推导时区 |

**`resistance` 是 FIT 标准字段（record 编号 10，uint8）。** 这个字段码表和手表都不会写，
只有我们能填 —— 前提是目标平台愿意展示它（COROS 导入后是否显示待实测）。

### 数据校验

导出时默认做两层校验：

**第一层 —— CSV 数据质量**（打印在终端）：

| 检查 | 处理 |
|---|---|
| 时间戳缺失/非正 | 丢弃该行 |
| 同一秒多条 | 保留最后一条 |
| 时间倒序 | 重新排序并计数 |
| 速度 > 70 km/h | 判为脏数据丢弃 |
| 步频 > 255、功率 > 2000 W、阻力 > 254 | 判为脏数据丢弃 |
| 心率超出 25–250 bpm | 置为无效 |
| 心率超 `--max-hr-age` 秒未刷新 | 置为无效 |
| 距离回退或缺失 | 用前一个值修补 |

越界值**不做截断**——截断会把 5000 W 变成 2000 W 污染最大值，直接丢弃更诚实。

**第二层 —— FIT 结构自检**（写盘前执行，不通过就不写文件）：

- 头部长度 / 签名 `.FIT` / 协议版本
- 头部 CRC 与文件 CRC
- 声明长度与实际长度一致
- 每个 data 消息都能找到对应定义
- base type 合法、消息不截断
- 必需的 `file_id` / `session` / `lap` / `record` / `activity` 齐全
- `lap` 与 `session` 数量匹配、`activity` 恰好 1 条

### 面板清零与末尾空闲的裁切

椭圆机闲置约 5 分钟会把计时器、距离、卡路里全部归零。这带来两个问题：

1. **跨度越长，FIT 越离谱** —— 计时器倒退会让时长对不上，距离倒退会被当成
   脏数据修补。所以默认 `--on-reset cut`：一旦发现计数器倒退，就在**第一处**
   截断，只导出前面那一局（一条 FIT 只能装一局）。
2. **末尾挂着几分钟零值** —— 面板关机前、或者你擦完汗坐着的时候还在推零。
   默认 `--trim-idle 60` 保留最后一次踩动后的 60 秒收尾，再后面的全裁掉。

想看完整原始数据（不裁）：

```bash
python3 export_fit.py data/ride.csv --on-reset ignore --trim-idle 0
```

报告里会写清楚发生了什么：

```
  面板计数器归零    1 处（已在首处截断）
  末尾空闲已裁掉    340 行
```

守护模式在归零的**那一刻**就收尾，所以正常情况下 CSV 里根本不会有归零。

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `-o, --output` | 同名 `.fit` | 输出路径 |
| `--sport` | `4` | FIT sport 枚举，4 = fitness_equipment |
| `--sub-sport` | `elliptical` | 子类型名称或数字 |
| `--calories` | `keytel_net` | 哪一路卡路里写进 FIT |
| `--resistance` | `percent` | `resistance` 字段写 raw / turns / percent |
| `--max-hr-age` | `10` | 心率过期阈值（秒） |
| `--max-gap` | `10` | 超过该秒数的空档算停顿，不计入计时 |
| `--on-reset` | `cut` | 面板计时器归零时：`cut` 在首处截断 / `ignore` 不管 |
| `--trim-idle` | `60` | 末尾连续空闲超过该秒数就裁掉（保留 60 s 收尾），0 = 不裁 |
| `--device-name` | `ftms_capture` | FIT 里的设备名 |
| `--manufacturer` | `255` | FIT manufacturer 编号（294 = coros） |
| `--mimic-coros` | 关 | 伪造 COROS 设备身份，见下 |
| `--strict` | 关 | 数据有问题时以退出码 2 结束 |
| `--no-verify` | 关 | 跳过结构自检 |

### `--calories` 选哪一路

| 值 | 含义 | 可信度 |
|---|---|---|
| `machine` | 机器报的 `kcal_active` | 口径不明，但量级正常 |
| `power` | 功率积分（1 kJ ≈ 1 kcal） | 口径明确，但功率本身是算出来的 |
| `keytel` | Keytel 心率公式（含静息） | 有独立生理输入 |
| `keytel_net` | Keytel 扣掉静息 | **默认**，最接近「运动净消耗」 |
| `none` | 不写卡路里 | — |

默认用 `keytel_net`，因为它唯一带独立生理输入。如果那场没戴心率带，
会自动退回机器值。

### 关于 `--resistance` 的口径

FIT 的 `record.resistance` 是 uint8，没有单位定义。三个选项：

| 值 | 写入 | 范围 | 适合 |
|---|---|---|---|
| `percent` | `resistance_pct` | 0–100 | **默认**，通用可读 |
| `raw` | `resistance_raw` | 0–91 | 与 PROTOCOL.md 对齐 |
| `turns` | `resistance_turns` | 0–6.5 | 会被取整，精度损失大 |

### 导入 COROS

生成的 `.fit` 在 COROS App 里走「导入」入口即可（与颂拓记录同样方式）。

**注意**：外部导入的活动不会进入 COROS 的训练负荷/恢复模型，那些只针对手表自己记录的活动。

### `--mimic-coros`：伪装成 COROS 设备

从真实 COROS 活动文件里提取出的身份信息：

| 字段 | 值 |
|---|---|
| `file_id.manufacturer` | `294`（coros） |
| `file_id.product` | `822`（APEX 2 Pro） |
| `file_id.product_name` | `COROS APEX 2 Pro` |
| `file_id.serial_number` | **不写**（COROS 自己不写） |
| `device_info` | 只有 timestamp / manufacturer / product_name 三个字段 |
| `developer_data_id` | `manufacturer_id=294`，`application_id=[0]×15+[71]` |

```bash
python3 export_fit.py data/ride_20261109_235238.csv --mimic-coros
```

**重要：COROS 的 FIT 里没有任何签名、标志位或校验值。** 除标准 FIT CRC 以外
（而 CRC 我们已经能逐字节复现），文件里没有能证明「这条活动是 COROS 录的」的东西。
所以这个开关只是把能伪装的元数据都伪装了，**能不能骗过导入管道是另一回事**。
它的用途是做一个 A/B 对照实验。详见 PROTOCOL.md §13。

---

## 注意事项

- **静止约 5 分钟机器会整场重置**，距离/时间/卡路里归零。这个归零被当成
  「一局结束」的信号：守护模式当场收尾，导出器也会在第一处归零处截断（见下）。
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
├── ftms_capture.py        # 采集脚本（单次 / 守护模式）
├── ftms_mqtt.py           # 零依赖 MQTT 3.1.1 发布客户端
├── ftms_tui.py            # 网络 TUI（nc / telnet，自适应终端尺寸）
├── export_fit.py          # CSV → FIT 导出（自带编码器与校验器）
├── deploy/
│   ├── install.sh                 # 目标机一键部署
│   ├── sync.sh                    # 开发机直推更新（不走 GitHub）
│   ├── mosquitto-elliptical.conf  # 局域网 MQTT broker 配置
│   └── elliptical-ftms.service    # systemd 用户服务
└── data/                  # 采集输出（已 gitignore）
```
