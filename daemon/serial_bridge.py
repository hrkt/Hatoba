#!/usr/bin/env python3
# serial_bridge.py -- Windows 側シリアルヘルパー (board-daemon 用)
#
# 役割: Windows の COM ポートを実際に開くのはこのスクリプトだけ。
# WSL2 側のデーモン (board_daemon.py) が Windows 相互運用でこのスクリプトを
# Windows Python (py.exe) から起動し、標準入出力をパイプとして使う。
#
# やり取りは NDJSON (1行1つの JSON) で、バイト列は base64 で運ぶ。
#   デーモン -> ブリッジ (stdin):
#     {"type":"tx","data_b64":"..."}        シリアルへ書き込む
#     {"type":"reset","line":"rts","pulse_ms":150}  リセット信号をパルスする
#     {"type":"set_lines","dtr":true,"rts":false}   DTR/RTS を直接倒す/立てる
#     {"type":"close"}                      ポートを閉じて終了する
#   ブリッジ -> デーモン (stdout):
#     {"type":"ready","port":"COM3","baud":115200}
#     {"type":"rx","data_b64":"..."}        シリアルから受信したバイト列
#     {"type":"error","message":"..."}
#     {"type":"closed"}
#
# 依存: Python 3 + pyserial (pip install pyserial) が Windows 側に必要。
# このファイル単体は WSL 側でも import しない。Windows 側の一時フォルダへ
# デーモンがコピーしてから実行する (WSL のパスを Windows Python に
# 渡すより確実なため)。

import argparse
import base64
import json
import os
import sys
import threading
import time


def emit(obj):
    """stdout へ NDJSON を1行書き、即フラッシュする (バイナリで書く)。"""
    line = (json.dumps(obj) + "\n").encode("utf-8")
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()

    try:
        import serial  # pyserial
    except ImportError:
        emit({"type": "error",
              "message": "pyserial is not installed on the Windows side. "
                         "Run: py -m pip install pyserial"})
        return 2

    try:
        # timeout は小さくし、受信ポーリングで全体を回す。
        ser = serial.Serial()
        ser.port = args.port
        ser.baudrate = args.baud
        ser.timeout = 0.05
        ser.write_timeout = 2
        # DTR/RTS は open する「前」に倒しておくこと。
        # pyserial の既定では open 時に両方とも立つ。Maix Dock では
        # RTS=True がリセット assert、DTR が BOOT (IO16) の駆動なので、
        # 既定のまま開くとボードがリセット保持され、後から倒すと
        # その瞬間に Flash から通常ブートしてしまう (SRAM 上の
        # プログラムが消える)。open 前に倒せば、実行中のボードを
        # リセットせずに接続できる (2026-11-03 実機で確認)。
        ser.dtr = False
        ser.rts = False
        ser.open()
    except Exception as e:  # ポートが無い・他プロセスが掴んでいる等
        emit({"type": "error", "message": f"cannot open {args.port}: {e}"})
        return 1

    emit({"type": "ready", "port": args.port, "baud": args.baud})
    stop = threading.Event()

    def stdin_loop():
        """デーモンからの命令を読み、シリアル側へ反映するスレッド。
        読みは os.read で行う。sys.stdin.buffer を反復/ readline すると、
        終了処理のときに別スレッドが BufferedReader のロックを持ったまま
        になり Fatal Python error (_enter_buffered_busy) を踏むことが
        あるため (2026-11-06 実機、USB 抜去後のブリッジ終了時)。"""
        buf = b""
        while not stop.is_set():
            try:
                chunk = os.read(0, 4096)
            except OSError:
                break
            if not chunk:  # stdin が閉じられた (デーモン終了)
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                if handle_line(raw):
                    return
        stop.set()

    def handle_line(raw):
        """NDJSON 1行を処理する。close なら True を返す。"""
        raw = raw.strip()
        if not raw:
            return False
        try:
            cmd = json.loads(raw.decode("utf-8"))
        except Exception:
            return False
        kind = cmd.get("type")
        try:
            if kind == "tx":
                ser.write(base64.b64decode(cmd.get("data_b64", "")))
            elif kind == "reset":
                line = cmd.get("line", "rts")
                pulse = float(cmd.get("pulse_ms", 150)) / 1000.0
                if line == "dtr":
                    ser.dtr = True
                    time.sleep(pulse)
                    ser.dtr = False
                else:
                    ser.rts = True
                    time.sleep(pulse)
                    ser.rts = False
            elif kind == "set_lines":
                if "dtr" in cmd:
                    ser.dtr = bool(cmd["dtr"])
                if "rts" in cmd:
                    ser.rts = bool(cmd["rts"])
            elif kind == "close":
                stop.set()
                return True
        except Exception as e:
            emit({"type": "error", "message": f"command {kind} failed: {e}"})
        return False

    t = threading.Thread(target=stdin_loop, daemon=True)
    t.start()

    read_failed = False
    try:
        while not stop.is_set():
            try:
                data = ser.read(4096)
            except Exception as e:
                # ポートが抜けた等。デーモン側に知らせて終わる。
                emit({"type": "error", "message": f"serial read failed: {e}"})
                read_failed = True
                break
            if data:
                emit({"type": "rx",
                      "data_b64": base64.b64encode(data).decode("ascii")})
    finally:
        try:
            ser.close()
        except Exception:
            pass
        emit({"type": "closed"})
    if read_failed:
        # 読み失敗時は stdin スレッドがまだ os.read で待っていることが
        # ある。通常の終了処理 (インタプリタ finalization) に入ると
        # ブリッジが異常終了のダンプを出すことがあるので、出力は
        # 送信済みのこの時点で即座に終わる (2026-11-06 実機)。
        os._exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
