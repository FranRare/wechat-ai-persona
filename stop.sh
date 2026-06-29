#!/usr/bin/env bash
set -euo pipefail

cd "$HOME/cass-weixin-openai"

stop_pid_file() {
  local file="$1"
  local name="$2"

  if [ -f "$file" ]; then
    local PID
    PID="$(cat "$file")"
    if kill -0 "$PID" 2>/dev/null; then
      kill "$PID"
      echo "Stopped $name. PID=$PID"
    else
      echo "$name PID file exists but process is not running."
    fi
    rm -f "$file"
  fi
}

stop_pid_file logs/wechat-cass.pid "Cass Weixin"
stop_pid_file logs/wechat-diary-scheduler.pid "Weixin diary scheduler"

pkill -f "standalone_wechat_cass.js" || true
pkill -f "wechat_diary_scheduler.py" || true

echo "Stopped Cass Weixin related processes."
