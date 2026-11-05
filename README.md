# Hatoba

A USB-serial bridge daemon (MCP server)

離れたマシンで動く AI エージェントが、ネットワーク越しに USB シリアル接続の
マイコンボードを操作するためのデーモン。書き込み (flash)、リセット、
シリアルコンソールの読み書きを MCP の道具として提供する。

KosabaOS の実機開発のために作った。現在の対象ボードは
Sipeed Maix Dock (Kendryte K210) で、書き込みは kflash を使う。

## 構成

```
エージェント ──Tailscale──> ホストPC (Windows) の tailscale serve (:8766)
   ──> 127.0.0.1:8766 ──WSL localhost forwarding──> daemon/board_daemon.py (WSL2)
   ──Windows相互運用 (py.exe)──> daemon/serial_bridge.py (Windows Python + pyserial)
   ──> COM3 (CH340) ──> Maix Dock
書き込みは board_daemon.py から py.exe -m kflash を呼ぶ。
```

- `daemon/board_daemon.py` — MCP サーバ本体。Python 標準ライブラリのみ。
  版はファイル冒頭の `VERSION` (現在 0.3.0) で、health と doctor の応答の
  `version` と起動ログに出る。どの版が動いているか遠隔から確認できる。
  道具: doctor / serial_list / board_status / board_attach・detach・
  reset・set_lines / serial_send・read・expect・send_expect /
  board_boot_log / flash_kflash / flash_log
- `daemon/serial_bridge.py` — Windows 側で COM ポートを開くヘルパー。
  デーモンが Windows の一時フォルダへ自動でコピーして起動するので、
  手動で起動する必要はない。
- `client/client.py` — デーモンを呼ぶ小さな CLI クライアント。
  接続先は環境変数 `BOARD_DAEMON_ENDPOINT` で指定する。
- シリアルポートは開いたまま保持し、受信は常時ログに貯める
  (開閉のたびにリセットがかかるのを避けるため)。
- 使う COM ポートは固定ではない。`board_attach` は、明示した port →
  前回成功したポート (`daemon/port_state.json` に記憶) → config の
  既定値 → 候補が 1 つだけ → 候補を走査して Maix/K210 らしい応答
  (MaixPy REPL の `print(6*7)` → `42` 等) があったもの、の順で解決する。
  USB の抜き差しで COM 番号が動いても追随する。`flash_kflash` も
  同じ解決を使い、再 attach も同じポートへ戻る。環境変数
  `BOARD_DAEMON_COM_PORT` / `BOARD_DAEMON_RESET_LINE` でも上書きできる。
- リセット線も基板ごとに記憶する (`board_attach` の `reset_line`)。
  Maix Dock でも個体差があり、1 台目は `rts`、2 台目は `dtr` で
  リセットが掛かる (2026-11-06 実機確認)。
- 2 ポート出る基板 (K210 + ESP32 の複合基板など) では、K210 側が
  Hatoba の対象。ESP32 側は REPL プローブに応答しないので自動選択で
  間違えない (2026-11-06 実機: COM4=K210 / COM5=ESP32)。

## ホストPC 側のセットアップ (手動)

### 1. 配置

このリポジトリを WSL2 のホーム直下などに置く (例 `~/hatoba/`)。
設定は既定値のままでよいが、変える場合は:

```bash
cd ~/hatoba/daemon
cp config.example.json config.json   # COMポートやリセット信号線はここで変更
```

### 2. Windows 側 Python の用意

PowerShell で確認:

```powershell
py --version
```

無ければ python.org から Python 3 を入れる (py ランチャーを含む標準
インストーラでよい)。その後:

```powershell
py -m pip install pyserial kflash
```

### 3. デーモン起動 (WSL2)

```bash
cd ~/hatoba/daemon
python3 board_daemon.py
```

`board-daemon listening on 127.0.0.1:8766` と出れば起動済み。
現状はこのターミナルを開いたままにする運用 (常駐化は今後)。

### 4. Tailscale の中継 (PowerShell)

```powershell
tailscale serve --bg --tcp=8766 tcp://127.0.0.1:8766
```

### 5. 疎通確認

クライアント側から:

```bash
BOARD_DAEMON_ENDPOINT=http://<ホストPCのtailnet IP>:8766 python3 client/client.py health
BOARD_DAEMON_ENDPOINT=http://<ホストPCのtailnet IP>:8766 python3 client/client.py call-tool --name doctor
```

## 実機検証で分かったこと (2026-11-03, Maix Dock / K210)

- 一連の経路 (health → doctor → attach → リセット → 起動ログ採取 →
  MicroPython REPL 対話 → kflash での SRAM ブート) が実機で動作確認済み。
- pyserial はポートを開くときに DTR/RTS を立てようとする。Maix Dock では
  RTS=True がリセット assert、DTR が BOOT (IO16) の駆動なので、既定のまま
  開くとボードがリセット保持され、後から倒すとその瞬間に Flash から
  通常ブートしてしまう (SRAM 上の実行中プログラムが消える)。
  serial_bridge.py は DTR/RTS を open する「前」に倒す。これで attach
  しても実行中のボードはリセットされない。
- リセットは RTS パルス (config の `reset_line` 既定) で通常ブートする。
- MicroPython REPL は行の実行に `\r` が必要 (serial_send の `eol` で指定)。
- `flash_kflash` は各段階 (配置・ブリッジ停止・kflash 実行・結果) を
  `last_flash.json` に記録し、新道具 `flash_log` で後から読める。
  kflash の出力はファイル経由で受け、タイムアウトは既定 60 秒。
- 送信の切り捨て (2026-11-03 切り分け): ポーリング受信のファーム
  (KosabaOS の Nish) へ 1 回で長く送ると、先頭 9 文字だけ届いて末尾が
  落ちる。原因はボード側の受信 FIFO (K210 UARTHS は 8 段 + 1) の溢れで、
  受信側が FIFO を読み出す前にバーストが届き切ると溢れた分が捨てられる。
  切り分けの根拠: 割り込み受信の MaixPy では同じ送り方で 23 文字が
  アイドル 8 秒後でも全量到達し、Hatoba の送信経路自体は健全だった。
  落ち方は送信前のアイドル時間にも依存した (0.3 秒では全量、1 秒では
  2 文字落ち、3 秒以上では 9 文字で頭打ち)。
  対策として `serial_send` / `serial_send_expect` にペース付き送信を
  追加した: `pace_ms` と `chunk` (既定 3 バイト) を指定すると小分けに
  間隔を空けて送る。KosabaOS 向けの実機確認済みレシピは
  `chunk=3, pace_ms=40` (21 文字のコマンドが全量到達)。
  なお同日、KosabaOS 側で入力を一切受け付けなくなる事象も 1 件観測
  したが、後の OS 側の切り分けで KosabaOS 自身のバグと判明した
  (存在しないファイルの exec 失敗時に sepc を進めず、同じ ecall が
  無限再実行されてシェルが getc に戻らなかった。OS 側で修正済み)。
  Hatoba・ブリッジ・受信回路は無実。

## 注意

- `flash_kflash` は既定で SRAM ブート (`-s`、電源を切れば元のファームに
  戻る非破壊モード)。Flash 書き込み (`sram:false`) は元のファームを
  消すので、明示の承認なしに実行しないこと。
- ホストPC がスリープするとデーモンごと止まる (割り切り)。
