#!/usr/bin/env bash
# 在目标机上以普通用户运行。只有安装系统包那一步需要 sudo。
#
#   ssh syhan@10.0.0.177
#   ~/elliptical-ftms/deploy/install.sh
#
set -euo pipefail

REPO="${REPO:-https://github.com/syhan/elliptical-ftms.git}"
DIR="${DIR:-$HOME/elliptical-ftms}"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "1/5 检查 Python"
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    sys.exit(f"需要 Python 3.11+（当前 {sys.version.split()[0]}），因为用到 tomllib")
print("  Python", sys.version.split()[0], "OK")
PY

say "2/5 安装 bluez + python3-gi（需要 sudo 密码）"
if ! dpkg -s bluez >/dev/null 2>&1 || ! dpkg -s python3-gi >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y bluez python3-gi
else
    echo "  已安装，跳过"
fi
sudo systemctl enable --now bluetooth

say "3/5 等待蓝牙适配器"
for i in $(seq 1 20); do
    if bluetoothctl list 2>/dev/null | grep -q Controller; then
        bluetoothctl list | sed 's/^/  /'
        break
    fi
    [ "$i" = 20 ] && { echo "  ! 20 秒内没有控制器，检查硬件/固件"; exit 1; }
    sleep 1
done

say "4/5 取仓库"
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

say "5/5 安装 systemd 用户服务"
mkdir -p "$HOME/.config/systemd/user"
cp "$DIR/deploy/elliptical-ftms.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
# 开机即启动（用户服务需要 linger 才能在没登录时跑）
if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
    sudo loginctl enable-linger "$USER"
fi
systemctl --user enable --now elliptical-ftms.service

say "完成"
systemctl --user --no-pager status elliptical-ftms.service || true
echo
echo "查看日志：  journalctl --user -u elliptical-ftms -f"
echo "重启服务：  systemctl --user restart elliptical-ftms"
echo "停止服务：  systemctl --user stop elliptical-ftms"
