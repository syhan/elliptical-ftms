#!/usr/bin/env bash
# 在目标机上以普通用户运行。会提示一次 sudo 密码（Debian 的 sudo 有 15 分钟
# 缓存，中间不会再问）。
#
#   ssh syhan@10.0.0.177
#   ~/elliptical-ftms/deploy/install.sh
#
# 环境变量：
#   WITH_MQTT=0      跳过 mosquitto（默认 1，在这台机器上起局域网 broker）
#   REPO=...         仓库地址
#   DIR=...          安装目录（默认 $HOME/elliptical-ftms）
#
set -euo pipefail

REPO="${REPO:-https://github.com/syhan/elliptical-ftms.git}"
DIR="${DIR:-$HOME/elliptical-ftms}"
WITH_MQTT="${WITH_MQTT:-1}"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
say "0/6 检查 sudo（只需输一次密码）"
sudo -v
# 后台续期，避免装到一半又要密码
( while kill -0 "$$" 2>/dev/null; do sudo -n true; sleep 50; done ) 2>/dev/null &

say "1/6 检查 Python"
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    sys.exit(f"需要 Python 3.11+（当前 {sys.version.split()[0]}），因为用到 tomllib")
print("  Python", sys.version.split()[0], "OK")
PY

say "2/6 安装系统包"
PKGS="bluez python3-gi"
[ "$WITH_MQTT" = "1" ] && PKGS="$PKGS mosquitto"
need=0
for p in $PKGS; do
    dpkg -s "$p" >/dev/null 2>&1 || need=1
done
if [ "$need" = "1" ]; then
    sudo apt-get update -qq
    sudo apt-get install -y $PKGS
else
    echo "  已安装，跳过"
fi

say "3/6 启动 bluetoothd 并确认适配器"
sudo systemctl enable --now bluetooth
OK=0
for i in $(seq 1 25); do
    if bluetoothctl list 2>/dev/null | grep -q Controller; then
        bluetoothctl list | sed 's/^/  /'
        OK=1
        break
    fi
    sleep 1
done
if [ "$OK" = "0" ]; then
    echo "  ! 25 秒内没找到控制器。检查：rfkill list / dmesg | grep -i blue" >&2
    exit 1
fi

if [ "$WITH_MQTT" = "1" ]; then
    say "3b/6 配置局域网 MQTT broker"
    sudo install -m 644 "$DIR/deploy/mosquitto-elliptical.conf" \
        /etc/mosquitto/conf.d/elliptical.conf
    # 注意：apt install 的 postinst 已经把 mosquitto 起来了，
    # 这里必须 restart 才会读到刚写进去的 conf.d 配置
    sudo systemctl enable mosquitto
    sudo systemctl restart mosquitto
    if systemctl is-active --quiet ufw; then
        sudo ufw allow 1883/tcp >/dev/null
        echo "  ufw: 已放行 1883/tcp"
    fi
    sleep 1
    if sudo ss -lnt | grep -q '0\.0\.0\.0:1883\|\*:1883\|\[::\]:1883'; then
        echo "  mosquitto 正在 1883 监听（局域网可访问）"
    else
        echo "  ! 1883 只监听了 localhost，检查 /etc/mosquitto/conf.d/elliptical.conf" >&2
    fi
fi

say "4/6 取仓库"
if [ -d "$DIR/.git" ]; then
    git -C "$DIR" pull --ff-only
else
    git clone "$REPO" "$DIR"
fi
if [ ! -f "$DIR/config.toml" ]; then
    cp "$DIR/config.toml.example" "$DIR/config.toml"
    echo "  已生成 config.toml —— 记得填 address / age / weight / mqtt_host"
fi
mkdir -p "$DIR/data"

say "5/6 安装 systemd 用户服务"
mkdir -p "$HOME/.config/systemd/user"
cp "$DIR/deploy/elliptical-ftms.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload

say "6/6 开机自启（linger）"
if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
    sudo loginctl enable-linger "$USER"
fi
systemctl --user enable --now elliptical-ftms.service

say "完成"
systemctl --user --no-pager status elliptical-ftms.service || true
echo
echo "看日志：  journalctl --user -u elliptical-ftms -f"
echo "重启：    systemctl --user restart elliptical-ftms"
echo "停止：    systemctl --user stop elliptical-ftms   # 会先写完 FIT"
echo "MQTT：    mosquitto_sub -h localhost -t 'elliptical/#' -v"
