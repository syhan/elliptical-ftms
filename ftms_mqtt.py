#!/usr/bin/env python3
"""极简 MQTT 3.1.1 发布客户端 —— 零第三方依赖。

只做「发布」，不做订阅，所以比通用客户端短得多：

- 后台线程维护连接，断线指数退避重连
- 支持 LWT（遗嘱消息）与 retain
- 断线期间的消息直接丢弃（1 Hz 实时数据补发没有意义）
- 不支持 TLS / QoS 2（局域网内没必要）

为什么不直接用 paho-mqtt：目标机器是 Debian 12，pip 有 PEP 668
限制，装包需要 --break-system-packages 或建 venv；而这里只需要
PUBLISH 一个动作，自己写反而更省事。

用法
----
    from ftms_mqtt import MqttClient

    c = MqttClient("10.0.0.136", 1883, "elliptical",
                   will_topic="elliptical/availability")
    c.start()
    c.publish("elliptical/state", '{"speed": 8.4}', retain=True)
    c.stop()
"""

from __future__ import annotations

import json
import queue
import socket
import struct
import threading
import time

CONNECT = 0x10
CONNACK = 0x20
PUBLISH = 0x30
PUBACK = 0x40
PINGREQ = 0xC0
PINGRESP = 0xD0
DISCONNECT = 0xE0

# qos | retain 组合到固定头的低 4 位
QOS_FLAG = {0: 0x00, 1: 0x02}


def _enc_remaining(n: int) -> bytes:
    """MQTT 的变长「剩余长度」编码。"""
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        out.append(byte | (0x80 if n else 0x00))
        if not n:
            return bytes(out)


def _enc_str(value) -> bytes:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return struct.pack(">H", len(data)) + data


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接被对端关闭")
        buf += chunk
    return buf


def _recv_packet(sock: socket.socket) -> tuple[int, bytes]:
    """读一个完整的 MQTT 控制包，返回 (固定头首字节, 剩余内容)。"""
    head = _recv_exact(sock, 1)[0]
    length, shift = 0, 0
    while True:
        byte = _recv_exact(sock, 1)[0]
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
        if shift > 21:
            raise ConnectionError("剩余长度编码非法")
    body = _recv_exact(sock, length) if length else b""
    return head, body


class MqttClient:
    """发布专用的后台 MQTT 客户端。线程安全。"""

    def __init__(self, host: str, port: int = 1883, client_id: str = "ftms",
                 username: str = "", password: str = "",
                 will_topic: str = "", will_payload: str = "offline",
                 keepalive: int = 60, verbose: bool = False) -> None:
        self.host = host
        self.port = int(port)
        self.client_id = client_id
        self.username = username
        self.password = password
        self.will_topic = will_topic
        self.will_payload = will_payload
        self.keepalive = int(keepalive)
        self.verbose = verbose

        self._queue: queue.Queue[tuple[str, bytes, int, bool]] = queue.Queue(128)
        self._thread: threading.Thread | None = None
        self._running = False
        self._sock: socket.socket | None = None
        self.connected = False
        self.sent = 0
        self.dropped = 0
        self.reconnects = 0
        self._last_state: tuple[str, bytes, int, bool] | None = None

    # -- 对外接口 ----------------------------------------------------------- #

    def start(self) -> "MqttClient":
        if self._thread is not None:
            return self
        self._running = True
        self._thread = threading.Thread(target=self._run, name="mqtt", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 3.0) -> None:
        if self.will_topic and self.connected:
            # 主动断开时先发 offline，避免遗嘱残留成“离线”状态
            self._publish_now(self.will_topic, b"offline", 0, True)
        sock = self._sock
        if sock and self.connected:
            try:
                sock.sendall(bytes([DISCONNECT, 0]))
            except OSError:
                pass
        self.connected = False
        self._running = False
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    def publish(self, topic: str, payload: str | bytes,
                retain: bool = False, qos: int = 0) -> None:
        """投递到发送队列；队列满则丢弃最旧的一条。"""
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        item = (topic, data, int(qos), bool(retain))
        if retain and topic.endswith(("/state", "/availability")):
            self._last_state = item
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass
            self.dropped += 1

    def publish_json(self, topic: str, obj, retain: bool = False, qos: int = 0) -> None:
        self.publish(topic, json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                     retain=retain, qos=qos)

    # -- 内部实现 ----------------------------------------------------------- #

    def _log(self, msg: str) -> None:
        if self.verbose:
            import sys
            print(f"* mqtt {msg}", file=sys.stderr, flush=True)

    def _connect_packet(self) -> bytes:
        flags = 0x02                              # clean session
        payload = _enc_str(self.client_id)
        if self.will_topic:
            flags |= 0x04 | 0x20                  # will flag + will retain
            payload += _enc_str(self.will_topic) + _enc_str(self.will_payload)
        if self.username:
            flags |= 0x80
            payload += _enc_str(self.username)
        if self.password:
            flags |= 0x40
            payload += _enc_str(self.password)

        vh = _enc_str("MQTT") + bytes([4, flags]) + struct.pack(">H", self.keepalive)
        body = vh + payload
        return bytes([CONNECT]) + _enc_remaining(len(body)) + body

    def _publish_packet(self, topic: str, data: bytes, qos: int, retain: bool) -> bytes:
        head = PUBLISH | QOS_FLAG.get(qos, 0) | (0x01 if retain else 0x00)
        body = _enc_str(topic)
        if qos > 0:
            body += struct.pack(">H", self._next_mid())
        body += data
        return bytes([head]) + _enc_remaining(len(body)) + body

    _mid = 0
    _mid_lock = threading.Lock()

    def _next_mid(self) -> int:
        with MqttClient._mid_lock:
            MqttClient._mid = (MqttClient._mid % 65535) + 1
            return MqttClient._mid

    def _publish_now(self, topic: str, data: bytes, qos: int, retain: bool) -> None:
        sock = self._sock
        if not sock or not self.connected:
            return
        try:
            sock.sendall(self._publish_packet(topic, data, qos, retain))
        except OSError:
            pass

    def _run(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                self._session()
                backoff = 1.0
            except Exception as exc:
                self._log(f"断开: {exc}")
            finally:
                self.connected = False
                if self._sock:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                    self._sock = None
            if not self._running:
                break
            self.reconnects += 1
            self._log(f"{backoff:.1f}s 后重连")
            for _ in range(int(backoff * 10)):
                if not self._running:
                    return
                time.sleep(0.1)
            backoff = min(backoff * 2, 30.0)

    def _session(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=8.0)
        self._sock = sock
        sock.sendall(self._connect_packet())

        head, body = _recv_packet(sock)
        if head != CONNACK or len(body) < 2:
            raise ConnectionError(f"CONNACK 异常: {head:#04x}")
        if body[1] != 0:
            raise ConnectionError(f"CONNACK 返回码 {body[1]}"
                                  "（4=用户名密码错误 5=未授权）")

        self.connected = True
        self._log(f"已连接 {self.host}:{self.port}")
        if self.will_topic:
            self._publish_now(self.will_topic, b"online", 0, True)
        if self._last_state:                      # 重连后补一次最新状态
            self._publish_now(*self._last_state)

        sock.settimeout(0.5)
        last_ping = time.monotonic()
        while self._running and self.connected:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                item = None

            if item is not None:
                topic, data, qos, retain = item
                sock.sendall(self._publish_packet(topic, data, qos, retain))
                self.sent += 1

            # 消费对端发来的字节（PINGRESP / PUBACK），顺便发现断连
            try:
                chunk = sock.recv(4096)
                if chunk == b"":
                    if not self._running:
                        return
                    raise ConnectionError("对端关闭")
            except socket.timeout:
                pass

            now = time.monotonic()
            if now - last_ping >= self.keepalive / 2:
                sock.sendall(bytes([PINGREQ, 0]))
                last_ping = now


# --------------------------------------------------------------------------- #
# Home Assistant MQTT Discovery（可选）
# --------------------------------------------------------------------------- #

# (object_id, 名字, 单位, device_class, icon)
HA_SENSORS = [
    ("speed", "速度", "km/h", None, "mdi:run-fast"),
    ("cadence", "步频", "spm", None, "mdi:walk"),
    ("power", "功率", "W", "power", "mdi:lightning-bolt"),
    ("resistance", "阻力", "%", None, "mdi:knob"),
    ("distance", "距离", "m", "distance", "mdi:map-marker-distance"),
    ("heart_rate", "心率", "bpm", None, "mdi:heart-pulse"),
    ("calories", "消耗", "kcal", "energy", "mdi:fire"),
    ("elapsed", "时长", "s", "duration", "mdi:timer-outline"),
]


def ha_discovery(client: "MqttClient", prefix: str, device_name: str,
                 state_topic: str, node_id: str = "elliptical") -> int:
    """发布 Home Assistant 自动发现配置，返回发布的条数。"""
    device = {
        "identifiers": [node_id],
        "name": device_name,
        "manufacturer": "YESOUL / FTMS",
        "model": "EF25 + ftms_capture",
    }
    count = 0
    for obj_id, name, unit, dev_class, icon in HA_SENSORS:
        uid = f"{node_id}_{obj_id}"
        payload = {
            "name": name,
            "unique_id": uid,
            "state_topic": state_topic,
            "value_template": "{{ value_json.%s }}" % (
                "hr" if obj_id == "heart_rate" else obj_id),
            "unit_of_measurement": unit,
            "device": device,
            "availability_topic": f"{prefix}/availability",
            "icon": icon,
        }
        if dev_class:
            payload["device_class"] = dev_class
        if obj_id in ("elapsed",):
            payload["state_class"] = "total_increasing"
        client.publish(f"{prefix}/sensor/{uid}/config",
                       json.dumps(payload, ensure_ascii=False), retain=True)
        count += 1
    return count
