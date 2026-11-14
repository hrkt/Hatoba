#!/bin/sh
# uninstall-systemd.sh -- Hatoba の systemd 常駐化を取り消す。
#
# 使い方:  ./uninstall-systemd.sh      (途中で sudo に入り直す)
# やること: systemctl disable --now hatoba、unit ファイルの削除、daemon-reload。
# daemon フォルダ内のファイル (ログ・config・port_state.json) には触らない。
# 取り消した後は ./run.sh の手動起動に戻れる。
# 環境変数: HATOBA_UNIT_DIR (unit の配置先。既定 /etc/systemd/system)
set -eu

UNIT_DIR=${HATOBA_UNIT_DIR:-/etc/systemd/system}
UNIT=hatoba.service

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl が見つかりません。" >&2
    exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "管理者権限が要るので sudo で入り直します"
    exec sudo env HATOBA_UNIT_DIR="$UNIT_DIR" "$0" --as-root
fi

if systemctl list-unit-files --no-legend hatoba.service 2>/dev/null | grep -q hatoba; then
    systemctl disable --now hatoba || true
    echo "サービスを停止・無効化しました。"
else
    echo "hatoba.service は登録されていませんでした。"
fi
if [ -f "$UNIT_DIR/$UNIT" ]; then
    rm -f "$UNIT_DIR/$UNIT"
    echo "unit を削除しました: $UNIT_DIR/$UNIT"
fi
systemctl daemon-reload
echo "完了。daemon フォルダとログはそのまま残っています。"
echo "手動で起動し直すときは ./run.sh です。"
