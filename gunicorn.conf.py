"""Gunicorn settings for the local WSL2 production service."""

bind = "127.0.0.1:8001"
workers = 2
worker_class = "gthread"
threads = 4
timeout = 30
graceful_timeout = 30
keepalive = 5

# Send logs to stdout/stderr so systemd journal owns rotation and retention.
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Periodic recycling limits the impact of slow per-process memory growth.
max_requests = 1000
max_requests_jitter = 100

capture_output = True
