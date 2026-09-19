#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_SERVICE="mcq-template.service"
readonly NGINX_SERVICE="nginx.service"
readonly HEALTH_URL="http://127.0.0.1:8080/health"
# Readiness is the check that matters after a content update: a worker still
# running a superseded (or restored-from-backup) publication answers 503 on that
# course's learning pages while /health keeps returning 200.
#
# Aggregate readiness covers the worker's *declared, enabled* courses.  When it
# reports degraded, use /ready/<course_id> to find the affected course: a stale
# or unavailable course A does not stop course B from serving learners.
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

if [[ "$ready_response" != *'"status": "ready"'* && "$ready_response" != *'"status":"ready"'* ]]; then
    echo "错误：服务已启动，但至少一门课程无法承接学习流量：$READY_URL" >&2
    echo "常见原因：仍有 worker 使用旧课程内容、课程包损坏，或数据库被恢复到较旧备份。" >&2
    echo "诊断步骤：" >&2
    echo "  1) $READY_URL          # 查看每门课程的 status/generation" >&2
    echo "  2) $READY_URL/<course_id>   # 定位具体课程（stale / unavailable）" >&2
    echo "  3) 查看日志中 'is stale on this worker' 或 'is unavailable' 的行：" >&2
    echo "     journalctl -u $APP_SERVICE -n 50 --no-pager" >&2
    echo "  4) 内容更新后重启全部 worker；只有受影响课程会被围栏。" >&2
    exit 1
fi

echo
echo "生产服务启动成功：$ready_response"
echo "存活检查：$HEALTH_URL"
echo "就绪检查（汇总）：$READY_URL"
echo "就绪检查（单课程）：$READY_URL/<course_id>"
echo "Sakura FRP TCP 本地目标：127.0.0.1:8080"
