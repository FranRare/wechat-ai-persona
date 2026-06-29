#!/usr/bin/env bash
set -euo pipefail

cd "$HOME/cass-weixin-openai"
source .venv/bin/activate

mkdir -p logs

if [ -f logs/wechat-cass.pid ] && kill -0 "$(cat logs/wechat-cass.pid)" 2>/dev/null; then
  echo "Cass Weixin already running. PID=$(cat logs/wechat-cass.pid)"
else
  nohup node standalone_wechat_cass.js >> logs/wechat-cass.log 2>&1 &
  echo $! > logs/wechat-cass.pid
  echo "Cass Weixin started. PID=$(cat logs/wechat-cass.pid)"
fi

if [ -f logs/wechat-diary-scheduler.pid ] && kill -0 "$(cat logs/wechat-diary-scheduler.pid)" 2>/dev/null; then
  echo "Weixin diary scheduler already running. PID=$(cat logs/wechat-diary-scheduler.pid)"
else
  nohup .venv/bin/python wechat_diary_scheduler.py >> logs/wechat-diary-scheduler.log 2>&1 &
  echo $! > logs/wechat-diary-scheduler.pid
  echo "Weixin diary scheduler started. PID=$(cat logs/wechat-diary-scheduler.pid)"
fi

echo "Logs:"
echo "  $HOME/cass-weixin-openai/logs/wechat-cass.log"
echo "  $HOME/cass-weixin-openai/logs/wechat-diary.log"
echo "  $HOME/cass-weixin-openai/logs/wechat-diary-scheduler.log"
