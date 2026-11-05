#!/bin/sh
# ウォッチドッグをバックグラウンドで起動する。ウォッチドッグが
# デーモンの起動と再起動の面倒を見るので、普段はこれだけでよい。
# 止め方: pkill -f watchdog.py (デーモンは止まらない)
cd "$(dirname "$0")"
if pgrep -f "watchdog.py" >/dev/null 2>&1; then
  echo "watchdog is already running"
  exit 0
fi
nohup python3 watchdog.py >> watchdog.out 2>&1 &
echo "watchdog started (pid $!). logs: watchdog.log / daemon.log"
