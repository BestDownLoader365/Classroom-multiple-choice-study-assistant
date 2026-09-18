#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_SERVICE="mcq-template.service"
readonly NGINX_SERVICE="nginx.service"
readonly HEALTH_URL="http://127.0.0.1:8080/health"
# Readiness is the check that matters after a bank update: a worker still
# running a superseded (or restored-from-backup) questions.json answers 503 on
# every learning page while /health keeps returning 200.
readonly READY_URL="http://127.0.0.1:8080/ready"

as_root() {
    if (( EUID == 0 )); then
        "$@"
    else
        sudo "$@"
    fi
}

for command_name in systemctl nginx curl; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "错误：找不到命令 $command_name。" >&2
        exit 1
    fi
done

if [[ "$(ps -p 1 -o comm= | tr -d '[:space:]')" != "systemd" ]]; then
    echo "错误：当前 WSL 会话未使用 systemd。" >&2
    exit 1
fi

echo "[1/4] 检查 Nginx 配置……"
as_root nginx -t

echo "[2/4] 启动 Gunicorn（$APP_SERVICE）……"
as_root systemctl start "$APP_SERVICE"
if ! systemctl is-active --quiet "$APP_SERVICE"; then
    echo "错误：Gunicorn 启动失败。最近日志：" >&2
    as_root journalctl -u "$APP_SERVICE" -n 30 --no-pager >&2 || true
    exit 1
fi

echo "[3/4] 启动 Nginx（$NGINX_SERVICE）……"
as_root systemctl start "$NGINX_SERVICE"
if ! systemctl is-active --quiet "$NGINX_SERVICE"; then
    echo "错误：Nginx 启动失败。" >&2
    as_root systemctl status "$NGINX_SERVICE" --no-pager -l >&2 || true
    exit 1
fi

echo "[4/4] 验证 Nginx → Gunicorn → Flask……"
health_response=""
ready_response=""
for _ in {1..10}; do
    if health_response="$(curl --fail --silent --show-error --max-time 3 "$HEALTH_URL" 2>/dev/null)"; then
        break
    fi
    sleep 1
done
for _ in {1..10}; do
    if ready_response="$(curl --fail --silent --show-error --max-time 3 "$READY_URL" 2>/dev/null)"; then
        break
    fi
    sleep 1
done

if [[ "$health_response" != *'"status":"ok"'* ]]; then
    echo "错误：服务已启动，但存活检查未通过：$HEALTH_URL" >&2
    echo "请检查：journalctl -u $APP_SERVICE -n 50 --no-pager" >&2
    exit 1
fi

if [[ "$ready_response" != *'"status":"ready"'* ]]; then
    echo "错误：服务已启动，但没有 worker 可以承接学习流量：$READY_URL" >&2
    echo "常见原因：仍有 worker 使用旧 questions.json 或数据库被恢复到较旧备份。" >&2
    echo "请查看日志中的 'Question bank generation mismatch' 并重启全部 worker：" >&2
    echo "  journalctl -u $APP_SERVICE -n 50 --no-pager" >&2
    exit 1
fi

echo
echo "生产服务启动成功：$ready_response"
echo "存活检查：$HEALTH_URL"
echo "就绪检查：$READY_URL"
echo "Sakura FRP TCP 本地目标：127.0.0.1:8080"
