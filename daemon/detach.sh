#!/bin/sh
# Hatoba デーモンからシリアルポートを切り離す。
# Tera Term などで基板に直接繋ぎたいとき用。デーモン自身は止まらない。
# WSL のターミナルで、デーモンと同じフォルダから実行する。
#   ./detach.sh
# 接続先を変えたいときは BOARD_DAEMON_URL で上書きできる。
URL="${BOARD_DAEMON_URL:-http://127.0.0.1:8766/mcp}"
curl -s --max-time 15 -X POST "$URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"board_detach","arguments":{}}}'
echo
