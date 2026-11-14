#!/bin/sh
# install-systemd.sh -- Hatoba デーモンを systemd サービスとして常駐化する。
#
# 使い方:  ./install-systemd.sh        (途中で sudo に入り直す)
# やること:
#   - hatoba.service.in から実パス入りの unit を生成し
#     /etc/systemd/system/hatoba.service へ配置
#   - systemctl daemon-reload && enable --now hatoba
#   - 起動後に status と /health で確認
# watchdog.py を systemd が直接見る形なので、run.sh の手動起動とは併用しない
# (二重に watchdog が立つとデーモンの取り合いになる)。手動分が動いている
# 場合は警告だけ出して、そのまま進むかどうかを尋ねる。
# 環境変数: HATOBA_USER (サービス実行ユーザー。既定は実行したユーザー)
#           HATOBA_UNIT_DIR (unit の配置先。既定 /etc/systemd/system)
set -eu

DAEMON_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
UNIT_DIR=${HATOBA_UNIT_DIR:-/etc/systemd/system}
UNIT=hatoba.service

if [ ! -f "$DAEMON_DIR/watchdog.py" ]; then
    echo "watchdog.py が $DAEMON_DIR にありません。daemon フォルダで実行してください。" >&2
    exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl が見つかりません。systemd のある Linux で実行してください。" >&2
    exit 1
fi

# sudo 入り直し (ユーザー名と python3 は入り直す前に確定させる)
if [ "$(id -u)" -ne 0 ]; then
    RUN_USER=${HATOBA_USER:-$(id -un)}
    PYTHON_BIN=$(command -v python3 || true)
    if [ -z "$PYTHON_BIN" ]; then
        echo "python3 が見つかりません。" >&2
        exit 1
    fi
    echo "管理者権限が要るので sudo で入り直します (user=$RUN_USER python=$PYTHON_BIN)"
    exec sudo env HATOBA_USER="$RUN_USER" HATOBA_PYTHON="$PYTHON_BIN" \
        HATOBA_UNIT_DIR="$UNIT_DIR" "$0" --as-root
fi

RUN_USER=${HATOBA_USER:-${SUDO_USER:-root}}
PYTHON_BIN=${HATOBA_PYTHON:-$(command -v python3)}

# 手動起動の watchdog が残っていないか (二重起動の取り合い防止)
if pgrep -f "watchdog.py" >/dev/null 2>&1 && ! systemctl is-active --quiet hatoba 2>/dev/null; then
    echo "警告: run.sh で起動した watchdog が動いているようです。"
    echo "systemd 側と取り合いになるので、先に止めるのを勧めます"
    echo "(止め方: pkill -f watchdog.py 。デーモン本体も道連れで落ちますが、"
    echo " systemd 側がすぐ起こし直します)。"
    printf "このまま続けますか? [y/N] "
    read -r ans || ans=N
    case "$ans" in y|Y) ;; *) echo "中止しました。"; exit 0 ;; esac
fi

TMP_UNIT=$(mktemp)
sed -e "s|@USER@|$RUN_USER|g" \
    -e "s|@DAEMON_DIR@|$DAEMON_DIR|g" \
    -e "s|@PYTHON@|$PYTHON_BIN|g" \
    "$DAEMON_DIR/hatoba.service.in" > "$TMP_UNIT"
install -m 0644 "$TMP_UNIT" "$UNIT_DIR/$UNIT"
rm -f "$TMP_UNIT"
echo "unit を配置しました: $UNIT_DIR/$UNIT (user=$RUN_USER)"

systemctl daemon-reload
systemctl enable --now hatoba
sleep 2
systemctl --no-pager --lines=5 status hatoba || true

echo "--- /health 確認:"
if curl -s --max-time 5 http://127.0.0.1:8766/health; then
    echo
    echo "OK: デーモンが systemd から起動しています。"
else
    echo "まだ応答がありません。journalctl -u hatoba -n 50 で様子を見てください。" >&2
fi

if command -v tailscale >/dev/null 2>&1; then
    if ! tailscale serve status 2>/dev/null | grep -q "8766"; then
        echo "注意: tailscale serve に 8766 が見えません。tailnet から使うには"
        echo "  sudo tailscale serve --bg --http=8766 http://127.0.0.1:8766"
        echo "が要ります (serve の設定は再起動後も残ります)。"
    fi
fi
echo "完了。止めるときは ./uninstall-systemd.sh です。"
