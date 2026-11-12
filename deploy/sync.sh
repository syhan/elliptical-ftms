#!/usr/bin/env bash
# 在开发机上执行：把当前分支推给目标机并重启服务。
#
# 目标机访问 GitHub 经常超时，所以走 SSH 直推，完全不碰 GitHub。
#
# 用法：
#   ./deploy/sync.sh syhan@10.0.0.177
#   ./deploy/sync.sh syhan@10.0.0.177 --no-restart
#
set -euo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
    echo "用法: $0 user@host [--no-restart]" >&2
    exit 1
fi
RESTART=1
[ "${2:-}" = "--no-restart" ] && RESTART=0

HOST="${TARGET#*@}"
PATH_ON_TARGET="/home/${TARGET%@*}/elliptical-ftms"
REMOTE_URL="ssh://${TARGET}${PATH_ON_TARGET}"

echo "==> 推送到 $TARGET:$PATH_ON_TARGET"
git push "$REMOTE_URL" HEAD:main

if [ "$RESTART" = "1" ]; then
    echo "==> 重启服务"
    ssh "$TARGET" 'export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user restart elliptical-ftms; sleep 3; systemctl --user is-active elliptical-ftms'
    echo "==> 完成。看日志：ssh $TARGET 'journalctl --user-unit elliptical-ftms -f'"
fi
