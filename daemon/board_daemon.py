#!/usr/bin/env python3
# board_daemon.py -- 実機ボード操作デーモン (WSL2 側で動く本体)
#
# 役割:
#   離れたマシンのエージェントから Tailscale 経由で届く MCP の道具呼び出しを受け、
#   ホストPC に USB 接続された RISC-V ボード (当面は Kendryte K210 /
#   Sipeed Maix Dock) を操作する。
#
#   - 入口は最小実装の MCP サーバ (Streamable HTTP, JSON 応答のみ)。
#     標準ライブラリだけで動く (WSL 側に追加インストールは不要)。
#     待ち受けは 127.0.0.1 のみ。外からの到達は Windows 側 tailscale serve が担う。
#   - シリアルポートを実際に開くのは Windows 側の serial_bridge.py で、
#     Windows 相互運用 (py.exe) で起動し NDJSON パイプでやり取りする。
#     ポートは開いたまま保持し、受信は裏でログに貯め続ける
#     (開くたびに DTR/RTS でボードがリセットされ得るのを避けるため)。
#   - 書き込み (flash) は Windows 側の kflash を py.exe 経由で呼ぶ。
#
# 起動:
#   python3 board_daemon.py [--config config.json] [--port-http 8766]
#
# config.json (同ディレクトリ、無ければ既定値):
#   {"com_port":"COM3","baud":115200,"board":"dan",
#    "windows_python":"py.exe","reset_line":"rts","reset_pulse_ms":150}

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STARTED_AT = time.time()

# デーモンのバージョン。health / doctor / 起動ログに出るので、ThinkPad で
# どの版が動いているか遠隔から確認できる (2026-11-06 ユーザー提案)。
# 0.3.0: ポート自動解決・daemon.log・ブリッジ終了競合の修正
VERSION = "0.3.0"

# ---------------------------------------------------------------------------
# ファイルログ (daemon.log)
# ---------------------------------------------------------------------------
# デーモンは WSL 端末で手動起動されるため、落ちたときの traceback は
# 端末にしか残らなかった (2026-11-06 未明、attach 直後にプロセスごと
# 落ちる事象が続き、遠隔からは何も見えなかった)。同じフォルダの
# daemon.log にも時刻つきで残し、次に落ちたら原因が追えるようにする。
# heartbeat も書くので、「プロセスは生きていたが HTTP が届かなかった」
# のか「プロセスごと死んだ」のかが後から区別できる。

LOG_PATH = os.path.join(HERE, "daemon.log")
_log_file = None
_log_lock = threading.Lock()


def log_event(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass
    if _log_file is not None:
        try:
            with _log_lock:
                _log_file.write(line + "\n")
                _log_file.flush()
        except Exception:
            pass


def setup_logging():
    global _log_file
    import faulthandler
    import traceback
    try:
        _log_file = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=_log_file, all_threads=True)
    except Exception:
        _log_file = None

    def _excepthook(etype, value, tb):
        log_event("FATAL unhandled exception:\n" + "".join(
            traceback.format_exception(etype, value, tb)))

    sys.excepthook = _excepthook

    def _thread_hook(args):
        log_event("thread %s died:\n%s" % (
            args.thread.name,
            "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback))))

    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass

DEFAULT_CONFIG = {
    "com_port": "COM3",
    "baud": 115200,
    "board": "dan",                # kflash の -B 指定 (Maix Dock)
    "windows_python": "py.exe",    # Windows 相互運用で呼ぶ Python ランチャ
    "reset_line": "rts",           # リセットに使う信号線 (dtr/rts)
    "reset_pulse_ms": 150,
    "http_host": "127.0.0.1",
    "http_port": 8766,
}


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def run_quiet(argv, timeout=20):
    """コマンドを実行し (returncode, 出力文字列) を返す。失敗しても例外にしない。"""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout)
        out = (p.stdout + p.stderr).decode("utf-8", errors="replace").strip()
        return p.returncode, out
    except FileNotFoundError:
        return None, "not found: %s" % argv[0]
    except subprocess.TimeoutExpired:
        return None, "timeout after %ss: %s" % (timeout, argv[0])
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------------------
# シリアル受信ログ (カーソル式)
# ---------------------------------------------------------------------------

class SerialLog:
    """受信バイト列を貯めるバッファ。カーソルは通算バイト位置で、
    読み出しは差分だけを返す。直近 1MB だけ保持する。"""

    MAX_KEEP = 1024 * 1024

    def __init__(self):
        self._buf = bytearray()
        self._base = 0
        self._lock = threading.Lock()

    def append(self, data):
        with self._lock:
            self._buf.extend(data)
            if len(self._buf) > self.MAX_KEEP:
                drop = len(self._buf) - self.MAX_KEEP
                del self._buf[:drop]
                self._base += drop

    def total(self):
        with self._lock:
            return self._base + len(self._buf)

    def read_since(self, cursor):
        with self._lock:
            total = self._base + len(self._buf)
            if cursor is None or cursor < self._base:
                cursor = self._base
            if cursor > total:
                cursor = total
            data = bytes(self._buf[cursor - self._base:])
            return data.decode("utf-8", errors="replace"), total


# ---------------------------------------------------------------------------
# Windows 側シリアルブリッジの管理
# ---------------------------------------------------------------------------

STATE_PATH = os.path.join(HERE, "port_state.json")


class Bridge:
    """serial_bridge.py を Windows Python で起動し、保持する。

    ポートは固定ではない。前回成功したポートとリセット線を
    port_state.json に覚え、board_attach や flash はそれを再利用する。
    USB の抜き差しで COM 番号が動いても追随できるようにするため。"""

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.proc = None
        self.lock = threading.Lock()
        self.last_error = None
        self._ready = False
        self._win_temp = None
        self.attached_port = None
        self.current_port = cfg["com_port"]
        self.reset_line = cfg["reset_line"]
        self._last_port_source = None
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                st = json.load(f)
            if st.get("port"):
                self.current_port = st["port"]
            if st.get("reset_line"):
                self.reset_line = st["reset_line"]
        except Exception:
            pass

    def _save_state(self):
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"port": self.current_port,
                           "reset_line": self.reset_line}, f)
        except Exception:
            pass

    def _available_ports(self):
        """Windows 側の COM ポート名一覧。取れなければ None。"""
        rc, out = run_quiet(
            ["powershell.exe", "-NoProfile", "-Command",
             "[System.IO.Ports.SerialPort]::GetPortNames() -join ','"],
            timeout=20)
        if rc != 0:
            return None
        return sorted(p.strip() for p in out.split(",") if p.strip())

    def resolve_port(self, requested=None):
        """使うポートを決める。戻り値は (ポート名, 決め方)。
        優先順: 明示指定 → 前回成功 (port_state) → config の既定 →
        候補が1つだけ → 候補を1つずつ試し Maix/K210 らしい応答で判定。
        どれでも決まらなければ例外 (候補一覧つき)。"""
        if requested:
            return requested, "explicit"
        avail = self._available_ports()
        if avail:
            if self.current_port in avail:
                return self.current_port, "last_used"
            if self.cfg["com_port"] in avail:
                return self.cfg["com_port"], "config"
            if len(avail) == 1:
                return avail[0], "only_available"
            for cand in avail:
                if self._probe(cand):
                    return cand, "probe"
            raise RuntimeError(
                "no Maix/K210-like board answered among %s; "
                "pass port explicitly to board_attach" % (avail,))
        return self.current_port, "fallback_no_list"

    def _probe(self, port):
        """候補ポートを開き、Maix/K210 らしい応答があるか軽く試す。
        MaixPy REPL に print(6*7) を送って 42 が返れば確定。KosabaOS が
        走っている場合は tick / プロンプト等の文字列でも判定する。
        ESP32 等の別デバイスは応答しないので弾かれる (2026-11-06 実機:
        2ポート基板の COM4=K210 が応答、COM5=ESP32 は無応答)。"""
        try:
            self._start_on(port, None)
        except Exception:
            return False
        try:
            cur = self.log.total()
            self._send({"type": "tx", "data_b64": base64.b64encode(
                b"print(6*7)\r").decode("ascii")})
            deadline = time.time() + 3.0
            while time.time() < deadline:
                text, _ = self.log.read_since(cur)
                if ("42" in text or "MaixPy" in text or "nish>" in text
                        or "KosabaOS" in text or "tick " in text):
                    return True
                time.sleep(0.1)
            return False
        except Exception:
            return False
        finally:
            self.stop()

    def _windows_temp_wsl(self):
        """Windows の %TEMP% を WSL パスで返す。"""
        if self._win_temp:
            return self._win_temp
        rc, out = run_quiet(
            ["powershell.exe", "-NoProfile", "-Command", "$env:TEMP"], timeout=15)
        if rc != 0 or not out:
            raise RuntimeError("cannot get Windows TEMP folder: " + out)
        win_path = out.splitlines()[0].strip()
        rc, out = run_quiet(["wslpath", "-u", win_path], timeout=10)
        if rc != 0 or not out:
            raise RuntimeError("wslpath failed for %s: %s" % (win_path, out))
        self._win_temp = out.splitlines()[0].strip()
        return self._win_temp

    def _stage_bridge_script(self):
        """serial_bridge.py を Windows 側一時フォルダへコピーし Windows パスを返す。
        WSL のパスをそのまま Windows Python に渡すより確実なため。"""
        temp_wsl = self._windows_temp_wsl()
        dest_dir = os.path.join(temp_wsl, "board-daemon")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "serial_bridge.py")
        shutil.copyfile(os.path.join(HERE, "serial_bridge.py"), dest)
        rc, out = run_quiet(["wslpath", "-w", dest], timeout=10)
        if rc != 0 or not out:
            raise RuntimeError("wslpath -w failed: %s" % out)
        return out.splitlines()[0].strip()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, port=None, baud=None, reset_line=None):
        """ポートを解決して attach する。reset_line を渡すと、その基板の
        リセット線として覚える (基板ごとに違う: 1台目は rts、
        2台目の Dock は dtr でリセットが掛かることを 2026-11-06 に確認)。"""
        if reset_line:
            self.reset_line = reset_line
            self._save_state()
        if self.alive():
            if port is None or port == self.attached_port:
                return {"attached": True, "port": self.attached_port,
                        "baud": baud or self.cfg["baud"],
                        "reset_line": self.reset_line,
                        "note": "already attached"}
            self.stop()  # 別のポート指定なら張り替える
        chosen, source = self.resolve_port(port)
        self._last_port_source = source
        res = self._start_on(chosen, baud)
        res["port_source"] = source
        res["reset_line"] = self.reset_line
        return res

    def _start_on(self, port, baud=None):
        with self.lock:
            if self.alive():
                return {"attached": True,
                        "port": self.attached_port,
                        "baud": baud or self.cfg["baud"],
                        "note": "already attached"}
            baud = int(baud or self.cfg["baud"])
            self._ready = False
            self.last_error = None
            script_win = self._stage_bridge_script()
            argv = [self.cfg["windows_python"], "-u", script_win,
                    "--port", port, "--baud", str(baud)]
            try:
                self.proc = subprocess.Popen(
                    argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE)
            except Exception as e:
                raise RuntimeError("cannot launch Windows python: %s" % e)
            threading.Thread(target=self._pump_stdout, daemon=True).start()
            threading.Thread(target=self._pump_stderr, daemon=True).start()
            deadline = time.time() + 8
            while time.time() < deadline:
                if self.last_error:
                    raise RuntimeError(self.last_error)
                if self._ready:
                    self.attached_port = port
                    self.current_port = port
                    self._save_state()
                    log_event("bridge attached port=%s baud=%d"
                              % (port, baud))
                    return {"attached": True, "port": port, "baud": baud}
                if not self.alive():
                    raise RuntimeError("bridge exited early: %s"
                                       % (self.last_error or "unknown"))
                time.sleep(0.05)
            raise RuntimeError("bridge did not become ready in 8s")

    def _pump_stdout(self):
        try:
            for raw in self.proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception:
                    continue
                kind = msg.get("type")
                if kind == "rx":
                    try:
                        self.log.append(base64.b64decode(msg.get("data_b64", "")))
                    except Exception:
                        pass
                elif kind == "ready":
                    self._ready = True
                elif kind == "error":
                    self.last_error = msg.get("message", "bridge error")
                    log_event("[bridge] %s" % self.last_error)
                elif kind == "closed":
                    self._ready = False
        except Exception:
            pass

    def _pump_stderr(self):
        try:
            for raw in self.proc.stderr:
                print("[bridge/stderr] %s"
                      % raw.decode("utf-8", errors="replace"),
                      end="", file=sys.stderr)
        except Exception:
            pass

    def _send(self, obj):
        if not self.alive():
            raise RuntimeError("bridge is not running (call board_attach first)")
        line = (json.dumps(obj) + "\n").encode("utf-8")
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def stop(self):
        with self.lock:
            if self.proc is not None:
                try:
                    self._send({"type": "close"})
                    self.proc.wait(timeout=3)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                self.proc = None
                self._ready = False
                self.attached_port = None
                log_event("bridge detached")
            return {"attached": False}

    # -- ブリッジへの操作 ---------------------------------------------------

    def tx_text(self, text, pace_ms=0, chunk=3):
        """テキストをブリッジへ送る。pace_ms > 0 のときは chunk バイト
        ごとに pace_ms だけ間隔を空ける (ペース付き送信)。

        ポーリング式に受信するボードでは、受信 FIFO (K210 UARTHS は
        8段+1) より速くバーストが届くと溢れた分が捨てられる。
        小分けに間隔を空けて送ると受信側が追いつけるため全量届く
        (2026-11-03 実機で確認: 無ペースでは 9 文字で切り捨て、
        3 バイト/40ms のペース送信では 21 文字全量到達)。
        戻り値は送ったチャンク数。"""
        data = text.encode("utf-8")
        if not pace_ms or pace_ms <= 0:
            self._send({"type": "tx",
                        "data_b64": base64.b64encode(data).decode("ascii")})
            return 1 if data else 0
        chunk = max(1, int(chunk))
        n = 0
        for i in range(0, len(data), chunk):
            self._send({"type": "tx",
                        "data_b64": base64.b64encode(
                            data[i:i + chunk]).decode("ascii")})
            n += 1
            if i + chunk < len(data):
                time.sleep(float(pace_ms) / 1000.0)
        return n

    def reset(self, line=None, pulse_ms=None):
        self._send({"type": "reset",
                    "line": line or self.reset_line,
                    "pulse_ms": int(pulse_ms or self.cfg["reset_pulse_ms"])})

    def set_lines(self, dtr=None, rts=None):
        cmd = {"type": "set_lines"}
        if dtr is not None:
            cmd["dtr"] = bool(dtr)
        if rts is not None:
            cmd["rts"] = bool(rts)
        self._send(cmd)


# ---------------------------------------------------------------------------
# Windows 側の情報取得 (doctor / serial_list 用)
# ---------------------------------------------------------------------------

def list_serial_ports(cfg):
    """COM ポート一覧を Windows 側から取る。
    Get-PnpDevice の全体スキャンは非常に遅いことがあるので使わず、
    [SerialPort]::GetPortNames() とレジストリ SERIALCOMM だけで済ませる。"""
    rc, names = run_quiet(
        ["powershell.exe", "-NoProfile", "-Command",
         "[System.IO.Ports.SerialPort]::GetPortNames() -join ','"],
        timeout=20)
    ports = []
    if rc == 0 and names:
        ports = [p.strip() for p in names.split(",") if p.strip()]
    rc2, mapping = run_quiet(
        ["powershell.exe", "-NoProfile", "-Command",
         "(Get-ItemProperty 'HKLM:\\HARDWARE\\DEVICEMAP\\SERIALCOMM' "
         "| Out-String)"],
        timeout=20)
    return {"ports": ports,
            "serialcomm_registry": mapping if rc2 == 0 else "",
            "default_port": cfg["com_port"]}


def doctor(cfg, bridge):
    """環境診断。失敗した項目も落とさず dict で返す。"""
    rep = {"version": VERSION,
           "daemon_uptime_sec": round(time.time() - STARTED_AT, 1),
           "config": {k: cfg[k] for k in
                      ("com_port", "baud", "board", "windows_python",
                       "reset_line", "reset_pulse_ms", "http_port")},
           "bridge_attached": bridge.alive(),
           "bridge_last_error": bridge.last_error,
           "attached_port": bridge.attached_port,
           "current_port": bridge.current_port,
           "reset_line": bridge.reset_line}
    rc, out = run_quiet([sys.executable, "--version"], timeout=10)
    rep["wsl_python"] = out
    rc, out = run_quiet(
        ["powershell.exe", "-NoProfile", "-Command", "echo ok"], timeout=20)
    rep["powershell_interop"] = (out == "ok")
    py = cfg["windows_python"]
    rc, out = run_quiet([py, "--version"], timeout=20)
    rep["windows_python"] = out if rc == 0 else "NOT AVAILABLE: %s" % out
    if rc == 0:
        rc2, out2 = run_quiet(
            [py, "-c", "import serial; print(serial.__version__)"],
            timeout=20)
        rep["windows_pyserial"] = out2 if rc2 == 0 else "NOT INSTALLED"
        rc3, out3 = run_quiet(
            [py, "-c", "import kflash; print('kflash importable')"],
            timeout=20)
        rep["windows_kflash"] = ("available" if rc3 == 0
                                 else "NOT INSTALLED (pip: py -m pip install kflash)")
    try:
        rep["serial_ports"] = list_serial_ports(cfg)
    except Exception as e:
        rep["serial_ports"] = {"error": str(e)}
    return rep


# ---------------------------------------------------------------------------
# 道具 (MCP tools)
# ---------------------------------------------------------------------------

def make_tools(cfg, bridge, log):
    """道具名 -> (説明, 引数スキーマ, 実装関数) の表を作る。"""

    def t_doctor(args):
        return doctor(cfg, bridge)

    def t_daemon_log(args):
        tail = int(args.get("tail_bytes", 4000) or 4000)
        try:
            with open(LOG_PATH, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - tail))
                return {"log_tail": f.read().decode("utf-8",
                                                   errors="replace"),
                        "log_path": LOG_PATH, "log_bytes": size}
        except Exception as e:
            return {"log_tail": "", "error": str(e)}

    def t_serial_list(args):
        out = list_serial_ports(cfg)
        out["current_port"] = bridge.current_port
        out["attached_port"] = bridge.attached_port
        return out

    def t_attach(args):
        return bridge.start(args.get("port"), args.get("baud"),
                            reset_line=args.get("reset_line"))

    def t_detach(args):
        return bridge.stop()

    def t_status(args):
        return {"attached": bridge.alive(),
                "port": bridge.attached_port,
                "current_port": bridge.current_port,
                "reset_line": bridge.reset_line,
                "log_bytes_total": log.total(),
                "last_error": bridge.last_error}

    def t_reset(args):
        bridge.reset(args.get("line"), args.get("pulse_ms"))
        return {"reset": True}

    def t_set_lines(args):
        bridge.set_lines(args.get("dtr"), args.get("rts"))
        return {"set": True}

    def t_send(args):
        text = args.get("text", "")
        if args.get("newline", True):
            text += args.get("eol", "\n")
        cur = log.total()
        pace_ms = int(args.get("pace_ms", 0) or 0)
        n = bridge.tx_text(text, pace_ms=pace_ms,
                           chunk=int(args.get("chunk", 3) or 3))
        return {"sent": True, "cursor": cur, "chunks": n,
                "pace_ms": pace_ms}

    def t_read(args):
        text, cur = log.read_since(args.get("cursor"))
        return {"text": text, "cursor": cur}

    def t_expect(args):
        import re
        pattern = args["pattern"]
        timeout = float(args.get("timeout_ms", 10000)) / 1000.0
        cursor = args.get("cursor")
        deadline = time.time() + timeout
        while True:
            text, cur = log.read_since(cursor)
            m = re.search(pattern, text)
            if m:
                return {"matched": True, "text": text[:m.end()], "cursor": cur}
            if time.time() >= deadline:
                return {"matched": False, "text": text, "cursor": cur}
            time.sleep(0.05)

    def t_send_expect(args):
        text = args.get("text", "")
        if args.get("newline", True):
            text += args.get("eol", "\n")
        cur = log.total()
        bridge.tx_text(text, pace_ms=int(args.get("pace_ms", 0) or 0),
                       chunk=int(args.get("chunk", 3) or 3))
        args2 = dict(args)
        args2["cursor"] = cur
        return t_expect(args2)

    def t_boot_log(args):
        # 起動ログは「パターン待ち」ではなく一定時間まるごと採取する。
        # (パターン ".*" だと空文字列に即マッチして何も取れないため)
        wait_ms = int(args.get("wait_ms", 4000))
        if not bridge.alive():
            bridge.start()
        cur = log.total()
        bridge.reset()
        pattern = args.get("pattern")
        if pattern:
            res = t_expect({"pattern": pattern, "timeout_ms": wait_ms,
                            "cursor": cur})
        else:
            time.sleep(wait_ms / 1000.0)
            text, cur2 = log.read_since(cur)
            res = {"matched": len(text) > 0, "text": text, "cursor": cur2}
        res["boot_log"] = True
        return res

    # flash の途中経過は last_flash.json に逐次残す。応答が届かない形で
    # 失敗しても、後から flash_log で「どこまで進んだか」が分かるように。
    flash_state_path = os.path.join(HERE, "last_flash.json")
    kflash_out_path = os.path.join(HERE, "last_kflash_output.log")

    def flash_note(state, step, **fields):
        state["step"] = step
        state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        state.update(fields)
        try:
            with open(flash_state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def t_flash_log(args):
        out = {"state": None, "kflash_output_tail": ""}
        try:
            with open(flash_state_path, encoding="utf-8") as f:
                out["state"] = json.load(f)
        except Exception:
            pass
        try:
            with open(kflash_out_path, "rb") as f:
                out["kflash_output_tail"] = \
                    f.read()[-3000:].decode("utf-8", errors="replace")
        except Exception:
            pass
        return out

    def t_flash_kflash(args):
        state = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        flash_note(state, "received",
                   has_file_b64=bool(args.get("file_b64")),
                   file_path=args.get("file_path"))
        data_b64 = args.get("file_b64")
        src = args.get("file_path")
        if not data_b64 and not src:
            flash_note(state, "error", error="file_b64 or file_path required")
            raise RuntimeError("file_b64 or file_path is required")
        temp_wsl = bridge._windows_temp_wsl()
        stage_dir = os.path.join(temp_wsl, "board-daemon")
        os.makedirs(stage_dir, exist_ok=True)
        local = os.path.join(stage_dir, "firmware.bin")
        if data_b64:
            with open(local, "wb") as f:
                f.write(base64.b64decode(data_b64))
        else:
            shutil.copyfile(src, local)
        size = os.path.getsize(local)
        flash_note(state, "staged", staged_path_wsl=local, size=size)
        rc, win = run_quiet(["wslpath", "-w", local], timeout=10)
        if rc != 0:
            flash_note(state, "error", error="wslpath -w failed: %s" % win)
            raise RuntimeError("wslpath -w failed: %s" % win)
        win_path = win.splitlines()[0].strip()
        # ポートは attach と同じ解決を使う (明示 > 前回成功 > 走査)。
        # 選択中のポートを覚えておき、最後の再 attach も同じポートへ戻す。
        fport, fsource = bridge.resolve_port(args.get("port"))
        bridge.current_port = fport
        bridge._save_state()
        flash_note(state, "port_resolved", port=fport, source=fsource)
        was_attached = bridge.alive()
        if was_attached:
            bridge.stop()
        flash_note(state, "bridge_stopped", was_attached=was_attached)
        try:
            argv = [cfg["windows_python"], "-m", "kflash",
                    "-p", fport,
                    "-B", args.get("board", cfg["board"]),
                    "-b", str(args.get("baud", 115200))]
            if args.get("sram", True):
                argv.append("-s")
            argv.append(win_path)
            flash_note(state, "kflash_started", argv=argv,
                       staged_path_win=win_path)
            # 出力はパイプでなくファイルへ受ける (パイプだと kflash 側の
            # 残留プロセスに読み取りが引きずられる恐れがあるため)。
            # stdin は DEVNULL にしてデーモンの端末を継がせない。
            timeout = float(args.get("timeout_sec", 60))
            with open(kflash_out_path, "wb") as out_f:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                        stdout=out_f,
                                        stderr=subprocess.STDOUT)
                deadline = time.time() + timeout
                while proc.poll() is None and time.time() < deadline:
                    time.sleep(0.2)
                timed_out = proc.poll() is None
                if timed_out:
                    proc.kill()
                    proc.wait(timeout=5)
            with open(kflash_out_path, "rb") as f:
                out_text = f.read().decode("utf-8", errors="replace")
            flash_note(state, "kflash_timeout" if timed_out else "done",
                       returncode=proc.returncode, timed_out=timed_out)
            return {"returncode": proc.returncode, "timed_out": timed_out,
                    "port": fport,
                    "output_tail": out_text[-3000:],
                    "staged_path": local, "size": size,
                    "success_marker_found":
                        "Boot user code from SRAM" in out_text
                        or "Burn SRAM OK" in out_text,
                    "sram": bool(args.get("sram", True))}
        finally:
            if was_attached:
                try:
                    bridge.start()
                except Exception:
                    pass

    obj = {"type": "object"}
    return {
        "doctor": ("環境診断 (Windows相互運用・Python・pyserial・kflash・COMポート)",
                   obj, t_doctor),
        "daemon_log": ("デーモンのファイルログ (daemon.log) の末尾を読む。"
                       "落ちた/おかしくなったときの切り分け用",
                       {"type": "object", "properties": {
                           "tail_bytes": {"type": "integer"}}}, t_daemon_log),
        "serial_list": ("Windows 側の COM ポート一覧 (選択中の current_port つき)",
                        obj, t_serial_list),
        "board_status": ("ブリッジ接続状態・使用中ポート・受信ログ量",
                         obj, t_status),
        "board_attach": ("シリアルポートを開いて保持する。port 省略時は"
                         "前回成功したポート → config の順で解決し、決まらなければ"
                         "候補を走査して Maix/K210 らしいポートを自動で選ぶ"
                         "(選んだポートは port_state.json に記憶)。"
                         "reset_line を渡すとその基板のリセット線として記憶する"
                         "(基板ごとに違う: Dock 1台目=rts、2台目=dtr)",
                         {"type": "object", "properties": {
                             "port": {"type": "string"},
                             "baud": {"type": "integer"},
                             "reset_line": {"type": "string"}}}, t_attach),
        "board_detach": ("シリアルポートを閉じる", obj, t_detach),
        "board_reset": ("リセット信号をパルスする (line 省略時は基板ごとに"
                        "記憶したリセット線、既定 rts)",
                        {"type": "object", "properties": {
                            "line": {"type": "string"},
                            "pulse_ms": {"type": "integer"}}}, t_reset),
        "board_set_lines": ("DTR/RTS を直接設定する (ISP 侵入の実験用)",
                            {"type": "object", "properties": {
                                "dtr": {"type": "boolean"},
                                "rts": {"type": "boolean"}}}, t_set_lines),
        "serial_send": ("シリアルへ文字列を送る (既定で改行付き、eol で変更可。"
                        "MicroPython REPL の実行は eol='\\r' が要る)。"
                        "pace_ms を付けると chunk バイトごとに間隔を空けて送る。"
                        "受信 FIFO の小さいポーリング受信のボード (K210 等) では"
                        "バーストが溢れて末尾が落ちるため、chunk=3 / pace_ms=40 が"
                        "実機確認済みの目安)",
                        {"type": "object", "properties": {
                            "text": {"type": "string"},
                            "newline": {"type": "boolean"},
                            "eol": {"type": "string"},
                            "pace_ms": {"type": "integer"},
                            "chunk": {"type": "integer"}},
                         "required": ["text"]}, t_send),
        "serial_read": ("受信ログをカーソル以降から読む",
                        {"type": "object", "properties": {
                            "cursor": {"type": "integer"}}}, t_read),
        "serial_expect": ("受信ログに正規表現が現れるまで待つ",
                          {"type": "object", "properties": {
                              "pattern": {"type": "string"},
                              "timeout_ms": {"type": "integer"},
                              "cursor": {"type": "integer"}},
                           "required": ["pattern"]}, t_expect),
        "serial_send_expect": ("送って、応答パターンを待つ (対話の基本形)。"
                               "pace_ms / chunk は serial_send と同じペース付き送信",
                               {"type": "object", "properties": {
                                   "text": {"type": "string"},
                                   "pattern": {"type": "string"},
                                   "timeout_ms": {"type": "integer"},
                                   "newline": {"type": "boolean"},
                                   "eol": {"type": "string"},
                                   "pace_ms": {"type": "integer"},
                                   "chunk": {"type": "integer"}},
                                "required": ["text", "pattern"]},
                               t_send_expect),
        "board_boot_log": ("リセットして起動ログを一定時間採取する",
                           {"type": "object", "properties": {
                               "wait_ms": {"type": "integer"},
                               "pattern": {"type": "string"}}}, t_boot_log),
        "flash_kflash": ("kflash でファームを書く (既定は SRAM ブート=非破壊。"
                         "途中経過は last_flash.json に残る)",
                         {"type": "object", "properties": {
                             "file_b64": {"type": "string"},
                             "file_path": {"type": "string"},
                             "port": {"type": "string"},
                             "board": {"type": "string"},
                             "baud": {"type": "integer"},
                             "sram": {"type": "boolean"},
                             "timeout_sec": {"type": "integer"}}},
                         t_flash_kflash),
        "flash_log": ("直近の flash_kflash の途中経過と kflash 出力を読む "
                      "(応答が届かなかった場合の切り分け用)", obj, t_flash_log),
    }


# ---------------------------------------------------------------------------
# 最小 MCP サーバ (Streamable HTTP・JSON 応答のみ)
# ---------------------------------------------------------------------------

class McpServer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = SerialLog()
        self.bridge = Bridge(cfg, self.log)
        self.tools = make_tools(cfg, self.bridge, self.log)

    def handle(self, req):
        """JSON-RPC リクエスト dict を受け、応答 dict か None を返す。"""
        method = req.get("method", "")
        rid = req.get("id")
        params = req.get("params") or {}
        if method.startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": params.get("protocolVersion",
                                                  "2025-03-26"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "board-daemon",
                                   "version": VERSION},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [
                    {"name": name, "description": desc, "inputSchema": schema}
                    for name, (desc, schema, _fn) in self.tools.items()]}
            elif method == "tools/call":
                name = params.get("name", "")
                if name not in self.tools:
                    return self._err(rid, -32602, "unknown tool: %s" % name)
                fn = self.tools[name][2]
                log_event("tool %s" % name)
                try:
                    out = fn(params.get("arguments") or {})
                    text = json.dumps(out, ensure_ascii=False)
                    result = {"content": [{"type": "text", "text": text}],
                              "isError": False}
                except Exception as e:
                    log_event("tool %s error: %s" % (name, e))
                    result = {"content": [{"type": "text",
                                           "text": "error: %s" % e}],
                              "isError": True}
            else:
                return self._err(rid, -32601, "method not found: %s" % method)
        except Exception as e:
            return self._err(rid, -32603, "internal error: %s" % e)
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    @staticmethod
    def _err(rid, code, message):
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": code, "message": message}}


def make_handler(mcp):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):  # アクセスログは標準エラーへ最小限
            try:
                print("[http] %s" % (fmt % a), file=sys.stderr)
            except Exception:
                pass

        def _reply(self, code, body_bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            if body_bytes:
                self.wfile.write(body_bytes)

        def do_GET(self):
            if self.path == "/health":
                body = json.dumps({
                    "ok": True, "service": "board-daemon",
                    "version": VERSION,
                    "uptime_sec": round(time.time() - STARTED_AT, 1),
                    "bridge_attached": mcp.bridge.alive()}).encode()
                self._reply(200, body)
            else:
                self._reply(405, b"")

        def do_POST(self):
            if self.path.split("?")[0] != "/mcp":
                self._reply(404, b"{}")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._reply(400, b"{}")
                return
            resp = mcp.handle(req)
            if resp is None:  # notification
                self._reply(202, b"")
                return
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Mcp-Session-Id", uuid.uuid4().hex)
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--port-http", type=int, default=None)
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.port_http:
        cfg["http_port"] = args.port_http
    # 環境変数は config より優先 (起動時に切り替えたいとき用)。
    # 通常は board_attach の解決と port_state.json の記憶で足りる。
    if os.environ.get("BOARD_DAEMON_COM_PORT"):
        cfg["com_port"] = os.environ["BOARD_DAEMON_COM_PORT"]
    if os.environ.get("BOARD_DAEMON_RESET_LINE"):
        cfg["reset_line"] = os.environ["BOARD_DAEMON_RESET_LINE"]
    setup_logging()
    mcp = McpServer(cfg)
    server = ThreadingHTTPServer((cfg["http_host"], cfg["http_port"]),
                                 make_handler(mcp))
    log_event("board-daemon v%s listening on %s:%d (POST /mcp, GET /health), "
              "config port=%s reset_line=%s, state port=%s"
              % (VERSION, cfg["http_host"], cfg["http_port"], cfg["com_port"],
                 cfg["reset_line"], mcp.bridge.current_port))

    def heartbeat():
        while True:
            time.sleep(60)
            log_event("heartbeat uptime=%ds attached=%s port=%s"
                      % (int(time.time() - STARTED_AT), mcp.bridge.alive(),
                         mcp.bridge.attached_port))

    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_event("serve_forever stopped (KeyboardInterrupt)")
    except BaseException as e:
        log_event("FATAL serve_forever died: %r" % (e,))
        raise
    log_event("serve_forever exited")


if __name__ == "__main__":
    main()
