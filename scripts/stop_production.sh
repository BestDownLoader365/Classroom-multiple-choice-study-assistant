#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_SERVICE="mcq-template.service"
readonly NGINX_SERVICE="nginx.service"

as_root() {
    if (( EUID == 0 )); then
        "$@"
    else
        sudo "$@"
    fi
}

if ! command -v systemctl >/dev/null 2>&1; then
    echo "错误：找不到 systemctl。" >&2
    exit 1
fi

if [[ "$(ps -p 1 -o comm= | tr -d '[:space:]')" != "systemd" ]]; then
    echo "错误：当前 WSL 会话未使用 systemd。" >&2
    exit 1
fi

result=0

echo "[1/2] 停止 Nginx（$NGINX_SERVICE）……"
if ! as_root systemctl stop "$NGINX_SERVICE"; then
    echo "错误：无法停止 Nginx。" >&2
    result=1
fi

echo "[2/2] 停止 Gunicorn（$APP_SERVICE）……"
if ! as_root systemctl stop "$APP_SERVICE"; then
    echo "错误：无法停止 Gunicorn。" >&2
    result=1
fi

if systemctl is-active --quiet "$NGINX_SERVICE"; then
    echo "错误：Nginx 仍在运行。" >&2
    result=1
fi
if systemctl is-active --quiet "$APP_SERVICE"; then
    echo "错误：Gunicorn 仍在运行。" >&2
    result=1
fi

if (( result == 0 )); then
    echo
    echo "生产服务已全部停止；127.0.0.1:8080 不再提供 HTTP 服务。"
fi

exit "$result"
