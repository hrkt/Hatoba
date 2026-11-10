#!/bin/sh
# Hatoba デーモンにシリアルポートを再接続させる。
# ポートとリセット線はデーモンが記憶している (port_state.json) ので、
# 通常は引数なしでよい。ポートを変えたいときだけ名前を渡す:
#   ./attach.sh            # 記憶しているポートへ
#   ./attach.sh COM4       # ポートを指定 (Pi では /dev/ttyUSB0 など)
# WSL のターミナルで、デーモンと同じフォルダから実行する。
# 接続先を変えたいときは BOARD_DAEMON_URL で上書きできる。
URL="${BOARD_DAEMON_URL:-http://127.0.0.1:8766/mcp}"
if [ -n "$1" ]; then
  ARGS="{\"port\": \"$1\"}"
else
  ARGS="{}"
fi
curl -s --max-time 30 -X POST "$URL" -H "Content-Type: application/json" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"board_attach\",\"arguments\":$ARGS}}"
echo
